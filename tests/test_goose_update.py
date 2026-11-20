import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_goose as goose


class GooseUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        self.binary = home / ".local/bin/goose"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"previous version")
        self.binary.chmod(0o755)
        for mock in (
            patch.object(goose.shutil, "which", return_value=str(self.binary)),
            patch.object(Path, "home", return_value=home),
            patch.object(goose.sys, "platform", "darwin"),
            patch.dict(os.environ, {"CANARY": ""}),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def test_current_version_does_not_download_archive(self):
        with patch.object(goose, "version", return_value="1.60.2"), \
             patch.object(goose, "latest_tag", return_value="v1.60.2"), \
             patch.object(goose.subprocess, "run") as run:
            self.assertEqual(goose.update(), 0)
            run.assert_not_called()

    def test_provenance_rejection_is_not_an_initialization_failure(self):
        native = subprocess.CompletedProcess([], 1, stdout="Sigstore verification failed")
        with patch.object(goose, "version", return_value="1.60.1"), \
             patch.object(goose, "latest_tag", return_value="v1.60.2"), \
             patch.object(goose.subprocess, "run", return_value=native), \
             patch.object(goose, "recover") as recover:
            self.assertEqual(goose.update(), 1)
            recover.assert_not_called()

    def test_failed_independent_verification_preserves_previous_binary(self):
        def reject(command, **_kwargs):
            if command[1] == "attestation":
                self.assertIn("refs/tags/v1.60.2", command)
                self.assertIn(f"https://github.com/{goose.WORKFLOW}@refs/tags/v1.60.2", command)
                raise subprocess.CalledProcessError(1, command)
        with patch.object(goose.subprocess, "check_output", return_value="arm64"), \
             patch.object(goose.subprocess, "run", side_effect=reject):
            with self.assertRaises(subprocess.CalledProcessError):
                goose.recover(self.binary, "v1.60.2", self.binary.lstat())
        self.assertEqual(self.binary.read_bytes(), b"previous version")

    def test_attested_archive_replaces_the_standalone_binary(self):
        content = b"#!/bin/sh\necho '1.60.2'\n"
        def download(command, **_kwargs):
            if command[1] == "release":
                workspace = Path(command[command.index("--dir") + 1])
                archive = workspace / command[command.index("--pattern") + 1]
                entry = tarfile.TarInfo("./goose")
                entry.size = len(content)
                with tarfile.open(archive, "w:bz2") as result:
                    result.addfile(entry, io.BytesIO(content))
        with patch.object(goose.subprocess, "check_output", return_value="arm64"), \
             patch.object(goose, "version", return_value="1.60.2"), \
             patch.object(goose.subprocess, "run", side_effect=download):
            goose.recover(self.binary, "v1.60.2", self.binary.lstat())
        self.assertEqual(self.binary.read_bytes(), content)
        self.assertTrue(os.access(self.binary, os.X_OK))
