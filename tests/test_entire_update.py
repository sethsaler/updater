import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_entire as entire


def binary(name, tag):
    return (f"path\tgithub.com/entireio/cli/cmd/{name}\nmod\tgithub.com/entireio/cli\t{tag}\t\n"
            "build\tGOARCH=arm64\nbuild\tGOOS=darwin\n").encode()


class EntireUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bin_dir = Path(directory.name)
        self.old = "v0.12.3-nightly.202610261128.c4f103d8df"
        self.new = "v0.13.5-nightly.202611160626.76f1b5129"
        for name in entire.NAMES:
            (self.bin_dir / name).write_bytes(binary(name, self.old))

    def test_current_stable_and_nightly_only_fetch_metadata(self):
        for tag, channel in [(self.old, "nightly"), ("v0.13.4", "stable")]:
            for name in entire.NAMES:
                (self.bin_dir / name).write_bytes(binary(name, tag))
            with patch.object(entire, "download", side_effect=lambda url, path: path.write_text(
                    json.dumps({"tag": tag, "channel": channel}))) as download:
                entire.update(self.bin_dir)
                self.assertEqual(download.call_count, 1)
                self.assertTrue(download.call_args[0][0].endswith(f"/channels/{channel}.json"))

    def test_misrouted_manifest_preserves_installed_nightly(self):
        with patch.object(entire, "download", side_effect=lambda url, path: path.write_text(
                json.dumps({"tag": "v0.13.4", "channel": "stable"}))):
            with self.assertRaisesRegex(ValueError, "different channel"):
                entire.update(self.bin_dir)
        self.assertEqual(entire.build_info(self.bin_dir / "entire")[0], self.old)

    def test_checksum_failure_preserves_both_then_verified_release_updates_both(self):
        content = io.BytesIO()
        with tarfile.open(mode="w:gz", fileobj=content) as archive:
            for name in entire.NAMES:
                data = binary(name, self.new)
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        raw = content.getvalue()
        digest = "0" * 64
        def serve(url, dest):
            if url.endswith(".json"):
                dest.write_text(json.dumps({"channel": "nightly", "tag": self.new}))
            elif url.endswith("checksums.txt"):
                dest.write_text(f"{digest}  entire_darwin_arm64.tar.gz\n")
            else:
                dest.write_bytes(raw)
        with patch.object(entire, "download", side_effect=serve):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                entire.update(self.bin_dir)
            for name in entire.NAMES:
                self.assertEqual(entire.build_info(self.bin_dir / name)[0], self.old)
            digest = hashlib.sha256(raw).hexdigest()
            entire.update(self.bin_dir)
        for name in entire.NAMES:
            self.assertEqual(entire.build_info(self.bin_dir / name)[0], self.new)

    def test_unowned_helper_is_left_untouched(self):
        helper = self.bin_dir / "git-remote-entire"
        helper.write_bytes(b"other executable")
        with patch.object(entire, "download") as download:
            with self.assertRaisesRegex(ValueError, "ownership"):
                entire.update(self.bin_dir)
            download.assert_not_called()
        self.assertEqual(helper.read_bytes(), b"other executable")
