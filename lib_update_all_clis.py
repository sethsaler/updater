#!/usr/bin/env python3
"""Merge tool config, validate, emit update lines for update_all_clis.sh."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Any, Optional

_UV_ORIGINS = frozenset({"uv", "uv/pip", "uv/venv"})
EMIT_SEP = "\x1e"
DEBUG = os.environ.get("UAC_DEBUG", "0") == "1"
RATE_LIMIT_DELAY = float(os.environ.get("UAC_RATE_LIMIT_DELAY", "0.01"))

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.WARNING,
    format="%(levelname)s: %(message)s" if DEBUG else "%(message)s"
)
logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple rate limiter for subprocess calls."""
    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self.last_call = 0
    
    def acquire(self):
        """Wait if necessary to respect rate limit."""
        if self.delay > 0:
            elapsed = time.time() - self.last_call
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
        self.last_call = time.time()


# Global rate limiter instance
_rate_limiter = RateLimiter(RATE_LIMIT_DELAY)


def load_merge(base_path: str, local_path: Optional[str]) -> dict[str, Any]:
    logger.debug(f"Loading base config from: {base_path}")
    try:
        with open(base_path, encoding="utf-8") as f:
            base = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Base config file not found: {base_path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in base config file {base_path}: {e}")
    
    if local_path and os.path.isfile(local_path):
        logger.debug(f"Merging local config from: {local_path}")
        try:
            with open(local_path, encoding="utf-8") as f:
                loc = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in local config file {local_path}: {e}")
        for key in ("known", "bulk"):
            if key in loc and isinstance(loc[key], dict):
                base.setdefault(key, {})
                base[key].update(loc[key])
                logger.debug(f"Merged {len(loc[key])} entries from local config {key}")
    return base


def validate(cfg: dict[str, Any]) -> None:
    """Validate config structure using schema-like validation."""
    # Check required top-level keys
    if not isinstance(cfg.get("known"), dict) or not isinstance(cfg.get("bulk"), dict):
        raise ValueError("config must contain 'known' and 'bulk' objects")
    
    # Validate known section
    for k, v in cfg["known"].items():
        if not isinstance(k, str) or not k:
            raise ValueError(f"known key must be a non-empty string, got {k!r}")
        if not isinstance(v, str):
            raise ValueError(f"known.{k!r} must be a string command")
    
    # Validate bulk section
    for k, v in cfg["bulk"].items():
        if not isinstance(k, str) or not k:
            raise ValueError(f"bulk key must be a non-empty string, got {k!r}")
        if not isinstance(v, str):
            raise ValueError(f"bulk.{k!r} must be a string command")


def _parse_csv(s: Optional[str]) -> set[str]:
    if not s or not str(s).strip():
        return set()
    return {x.strip() for x in str(s).split(",") if x.strip()}


def lock_group_for(origin: str, cmd: str, name: str) -> str:
    """Package-manager lock key for parallel runs (serialize same manager)."""
    if origin in _UV_ORIGINS:
        return "uv"
    if origin and origin not in ("manual", "path", "?", "go"):
        return origin
    lowered = cmd.lower()
    if "npm " in lowered or "npm update" in lowered or "npm install" in lowered:
        return "npm"
    if "brew " in lowered:
        return "brew"
    if "cargo " in lowered:
        return "cargo"
    if "gem " in lowered:
        return "gem"
    if "go install" in lowered:
        return "go"
    if "uv " in lowered:
        return "uv"
    if "pipx " in lowered:
        return "pipx"
    if "conda " in lowered:
        return "conda"
    if "dotnet " in lowered:
        return "dotnet"
    return name


def _infer_origin_from_symlink(name: str, origin: str) -> str | None:
    """If the binary is a symlink into a known package-manager tree, return that origin."""
    if origin not in ("manual", "path", "?"):
        return None
    path = shutil.which(name)
    if not path:
        return None
    if not os.path.islink(path):
        return None
    target = os.path.realpath(path)
    if "node_modules" in target:
        return "npm"
    if ".cargo" in target or "cargo" in target:
        return "cargo"
    if ".dotnet" in target:
        return "dotnet"
    if ".pipx" in target:
        return "pipx"
    return None


def collect_emit_lines(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str],
    skip_origins: Optional[str],
) -> list[str]:
    only = _parse_csv(only_origins)
    skip = _parse_csv(skip_origins)

    logger.debug(f"Loading cache from: {cache_path}")
    logger.debug(f"Only origins: {only}, Skip origins: {skip}")

    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Cache file not found: {cache_path}. Run discovery scan first.")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cache file {cache_path}: {e}")

    tools = [t for t in data if "name" in t]
    self_cmd = cfg["known"]
    bulk_origins = cfg["bulk"]
    known_names = set(self_cmd.keys())
    seen_names: set[str] = set()
    seen_bulk: set[str] = set()
    lines: list[str] = []

    logger.debug(f"Processing {len(tools)} tools from cache")

    def origin_allowed_for_known(origin: str, name: str) -> bool:
        if not only:
            return True
        return origin in only or name in only

    def should_emit_bulk(origin: str) -> bool:
        if origin in skip:
            return False
        if only and origin not in only:
            return False
        return True

    def write_line(kind: str, name: str, cmd: str, origin: str) -> None:
        lock = lock_group_for(origin, cmd, name)
        lines.append(f"{kind}{EMIT_SEP}{name}{EMIT_SEP}{cmd}{EMIT_SEP}{lock}")

    for t in tools:
        name = t["name"]
        origin = t.get("origin", "?")

        # Skip if already processed
        if name in seen_names:
            continue

        # Handle known tools
        if name in known_names:
            if origin in skip or not origin_allowed_for_known(origin, name):
                seen_names.add(name)
                continue
            cmd = self_cmd[name]
            if not cmd or not cmd.strip():
                seen_names.add(name)
                continue
            seen_names.add(name)
            write_line("known", name, cmd, origin)
            seen_bulk.add(origin)
            continue

        # Infer origin from symlink if possible
        inferred = _infer_origin_from_symlink(name, origin)
        if inferred:
            origin = inferred

        # Handle bulk origins
        if origin in bulk_origins:
            if origin in seen_bulk:
                seen_names.add(name)
                continue
            if not should_emit_bulk(origin):
                seen_names.add(name)
                continue
            cmd = bulk_origins[origin]
            if not cmd or not cmd.strip():
                seen_bulk.add(origin)
                seen_names.add(name)
                lines.append(f"skip{EMIT_SEP}{name}{EMIT_SEP}{EMIT_SEP}")
                continue
            seen_bulk.add(origin)
            write_line("bulk", origin, cmd, origin)
            seen_names.add(name)
            continue

        # Skip unknown tools
        seen_names.add(name)
        lines.append(f"skip{EMIT_SEP}{name}{EMIT_SEP}{EMIT_SEP}")

    return lines


