#!/usr/bin/env python3
"""Update Goose with a release precheck and attested recovery for standalone macOS."""

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


REPO = "aaif-goose/goose"
WORKFLOW = f"{REPO}/.github/workflows/release.yml"
STABLE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def version(binary):
    return subprocess.check_output([binary, "--version"], text=True).strip()


def latest_tag():
    response = subprocess.check_output([
        "curl", "--fail", "--silent", "--show-error", "--max-time", "30",
        f"https://api.github.com/repos/{REPO}/releases/latest",
    ])
    release = json.loads(response)
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("GitHub did not return a stable Goose release tag")
    return tag


def recover(binary, tag, original):
    arch = subprocess.check_output(["lipo", "-archs", binary], text=True).strip()
    target = {"arm64": "aarch64", "x86_64": "x86_64"}.get(arch)
    if not target:
        raise ValueError(f"Unsupported installed Mach-O architecture: {arch}")
    asset = f"goose-{target}-apple-darwin.tar.bz2"
    ref = f"refs/tags/{tag}"
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / asset
        subprocess.run(["gh", "release", "download", tag, "--repo", REPO,
                        "--pattern", asset, "--dir", directory], check=True)
        subprocess.run(["gh", "attestation", "verify", archive, "--repo", REPO,
                        "--cert-identity", f"https://github.com/{WORKFLOW}@{ref}",
                        "--source-ref", ref, "--deny-self-hosted-runners",
                        "--predicate-type", "https://slsa.dev/provenance/v1"], check=True)

        # Extract only the named regular file. Stage it on the target filesystem
        # so readers see either the original executable or the complete replacement.
        with tempfile.TemporaryDirectory(prefix=".goose-update-", dir=binary.parent) as staging:
            staged = Path(staging) / "goose"
            with tarfile.open(archive, "r:bz2") as release:
                files = [item for item in release if item.name in {"goose", "./goose"}]
                if len(files) != 1 or not files[0].isfile():
                    raise ValueError("Verified Goose archive lacks a unique regular CLI executable")
                with release.extractfile(files[0]) as source, staged.open("xb") as output:
                    shutil.copyfileobj(source, output)
            staged.chmod(stat.S_IMODE(original.st_mode) & 0o777)
            if version(staged) != tag[1:]:
                raise ValueError("Verified executable version differs from release tag")
            current = binary.lstat()
            if (current.st_dev, current.st_ino, current.st_mtime_ns) != (
                original.st_dev, original.st_ino, original.st_mtime_ns
            ):
                raise ValueError("Goose changed during verification; retry against the new installation")
            os.replace(staged, binary)


def update():
    executable = shutil.which("goose")
    if not executable:
        raise ValueError("Goose is missing from PATH")
    binary = Path(executable)
    resolved = binary.resolve()
    if "Cellar" in resolved.parts:
        formula = resolved.parts[resolved.parts.index("Cellar") + 1]
        return subprocess.call(["brew", "upgrade", formula])
    if os.environ.get("CANARY", "").lower() == "true":
        return subprocess.call([binary, "update", "--canary"])

    # Restrict independent replacement to a regular binary in the upstream
    # stand-alone installer's default location. Other installers retain their
    # native command and ownership of their own links and package files.
    standalone = (sys.platform == "darwin" and binary == Path.home() / ".local/bin/goose"
                  and not binary.is_symlink())
    if not standalone:
        return subprocess.call([binary, "update"])
    original = binary.lstat()
    installed = version(binary)
    if not STABLE.fullmatch(installed):
        raise ValueError(f"Goose version {installed!r} is outside the stable channel")
    tag = latest_tag()
    latest = tag[1:]
    if installed == latest:
        print(f"Goose {installed} is current")
        return 0
    if tuple(map(int, installed.split("."))) > tuple(map(int, latest.split("."))):
        raise ValueError(f"Installed Goose {installed} is newer than the stable release {latest}")
    native = subprocess.run([binary, "update"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    print(native.stdout, end="")
    if native.returncode == 0 or "no sigstore-crypto CryptoProvider installed" not in native.stdout:
        return native.returncode
    print("Goose's built-in verifier could not initialize. Verifying the tagged release with GitHub CLI.")
    recover(binary, tag, original)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(update())
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        sys.exit(f"Goose update failed: {error}")
