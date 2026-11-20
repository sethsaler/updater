#!/usr/bin/env python3
"""Maintain the Git-sourced Jevgram package behind the two installer launchers."""

import configparser
import json
import os
from pathlib import Path
import re
import subprocess
import sys


URL = "https://github.com/sethsaler/jevgram.git"
ENTRY_POINTS = {"jevgram": "jevgram.cli:main", "jevgram-web": "jevgram.web:main"}
COMMIT = re.compile(r"[0-9a-f]{40}")


def installed_source(venv):
    receipts = list(venv.glob("lib/python*/site-packages/jevgram-*.dist-info"))
    if len(receipts) != 1 or (receipts[0] / "INSTALLER").read_text().strip() != "pip":
        raise ValueError("Expected one pip-owned Jevgram installation")
    entries = configparser.ConfigParser()
    entries.read(receipts[0] / "entry_points.txt")
    if any(entries.get("console_scripts", name, fallback=None) != entry
           for name, entry in ENTRY_POINTS.items()):
        raise ValueError("The Jevgram distribution does not own both console scripts")
    source = json.loads((receipts[0] / "direct_url.json").read_text())
    vcs = source.get("vcs_info", {})
    commit = vcs.get("commit_id", "")
    if source.get("url") != URL or vcs.get("vcs") != "git" or not COMMIT.fullmatch(commit):
        raise ValueError("Jevgram's pip receipt does not match its installer Git source")
    return commit, vcs.get("requested_revision") or ""


def upstream(revision, env):
    refs = ["HEAD"]
    if revision:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_-]*", revision):
            raise ValueError("Unrecognized pinned Jevgram revision")
        if revision.startswith(("refs/tags/", "refs/heads/")):
            refs = [revision]
        else:
            refs = ["refs/heads/" + revision, "refs/tags/" + revision]
        refs.extend(ref + "^{}" for ref in list(refs) if ref.startswith("refs/tags/"))
    result = subprocess.run(["git", "ls-remote", "--symref", URL, *refs],
                            env=env, capture_output=True, text=True)
    if result.returncode:
        if re.search(r"(?im)^remote: repository not found\.?\s*$|^fatal: repository '[^']+' not found\s*$",
                     result.stderr):
            print("Deferred Jevgram and jevgram-web: GitHub reports the recorded repository "
                  f"{URL} is missing or inaccessible. Will retry on the next run.", file=sys.stderr)
            return None
        detail = next((term for term in ("Could not resolve host", "Authentication failed",
                                         "Connection timed out", "Failed to connect")
                       if term.lower() in result.stderr.lower()), "git ls-remote failed")
        raise RuntimeError(f"Cannot check Jevgram's upstream: {detail} (exit {result.returncode})")
    remote = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref in refs and COMMIT.fullmatch(sha):
            remote[ref] = sha
    # For annotated tags, compare the dereferenced commit instead of the tag object.
    candidates = {
        remote.get(ref + "^{}", sha) for ref, sha in remote.items()
        if not ref.endswith("^{}")
    }
    if len(candidates) != 1:
        raise ValueError("The recorded Jevgram revision has no unique upstream commit")
    return candidates.pop()


def update(home):
    venv = home / ".local/share/jevgram/.venv"
    if venv.is_symlink() or not (venv / "pyvenv.cfg").is_file():
        raise ValueError("Expected Jevgram's dedicated installer environment")
    for name in ENTRY_POINTS:
        wrapper = home / ".local/bin" / name
        target = venv / "bin" / name
        if (wrapper.is_symlink() or not wrapper.is_file() or not target.is_file()
                or f'exec "{target}" "$@"' not in wrapper.read_text().splitlines()):
            raise ValueError(f"{name} does not use the dedicated Jevgram installation")
    current, revision = installed_source(venv)
    if COMMIT.fullmatch(revision):
        if current != revision:
            raise ValueError("Jevgram's receipt conflicts with its requested immutable revision")
        print(f"Jevgram and jevgram-web are pinned to {current[:12]}")
        return
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    latest = upstream(revision, env)
    if latest is None:
        return
    if latest == current:
        print(f"Jevgram and jevgram-web are current at {current[:12]}")
        return
    # Preserve the original branch specification in the receipt so subsequent
    # updates keep tracking that branch. Resolve the web extra's dependencies.
    requirement = f"jevgram[web] @ git+{URL}" + (f"@{revision}" if revision else "")
    pip = [str(venv / "bin/python"), "-m", "pip", "--isolated", "--disable-pip-version-check",
           "--no-input"]
    subprocess.run([*pip, "install", "--upgrade", requirement], env=env, check=True)
    if installed_source(venv) == (current, revision):
        # Pip can leave the old commit installed when an upstream project keeps
        # the same package version. Its dependency resolution above still ran.
        subprocess.run([*pip, "install", "--no-deps", "--force-reinstall", requirement],
                       env=env, check=True)
    installed, recorded_revision = installed_source(venv)
    if installed != latest or recorded_revision != revision:
        raise ValueError("Jevgram's installed receipt differs from the checked revision; retry")
    subprocess.run([*pip, "check"], env=env, check=True)
    print(f"Updated Jevgram and jevgram-web to {installed[:12]}")


if __name__ == "__main__":
    try:
        update(Path.home())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"Jevgram update failed: {error}")
