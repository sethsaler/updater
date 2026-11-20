#!/usr/bin/env python3
"""Follow Raft Computer's saved channel and approve only its exact resolved target."""

import json
import re
import subprocess
import sys


CONFIRM_TARGET = re.compile(
    r"^What happened \(UPGRADE_CONFIRMATION_REQUIRED\): Resolved Computer "
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._]+)?)\. "
    r"Re-run with --target-version (?P=version) to approve that exact version "
    r"in a non-interactive session\.$", re.MULTILINE
)


def channel_is_current():
    query = subprocess.run(["raft-computer", "channel", "versions", "--json", "--limit", "1"],
                           capture_output=True, text=True)
    if query.returncode:
        return False
    try:
        release = json.loads(query.stdout)
        candidate = release["versions"][0]
        channel = release["channel"]
        installed = release["installedVersion"]
        return (release["schemaVersion"] == 1 and candidate["installed"] is True
                and candidate["status"] == "active" and candidate["version"] == installed
                and (candidate["label"] == channel or channel == f"pinned:{installed}"))
    except (ValueError, IndexError, KeyError, TypeError):
        return False


def update():
    if channel_is_current():
        print("Raft Computer matches the active release on its saved channel.")
        return 0
    # Modern clients support an unattended install. Older clients first
    # resolve their saved channel and then require approval of its exact tag.
    help_text = subprocess.check_output(["raft-computer", "upgrade", "--help"], text=True)
    if re.search(r"^\s*(?:-y,\s*)?--yes(?:\s|$)", help_text, re.MULTILINE):
        return subprocess.call(["raft-computer", "upgrade", "--yes"], stdin=subprocess.DEVNULL)
    result = subprocess.run(["raft-computer", "upgrade"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode == 0:
        print(result.stdout, end="")
        return 0
    confirmation = CONFIRM_TARGET.search(result.stdout)
    if not confirmation:
        print(result.stdout, end="", file=sys.stderr)
        return result.returncode
    target = confirmation["version"]
    subprocess.run(["raft-computer", "upgrade", "--target-version", target],
                   stdin=subprocess.DEVNULL, check=True)
    installed = subprocess.check_output(["raft-computer", "--version"], text=True).strip()
    if installed != target:
        print(f"Raft reported success for {target}, but its installed version is {installed}.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(update())
    except (OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Raft Computer update failed: {error}")
