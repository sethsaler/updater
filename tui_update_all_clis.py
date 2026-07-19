#!/usr/bin/env python3
"""tui_update_all_clis.py — live TUI executor for update-all-clis.

Replaces the bash parallel executor in update_all_clis.sh when stdout is an
interactive terminal: it reads the plan ("emit lines") produced by
lib_update_all_clis.py, runs every job with the exact same semantics as the
bash executor (parallel cap, per-origin lock-group serialization, per-job
watchdog timeout, process-tree kills, exit-code conventions), renders a live
dashboard while jobs run, and writes result records in the byte-exact format
update_all_clis.sh's own executors produce — so every post-run step in the
shell script (version snapshots, run summary, history, notify, changelog)
works unchanged.

Stdlib only — no third-party dependencies, matching the rest of the project.

Usage:
    python3 tui_update_all_clis.py \
        --emit-file PATH --results-file PATH \
        [--parallel N] [--timeout SECONDS] [--skip a,b,c] \
        [--mode auto|live|plain] [--version-string X]

Emit-line format (one per line, fields separated by \\x1e):
    <kind>\\x1e<name>\\x1e<cmd>\\x1e<lock-group>
    kind: known | bulk | skip | held | quarantined | uptodate

Results-file format (one per line):
    <ec>\\x1e<record>
    where record is "<kind>\\x1e<name>\\x1e<cmd>\\x1e<ec>\\x1e<start>\\x1e<end>"
    for known/bulk/uptodate/held jobs and empty for skip/quarantined jobs —
    mirroring the *.result files update_all_clis.sh's run_updates_parallel
    collects. Exit-code convention: 0 = ok, 3 = skipped (not counted),
    anything else = failed.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import os
import re
import shutil
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import IO, Optional

SEP = "\x1e"

# Job kinds found in emit lines.
KIND_KNOWN = "known"
KIND_BULK = "bulk"
KIND_SKIP = "skip"
KIND_HELD = "held"
KIND_QUARANTINED = "quarantined"
KIND_UPTODATE = "uptodate"

# Kinds that produce a history record (mirrors update_all_clis.sh).
RECORD_KINDS = frozenset({KIND_KNOWN, KIND_BULK, KIND_UPTODATE, KIND_HELD})

# Exit-code convention shared with update_all_clis.sh: 3 means "skipped —
# do not count toward ok/failed".
EC_SKIP = 3

# Job statuses (lifecycle: pending -> running -> done/failed; the instant
# kinds land directly on their terminal status).
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"
ST_HELD = "held"
ST_QUARANTINED = "quarantined"
ST_UPTODATE = "uptodate"

FINISHED_STATUSES = frozenset(
    {ST_DONE, ST_FAILED, ST_SKIPPED, ST_HELD, ST_QUARANTINED, ST_UPTODATE}
)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Minimum terminal size for the live dashboard; smaller falls back to plain.
MIN_LIVE_WIDTH = 50
MIN_LIVE_HEIGHT = 10

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SEGMENT_SPLIT_RE = re.compile(rb"[\r\n]+")


# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------
@dataclass
class Job:
    kind: str
    name: str
    cmd: str
    lock: str
    seq: int = 0  # position in the plan (display order)
    status: str = ST_PENDING
    ec: Optional[int] = None
    # The subprocess's real exit code, for display only. Result records and
    # the shell's ok/fail counters use ec, which — exactly like the shell
    # executor's run_update — is normalized to 0/1/3 (ok/fail/skip): a
    # command exiting 3 must not be mistaken for the skip sentinel.
    raw_ec: Optional[int] = None
    start: Optional[float] = None  # epoch seconds, for result records
    end: Optional[float] = None
    mono_start: float = 0.0  # monotonic clock, for display
    mono_end: float = 0.0
    note: str = ""  # extra detail ("timed out after 900s", hold source, ...)
    last_line: str = ""  # rolling last output line (ANSI-stripped)
    tail: deque = field(default_factory=lambda: deque(maxlen=8))
    completed_seq: int = 0  # completion order across all jobs
    announced: bool = False  # plain renderer: start banner already printed


def parse_emit_line(line: str, seq: int = 0) -> Job:
    """Parse one emit line into a Job. Mirrors the shell's _parse_emit_line,
    including its empty-lock -> job-name fallback."""
    parts = line.rstrip("\n").split(SEP)
    parts += [""] * (4 - len(parts))
    kind, name, cmd, lock = parts[:4]
    return Job(kind=kind, name=name, cmd=cmd, lock=lock or name, seq=seq)


# ---------------------------------------------------------------------------
# Pure formatting helpers (unit-tested without a terminal)
# ---------------------------------------------------------------------------
def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s).replace("\x1b", "").strip()


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def progress_bar(done: int, total: int, width: int) -> tuple[int, str]:
    """Return (filled_cells, bar_string) for a bar of `width` cells."""
    if width <= 0:
        return 0, ""
    total = max(total, 1)
    done = min(max(done, 0), total)
    fill = int(round(width * done / total))
    return fill, "█" * fill + "░" * (width - fill)


def job_detail(job: Job) -> str:
    """The free-text trailing column for a job row."""
    if job.status == ST_RUNNING:
        return job.last_line
    if job.status == ST_FAILED:
        shown = job.raw_ec if job.raw_ec is not None else job.ec
        base = f"exit {shown}" if shown is not None else "failed"
        return f"{base} — {job.note}" if job.note else base
    if job.status == ST_UPTODATE:
        return "already up to date (pre-check)"
    if job.status == ST_SKIPPED:
        return job.note or "skipped"
    if job.status == ST_HELD:
        return "held (HOLD= env)" if job.cmd == "env" else 'held (config "hold")'
    if job.status == ST_QUARANTINED:
        return f"quarantined after {job.cmd} consecutive failures"
    return ""


def _float_or_zero(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


class Style:
    """Tiny ANSI styler; every method is a no-op when color is disabled."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def bold(self, t: str) -> str: return self.wrap("1", t)
    def dim(self, t: str) -> str: return self.wrap("2", t)
    def red(self, t: str) -> str: return self.wrap("31", t)
    def green(self, t: str) -> str: return self.wrap("32", t)
    def yellow(self, t: str) -> str: return self.wrap("33", t)
    def cyan(self, t: str) -> str: return self.wrap("36", t)


