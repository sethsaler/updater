#!/usr/bin/env python3
"""Upgrade standalone Gemini API and Superwhisper CLIs from their stable macOS releases."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

if __package__:
    from .update_entire import download, file_identity
else:
    from update_entire import download, file_identity


REPOS = {"gemini-api": "google-gemini/gemini-api-cli",
         "superwhisper": "superultrainc/superwhisper-cli-release"}
SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+"


def version(binary):
    output = subprocess.check_output([binary, "--version"], text=True)
    match = re.fullmatch(rf"{re.escape(binary.name)}\s+v?({SEMVER})", output.splitlines()[0])
    if not match:
        raise ValueError(f"Unrecognized {binary.name} version or executable identity")
    return tuple(map(int, match[1].split(".")))


def archive_name(binary, tag):
    if binary.name == "superwhisper":
        return f"superwhisper-{tag}-macos-universal.tar.gz"
    arch = subprocess.check_output(["lipo", "-archs", binary], text=True).strip()
    if arch not in {"arm64", "x86_64"}:
        raise ValueError(f"Unsupported Gemini CLI architecture: {arch}")
    return f"gemini-api_Darwin_{arch}.tar.gz"


def update(binary):
    name = binary.name
    repo = REPOS[name]
    original = binary.lstat()
    if not stat.S_ISREG(original.st_mode) or original.st_uid != os.geteuid():
        raise ValueError(f"{binary} is not a standalone regular file owned by this user")
    identity = file_identity(binary)
    installed = version(binary)
    with tempfile.TemporaryDirectory(prefix=f".{name}-update-", dir=binary.parent) as directory:
        stage = Path(directory)
        metadata = stage / "release.json"
        download(f"https://api.github.com/repos/{repo}/releases/latest", metadata)
        release = json.loads(metadata.read_bytes())
        tag = release["tag_name"]
        match = re.fullmatch(rf"v({SEMVER})", tag)
        if not match or release["draft"] is not False or release["prerelease"] is not False:
            raise ValueError(f"{repo} did not return a stable tagged release")
        target = tuple(map(int, match[1].split(".")))
        if installed == target:
            print(f"{name} {tag} is current")
            return
        if installed > target:
            raise ValueError(f"Installed {name} is newer than {repo} stable release {tag}")
        filename = archive_name(binary, tag)
        url = f"https://github.com/{repo}/releases/download/{tag}/{filename}"
        assets = [asset for asset in release["assets"] if asset["name"] == filename]
        if len(assets) != 1 or assets[0]["browser_download_url"] != url:
            raise ValueError(f"No unique official {filename} asset for {tag}")
        expected = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", assets[0].get("digest") or "")
        if not expected:
            raise ValueError(f"GitHub did not publish the SHA-256 of {filename}")

        archive = stage / filename
        download(url, archive)
        digest = hashlib.sha256()
        with archive.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected[1].lower():
            raise ValueError(f"{filename} disagrees with GitHub's asset digest")
        staged = stage / name
        with tarfile.open(archive, "r:gz") as release_archive:
            members = [item for item in release_archive if item.name == name]
            if len(members) != 1 or not members[0].isfile():
                raise ValueError(f"Expected one top-level regular {name} in {filename}")
            with release_archive.extractfile(members[0]) as source, staged.open("xb") as dest:
                shutil.copyfileobj(source, dest)
        if staged.stat().st_gid != original.st_gid:
            os.chown(staged, -1, original.st_gid)
        staged.chmod(stat.S_IMODE(original.st_mode) & 0o777)
        if version(staged) != target:
            raise ValueError(f"Staged {name} executable did not report {tag}")
        if file_identity(binary) != identity:
            raise ValueError(f"{name} changed during download; retry against the new installation")
        os.replace(staged, binary)
        print(f"Updated {name} to {tag}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in REPOS:
        sys.exit("Usage: update_standalone_release.py {gemini-api|superwhisper}")
    try:
        name = sys.argv[1]
        binary = Path.home() / ".local/bin" / name
        if sys.platform != "darwin" or shutil.which(name) != str(binary):
            sys.exit(f"The {name} recipe requires the standalone macOS installation at {binary}.")
        update(binary)
    except (KeyError, IndexError, OSError, ValueError, TypeError, tarfile.TarError,
            subprocess.CalledProcessError) as error:
        sys.exit(f"{sys.argv[1]} update failed: {error}")
