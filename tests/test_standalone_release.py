import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_standalone_release as releases


def executable(name, version):
    return f"#!/bin/sh\nprintf '%s\\n' '{name} {version}'\n".encode()


def make_archive(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return result.getvalue()


class StandaloneReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def prepare(self, name, installed, target, entries, digest=None):
        binary = self.directory / name
        binary.write_bytes(executable(name, installed))
        binary.chmod(0o750)
        archive = make_archive(entries)
        tag = "v" + target
        filename = (f"superwhisper-{tag}-macos-universal.tar.gz" if name == "superwhisper"
                    else "gemini-api_Darwin_arm64.tar.gz")
        url = f"https://github.com/{releases.REPOS[name]}/releases/download/{tag}/{filename}"
        metadata = {"tag_name": tag, "draft": False, "prerelease": False, "assets": [
            {"name": filename, "browser_download_url": url,
             "digest": "sha256:" + (digest or hashlib.sha256(archive).hexdigest())}]}
        def fetch(address, dest):
            if address.endswith("releases/latest"):
                dest.write_text(json.dumps(metadata))
            else:
                self.assertEqual(address, url)
                dest.write_bytes(archive)
        return binary, patch.object(releases, "download", side_effect=fetch)

    def test_current_superwhisper_skips_download(self):
        binary, download = self.prepare("superwhisper", "0.2.0", "0.2.0", [])
        with download as calls:
            releases.update(binary)
        self.assertEqual(calls.call_count, 1)

    def test_gemini_installs_verified_target_and_retains_permissions(self):
        name = "gemini-api"
        binary, download = self.prepare(name, "0.4.9", "0.10.0", [(name, executable(name, "0.10.0"))])
        check = releases.subprocess.check_output
        def check_output(args, **kwargs):
            return "arm64" if args[0] == "lipo" else check(args, **kwargs)
        with download, patch.object(releases.subprocess, "check_output", side_effect=check_output):
            releases.update(binary)
        self.assertEqual(releases.version(binary), (0, 10, 0))
        self.assertEqual(binary.stat().st_mode & 0o777, 0o750)

    def test_invalid_checksum_or_binary_member_preserves_installation(self):
        name = "superwhisper"
        data = executable(name, "0.3.0")
        for entries, digest in [
            ([(name, data)], "0" * 64),
            ([("subdir/" + name, data)], None),
            ([(name, data), (name, data)], None),
            ([(name, executable(name, "0.1.9"))], None),
        ]:
            with self.subTest(entries=[entry[0] for entry in entries], digest=digest):
                binary, download = self.prepare(name, "0.2.0", "0.3.0", entries, digest)
                with download, self.assertRaises(ValueError):
                    releases.update(binary)
                self.assertEqual(releases.version(binary), (0, 2, 0))

    def test_symlink_target_is_not_replaced(self):
        actual = self.directory / "managed"
        actual.write_bytes(executable("superwhisper", "0.1.0"))
        binary = self.directory / "superwhisper"
        binary.symlink_to(actual)
        with patch.object(releases, "download") as download, self.assertRaises(ValueError):
            releases.update(binary)
        download.assert_not_called()
        self.assertTrue(binary.is_symlink())