def emit_lines(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str],
    skip_origins: Optional[str],
) -> None:
    for line in collect_emit_lines(cache_path, cfg, only_origins, skip_origins):
        sys.stdout.write(line + "\n")


def emit_plan_json(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str],
    skip_origins: Optional[str],
) -> None:
    plan: list[dict[str, str]] = []
    for line in collect_emit_lines(cache_path, cfg, only_origins, skip_origins):
        parts = line.split(EMIT_SEP, 3)
        if len(parts) < 3:
            continue
        kind, name, cmd = parts[0], parts[1], parts[2]
        lock = parts[3] if len(parts) > 3 else name
        entry: dict[str, str] = {"type": kind, "name": name, "command": cmd}
        if lock:
            entry["lock_group"] = lock
        plan.append(entry)
    print(json.dumps({"plan": plan, "count": len(plan)}, indent=2))


def list_json(cache_path: str) -> None:
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Cache file not found: {cache_path}. Run discovery scan first.")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cache file {cache_path}: {e}")
    
    tools = [t for t in data if "name" in t]
    meta = next((t for t in data if "scanned_at" in t), None)
    out = {
        "tools": tools,
        "count": len(tools),
        "scanned_at": meta.get("scanned_at") if meta else None,
    }
    print(json.dumps(out, indent=2))


@lru_cache(maxsize=512)
def probe_version(name: str) -> str:
    """Best-effort version string for a CLI on PATH."""
    if not shutil.which(name):
        return "?"
    for args in ((name, "--version"), (name, "-V"), (name, "version")):
        _rate_limiter.acquire()
        try:
            r = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                timeout=5,
                env={**os.environ, "LC_ALL": "C"},
            )
            if r.stdout:
                line = r.stdout.strip().split("\n")[0].strip()
                if line:
                    return line[:220]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "?"


