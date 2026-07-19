#!/usr/bin/env python3
"""Unit tests for tui_update_all_clis.py."""
import asyncio
import io
import os
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tui_update_all_clis import (
    EC_SKIP, BaseRenderer, Executor, Job, SEP, build_frame, format_elapsed,
    job_detail, job_row, parse_emit_line, progress_bar, read_emit_file,
    select_visible, status_counts, strip_ansi, truncate,
    ST_DONE, ST_FAILED, ST_HELD, ST_PENDING, ST_QUARANTINED, ST_RUNNING,
    ST_SKIPPED, ST_UPTODATE,
)


def make_emit_line(kind, name, cmd="", lock=""):
    return SEP.join([kind, name, cmd, lock])


class TestParseEmitLine(unittest.TestCase):
    def test_full_line(self):
        j = parse_emit_line(make_emit_line("bulk", "npm", "npm update -g", "npm"))
        self.assertEqual((j.kind, j.name, j.cmd, j.lock), ("bulk", "npm", "npm update -g", "npm"))

    def test_empty_lock_falls_back_to_name(self):
        # Mirrors the shell's `lock_group="${EMIT_LOCK:-$name}"`.
        j = parse_emit_line(make_emit_line("skip", "foo", "", ""))
        self.assertEqual(j.lock, "foo")

    def test_missing_fields_padded(self):
        j = parse_emit_line("held\x1emytool")
        self.assertEqual(j.kind, "held")
        self.assertEqual(j.name, "mytool")
        self.assertEqual(j.cmd, "")

    def test_cmd_may_contain_spaces(self):
        j = parse_emit_line(make_emit_line("known", "eza", "cargo install eza --locked", "cargo"))
        self.assertEqual(j.cmd, "cargo install eza --locked")


class TestFormatting(unittest.TestCase):
    def test_format_elapsed(self):
        self.assertEqual(format_elapsed(0), "0:00")
        self.assertEqual(format_elapsed(59.9), "0:59")
        self.assertEqual(format_elapsed(83), "1:23")
        self.assertEqual(format_elapsed(3600), "60:00")
        self.assertEqual(format_elapsed(-5), "0:00")

    def test_truncate(self):
        self.assertEqual(truncate("hello", 10), "hello")
        self.assertEqual(truncate("hello world", 8), "hello w…")
        self.assertEqual(truncate("hello", 1), "…")
        self.assertEqual(truncate("hello", 0), "")

    def test_strip_ansi(self):
        self.assertEqual(strip_ansi("\x1b[32mok\x1b[0m"), "ok")
        self.assertEqual(strip_ansi("  padded  "), "padded")

    def test_progress_bar(self):
        fill, bar = progress_bar(0, 10, 20)
        self.assertEqual((fill, bar), (0, "░" * 20))
        fill, bar = progress_bar(5, 10, 20)
        self.assertEqual((fill, len(bar)), (10, 20))
        fill, bar = progress_bar(10, 10, 20)
        self.assertEqual(bar, "█" * 20)
        self.assertEqual(progress_bar(1, 1, 0), (0, ""))

    def test_job_detail_statuses(self):
        j = Job(kind="known", name="x", cmd="true", lock="x")
        j.status = ST_RUNNING
        j.last_line = "Downloading..."
        self.assertEqual(job_detail(j), "Downloading...")
        j.status = ST_FAILED
        j.ec = 1
        self.assertEqual(job_detail(j), "exit 1")
        j.note = "timed out after 5s"
        self.assertEqual(job_detail(j), "exit 1 — timed out after 5s")
        j.status = ST_UPTODATE
        self.assertEqual(job_detail(j), "already up to date (pre-check)")
        j.status = ST_HELD
        j.cmd = "env"
        self.assertIn("HOLD=", job_detail(j))
        j.cmd = "config"
        self.assertIn("hold", job_detail(j))
        j.status = ST_QUARANTINED
        j.cmd = "3"
        self.assertIn("3 consecutive", job_detail(j))

    def test_job_row_fits_width(self):
        j = Job(kind="bulk", name="a-very-long-tool-name-indeed", cmd="c", lock="c")
        j.status = ST_RUNNING
        j.mono_start = time.monotonic()
        j.last_line = "x" * 500
        row = job_row(j, name_w=20, width=60, frame_idx=0, style=_no_color())
        self.assertLessEqual(len(strip_ansi(row)), 60)

    def test_status_counts(self):
        jobs = [
            Job("known", "a", "", "a", status=ST_DONE),
            Job("known", "b", "", "b", status=ST_FAILED),
            Job("known", "c", "", "c", status=ST_RUNNING),
            Job("skip", "d", "", "d", status=ST_SKIPPED),
            Job("bulk", "e", "", "e", status=ST_PENDING),
        ]
        c = status_counts(jobs)
        self.assertEqual(
            (c["total"], c["done"], c["failed"], c["finished"], c["running"], c["pending"]),
            (5, 1, 1, 3, 1, 1),
        )


