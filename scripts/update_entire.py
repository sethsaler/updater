#!/usr/bin/env python3
"""Update the standalone Entire CLI and its bundled Git transport on their installed channel."""

import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


ORIGIN = "https://install.entire.com"
NAMES = ("entire", "git-remote-entire")
TAG = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)(?:-nightly\.([0-9]+)\.[a-f0-9]+)?")


def build_info(binary):
    # Older nightly binaries printed the preceding stable semver. The Go build
    # info identifies both the actual main module version and the executable.
    if binary.is_symlink() or not binary.is_file():
        raise ValueError(f"Expected a standalone regular file at {binary}")
    prefix = f"path\tgithub.com/entireio/cli/cmd/{binary.name}\nmod\tgithub.com/entireio/cli\t".encode()
    with binary.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
        position = data.find(prefix)
        if position < 0:
            raise ValueError(f"Could not verify Entire module ownership of {binary}")
        # The Go module and build settings are stored as one text record.
        record = data[position + len(prefix):position + len(prefix) + 65536]
    tag = record.partition(b"\t")[0].decode("ascii")
    os_match = re.search(rb"(?m)^build\tGOOS=(darwin|linux)$", record)
    arch_match = re.search(rb"(?m)^build\tGOARCH=(arm64|amd64)$", record)
    if not TAG.fullmatch(tag) or not os_match or not arch_match:
        raise ValueError(f"Unrecognized Entire release or target in {binary}")
    return tag, os_match[1].decode(), arch_match[1].decode()


def tag_order(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"Invalid Entire release tag: {tag!r}")
    return tuple(int(part or "0") for part in match.groups())


def download(url, target):
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10",
                    "--speed-limit", "1024", "--speed-time", "30", "--output", target, url], check=True)


def file_identity(path):
    try:
        current = path.lstat()
    except FileNotFoundError:
        return None
    return current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns


def update(bin_dir):
    binary = bin_dir / "entire"
    installed, system, arch = build_info(binary)
    helper = bin_dir / "git-remote-entire"
    helper_build = build_info(helper) if os.path.lexists(helper) else None
    original = (file_identity(binary), file_identity(helper))
    channel = "nightly" if "-nightly." in installed else "stable"
    with tempfile.TemporaryDirectory(prefix=".entire-update-", dir=bin_dir) as workspace:
        stage = Path(workspace)
        manifest = stage / "channel.json"
        download(f"{ORIGIN}/channels/{channel}.json", manifest)
        metadata = json.loads(manifest.read_bytes())
        target = metadata["tag"]
        order = tag_order(target)
        if metadata["channel"] != channel or ("-nightly." in target) != (channel == "nightly"):
            raise ValueError(f"Entire {channel} manifest points to a different channel")
        if order < tag_order(installed):
            raise ValueError(f"Installed Entire {installed} is newer than the {channel} manifest {target}")
        expected = (target, system, arch)
        if target == installed and helper_build == expected:
            print(f"Entire {installed} and its Git helper are current on {channel}")
            return

        filename = f"entire_{system}_{arch}.tar.gz"
        archive = stage / filename
        sums = stage / "checksums.txt"
        download(f"{ORIGIN}/{target}/checksums.txt", sums)
        digests = re.findall(rf"(?m)^([a-fA-F0-9]{{64}})[ \t]+\*?{re.escape(filename)}$", sums.read_text())
        if len(digests) != 1:
            raise ValueError(f"Entire did not publish a unique SHA-256 for {filename}")
        download(f"{ORIGIN}/{target}/{filename}", archive)
        digest = hashlib.sha256()
        with archive.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != digests[0].lower():
            raise ValueError("Entire archive checksum mismatch")

        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for name in NAMES:
                matches = [entry for entry in members if entry.name in (name, f"./{name}")]
                if len(matches) != 1 or not matches[0].isfile():
                    raise ValueError(f"Expected exactly one regular {name} in the Entire release")
                staged = stage / name
                with tar.extractfile(matches[0]) as source, staged.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
                staged.chmod(0o755)
                if build_info(staged) != expected:
                    raise ValueError(f"Staged {name} does not match {target} for {system}/{arch}")

        # Publish the primary executable last. Restore the Git helper if the
        # second rename fails; an interrupted run checks both versions on retry.
        previous = stage / "previous-helper"
        current = (file_identity(binary), file_identity(helper))
        if current != original:
            raise ValueError("Entire installation changed during download; retry the update")
        if helper_build:
            os.link(helper, previous)
        os.replace(stage / helper.name, helper)
        try:
            os.replace(stage / binary.name, binary)
        except OSError:
            if helper_build:
                os.replace(previous, helper)
            else:
                helper.unlink()
            raise
        print(f"Updated Entire and its Git helper from {installed} to {target}")


if __name__ == "__main__":
    try:
        standalone = Path.home() / ".local/bin"
        found = shutil.which("entire")
        if not found or Path(found) != standalone / "entire":
            sys.exit("The active Entire executable belongs to another installation; use its package manager.")
        update(standalone)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        sys.exit(f"Entire update failed: {error}")