def status_counts(jobs: list[Job]) -> dict[str, int]:
    counts = {"total": len(jobs), "done": 0, "failed": 0, "finished": 0,
              "running": 0, "pending": 0}
    for j in jobs:
        if j.status in (ST_DONE, ST_UPTODATE):
            counts["done"] += 1
        if j.status == ST_FAILED:
            counts["failed"] += 1
        if j.status in FINISHED_STATUSES:
            counts["finished"] += 1
        elif j.status == ST_RUNNING:
            counts["running"] += 1
        elif j.status == ST_PENDING:
            counts["pending"] += 1
    return counts


def job_glyph(job: Job, frame_idx: int, style: Style) -> str:
    st = job.status
    if st == ST_RUNNING:
        return style.cyan(SPINNER[frame_idx % len(SPINNER)])
    if st == ST_DONE:
        return style.green("✓")
    if st == ST_UPTODATE:
        return style.cyan("✓")
    if st == ST_FAILED:
        return style.red("✗")
    if st == ST_HELD:
        return style.yellow("‖")
    if st == ST_QUARANTINED:
        return style.yellow("!")
    if st == ST_SKIPPED:
        return style.dim("–")
    return style.dim("·")


def job_row(job: Job, name_w: int, width: int, frame_idx: int,
            style: Style, now: Optional[float] = None) -> str:
    """One job table row, visible length <= width."""
    name = truncate(job.name, name_w)
    badge = job.kind if job.kind in (KIND_KNOWN, KIND_BULK) else ""
    if job.status == ST_RUNNING:
        elapsed = format_elapsed((now if now is not None else time.monotonic()) - job.mono_start)
    elif job.status in (ST_DONE, ST_FAILED):
        elapsed = format_elapsed(job.mono_end - job.mono_start)
    elif job.status == ST_UPTODATE:
        elapsed = format_elapsed(_float_or_zero(job.cmd))
    else:
        elapsed = ""
    # glyph(1)+sp + name + sp + badge(5) + sp + elapsed(5) + sp + detail
    fixed = 1 + 1 + name_w + 1 + 5 + 1 + 5 + 1
    detail = truncate(job_detail(job), max(0, width - fixed))
    dim_row = job.status in (ST_PENDING, ST_SKIPPED)
    row = (
        f"{job_glyph(job, frame_idx, style)} "
        f"{style.dim(name.ljust(name_w)) if dim_row else name.ljust(name_w)} "
        f"{style.dim(badge.ljust(5))} "
        f"{style.dim(elapsed.rjust(5))} "
        f"{style.dim(detail) if job.status != ST_FAILED else style.red(detail)}"
    )
    return row.rstrip()