def _no_color():
    from tui_update_all_clis import Style
    return Style(False)


class TestSelectVisible(unittest.TestCase):
    def _jobs(self, n, status):
        return [Job("known", f"t{i}", "", f"t{i}", status=status, seq=i) for i in range(n)]

    def test_all_fit(self):
        jobs = self._jobs(5, ST_PENDING)
        rows, note = select_visible(jobs, 10)
        self.assertEqual(len(rows), 5)
        self.assertEqual(note, "")

    def test_running_prioritized_with_recent_finished(self):
        jobs = self._jobs(10, ST_PENDING)
        for i, j in enumerate(jobs[:6]):
            j.status = ST_DONE
            j.completed_seq = i + 1
        jobs[6].status = ST_RUNNING
        jobs[7].status = ST_RUNNING
        rows, note = select_visible(jobs, 6)
        statuses = [r.status for r in rows]
        self.assertEqual(statuses.count(ST_RUNNING), 2)
        # 2 running + 2 pending shown first; remaining 2 slots go to the most
        # recent completions.
        finished_shown = [r.name for r in rows if r.status == ST_DONE]
        self.assertEqual(finished_shown, ["t4", "t5"])
        self.assertEqual(statuses.count(ST_PENDING), 2)
        self.assertIn("done", note)

    def test_no_room(self):
        jobs = self._jobs(3, ST_PENDING)
        rows, note = select_visible(jobs, 0)
        self.assertEqual(rows, [])
        self.assertIn("3", note)


class TestBuildFrame(unittest.TestCase):
    def _mixed_jobs(self):
        jobs = []
        for i in range(6):
            jobs.append(Job("known", f"tool{i}", f"cmd{i}", f"lock{i}", seq=i))
        jobs[0].status = ST_DONE
        jobs[0].mono_end = jobs[0].mono_start = 0
        jobs[1].status = ST_FAILED
        jobs[1].ec = 1
        jobs[2].status = ST_RUNNING
        jobs[2].mono_start = time.monotonic()
        jobs[3].status = ST_UPTODATE
        jobs[3].cmd = "2.5"
        return jobs

    def test_frame_shape_and_width(self):
        jobs = self._mixed_jobs()
        for width in (50, 80, 120):
            lines = build_frame(jobs, width, 20, "0.8.0", time.monotonic(), frame_idx=0)
            self.assertLessEqual(len(lines), 20)
            for line in lines:
                self.assertLessEqual(len(strip_ansi(line)), width,
                                     f"line too wide at width {width}: {line!r}")

    def test_frame_contains_header_progress_and_jobs(self):
        jobs = self._mixed_jobs()
        lines = build_frame(jobs, 80, 20, "9.9.9", time.monotonic())
        plain = [strip_ansi(l) for l in lines]
        self.assertIn("update-all-clis 9.9.9", plain[0])
        self.assertIn("3/6", plain[1])           # finished: done + failed + uptodate
        joined = "\n".join(plain)
        self.assertIn("tool0", joined)
        self.assertIn("already up to date", joined)

    def test_frame_narrow_terminal(self):
        jobs = self._mixed_jobs()
        lines = build_frame(jobs, 40, 8, "0.8.0", time.monotonic())
        self.assertLessEqual(len(lines), 8)
        for line in lines:
            self.assertLessEqual(len(strip_ansi(line)), 40)


class ExecutorTestBase(unittest.IsolatedAsyncioTestCase):
    def make_executor(self, jobs, parallel=4, timeout=0, skip=frozenset()):
        results = io.StringIO()
        renderer = BaseRenderer(jobs)
        ex = Executor(jobs, parallel, timeout, set(skip), results, renderer)
        return ex, results

    def parse_results(self, results: io.StringIO):
        """Parse the results file format: '<ec>\\x1e<record>' per line.
        NB: split on "\\n" explicitly — str.splitlines() also splits on \\x1e
        and would shred every record field onto its own "line"."""
        out = []
        for line in results.getvalue().split("\n"):
            if not line:
                continue
            ec, _, rec = line.partition(SEP)
            out.append((int(ec), rec.split(SEP) if rec else []))
        return out


