import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_jevgram as helper


class JevgramUpdateTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.home = Path(workspace.name)
        self.venv = self.home / ".local/share/jevgram/.venv"
        self.receipt = self.venv / "lib/python3.14/site-packages/jevgram-0.1.0.dist-info"
        self.receipt.mkdir(parents=True)
        (self.receipt / "INSTALLER").write_text("pip\n")
        (self.receipt / "entry_points.txt").write_text(
            "[console_scripts]\njevgram = jevgram.cli:main\njevgram-web = jevgram.web:main\n")
        (self.venv / "pyvenv.cfg").touch()
        (self.venv / "bin").mkdir()
        (self.home / ".local/bin").mkdir(parents=True)
        for name in helper.ENTRY_POINTS:
            target = self.venv / "bin" / name
            target.touch()
            (self.home / ".local/bin" / name).write_text(f'exec "{target}" "$@"\n')
        self.current = "a" * 40
        self.latest = "b" * 40
        self.record(self.current)

    def record(self, sha, revision=None, url=helper.URL):
        vcs = {"vcs": "git", "commit_id": sha}
        if revision:
            vcs["requested_revision"] = revision
        (self.receipt / "direct_url.json").write_text(json.dumps({"url": url, "vcs_info": vcs}))

    def test_current_checks_git_once_and_never_calls_pip(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, f"ref: refs/heads/main\tHEAD\n{self.current}\tHEAD\n", "")) as run:
            helper.update(self.home)
        self.assertEqual(run.call_args.args[0], ["git", "ls-remote", "--symref", helper.URL, "HEAD"])
        self.assertEqual(run.call_count, 1)

    def test_missing_source_is_visible_and_network_failure_remains_an_error(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), patch.object(subprocess, "run", return_value=
                subprocess.CompletedProcess([], 128, "", "remote: Repository not found.\n")) as run:
            helper.update(self.home)
        self.assertEqual(run.call_count, 1)
        self.assertIn("Deferred Jevgram and jevgram-web", errors.getvalue())
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 128, "", "fatal: Could not resolve host: github.com\n")), self.assertRaisesRegex(
                    RuntimeError, "Could not resolve host"):
            helper.update(self.home)

    def test_reinstall_only_same_version_package_after_resolving_dependencies(self):
        commands = []

        def run(args, **options):
            commands.append(args)
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, f"{self.latest}\tHEAD\n", "")
            if "--force-reinstall" in args:
                self.record(self.latest)
            return subprocess.CompletedProcess(args, 0)

        with patch.object(subprocess, "run", side_effect=run):
            helper.update(self.home)
        self.assertEqual(len(commands), 4)
        requirement = "jevgram[web] @ git+" + helper.URL
        self.assertEqual(commands[1][-3:], ["install", "--upgrade", requirement])
        self.assertEqual(commands[2][-4:], ["install", "--no-deps", "--force-reinstall", requirement])
        self.assertEqual(commands[3][-1], "check")
        self.assertTrue(all(cmd[0] == str(self.venv / "bin/python") for cmd in commands[1:]))

    def test_new_version_and_tracking_branch_require_only_one_install(self):
        self.record(self.current, "main")
        commands = []

        def run(args, **options):
            commands.append(args)
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, f"{self.latest}\trefs/heads/main\n", "")
            if "install" in args:
                self.record(self.latest, "main")
            return subprocess.CompletedProcess(args, 0)

        with patch.object(subprocess, "run", side_effect=run):
            helper.update(self.home)
        self.assertEqual(len(commands), 3)
        self.assertIn("refs/heads/main", commands[0])
        self.assertEqual(commands[1][-1], "jevgram[web] @ git+" + helper.URL + "@main")

    def test_immutable_pin_and_changed_ownership(self):
        self.record(self.current, self.current)
        with patch.object(subprocess, "run") as run:
            helper.update(self.home)
            run.assert_not_called()
            self.record(self.current, url="file:///workspace")
            with self.assertRaisesRegex(ValueError, "receipt"):
                helper.update(self.home)
            self.record(self.current)
            (self.home / ".local/bin/jevgram-web").write_text("exec another-package\n")
            with self.assertRaisesRegex(ValueError, "jevgram-web"):
                helper.update(self.home)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