def select_visible(jobs: list[Job], rows_avail: int) -> tuple[list[Job], str]:
    """Choose which job rows fit the screen. Plan order is preserved within
    each group; running jobs are always shown, then the most recently
    finished (a scrolling completion log), then the next few queued jobs.
    Returns (rows, hidden-note)."""
    if rows_avail <= 0:
        return [], f"… {len(jobs)} jobs"
    if len(jobs) <= rows_avail:
        return list(jobs), ""
    running = [j for j in jobs if j.status == ST_RUNNING]
    finished = sorted((j for j in jobs if j.status in FINISHED_STATUSES),
                      key=lambda j: j.completed_seq)
    pending = [j for j in jobs if j.status == ST_PENDING]

    show_running = running[:rows_avail]
    slots = rows_avail - len(show_running)
    show_pending = pending[:max(0, min(3, slots))]
    slots -= len(show_pending)
    show_finished = finished[-slots:] if slots > 0 else []

    rows = show_finished + show_running + show_pending
    bits = []
    hidden_finished = len(finished) - len(show_finished)
    hidden_running = len(running) - len(show_running)
    hidden_pending = len(pending) - len(show_pending)
    if hidden_finished:
        bits.append(f"+{hidden_finished} done")
    if hidden_running:
        bits.append(f"+{hidden_running} running")
    if hidden_pending:
        bits.append(f"{hidden_pending} queued")
    return rows, ("… " + " · ".join(bits)) if bits else ""


def build_frame(jobs: list[Job], width: int, height: int, version: str,
                started_mono: float, frame_idx: int = 0,
                color: bool = True, now: Optional[float] = None) -> list[str]:
    """Pure frame builder: returns the visible lines of the dashboard.
    Every line's visible (ANSI-stripped) length is <= width."""
    style = Style(color)
    now = now if now is not None else time.monotonic()
    # Budget is width-1: a line exactly `width` cells long leaves the cursor
    # in the terminal's deferred-wrap state and the newline that follows
    # would scroll an extra blank line into the frame.
    width = max(width, 10) - 1
    counts = status_counts(jobs)
    total = counts["total"]

    # Header: title left, run clock right.
    left = f" update-all-clis {version}".rstrip()
    right = format_elapsed(now - started_mono)
    pad = max(1, width - len(left) - len(right))
    header = style.bold(left) + " " * pad + style.dim(right)

    # Progress bar with counts.
    suffix = f" {counts['finished']}/{total}  ✓ {counts['done']}  ✗ {counts['failed']}"
    bar_w = max(8, width - len(suffix) - 3)
    fill, bar = progress_bar(counts["finished"], total, bar_w)
    bar_colored = style.green(bar[:fill]) + style.dim(bar[fill:])
    ok_part = style.green(f"✓ {counts['done']}")
    fail_part = (style.red(f"✗ {counts['failed']}") if counts["failed"]
                 else style.dim(f"✗ {counts['failed']}"))
    progress = f" {bar_colored} {counts['finished']}/{total}  {ok_part}  {fail_part}"

    # Job rows within the remaining vertical space. Reserve: header,
    # progress, one blank separator, footer.
    rows_avail = max(1, height - 4)
    rows, note = select_visible(jobs, rows_avail)
    if note and rows_avail > 1:
        rows, note = select_visible(jobs, rows_avail - 1)

    max_name = max((len(j.name) for j in jobs), default=8)
    name_w = min(20, max(8, max_name), max(8, width - 42))

    lines = [header, progress, ""]
    for j in rows:
        lines.append(job_row(j, name_w, width, frame_idx, style, now))
    if note:
        lines.append(style.dim(note))
    footer = style.dim("^C abort".rjust(width - 1))
    lines.append(footer)
    return lines[:height]