class TestExecutorBasic(ExecutorTestBase):
    async def test_success_and_failure(self):
        jobs = [
            parse_emit_line(make_emit_line("known", "oktool", "true", "oktool")),
            parse_emit_line(make_emit_line("known", "badtool", "exit 7", "badtool")),
        ]
        ex, results = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].status, ST_DONE)
        self.assertEqual(jobs[0].ec, 0)
        self.assertEqual(jobs[1].status, ST_FAILED)
        # The raw exit code is kept for display...
        self.assertEqual(jobs[1].raw_ec, 7)
        # ...but result records normalize failures to 1, exactly like the
        # shell executor's run_update (a real exit 3 must not alias the
        # skip sentinel).
        self.assertEqual(jobs[1].ec, 1)
        parsed = dict()
        for ec, rec in self.parse_results(results):
            parsed[rec[1]] = (ec, rec)
        self.assertEqual(parsed["oktool"][0], 0)
        self.assertEqual(parsed["badtool"][0], 1)
        # Record format: kind, name, cmd, ec, start, end
        self.assertEqual(parsed["oktool"][1][0], "known")
        self.assertEqual(parsed["oktool"][1][2], "true")
        self.assertTrue(parsed["oktool"][1][4].isdigit())
        self.assertTrue(parsed["oktool"][1][5].isdigit())

    async def test_command_exit_3_counts_as_failed_not_skipped(self):
        jobs = [parse_emit_line(make_emit_line("known", "three", "exit 3", "three"))]
        ex, results = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].status, ST_FAILED)
        self.assertEqual(jobs[0].raw_ec, 3)
        ec, rec = self.parse_results(results)[0]
        self.assertEqual(ec, 1)  # normalized; 3 would mean "skipped"

    async def test_output_captured_for_last_line_and_tail(self):
        jobs = [parse_emit_line(make_emit_line("known", "t", "echo hello-world", "t"))]
        ex, _ = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].last_line, "hello-world")
        self.assertIn("hello-world", list(jobs[0].tail))

    async def test_warn_lines_filtered(self):
        jobs = [parse_emit_line(make_emit_line(
            "known", "t", "echo 'npm warn deprecated x'; echo real-output", "t"))]
        ex, _ = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].last_line, "real-output")

    async def test_carriage_return_progress_split(self):
        # Progress spinners rewrite the line with \r; each segment should be
        # treated as a separate display line, not one giant blob.
        jobs = [parse_emit_line(make_emit_line(
            "known", "t", "printf 'one\\rtwo\\rthree\\n'", "t"))]
        ex, _ = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].last_line, "three")


class TestExecutorInstantKinds(ExecutorTestBase):
    async def test_skip_kind_no_record(self):
        jobs = [parse_emit_line(make_emit_line("skip", "unknown-tool", "", ""))]
        ex, results = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].status, ST_SKIPPED)
        parsed = self.parse_results(results)
        self.assertEqual(len(parsed), 1)
        ec, rec = parsed[0]
        self.assertEqual(ec, EC_SKIP)
        self.assertEqual(rec, [])  # no history record for plain skips

    async def test_held_config_and_env(self):
        jobs = [
            parse_emit_line(make_emit_line("held", "a", "config", "")),
            parse_emit_line(make_emit_line("held", "b", "env", "")),
        ]
        ex, results = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].status, ST_HELD)
        self.assertEqual(jobs[1].status, ST_HELD)
        parsed = self.parse_results(results)
        self.assertEqual(len(parsed), 2)
        for ec, rec in parsed:
            self.assertEqual(ec, EC_SKIP)
            self.assertEqual(rec[0], "held")  # held jobs DO get a record

    async def test_quarantined_no_record(self):
        jobs = [parse_emit_line(make_emit_line("quarantined", "bad", "3", ""))]
        ex, results = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].status, ST_QUARANTINED)
        ec, rec = self.parse_results(results)[0]
        self.assertEqual(ec, EC_SKIP)
        self.assertEqual(rec, [])

    async def test_uptodate_record_uses_duration(self):
        jobs = [parse_emit_line(make_emit_line("uptodate", "brew", "12.7", ""))]
        ex, results = self.make_executor(jobs)
        await ex.run()
        self.assertEqual(jobs[0].status, ST_UPTODATE)
        ec, rec = self.parse_results(results)[0]
        self.assertEqual(ec, 0)
        self.assertEqual(rec[0], "uptodate")
        self.assertEqual(rec[2], "12.7")  # raw cmd (duration) preserved
        start, end = int(rec[4]), int(rec[5])
        self.assertEqual(end - start, 12)  # int(duration), shell-compatible

    async def test_skip_list_marks_known_tool_skipped(self):
        jobs = [parse_emit_line(make_emit_line("known", "skipme", "exit 1", "skipme"))]
        ex, results = self.make_executor(jobs, skip={"skipme"})
        await ex.run()
        self.assertEqual(jobs[0].status, ST_SKIPPED)
        ec, rec = self.parse_results(results)[0]
        self.assertEqual(ec, EC_SKIP)
        self.assertEqual(rec[0], "known")  # skipped known tools keep a record


