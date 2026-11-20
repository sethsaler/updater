#!/usr/bin/env python3
"""Upgrade the standalone uv runtime, managed Python patches and installed tools."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
try:
    import tomllib
except ImportError:  # System Python on older macOS installations
    tomllib = None


def owns_runtime(binary):
    home_config = Path.home() / ".config"
    receipt_dirs = [Path(os.environ.get("XDG_CONFIG_HOME", home_config)) / "uv", home_config / "uv"]
    if "AXOUPDATER_CONFIG_WORKING_DIR" in os.environ:
        receipt_dirs = [Path.cwd()]
    elif "AXOUPDATER_CONFIG_PATH" in os.environ:
        receipt_dirs = [Path(os.environ["AXOUPDATER_CONFIG_PATH"])]
    for directory in receipt_dirs:
        receipt = directory / "uv-receipt.json"
        if receipt.is_file():
            data = json.loads(receipt.read_text())
            prefix = Path(data["install_prefix"])
            # Standalone receipts can record a flat bin directory or a prefix.
            return any(path.is_file() and not path.is_symlink() and path.samefile(binary)
                       for path in (prefix / "uv", prefix / "bin" / "uv"))
    return False


def tool_update_command(binary, directory, name):
    environment = directory / name
    receipt = environment / "uv-receipt.toml"
    content = receipt.read_text()
    standard_install = False
    if tomllib:
        tool = tomllib.loads(content)["tool"]
        requirements = tool["requirements"]
        paths = [req[key] for req in requirements for key in ("path", "editable", "directory")
                 if isinstance(req, dict) and isinstance(req.get(key), str)]
        installed = [(Path(entry["install-path"]), entry["name"])
                     for entry in tool["entrypoints"]]
        # An isolated pip upgrade can reproduce an ordinary registry install.
        # Tools with additional requirements, indexes or constraints need
        # uv's receipt-aware updater and must wait for their launcher conflict.
        standard_install = (requirements == [{"name": name}]
                            and set(tool) <= {"requirements", "entrypoints", "python"})
    else:
        field = re.search(r"(?ms)^requirements\s*=\s*\[(.*?)\]\s*$", content)
        paths = [json.loads(match[1]) for match in re.finditer(
            r'\b(?:path|editable|directory)\s*=\s*("(?:[^"\\]|\\.)*")', field[1] if field else "")]
        installed = []
        for match in re.finditer(r'\binstall-path\s*=\s*("(?:[^"\\]|\\.)*")', content):
            path = Path(json.loads(match[1]))
            installed.append((path, path.name))
    for value in paths:
        source = Path(value)
        if not source.exists():
            print(f"Deferred {name}: installation source {source} is missing.", flush=True)
            return None
    if not installed:
        raise ValueError("install receipt has no verifiable launchers")
    foreign = []
    for launcher, executable in installed:
        target = environment / "bin" / executable
        if os.path.lexists(launcher) and launcher.resolve() != target.resolve():
            if not (target.exists() and launcher.exists() and launcher.samefile(target)):
                foreign.append(launcher.name)
    if foreign:
        owners = ", ".join(foreign)
        if standard_install:
            print(f"Updating {name} inside its environment, preserving external launchers: {owners}.",
                  flush=True)
            return [binary, "pip", "install", "--python", str(environment / "bin" / "python"),
                    "--upgrade", name]
        print(f"Deferred {name}: external launchers ({owners}) conflict with the tool receipt.", flush=True)
        return None
    return [binary, "tool", "upgrade", name]


def main():
    binary = shutil.which("uv")
    if not binary:
        print("uv executable is missing.", file=sys.stderr)
        return 1
    failed = False
    try:
        standalone = owns_runtime(binary)
    except (ValueError, KeyError, OSError) as exc:
        print(f"Cannot verify uv install receipt: {exc}", file=sys.stderr)
        failed = True
        standalone = False
    if standalone:
        failed = subprocess.run([binary, "self", "update"]).returncode != 0

    supported = subprocess.run([binary, "python", "upgrade", "--help"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).returncode == 0
    if supported:
        managed = subprocess.run([binary, "python", "list", "--managed-python", "--only-installed"],
                                 stdout=subprocess.PIPE, text=True)
        if managed.returncode:
            failed = True
        elif any(line.startswith("cpython-") for line in managed.stdout.splitlines()):
            if subprocess.run([binary, "python", "upgrade"]).returncode:
                failed = True

    directory = Path(subprocess.check_output([binary, "tool", "dir"], text=True).strip())
    inventory = subprocess.run([binary, "tool", "list"], capture_output=True, text=True)
    if inventory.stderr:
        print(inventory.stderr, end="", file=sys.stderr)
    if inventory.returncode:
        return inventory.returncode
    if "Ignoring malformed tool" in inventory.stderr:
        failed = True
    for line in inventory.stdout.splitlines():
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        header = re.match(r"^([A-Za-z0-9][\w.-]*)\s+v\S+", line)
        if not header:
            print(f"Unrecognized uv tool entry: {line}", file=sys.stderr)
            failed = True
            continue
        name = header[1]
        try:
            command = tool_update_command(binary, directory, name)
        except (OSError, KeyError, ValueError) as exc:
            print(f"Cannot read {name} install receipt: {exc}", file=sys.stderr)
            failed = True
            continue
        if command and subprocess.run(command).returncode:
            failed = True
    return int(failed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"uv update failed: {exc}", file=sys.stderr)
        sys.exit(1)