def probe_known(name: str) -> str:
    return probe_version(name)


def _probe_single(cmd: tuple[str, ...]) -> str:
    try:
        r = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "LC_ALL": "C"},
        )
        if r.stdout:
            return r.stdout.strip().split("\n")[0].strip()[:220]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "?"


def probe_bulk(origin: str) -> str:
    plans: dict[str, Any] = {
        "brew": ("brew", "--version"),
        "npm": ("npm", "--version"),
        "cargo": ("cargo", "--version"),
        "gem": ("gem", "--version"),
        "pip": ("pip3", "--version"),
        "uv": ("uv", "--version"),
        "uv/pip": ("uv", "--version"),
        "uv/venv": ("uv", "--version"),
        "fnm": ("fnm", "--version"),
        "bun": ("bun", "--version"),
        "deno": ("deno", "--version"),
        "pyenv": ("pyenv", "--version"),
        "rbenv": ("rbenv", "--version"),
        "conda": ("conda", "--version"),
        "opencode": ("opencode", "--version"),
        "manual": ("brew", "--version"),
        "dotnet": ("dotnet", "--version"),
        "krew": ("kubectl", "krew", "version"),
        "mise": ("mise", "--version"),
        "pipx": ("pipx", "--version"),
        "grok": ("grok", "--version"),
        "path": (),
    }
    if origin == "path":
        return "many tools (PATH scan)"
    cmd = plans.get(origin)
    if not cmd:
        return f"({origin})"
    try:
        if origin == "sdkman":
            r = subprocess.run(
                [
                    "bash",
                    "-lc",
                    'test -s "$HOME/.sdkman/bin/sdkman-init.sh" && . '
                    '"$HOME/.sdkman/bin/sdkman-init.sh" && sdk version',
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            out = (r.stdout or r.stderr or "").strip().split("\n")[0]
            return out[:220] if out else "?"
        if not cmd:
            return f"({origin})"
        return _probe_single(cmd)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "?"


def snapshot_versions(lines: list[str]) -> dict[str, dict[str, str]]:
    logger.debug(f"Snapshotting versions for {len(lines)} lines")
    known: dict[str, str] = {}
    bulk: dict[str, str] = {}
    seen_bulk: set[str] = set()
    
    # Collect tasks for parallel execution
    known_tasks: list[tuple[str, str]] = []
    bulk_tasks: list[tuple[str, str]] = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(EMIT_SEP, 3)
        if len(parts) < 3:
            continue
        kind, name = parts[0], parts[1]
        if kind == "skip":
            continue
        if kind == "known":
            known_tasks.append((name, "known"))
        elif kind == "bulk" and name not in seen_bulk:
            seen_bulk.add(name)
            bulk_tasks.append((name, "bulk"))
    
    # Probe versions in parallel with progress tracking
    def probe_task(task: tuple[str, str]) -> tuple[str, str, str]:
        name, kind = task
        if kind == "known":
            return name, "known", probe_known(name)
        else:
            return name, "bulk", probe_bulk(name)
    
    all_tasks = known_tasks + bulk_tasks
    total_tasks = len(all_tasks)
    completed = 0
    
    if DEBUG and total_tasks > 0:
        logger.info(f"Probing versions for {total_tasks} tools...")
    
    with ThreadPoolExecutor(max_workers=16) as executor:
        future_to_task = {executor.submit(probe_task, task): task for task in all_tasks}
        for future in as_completed(future_to_task):
            completed += 1
            if DEBUG and completed % 5 == 0:
                logger.info(f"Progress: {completed}/{total_tasks} tools probed")
            name, kind, version = future.result()
            if kind == "known":
                known[name] = version
            else:
                bulk[name] = version
    
    if DEBUG and total_tasks > 0:
        logger.info(f"Completed probing {completed} tools")
    
    return {"known": known, "bulk": bulk}


def suggest_config(cache_path: str, cfg: dict[str, Any]) -> None:
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Cache file not found: {cache_path}. Run discovery scan first.")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cache file {cache_path}: {e}")
    tools = [t for t in data if "name" in t]
    self_cmd = cfg["known"]
    bulk_origins = cfg["bulk"]
    known = set(self_cmd.keys())
    unknown: list[dict] = []
    for t in tools:
        name = t["name"]
        origin = t.get("origin", "?")
        if name in known:
            continue
        if origin in bulk_origins:
            continue
        inferred = _infer_origin_from_symlink(name, origin)
        if inferred and inferred in bulk_origins:
            continue
        unknown.append(t)
    if not unknown:
        print("All discovered tools have a known update path already.", file=sys.stderr)
        return
    unknown.sort(key=lambda x: x["name"])
    print("Discovered tools with no update command:\n")
    for t in unknown:
        print(f'  "{t["name"]}": "UPDATE_COMMAND_HERE",  # origin: {t.get("origin", "?")}')
    print()
    print("Copy the entries above into ~/.config/update-all-clis/config.local.json")
    print("under the \"known\" section, replacing UPDATE_COMMAND_HERE with the actual")
    print("update command (e.g. \"brew upgrade <tool>\", \"cargo install <tool>\", etc.).")
    print()


UNKNOWN_LOG_DEFAULT = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "update-all-clis",
    "unknown_tools.json",
)


