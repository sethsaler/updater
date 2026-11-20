import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_npm_globals as updater


class NpmGlobalsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home = root / "user home"
        self.primary = self.home / ".npm-global"
        self.secondary = self.home / ".local"
        self.system = root / "brew"
        self.nvm = self.home / ".nvm/versions/node/v22"
        self.report = {}
        self.updates = []
        self.probes = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"HOME": str(self.home), "NVM_DIR": str(self.home / ".nvm")}).start()
        self.prefix = patch.object(subprocess, "check_output", return_value=str(self.primary)).start()
        patch.object(subprocess, "run", side_effect=self.fake_npm).start()

    def fake_npm(self, args, **kwargs):
        prefix = Path(args[args.index("--prefix") + 1])
        if args[1] == "outdated":
            self.probes.append(prefix)
            data = self.report.get(prefix, {})
            return subprocess.CompletedProcess(args, 1 if data else 0, json.dumps(data), "")
        self.updates.append((prefix, args[-1]))
        return subprocess.CompletedProcess(args, 0)

    def package(self, prefix, name, **extra):
        path = prefix / "lib/node_modules" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text(json.dumps({"name": name, **extra}))
        self.report.setdefault(prefix, {})[name] = dict(current="1.0.0", wanted="2.0.0", latest="2.0.0",
                                                        location=str(path))
        return path

    def test_externally_managed_active_packages_and_native_downgrades(self):
        skips = ["@scope/grok", "second-native"]
        for name in skips:
            self.package(self.primary, name)
        self.package(self.secondary, "@scope/grok")
        cases = [
            ("alpha-ahead", "1.0.92-alpha.1", "1.0.89", False),
            ("stable-ahead", "1.0.92", "1.0.89", False),
            ("stable-to-preview", "2.0.0", "2.0.0-rc.1", False),
            ("build-only", "2.0.0+alpha", "2.0.0+beta", False),
            ("preview-number", "2.0.0-rc.2", "2.0.0-rc.10", True),
            ("preview-to-stable", "2.0.0-beta", "2.0.0", True),
        ]
        for name, current, wanted, _ in cases:
            self.package(self.primary, name, version=current)
            self.report[self.primary][name].update(current=current, wanted=wanted, latest=wanted)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(updater.main(check=True, skip_active_package=skips), 1)
        rows = output.getvalue().splitlines()[1:]
        self.assertEqual(len(rows), 3)
        self.assertTrue(any(f"{self.secondary}/lib/node_modules/@scope/grok:" in row for row in rows))
        sidecar = self.home / "precheck.json"
        sidecar.write_text(json.dumps({"stdout": {"npm": output.getvalue()}}))
        with redirect_stdout(io.StringIO()), patch.dict(os.environ, {"UAC_PRECHECK_UPTODATE_FILE": str(sidecar)}):
            self.assertEqual(updater.main(skip_active_package=skips), 0)
        expected = [(self.primary, name) for name, _, _, upgrade in cases if upgrade]
        self.assertEqual(self.updates, expected + [(self.secondary, "@scope/grok")])
        self.assertEqual(self.probes, [self.primary, self.secondary])

    def test_cache_rechecks_exclusions_and_freshly_updated_installs(self):
        self.package(self.primary, "external")
        path = self.package(self.primary, "moving", version="1.0.0")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(updater.main(check=True), 1)
        sidecar = self.home / "precheck.json"
        sidecar.write_text(json.dumps({"stdout": {"npm": output.getvalue()}}))
        (path / "package.json").write_text(json.dumps({"name": "moving", "version": "3.0.0"}))
        with redirect_stdout(io.StringIO()), patch.dict(os.environ, {"UAC_PRECHECK_UPTODATE_FILE": str(sidecar)}):
            self.assertEqual(updater.main(skip_active_package=["external"]), 0)
        self.assertEqual(self.updates, [])
        self.assertEqual(self.probes, [self.primary, self.primary])

    def test_checks_and_updates_each_prefix_once_including_scoped_duplicates(self):
        for prefix in (self.primary, self.secondary, self.nvm):
            self.package(prefix, "@scope/tool")
        self.package(self.nvm, "npm")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(updater.main(check=True), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 4)
        self.assertIn(f"{self.secondary}/lib/node_modules/@scope/tool:@scope/tool@2.0", output.getvalue())
        sidecar = self.home / "precheck.json"
        sidecar.write_text(json.dumps({"stdout": {"npm": output.getvalue()}}))
        with redirect_stdout(io.StringIO()), patch.dict(os.environ, {"UAC_PRECHECK_UPTODATE_FILE": str(sidecar)}):
            self.assertEqual(updater.main(), 0)
        self.assertEqual(self.updates, [(p, "@scope/tool") for p in (self.primary, self.secondary, self.nvm)])
        self.assertEqual(self.probes, [self.primary, self.secondary, self.nvm])

    def test_preserves_local_and_host_owned_packages(self):
        self.prefix.return_value = str(self.system)
        self.package(self.system, "npm")
        self.package(self.system, "corepack")
        self.package(self.primary, "local", _from="local@file:../source")
        self.package(self.primary, "private-tool", private=True)
        path = self.package(self.primary, "checkout")
        path.rename(self.home / "checkout")
        path.symlink_to(self.home / "checkout")
        self.package(self.primary, "tarball")
        (self.primary / "lib/node_modules/.package-lock.json").write_text(json.dumps(
            {"packages": {"node_modules/tarball": {"resolved": "file:../pkg.tgz"}}}))
        self.package(self.primary, "missing")
        self.report[self.primary]["missing"].pop("current")
        self.package(self.primary, "npm")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(updater.main(), 0)
        self.assertEqual(self.updates, [(self.primary, "npm")])

    def test_failed_prefix_supplies_no_partial_precheck_and_others_still_update(self):
        self.package(self.primary, "tool")
        self.package(self.secondary, "second")
        self.report[self.primary] = {"error": {"code": "EAI_AGAIN"}}
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(updater.main(check=True), 2)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(updater.main(), 1)
        self.assertEqual(self.updates, [(self.secondary, "second")])