class TestExecutorConcurrency(ExecutorTestBase):
    async def test_lock_group_serializes(self):
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "markers")
            def cmd(tag):
                return f"echo S{tag} >> {marker}; sleep 0.2; echo E{tag} >> {marker}"
            jobs = [
                parse_emit_line(make_emit_line("known", "a", cmd("A"), "same")),
                parse_emit_line(make_emit_line("known", "b", cmd("B"), "same")),
            ]
            ex, _ = self.make_executor(jobs, parallel=4)
            await ex.run()
            with open(marker) as f:
                order = f.read().split()
            # Same lock group: one job must fully finish before the other starts.
            self.assertIn(order, [["SA", "EA", "SB", "EB"], ["SB", "EB", "SA", "EA"]])

    async def test_different_lock_groups_run_in_parallel(self):
        start = time.monotonic()
        jobs = [
            parse_emit_line(make_emit_line("known", "a", "sleep 0.5", "a")),
            parse_emit_line(make_emit_line("known", "b", "sleep 0.5", "b")),
            parse_emit_line(make_emit_line("known", "c", "sleep 0.5", "c")),
        ]
        ex, _ = self.make_executor(jobs, parallel=3)
        await ex.run()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.2, f"3x sleep 0.5 in parallel took {elapsed:.2f}s")

    async def test_parallel_cap_enforced(self):
        start = time.monotonic()
        jobs = [
            parse_emit_line(make_emit_line("known", n, "sleep 0.4", n))
            for n in ("a", "b", "c")
        ]
        ex, _ = self.make_executor(jobs, parallel=1)
        await ex.run()
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.1,
                                f"parallel=1 should serialize, took {elapsed:.2f}s")

    async def test_watchdog_kills_stuck_job(self):
        jobs = [parse_emit_line(make_emit_line("known", "stuck", "sleep 60", "stuck"))]
        ex, results = self.make_executor(jobs, timeout=1)
        start = time.monotonic()
        await ex.run()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10)
        self.assertEqual(jobs[0].status, ST_FAILED)
        self.assertEqual(jobs[0].ec, 1)
        self.assertIn("timed out", jobs[0].note)
        ec, rec = self.parse_results(results)[0]
        self.assertEqual(ec, 1)
        self.assertEqual(rec[0], "known")

    async def test_timeout_kills_whole_process_tree(self):
        # The child spawns a background grandchild; the watchdog must reap
        # the whole process group, not just the direct child.
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "grandchild-alive")
            cmd = f"sh -c 'sleep 30; touch {marker}' & sleep 30"
            jobs = [parse_emit_line(make_emit_line("known", "t", cmd, "t"))]
            ex, _ = self.make_executor(jobs, timeout=1)
            await ex.run()
            await asyncio.sleep(0.5)  # give any survivor a chance to run
            self.assertFalse(os.path.exists(marker),
                             "grandchild survived the watchdog (process-tree leak)")


class TestReadEmitFile(ExecutorTestBase):
    def test_reads_jobs_and_skips_blanks(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
            f.write(make_emit_line("known", "a", "true", "a") + "\n")
            f.write("\n")
            f.write(make_emit_line("bulk", "npm", "npm update -g", "npm") + "\n")
            path = f.name
        try:
            jobs = read_emit_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(jobs), 2)
        self.assertEqual([j.seq for j in jobs], [0, 1])
        self.assertEqual(jobs[1].kind, "bulk")


if __name__ == "__main__":
    unittest.main()