def log_unknowns(cache_path: str, cfg: dict[str, Any], unknown_log_path: str) -> None:
    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
    tools = [t for t in data if "name" in t]
    meta = next((t for t in data if "scanned_at" in t), None)
    scanned_at = meta.get("scanned_at") if meta else None

    known = set(cfg["known"].keys())
    bulk = set(cfg["bulk"].keys())

    existing: dict = {}
    if os.path.isfile(unknown_log_path):
        try:
            with open(unknown_log_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing_tools = existing.get("tools", {})

    for t in tools:
        name = t["name"]
        origin = t.get("origin", "?")

        if name in known:
            continue
        if origin in bulk:
            continue
        inferred = _infer_origin_from_symlink(name, origin)
        if inferred and inferred in bulk:
            continue

        if name in existing_tools:
            existing_tools[name]["last_seen"] = (
                scanned_at or existing_tools[name].get("last_seen")
            )
            existing_tools[name]["times_seen"] += 1
        else:
            existing_tools[name] = {
                "name": name,
                "origin": origin,
                "first_seen": scanned_at,
                "last_seen": scanned_at,
                "times_seen": 1,
                "acknowledged": False,
            }

    output = {
        "scanned_at": scanned_at,
        "tools": existing_tools,
    }
    os.makedirs(os.path.dirname(unknown_log_path), exist_ok=True)
    with open(unknown_log_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)


def report_unknown(unknown_log_path: str, min_times: int = 1) -> None:
    if not os.path.isfile(unknown_log_path):
        print("No unknown tools log found.", file=sys.stderr)
        return
    with open(unknown_log_path, encoding="utf-8") as f:
        data = json.load(f)
    tools = data.get("tools", {})

    unhandled = [t for t in tools.values() if t["times_seen"] >= min_times and not t.get("acknowledged")]
    acked = [t for t in tools.values() if t.get("acknowledged")]

    if not unhandled and not acked:
        print("No unknown tools recorded.")
        return

    if unhandled:
        unhandled.sort(key=lambda x: (-x["times_seen"], x["name"]))
        print("Tools with no update path (seen in recent scans):")
        print()
        for t in unhandled:
            flag = ""
            if t["times_seen"] >= 2:
                flag = f"  (run with --ack-unknown={t['name']} to dismiss)"
            print(f'  {t["name"]}  [origin: {t["origin"]}]  '
                  f'(seen {t["times_seen"]}x, last: {t["last_seen"]}){flag}')
            print(f'    add to known: "{t["name"]}": "UPDATE_COMMAND_HERE",')
            print()
        print("Tip: Add entries above to ~/.config/update-all-clis/config.local.json")
        print("under the \"known\" section to give them an update path.")
        print()

    if acked:
        print("Acknowledged (dismissed from report):")
        for t in acked:
            print(f'  {t["name"]}  (seen {t["times_seen"]}x, last: {t["last_seen"]})')


def ack_unknown(unknown_log_path: str, name: str) -> None:
    if not os.path.isfile(unknown_log_path):
        print(f"No unknown tools log found at {unknown_log_path}.", file=sys.stderr)
        sys.exit(1)
    with open(unknown_log_path, encoding="utf-8") as f:
        data = json.load(f)
    tools = data.get("tools", {})
    if name not in tools:
        print(f"Tool '{name}' not found in unknown tools log.", file=sys.stderr)
        sys.exit(1)
    tools[name]["acknowledged"] = True
    with open(unknown_log_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Acknowledged '{name}' — it will no longer appear in reports.")


def format_run_summary(before: dict[str, Any], after: dict[str, Any], ok: int, fail: int) -> str:
    lines_out: list[str] = [
        "update-all-clis",
        f"Steps: {ok} ok, {fail} failed",
        "",
        "Known tools:",
    ]
    kn = set(before.get("known", {})) | set(after.get("known", {}))
    if not kn:
        lines_out.append("  (none)")
    for name in sorted(kn):
        b = before.get("known", {}).get(name, "?")
        a = after.get("known", {}).get(name, "?")
        if b == a:
            lines_out.append(f"  {name}: {a}")
        else:
            lines_out.append(f"  {name}: {b} → {a}")
    lines_out.append("")
    lines_out.append("Bulk (package managers / env):")
    bk = set(before.get("bulk", {})) | set(after.get("bulk", {}))
    if not bk:
        lines_out.append("  (none)")
    for name in sorted(bk):
        b = before.get("bulk", {}).get(name, "?")
        a = after.get("bulk", {}).get(name, "?")
        if b == a:
            lines_out.append(f"  {name}: {a}")
        else:
            lines_out.append(f"  {name}: {b} → {a}")
    return "\n".join(lines_out) + "\n"


def notify_macos_dialog(before: dict[str, Any], after: dict[str, Any], ok: int, fail: int) -> None:
    if sys.platform != "darwin":
        return
    body = format_run_summary(before, after, ok, fail).rstrip("\n")
    if len(body) > 950:
        body = body[:947] + "\n…"
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(body)
        path_esc = path.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            [
                "osascript",
                "-e",
                f'set f to POSIX file "{path_esc}"',
                "-e",
                "set msg to read file f as Unicode text",
                "-e",
                'display dialog msg with title "update-all-clis" buttons {"OK"} default button "OK"',
            ],
            check=False,
            timeout=120,
        )
    except OSError:
        pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def notify_linux(before: dict[str, Any], after: dict[str, Any], ok: int, fail: int) -> None:
    if sys.platform == "linux" and shutil.which("notify-send"):
        body = format_run_summary(before, after, ok, fail).rstrip("\n")
        if len(body) > 500:
            body = body[:497] + "…"
        subprocess.run(
            [
                "notify-send",
                "update-all-clis",
                body,
            ],
            check=False,
            timeout=10,
        )


def notify_diff(before: dict[str, Any], after: dict[str, Any], ok: int, fail: int) -> None:
    notify_macos_dialog(before, after, ok, fail)
    notify_linux(before, after, ok, fail)


def _load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_npm_globals_json(json_input: str) -> str:
    """Parse npm ls -g --json output and extract package directory paths."""
    try:
        data = json.loads(json_input)
        deps = data.get('dependencies', {})
        paths = []
        for name, info in deps.items():
            rp = info.get('resolved', info.get('path', ''))
            if rp:
                paths.append(rp)
        return '|'.join(paths)
    except Exception:
        return ""


def convert_tools_array_to_json(tools_input: str, scanned_at: str, existing_cache_path: Optional[str] = None) -> str:
    """Convert tools array format to JSON cache file, preserving version data from existing cache."""
    lines = [l.strip() for l in tools_input.split('\n') if '|' in l]
    
    # Load existing cache to preserve version data
    existing_versions = {}
    if existing_cache_path and os.path.isfile(existing_cache_path):
        try:
            with open(existing_cache_path, encoding="utf-8") as f:
                existing_data = json.load(f)
            for item in existing_data:
                if "name" in item and "version" in item:
                    existing_versions[item["name"]] = item["version"]
        except (json.JSONDecodeError, OSError):
            pass
    
    tools = []
    for line in lines:
        parts = line.split('|', 1)
        if len(parts) == 2:
            tool_entry = {'name': parts[0], 'origin': parts[1]}
            # Preserve existing version if available
            if parts[0] in existing_versions:
                tool_entry['version'] = existing_versions[parts[0]]
            tools.append(tool_entry)
    tools.append({'scanned_at': scanned_at, 'count': len(tools)})
    return json.dumps(tools, indent=2)


def update_cache_versions(cache_path: str, versions: dict[str, dict[str, str]]) -> None:
    """Update cache with new version data after updates."""
    if not os.path.isfile(cache_path):
        logger.debug(f"Cache file not found: {cache_path}")
        return
    
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to read cache file: {e}")
        return
    
    # Update versions for known tools
    known_versions = versions.get("known", {})
    bulk_versions = versions.get("bulk", {})
    
    updated_count = 0
    bulk_updated = False
    for item in data:
        if "name" not in item:
            continue
        name = item["name"]
        if name in known_versions:
            item["version"] = known_versions[name]
            item["version_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            updated_count += 1
        # For bulk origins, store the package manager version
        origin = item.get("origin", "?")
        if origin in bulk_versions:
            item["pm_version"] = bulk_versions[origin]
            bulk_updated = True
    
    if updated_count > 0 or bulk_updated:
        logger.debug(f"Updated {updated_count} tool versions and bulk PM versions in cache")
        # Write back to cache
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, cache_path)
        logger.debug(f"Cache updated with new versions: {cache_path}")


def validate_cache(cache_path: str) -> dict[str, Any]:
    """Validate cache structure and return diagnostic information."""
    result = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "stats": {},
        "tools_with_versions": 0,
        "tools_without_versions": 0,
        "origins": {},
    }
    
    if not os.path.isfile(cache_path):
        result["valid"] = False
        result["errors"].append(f"Cache file not found: {cache_path}")
        return result
    
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        result["valid"] = False
        result["errors"].append(f"Invalid JSON: {e}")
        return result
    
    if not isinstance(data, list):
        result["valid"] = False
        result["errors"].append("Cache must be a JSON array")
        return result
    
    tools = [t for t in data if "name" in t]
    meta = next((t for t in data if "scanned_at" in t), None)
    
    result["stats"]["total_items"] = len(data)
    result["stats"]["tool_count"] = len(tools)
    
    if not meta:
        result["warnings"].append("Missing metadata (scanned_at, count)")
    else:
        result["stats"]["scanned_at"] = meta.get("scanned_at")
        result["stats"]["count"] = meta.get("count")
        if meta.get("count") != len(tools):
            result["warnings"].append(f"Count mismatch: metadata says {meta.get('count')}, found {len(tools)}")
    
    # Analyze tools
    for tool in tools:
        name = tool.get("name")
        origin = tool.get("origin", "?")
        
        if not isinstance(name, str) or not name:
            result["errors"].append(f"Invalid tool name: {name}")
            continue
        
        result["origins"][origin] = result["origins"].get(origin, 0) + 1
        
        if "version" in tool:
            result["tools_with_versions"] += 1
        else:
            result["tools_without_versions"] += 1
    
    # Check for duplicates
    names = [t["name"] for t in tools]
    duplicates = [name for name in set(names) if names.count(name) > 1]
    if duplicates:
        result["warnings"].append(f"Duplicate tool names: {', '.join(duplicates)}")
    
    return result


