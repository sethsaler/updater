"""Check or update global npm packages in their owning installation prefix."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def npm_prefixes():
    home = Path.home()
    active = subprocess.check_output(["npm", "prefix", "-g"], text=True).strip()
    if not Path(active).is_absolute():
        raise ValueError("npm prefix -g returned an invalid path")
    nvm = Path(os.environ.get("NVM_DIR", home / ".nvm")) / "versions/node"
    candidates = [Path(active), home / ".local", home / ".npm-global", home / ".npm-packages",
                  *sorted(nvm.glob("*"))]
    return Path(active).resolve(), list(dict.fromkeys(p.resolve() for p in candidates if (p / "lib/node_modules").is_dir()))


def semver_key(version):
    if not isinstance(version, str):
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
                         r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version)
    if not match:
        return None
    # Numeric prerelease identifiers sort below text. A release with no
    # prerelease identifiers sorts above every prerelease of the same core.
    prerelease = match.group(4)
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in prerelease.split(".")) if prerelease else ()
    return int(match[1]), int(match[2]), int(match[3]), prerelease is None, parts


def registry_owned(prefix, name, location, receipt, wanted):
    # npm reports missing, linked and copied file installs as outdated on
    # some versions. Never replace them with a registry package of the same name.
    modules = prefix / "lib/node_modules"
    package = modules / name
    parts = Path(name).parts
    if not ((len(parts) == 1 and not name.startswith(("@", ".", "-"))) or
            (len(parts) == 2 and parts[0].startswith("@") and not parts[1].startswith("."))):
        return False
    if package != Path(location) or package.resolve() != package or not (package / "package.json").is_file():
        return False
    nvm = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")).resolve() / "versions/node"
    if name in ("npm", "corepack") and (not prefix.is_relative_to(Path.home().resolve()) or prefix.parent == nvm):
        return False  # The host's Node installation owns its bundled tools.
    manifest = json.loads((package / "package.json").read_text())
    if manifest.get("private") or manifest.get("name") != name:
        return False
    # A native updater may have advanced the active binary since the check.
    if manifest.get("version"):
        installed = semver_key(manifest["version"])
        if installed is None or installed >= wanted:
            return False
    entry = receipt.get("packages", {}).get("node_modules/" + name, {})
    sources = (entry.get("resolved", ""), manifest.get("_resolved", ""), manifest.get("_from", ""))
    return not entry.get("link") and not any(
        source.removeprefix(name + "@").startswith(("file:", "link:", "workspace:", "git", "/", "./", "../"))
        for source in sources if isinstance(source, str)
    )


def pending_packages(prefix, env, report=None, skipped=()):
    if report is None:
        result = subprocess.run(["npm", "outdated", "-g", "--json", "--prefix", str(prefix)],
                                capture_output=True, text=True, env=env)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or (result.returncode and
                (result.returncode != 1 or not report or "error" in report)):
            raise ValueError(f"npm outdated failed for {prefix} (exit {result.returncode})")
    receipt = prefix / "lib/node_modules/.package-lock.json"
    lock = json.loads(receipt.read_text()) if receipt.is_file() else {}
    pending = []
    for name, info in report.items():
        if name in skipped:
            continue
        if not isinstance(info, dict):
            raise ValueError(f"invalid npm outdated record for {name} under {prefix}")
        current = semver_key(info.get("current"))
        wanted = semver_key(info.get("wanted"))
        if (current is not None and wanted is not None and current < wanted and
                registry_owned(prefix, name, info["location"], lock, wanted)):
            pending.append((name, info))
    return pending


def cached_reports(prefixes, header):
    """Reuse only this helper's complete, same-run check of the same prefixes."""
    sidecar = os.environ.get("UAC_PRECHECK_UPTODATE_FILE")
    if not sidecar:
        return {}
    try:
        data = json.loads(Path(sidecar).read_text())
        lines = data["stdout"]["npm"].splitlines()
        if not lines or lines[0] != header:
            return {}
        reports = {p: {} for p in prefixes}
        for line in lines[1:]:
            path, wanted, current, latest, _ = line.rsplit(":", 4)
            name, version = wanted.rsplit("@", 1)
            prefix = Path(path).parents[len(Path(name).parts) + 1]
            reports[prefix][name] = dict(location=path, current=current.rsplit("@", 1)[1],
                                         wanted=version, latest=latest.rsplit("@", 1)[1])
        return reports
    except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError):
        return {}


def main(check=False, skip_active_package=()):
    try:
        active, prefixes = npm_prefixes()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"npm discovery failed: {exc}", file=sys.stderr)
        return 2
    failed = False
    check_lines = []
    header = "# npm-global-prefixes " + json.dumps([str(active), list(map(str, prefixes)), sorted(set(skip_active_package))])
    cached = {} if check else cached_reports(prefixes, header)
    for prefix in prefixes:
        env = os.environ.copy()
        if (prefix / "bin/node").is_file() and (prefix / "bin/npm").is_file():
            env["PATH"] = str(prefix / "bin") + os.pathsep + env["PATH"]
        try:
            packages = pending_packages(prefix, env, cached.get(prefix),
                                        skip_active_package if prefix == active else ())
            for name, info in packages:
                if check:
                    check_lines.append(f"{info['location']}:{name}@{info['wanted']}:{name}@{info['current']}:"
                                       f"{name}@{info['latest']}:global")
                    continue
                print(f"Updating {name} under {prefix}", flush=True)
                rc = subprocess.run(["npm", "update", "-g", "--prefix", str(prefix),
                                     "--no-audit", "--no-fund", "--", name], env=env).returncode
                failed |= rc != 0
        except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"npm operation failed under {prefix}: {exc}", file=sys.stderr)
            failed = True
    if failed:
        return 2 if check else 1  # Never publish a partial pre-check as an exhaustive list.
    if check_lines:
        print(header)
        print("\n".join(check_lines))
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--skip-active-package", action="append", default=[], metavar="NAME",
                        help="leave this package in npm's active prefix to its external updater (repeatable)")
    args = parser.parse_args()
    sys.exit(main(args.check, args.skip_active_package))