def compose_frame(lines: list[str]) -> str:
    """Turn visible lines into a full-screen repaint string: home cursor,
    clear each line before writing it, clear anything below the frame."""
    return "\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
class BaseRenderer:
    """No-op renderer interface; also used as a test double."""

    def __init__(self, jobs: list[Job], version: str = ""):
        self.jobs = jobs
        self.version = version
        self.started_mono = time.monotonic()

    async def start(self) -> None:
        pass

    def job_event(self, job: Job) -> None:
        pass

    def line_event(self, job: Job) -> None:
        pass

    async def finish(self, aborted: bool = False) -> None:
        pass


def _supports_live(out: IO[str]) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    if not hasattr(out, "isatty") or not out.isatty():
        return False
    size = shutil.get_terminal_size()
    return size.columns >= MIN_LIVE_WIDTH and size.lines >= MIN_LIVE_HEIGHT


class PlainRenderer(BaseRenderer):
    """Line-oriented output matching the bash executor's messages. Used for
    --mode plain and as the automatic fallback on non-terminals."""

    def __init__(self, jobs: list[Job], version: str = "", out: IO[str] = sys.stdout):
        super().__init__(jobs, version)
        self.out = out
        is_tty = getattr(out, "isatty", lambda: False)()
        self.style = Style(is_tty and not os.environ.get("NO_COLOR")
                           and os.environ.get("TERM", "") != "dumb")

    def _print(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def job_event(self, job: Job) -> None:
        s = self.style
        if job.status == ST_RUNNING and not job.announced:
            job.announced = True
            if job.kind == KIND_BULK:
                self._print(s.bold("==>") + f" Updating all {job.name}...")
            else:
                self._print(s.bold("==>") + f" Updating {job.name}...")
            self._print(f"  {s.bold('→')} {job.cmd}")
        elif job.status == ST_DONE:
            self._print(s.green("✓") + f" {job.name}")
        elif job.status == ST_UPTODATE:
            self._print(s.green("✓") + f" {job.name}: already up to date (pre-check)")
        elif job.status == ST_FAILED:
            if job.note.startswith("timed out"):
                self._print(s.yellow("!!") + f" {job.name} {job.note} and was killed — "
                            "it was probably waiting on something (e.g. an open app "
                            "blocking a cask upgrade, or a prompt). Other updates were "
                            "not blocked.")
            else:
                shown = job.raw_ec if job.raw_ec is not None else job.ec
                self._print(s.yellow("!!") + f" {job.name} failed (exit {shown})")
            for line in list(job.tail)[-3:]:
                self._print(f"   {line}")
        elif job.status == ST_SKIPPED and job.kind == KIND_KNOWN:
            self._print(s.cyan(f"-- {job.name} skipped"))
        elif job.status == ST_HELD:
            if job.cmd == "env":
                self._print(s.yellow("!!") + f" held (env HOLD=): {job.name} — "
                            "remove from HOLD= to resume this run only")
            else:
                self._print(s.yellow("!!") + f" held (config): {job.name} — "
                            'remove from "hold" to resume updates')
        elif job.status == ST_QUARANTINED:
            self._print(s.yellow("!!") + f" skipped (quarantined after {job.cmd} "
                        f"consecutive failures): {job.name} — run with "
                        "--include-quarantined to retry")


class LiveRenderer(BaseRenderer):
    """Alternate-screen live dashboard. Renders at ~10 fps from the event
    loop; restores the terminal on finish, abort, or process exit."""

    def __init__(self, jobs: list[Job], version: str = "", out: IO[str] = sys.stdout):
        super().__init__(jobs, version)
        self.out = out
        self.frame_idx = 0
        self._dirty = True
        self._tick_task: Optional[asyncio.Task] = None
        self._restored = False

    async def start(self) -> None:
        self.out.write("\x1b[?1049h\x1b[?25l")  # alt screen + hide cursor
        self.out.flush()
        # Belt and braces: even if the process dies without finish() running
        # (second Ctrl+C, SIGTERM, an unexpected exception), never strand the
        # user's terminal in the alternate screen.
        atexit.register(self.restore)
        self._tick_task = asyncio.create_task(self._tick_loop())

    def job_event(self, job: Job) -> None:
        self._dirty = True

    def line_event(self, job: Job) -> None:
        self._dirty = True

    async def _tick_loop(self) -> None:
        try:
            while True:
                self.frame_idx += 1
                self.draw()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    def draw(self) -> None:
        size = shutil.get_terminal_size()
        lines = build_frame(self.jobs, size.columns, size.lines, self.version,
                            self.started_mono, self.frame_idx, color=True)
        self.out.write(compose_frame(lines))
        self.out.flush()
        self._dirty = False

    def restore(self) -> None:
        """Idempotent terminal restore (also registered with atexit)."""
        if self._restored:
            return
        self._restored = True
        try:
            self.out.write("\x1b[?25h\x1b[?1049l")  # show cursor + leave alt screen
            self.out.flush()
        except Exception:
            pass

    async def finish(self, aborted: bool = False) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        # One last frame so the closing state of every job is visible before
        # the dashboard leaves the alternate screen.
        with contextlib.suppress(Exception):
            self.draw()
        self.restore()
        print_epilogue(self.jobs, aborted)


def print_epilogue(jobs: list[Job], aborted: bool = False,
                   out: IO[str] = sys.stdout, color: bool = True) -> None:
    """After the live display closes, print the one thing not preserved
    anywhere else: failure details with output tails. Everything else was
    visible in the dashboard and is summarized by the shell epilogue."""
    style = Style(color)
    failures = sorted((j for j in jobs if j.status == ST_FAILED),
                      key=lambda j: j.completed_seq)
    if aborted:
        out.write(style.yellow("!!") + " interrupted — running updates were killed\n")
    for j in failures:
        if j.note.startswith("timed out"):
            out.write(style.yellow("!!") + f" {j.name} {j.note} and was killed — "
                      "it was probably waiting on something (e.g. an open app "
                      "blocking a cask upgrade, or a prompt). Other updates were "
                      "not blocked.\n")
        elif j.note == "interrupted":
            out.write(style.yellow("!!") + f" {j.name} interrupted\n")
        else:
            shown = j.raw_ec if j.raw_ec is not None else j.ec
            out.write(style.yellow("!!") + f" {j.name} failed (exit {shown})\n")
        for line in list(j.tail)[-3:]:
            out.write(f"   {line}\n")
    out.flush()


# ---------------------------------------------------------------------------
# Executor — async port of update_all_clis.sh's run_updates_parallel
# ---------------------------------------------------------------------------
async def kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the job's whole process group, then SIGKILL after a grace
    period. Equivalent to the shell's _kill_tree (start_new_session makes
    the child a process-group leader, so killpg reaches grandchildren)."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=2)