def debug_cache(cache_path: str) -> None:
    """Print detailed cache debugging information."""
    validation = validate_cache(cache_path)
    
    print("Cache Validation Report")
    print("=" * 50)
    print(f"Valid: {validation['valid']}")
    print(f"Total items: {validation['stats'].get('total_items', 0)}")
    print(f"Tool count: {validation['stats'].get('tool_count', 0)}")
    print(f"Scanned at: {validation['stats'].get('scanned_at', 'N/A')}")
    print()
    
    print("Version Coverage:")
    print(f"  Tools with versions: {validation['tools_with_versions']}")
    print(f"  Tools without versions: {validation['tools_without_versions']}")
    print()
    
    print("Origins:")
    for origin, count in sorted(validation["origins"].items()):
        print(f"  {origin}: {count}")
    print()
    
    if validation["errors"]:
        print("Errors:")
        for error in validation["errors"]:
            print(f"  - {error}")
        print()
    
    if validation["warnings"]:
        print("Warnings:")
        for warning in validation["warnings"]:
            print(f"  - {warning}")
        print()


def health_check() -> dict[str, Any]:
    """Check availability of required tools and package managers."""
    checks = {
        "python3": {"available": shutil.which("python3") is not None, "required": True, "version": None},
        "bash": {"available": shutil.which("bash") is not None, "required": True, "version": None},
        "npm": {"available": shutil.which("npm") is not None, "required": False, "version": None},
        "brew": {"available": shutil.which("brew") is not None, "required": False, "version": None},
        "cargo": {"available": shutil.which("cargo") is not None, "required": False, "version": None},
        "pip3": {"available": shutil.which("pip3") is not None, "required": False, "version": None},
        "go": {"available": shutil.which("go") is not None, "required": False, "version": None},
        "gem": {"available": shutil.which("gem") is not None, "required": False, "version": None},
        "uv": {"available": shutil.which("uv") is not None, "required": False, "version": None},
        "dotnet": {"available": shutil.which("dotnet") is not None, "required": False, "version": None},
    }
    
    # Get versions for available tools
    for name, info in checks.items():
        if info["available"]:
            try:
                if name == "python3":
                    checks[name]["version"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                elif name in ("npm", "cargo", "pip3", "go", "gem", "uv", "dotnet"):
                    checks[name]["version"] = probe_version(name)
            except Exception:
                pass
    
    missing_required = [name for name, info in checks.items() if info["required"] and not info["available"]]
    missing_optional = [name for name, info in checks.items() if not info["required"] and not info["available"]]
    
    result = {
        "status": "healthy" if not missing_required else "unhealthy",
        "checks": checks,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }
    return result


def create_backup(cache_path: str) -> str:
    """Create a backup of the cache file before updates."""
    if not os.path.isfile(cache_path):
        logger.debug(f"No cache file to backup: {cache_path}")
        return ""
    
    backup_dir = os.path.join(os.path.dirname(cache_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    timestamp = os.path.basename(cache_path) + "." + os.environ.get("USER", "unknown") + "." + os.path.getpid().__str__()
    backup_path = os.path.join(backup_dir, timestamp)
    
    shutil.copy2(cache_path, backup_path)
    logger.debug(f"Created backup: {backup_path}")
    return backup_path


def list_backups(cache_path: str) -> list[str]:
    """List available backup files for the cache."""
    backup_dir = os.path.join(os.path.dirname(cache_path), "backups")
    if not os.path.isdir(backup_dir):
        return []
    
    cache_basename = os.path.basename(cache_path)
    backups = []
    for f in os.listdir(backup_dir):
        if f.startswith(cache_basename + "."):
            backups.append(os.path.join(backup_dir, f))
    
    return sorted(backups, key=os.path.getmtime, reverse=True)


def restore_backup(cache_path: str, backup_path: str) -> bool:
    """Restore a backup file to the cache location."""
    if not os.path.isfile(backup_path):
        logger.error(f"Backup file not found: {backup_path}")
        return False
    
    try:
        shutil.copy2(backup_path, cache_path)
        logger.info(f"Restored backup from: {backup_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to restore backup: {e}")
        return False


def benchmark_operation(cache_path: str, cfg: dict[str, Any]) -> dict[str, float]:
    """Benchmark key operations and return timing results."""
    results = {}
    
    # Benchmark config loading
    start = time.time()
    load_merge(cfg.get("base_path", ""), cfg.get("local_path"))
    results["load_merge"] = time.time() - start
    
    # Benchmark emit lines generation
    start = time.time()
    lines = collect_emit_lines(cache_path, cfg, None, None)
    results["collect_emit_lines"] = time.time() - start
    
    # Benchmark version probing (sample)
    start = time.time()
    if lines:
        sample_lines = lines[:min(5, len(lines))]
        for line in sample_lines:
            parts = line.split(EMIT_SEP)
            if len(parts) >= 2:
                probe_known(parts[1])
    results["probe_versions_sample"] = time.time() - start
    
    return results


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: lib_update_all_clis.py emit|emit-json|list-json|snapshot-versions|"
            "notify-diff|run-summary|suggest|log-unknowns|report-unknown|ack-unknown|"
            "parse-npm-globals|convert-tools-array|update-cache-versions|validate-cache|debug-cache|"
            "health-check|backup|restore|list-backups|benchmark …",
            file=sys.stderr,
        )
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "benchmark":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        base = os.environ.get("CONFIG_FILE", "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        results = benchmark_operation(cache_path, cfg)
        print(json.dumps(results, indent=2))
        sys.exit(0)
    elif cmd == "health-check":
        result = health_check()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["status"] == "healthy" else 1)
    elif cmd == "backup":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        if not cache_path:
            print("Usage: lib_update_all_clis.py backup <cache_path>", file=sys.stderr)
            sys.exit(2)
        backup_path = create_backup(cache_path)
        if backup_path:
            print(f"Backup created: {backup_path}")
        else:
            print("No backup created (cache file not found)")
        sys.exit(0)
    elif cmd == "list-backups":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        if not cache_path:
            print("Usage: lib_update_all_clis.py list-backups <cache_path>", file=sys.stderr)
            sys.exit(2)
        backups = list_backups(cache_path)
        if backups:
            print(f"Found {len(backups)} backup(s):")
            for b in backups:
                mtime = os.path.getmtime(b)
                print(f"  {b} (modified: {mtime})")
        else:
            print("No backups found")
        sys.exit(0)
    elif cmd == "restore":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        backup_path = sys.argv[3] if len(sys.argv) > 3 else ""
        if not cache_path or not backup_path:
            print("Usage: lib_update_all_clis.py restore <cache_path> <backup_path>", file=sys.stderr)
            sys.exit(2)
        success = restore_backup(cache_path, backup_path)
        sys.exit(0 if success else 1)
    elif cmd == "parse-npm-globals":
        json_input = sys.stdin.read()
        result = parse_npm_globals_json(json_input)
        print(result)
    elif cmd == "convert-tools-array":
        scanned_at = sys.argv[2] if len(sys.argv) > 2 else ""
        existing_cache = sys.argv[3] if len(sys.argv) > 3 else None
        tools_input = sys.stdin.read()
        result = convert_tools_array_to_json(tools_input, scanned_at, existing_cache)
        print(result)
    elif cmd == "update-cache-versions":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        versions_input = sys.stdin.read()
        versions = json.loads(versions_input) if versions_input.strip() else {}
        update_cache_versions(cache_path, versions)
        sys.exit(0)
    elif cmd == "validate-cache":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        result = validate_cache(cache_path)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["valid"] else 1)
    elif cmd == "debug-cache":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        debug_cache(cache_path)
        sys.exit(0)
    elif cmd == "emit":
        cache_path = sys.argv[2]
        base = os.environ.get("CONFIG_FILE", "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        cfg = load_merge(base, local or None)
        validate(cfg)
        emit_lines(
            cache_path,
            cfg,
            os.environ.get("ONLY_ORIGINS"),
            os.environ.get("SKIP_ORIGINS"),
        )
    elif cmd == "emit-json":
        cache_path = sys.argv[2]
        base = os.environ.get("CONFIG_FILE", "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        cfg = load_merge(base, local or None)
        validate(cfg)
        emit_plan_json(
            cache_path,
            cfg,
            os.environ.get("ONLY_ORIGINS"),
            os.environ.get("SKIP_ORIGINS"),
        )
    elif cmd == "list-json":
        cache_path = sys.argv[2]
        list_json(cache_path)
    elif cmd == "snapshot-versions":
        emit_path = sys.argv[2]
        with open(emit_path, encoding="utf-8") as f:
            snap = snapshot_versions(f.read().splitlines())
        print(json.dumps(snap))
    elif cmd == "notify-diff":
        before = _load_json(sys.argv[2])
        after = _load_json(sys.argv[3])
        notify_diff(before, after, int(sys.argv[4]), int(sys.argv[5]))
    elif cmd == "run-summary":
        before = _load_json(sys.argv[2])
        after = _load_json(sys.argv[3])
        sys.stdout.write(format_run_summary(before, after, int(sys.argv[4]), int(sys.argv[5])))
    elif cmd == "suggest":
        cache_path = sys.argv[2]
        base = os.environ.get("CONFIG_FILE", sys.argv[3] if len(sys.argv) > 3 else "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        validate(cfg)
        suggest_config(cache_path, cfg)
    elif cmd == "log-unknowns":
        cache_path = sys.argv[2]
        unknown_log = os.environ.get("UNKNOWN_LOG_FILE", UNKNOWN_LOG_DEFAULT)
        base = os.environ.get("CONFIG_FILE", sys.argv[3] if len(sys.argv) > 3 else "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        validate(cfg)
        log_unknowns(cache_path, cfg, unknown_log)
    elif cmd == "report-unknown":
        unknown_log = sys.argv[2] if len(sys.argv) > 2 else UNKNOWN_LOG_DEFAULT
        min_times = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        report_unknown(unknown_log, min_times)
    elif cmd == "ack-unknown":
        if len(sys.argv) < 4:
            print("usage: lib_update_all_clis.py ack-unknown UNKNOWN_LOG TOOL_NAME", file=sys.stderr)
            sys.exit(2)
        ack_unknown(sys.argv[2], sys.argv[3])
    else:
        print("unknown command", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
