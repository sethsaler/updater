import subprocess
import unittest
from unittest.mock import patch

from scripts import update_raft as raft


def confirmation(version, target):
    return (f"What happened (UPGRADE_CONFIRMATION_REQUIRED): Resolved Computer {version}. "
            f"Re-run with --target-version {target} to approve that exact version "
            "in a non-interactive session.\nNext: raft-computer upgrade\n")


class RaftUpdateTests(unittest.TestCase):
    def test_current_install_only_uses_native_check(self):
        metadata = '{"schemaVersion":1,"channel":"latest","installedVersion":"1.25.9","versions":[{"version":"1.25.9","status":"active","label":"latest","installed":true}]}'
        with patch.object(raft.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=metadata)) as run:
            self.assertEqual(raft.update(), 0)
            self.assertEqual(run.call_count, 1)

    def test_modern_client_uses_native_unattended_flag(self):
        with patch.object(raft, "channel_is_current", return_value=False), \
             patch.object(raft.subprocess, "check_output", return_value="  -y, --yes   run unattended\n"), \
             patch.object(raft.subprocess, "call", return_value=0) as call:
            self.assertEqual(raft.update(), 0)
            self.assertEqual(call.call_args[0][0], ["raft-computer", "upgrade", "--yes"])

    def test_exact_target_from_saved_channel_is_confirmed_and_verified(self):
        output = confirmation("1.2.3-alpha.13", "1.2.3-alpha.13")
        first = subprocess.CompletedProcess([], 1, stdout=output)
        with patch.object(raft, "channel_is_current", return_value=False), \
             patch.object(raft.subprocess, "run", side_effect=[first, None]) as run, \
             patch.object(raft.subprocess, "check_output", side_effect=["  --dry-run", "1.2.3-alpha.13\n"]):
            self.assertEqual(raft.update(), 0)
            self.assertEqual(run.call_args[0][0], ["raft-computer", "upgrade", "--target-version", "1.2.3-alpha.13"])

    def test_other_failures_and_mismatched_targets_are_not_approved(self):
        for output in ("What happened (UPGRADE_SWAP_FAILED): Upgrade rolled back.",
                       confirmation("1.25.8", "1.25.9")):
            with patch.object(raft, "channel_is_current", return_value=False), \
                 patch.object(raft.subprocess, "check_output", return_value="  --dry-run"), \
                 patch.object(raft.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout=output)) as run:
                self.assertNotEqual(raft.update(), 0)
                self.assertEqual(run.call_count, 1)