class Executor:
    def __init__(self, jobs: list[Job], parallel: int, timeout: int,
                 skip: set[str], results: IO[str], renderer: BaseRenderer):
        self.jobs = jobs
        self.parallel = max(1, parallel)
        self.timeout = max(0, timeout)
        self.skip = skip
        self.results = results
        self.renderer = renderer
        self.sem = asyncio.Semaphore(self.parallel)
        self.group_locks: dict[str, asyncio.Lock] = {}
        self.procs: set[asyncio.subprocess.Process] = set()
        self.completed_counter = 0
        self.aborted = False

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.run_one(j)) for j in self.jobs]
        waiter = asyncio.ensure_future(self._wait_all(tasks))
        try:
            await waiter
        except asyncio.CancelledError:
            # SIGINT (possibly followed by a SIGTERM from the shell's cleanup
            # trap — both arrive while we're shutting down): cancel the
            # children, then keep waiting for their cancellation handlers
            # (which kill processes and write result records) to settle,
            # swallowing any repeated cancellations so cleanup always runs
            # to completion.
            self.aborted = True
            for t in tasks:
                t.cancel()
            while not waiter.done():
                try:
                    await asyncio.wait({waiter})
                except asyncio.CancelledError:
                    pass

    async def _wait_all(self, tasks: list[asyncio.Task]) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._kill_all()

    async def _kill_all(self) -> None:
        for proc in list(self.procs):
            await kill_proc_tree(proc)

    def _complete(self, job: Job, status: str, ec: int, note: str = "") -> None:
        now_mono = time.monotonic()
        job.status = status
        job.ec = ec
        job.note = note
        if job.start is None:
            job.start = time.time()
        job.end = time.time()
        if not job.mono_start:
            job.mono_start = now_mono
        job.mono_end = now_mono
        self.completed_counter += 1
        job.completed_seq = self.completed_counter
        self._write_result(job)
        self.renderer.job_event(job)

    def _write_result(self, job: Job) -> None:
        ec = job.ec if job.ec is not None else 1
        rec = ""
        if job.kind in RECORD_KINDS:
            start = int(job.start if job.start is not None else time.time())
            end = int(job.end if job.end is not None else start)
            rec = SEP.join([job.kind, job.name, job.cmd, str(ec), str(start), str(end)])
        self.results.write(f"{ec}{SEP}{rec}\n")
        self.results.flush()

    async def run_one(self, job: Job) -> None:
        try:
            await self._run_one_inner(job)
        except asyncio.CancelledError:
            # Aborted mid-flight (or never started): count as failed, like
            # the shell executor does for jobs killed by its cleanup trap.
            if job.status in (ST_PENDING, ST_RUNNING):
                self._complete(job, ST_FAILED, 1, note="interrupted")
            raise

    async def _run_one_inner(self, job: Job) -> None:
        # Instant kinds — no subprocess, same outcomes as the shell's
        # _run_one_emit_line_core.
        if job.kind == KIND_SKIP:
            self._complete(job, ST_SKIPPED, EC_SKIP)
            return
        if job.kind == KIND_HELD:
            self._complete(job, ST_HELD, EC_SKIP,
                           note="env" if job.cmd == "env" else "config")
            return
        if job.kind == KIND_QUARANTINED:
            self._complete(job, ST_QUARANTINED, EC_SKIP)
            return
        if job.kind == KIND_UPTODATE:
            # The cmd field carries the pre-check's duration; history wants
            # start = end - int(duration), mirroring the shell executors.
            dur_int = int(_float_or_zero(job.cmd))
            job.end = time.time()
            job.start = job.end - dur_int
            self._complete(job, ST_UPTODATE, 0)
            return
        if job.kind == KIND_KNOWN and job.name in self.skip:
            self._complete(job, ST_SKIPPED, EC_SKIP, note="--skip")
            return

        async with self.sem:
            # Jobs sharing a lock group (same package manager) serialize —
            # the in-process equivalent of the shell's per-origin lockdirs.
            lock = self.group_locks.setdefault(job.lock, asyncio.Lock())
            async with lock:
                await self._run_cmd(job)

    async def _run_cmd(self, job: Job) -> None:
        job.status = ST_RUNNING
        job.start = time.time()
        job.mono_start = time.monotonic()
        self.renderer.job_event(job)

        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", job.cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.procs.add(proc)
        timed_out = False
        try:
            if self.timeout > 0:
                cm = asyncio.timeout(self.timeout)
            else:
                cm = contextlib.nullcontext()
            async with cm:  # type: ignore[attr-defined]
                await self._pump(job, proc)
                await proc.wait()
        except TimeoutError:
            timed_out = True
            await kill_proc_tree(proc)
        except asyncio.CancelledError:
            await kill_proc_tree(proc)
            raise
        finally:
            self.procs.discard(proc)

        if timed_out:
            self._complete(job, ST_FAILED, 1,
                           note=f"timed out after {self.timeout}s")
        else:
            rc = proc.returncode if proc.returncode is not None else 1
            job.raw_ec = rc
            self._complete(job, ST_DONE if rc == 0 else ST_FAILED,
                           0 if rc == 0 else 1)

    async def _pump(self, job: Job, proc: asyncio.subprocess.Process) -> None:
        """Read job output in chunks, splitting on \\r and \\n so progress
        spinners (brew/npm use carriage returns) don't grow one endless
        "line". Keeps a short tail for failure reports and the last
        non-empty line for the dashboard."""
        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            *segs, buf = _SEGMENT_SPLIT_RE.split(buf)
            for seg in segs:
                self._handle_line(job, seg)
            if len(buf) > 65536:  # a line with no \r\n at all; bound it
                self._handle_line(job, buf[:65536])
                buf = buf[65536:]
        if buf:
            self._handle_line(job, buf)

    def _handle_line(self, job: Job, raw: bytes) -> None:
        text = strip_ansi(raw.decode("utf-8", "replace")).replace("\t", "    ")
        if not text:
            return
        if text.lower().startswith(("npm warn", "brew warn")):
            return
        if len(text) > 300:
            text = text[:300]
        job.tail.append(text)
        job.last_line = text
        self.renderer.line_event(job)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tui_update_all_clis.py",
        description="Live TUI executor for update-all-clis (see module docstring).",
    )
    p.add_argument("--emit-file", required=True,
                   help="plan file: one emit line per row")
    p.add_argument("--results-file", required=True,
                   help="output file for per-job result records")
    p.add_argument("--parallel", type=int, default=8,
                   help="max concurrent updates (default 8)")
    p.add_argument("--timeout", type=int, default=900,
                   help="per-job watchdog seconds; 0 disables (default 900)")
    p.add_argument("--skip", default="",
                   help="comma-separated known-tool names to skip")
    p.add_argument("--mode", choices=("auto", "live", "plain"), default="auto",
                   help="rendering mode (default: auto = live on terminals)")
    p.add_argument("--version-string", default="",
                   help="update-all-clis version shown in the dashboard header")
    return p.parse_args(argv)


