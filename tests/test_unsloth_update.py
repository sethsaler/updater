import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_unsloth as updater


class UnslothUpdateTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.studio = Path(td.name).resolve()
        self.prefix = self.studio / "unsloth_studio"
        self.launcher = self.prefix / "bin/unsloth"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.touch()
        (self.prefix / ".unsloth-studio-owned").touch()
        self.metadata = self.prefix / "lib/python3.13/site-packages/unsloth-1.dist-info"
        self.metadata.mkdir(parents=True)
        self.write_version("1")

    def write_version(self, version):
        (self.metadata / "METADATA").write_text(f"Name: unsloth\nVersion: {version}\n")
        (self.prefix / "unsloth_install_manifest.json").write_text(json.dumps(
            {"package": "unsloth", "prefix": str(self.prefix), "package_version": version}))

    def test_direct_source_skips_registry_and_setup(self):
        (self.metadata / "direct_url.json").write_text('{"url":"file:///a-checkout"}')
        with patch.object(updater, "compatible_version") as lookup:
            self.assertEqual(updater.run(self.launcher, self.studio), 0)
            lookup.assert_not_called()

    def test_current_install_skips_setup(self):
        with patch.object(updater, "compatible_version", return_value="1"), \
             patch.object(updater.subprocess, "check_output", return_value="False"), \
             patch.object(updater.subprocess, "run") as setup:
            self.assertEqual(updater.run(self.launcher, self.studio), 0)
            setup.assert_not_called()

    def test_upgrade_requires_idle_install_and_matching_receipt(self):
        with patch.object(updater, "compatible_version", return_value="2"), \
             patch.object(updater.subprocess, "check_output", return_value="True"), \
             patch.object(updater, "active_workloads", side_effect=[True, False, False]), \
             patch.object(updater.subprocess, "run") as setup:
            self.assertEqual(updater.run(self.launcher, self.studio), 0)
            setup.assert_not_called()
            with self.assertRaisesRegex(ValueError, "did not activate"):
                updater.run(self.launcher, self.studio)
            setup.side_effect = lambda *args, **kwargs: self.write_version("2")
            self.assertEqual(updater.run(self.launcher, self.studio), 0)
            setup.assert_called_with([str(self.launcher), "studio", "update"], cwd=self.prefix, check=True)

    def test_studio_processes_training_and_detached_children(self):
        current = f"{os.getpid()} python3 /path/to/updater.py\n"
        records = self.studio / "run/children"
        records.mkdir(parents=True)
        (records / "100.json").write_text('{"owner_pid":100,"children":[{"pid":101}]}')
        for command, busy in [(f"{self.prefix}/bin/python worker.py", True),
                              ("accelerate launch train.py", True), ("unrelated", False)]:
            with patch.object(updater.subprocess, "check_output", return_value=current + f"133 {command}"):
                self.assertEqual(updater.active_workloads(self.studio), busy)
        with patch.object(updater.subprocess, "check_output", return_value=current + "101 worker"):
            self.assertTrue(updater.active_workloads(self.studio))