def read_emit_file(path: str) -> list[Job]:
    jobs: list[Job] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            jobs.append(parse_emit_line(line, seq=len(jobs)))
    return jobs


async def amain(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        jobs = read_emit_file(args.emit_file)
    except OSError as e:
        print(f"tui_update_all_clis: cannot read emit file: {e}", file=sys.stderr)
        return 2

    live = args.mode == "live" or (args.mode == "auto" and _supports_live(sys.stdout))
    renderer: BaseRenderer
    if live and sys.stdout.isatty():
        renderer = LiveRenderer(jobs, args.version_string)
    else:
        renderer = PlainRenderer(jobs, args.version_string)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    with open(args.results_file, "w", encoding="utf-8") as results:
        executor = Executor(jobs, args.parallel, args.timeout, skip,
                            results, renderer)
        await renderer.start()
        try:
            await executor.run()
        finally:
            await renderer.finish(aborted=executor.aborted)

    return 130 if executor.aborted else 0


def main() -> int:
    # The shell's cleanup trap SIGTERMs this process on Ctrl+C (we're its
    # foreground child). Turn SIGTERM into the same graceful abort path as
    # SIGINT — kill job processes, flush results, restore the terminal —
    # instead of dying instantly mid-frame.
    def _sigterm_as_interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_as_interrupt)
    try:
        return asyncio.run(amain(sys.argv[1:]))
    except KeyboardInterrupt:
        return 130
    except asyncio.CancelledError:
        return 130


if __name__ == "__main__":
    sys.exit(main())
