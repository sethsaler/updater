#!/usr/bin/env python3
"""Merge tool config, validate, emit update lines for update_all_clis.sh."""
from __future__ import annotations

import fcntl
import glob
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Any, Optional

_UV_ORIGINS = frozenset({"uv", "uv/pip", "uv/venv"})
EMIT_SEP = "\x1e"


def _read_lines(path: str) -> list[str]:
    """Read a text file and split strictly on "\\n".

    NOT `str.splitlines()`: emit/result lines are joined with EMIT_SEP
    ("\\x1e", ASCII Record Separator), and `splitlines()` treats \\x1e (along
    with \\x1c, \\x1d, \\x85, \\u2028, \\u2029) as its own line boundary,
    which would shred every EMIT_SEP-delimited field onto its own "line".
    """
    with open(path, encoding="utf-8") as f:
        content = f.read()
    return content.split("\n")
DEBUG = os.environ.get("UAC_DEBUG", "0") == "1"
RATE_LIMIT_DELAY = float(os.environ.get("UAC_RATE_LIMIT_DELAY", "0.01"))

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.WARNING,
    format="%(levelname)s: %(message)s" if DEBUG else "%(message)s"
)
logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple thread-safe rate limiter for subprocess calls."""
    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self.last_call = 0.0
        self._lock = threading.Lock()
    
    def acquire(self):
        """Wait if necessary to respect rate limit."""
        if self.delay <= 0:
            return
        with self._lock:
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
        for key in ("known", "bulk", "check", "repos"):
            if key in loc and isinstance(loc[key], dict):
                base.setdefault(key, {})
                base[key].update(loc[key])
                logger.debug(f"Merged {len(loc[key])} entries from local config {key}")
        # "hold" is a flat list, not a dict: local entries ADD to (rather than
        # replace) the base list, deduplicated, preserving base order first.
        if "hold" in loc and isinstance(loc["hold"], list):
            base_hold = base.get("hold", [])
            if not isinstance(base_hold, list):
                base_hold = []
            merged_hold = list(base_hold)
            for entry in loc["hold"]:
                if entry not in merged_hold:
                    merged_hold.append(entry)
            base["hold"] = merged_hold
            logger.debug(f"Merged hold list, now {len(merged_hold)} entries")
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

    # Validate optional check section (origin -> "is anything outdated?" probe
    # command). Missing entirely is fine; every origin without a check simply
    # never gets pre-checked and always runs its bulk update as before.
    if "check" in cfg:
        if not isinstance(cfg["check"], dict):
            raise ValueError("'check' must be an object mapping origin to a check command")
        for k, v in cfg["check"].items():
            if not isinstance(k, str) or not k:
                raise ValueError(f"check key must be a non-empty string, got {k!r}")
            if not isinstance(v, str):
                raise ValueError(f"check.{k!r} must be a string command")

    # Validate optional "hold" list (pinned tools/origins). Each entry is
    # either a plain known-tool name / bulk-origin name, or "name:major" —
    # the latter is accepted but (v1) treated identically to a plain hold;
    # see README for why semver-aware holds only apply at the summary level.
    if "hold" in cfg:
        if not isinstance(cfg["hold"], list):
            raise ValueError("'hold' must be an array of strings")
        for entry in cfg["hold"]:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(f"hold entries must be non-empty strings, got {entry!r}")

    # Validate optional "repos" mapping (tool/origin name -> GitHub owner/repo,
    # used for the best-effort changelog digest).
    if "repos" in cfg:
        if not isinstance(cfg["repos"], dict):
            raise ValueError("'repos' must be an object mapping name to 'owner/repo'")
        for k, v in cfg["repos"].items():
            if not isinstance(k, str) or not k:
                raise ValueError(f"repos key must be a non-empty string, got {k!r}")
            if not isinstance(v, str) or "/" not in v:
                raise ValueError(f"repos.{k!r} must be a string 'owner/repo'")


def _parse_csv(s: Optional[str]) -> set[str]:
    if not s or not str(s).strip():
        return set()
    return {x.strip() for x in str(s).split(",") if x.strip()}


def _hold_base_name(entry: str) -> str:
    """Strip a ":major" suffix from a hold entry, e.g. "claude:major" -> "claude".

    v1 treats "name:major" identically to a plain hold (we can't reliably know
    the target version ahead of time for most managers); the ":major" suffix
    is accepted for forward-compat and documented in the README.
    """
    if entry.endswith(":major"):
        return entry[: -len(":major")]
    return entry


def normalize_hold_entries(entries: Optional[list[str]]) -> set[str]:
    """Config `hold` list -> set of plain names/origins (":major" suffix stripped)."""
    if not entries:
        return set()
    return {_hold_base_name(e) for e in entries if isinstance(e, str) and e.strip()}


def edit_local_hold(
    local_path: str,
    add: Optional[set[str]] = None,
    remove: Optional[set[str]] = None,
) -> list[str]:
    """Add/remove entries in `local_path`'s "hold" array in place (creates the file if needed).

    Backs `--hold=`/`--unhold=` CLI flags. Preserves every other key already
    in the local config file untouched. Returns the resulting hold list.
    """
    data: dict[str, Any] = {}
    if os.path.isfile(local_path):
        try:
            with open(local_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict):
        data = {}

    hold = [h for h in data.get("hold", []) if isinstance(h, str)]
    if add:
        for name in sorted(add):
            if name not in hold:
                hold.append(name)
    if remove:
        remove_bases = {_hold_base_name(r) for r in remove}
        hold = [h for h in hold if _hold_base_name(h) not in remove_bases]
    data["hold"] = hold

    local_dir = os.path.dirname(local_path)
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
    tmp_path = local_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, local_path)
    return hold


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


def _stdout_signals_uptodate(stdout: str) -> bool:
    """True if a check command's stdout means "nothing to update".

    Empty output, or an empty JSON array/object (`[]`/`{}`), both count as
    up to date. Any other output (including unparseable non-empty text,
    which is treated conservatively as "there might be something") means
    the bulk update should still run.
    """
    s = (stdout or "").strip()
    if not s:
        return True
    if s in ("[]", "{}"):
        return True
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return False
    if isinstance(parsed, (list, dict)):
        return len(parsed) == 0
    return False


def run_check_command(cmd: str, timeout: int = 60) -> tuple[bool, float]:
    """Run one `check` command; return (is_up_to_date, duration_s).

    Fails open: a missing binary, non-zero exit, or timeout is treated as
    "not up to date" (i.e. the real bulk update still runs).
    """
    start = time.time()
    try:
        r = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, time.time() - start
    duration = time.time() - start
    if r.returncode != 0:
        return False, duration
    return _stdout_signals_uptodate(r.stdout), duration


def _precheck_candidates(
    cfg: dict[str, Any],
    only_origins: Optional[str] = None,
    skip_origins: Optional[str] = None,
) -> list[tuple[str, str]]:
    """(origin, cmd) pairs eligible to be pre-checked, honoring only/skip filters."""
    checks = cfg.get("check", {}) or {}
    only = _parse_csv(only_origins)
    skip = _parse_csv(skip_origins)
    out: list[tuple[str, str]] = []
    for origin, cmd in checks.items():
        if not cmd or not str(cmd).strip():
            continue
        if origin in skip:
            continue
        if only and origin not in only:
            continue
        out.append((origin, cmd))
    return out


def precheck_candidate_origins(
    cfg: dict[str, Any],
    only_origins: Optional[str] = None,
    skip_origins: Optional[str] = None,
) -> list[str]:
    """Origins that WOULD be pre-checked this run (no commands executed)."""
    return sorted(o for o, _ in _precheck_candidates(cfg, only_origins, skip_origins))


def run_prechecks(
    cfg: dict[str, Any],
    only_origins: Optional[str] = None,
    skip_origins: Optional[str] = None,
) -> dict[str, float]:
    """Run all configured `check` commands concurrently.

    Returns {origin: duration_s} for origins confirmed up to date (and thus
    safe to skip this run). Origins with no check, or whose check fails/errors/
    produces real output, are simply absent from the result (fail open).
    """
    candidates = _precheck_candidates(cfg, only_origins, skip_origins)
    if not candidates:
        return {}
    results: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        future_to_origin = {
            executor.submit(run_check_command, cmd): origin for origin, cmd in candidates
        }
        for future in as_completed(future_to_origin):
            origin = future_to_origin[future]
            try:
                uptodate, duration = future.result()
            except Exception:
                continue
            if uptodate:
                results[origin] = round(duration, 3)
    return results


def default_history_path() -> str:
    """Default location for the run-history JSONL file (override via env)."""
    return os.environ.get(
        "UPDATE_ALL_CLIS_HISTORY_FILE",
        os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
            "update-all-clis",
            "history.jsonl",
        ),
    )


HISTORY_MAX_LINES = 2000
DEFAULT_QUARANTINE_AFTER = 3
HISTORY_JOBS_PER_MEAN = 10


def load_history_records(history_path: Optional[str]) -> list[dict[str, Any]]:
    """Read all JSONL history records in file order (oldest first)."""
    if not history_path or not os.path.isfile(history_path):
        return []
    records: list[dict[str, Any]] = []
    with open(history_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def load_history_by_name(history_path: Optional[str]) -> dict[str, list[dict[str, Any]]]:
    """Group history records by job name, preserving chronological order."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for rec in load_history_records(history_path):
        name = rec.get("name")
        if not name:
            continue
        by_name.setdefault(str(name), []).append(rec)
    return by_name


def historical_mean_durations(
    by_name: dict[str, list[dict[str, Any]]],
    per_job: int = HISTORY_JOBS_PER_MEAN,
) -> dict[str, float]:
    """Mean duration_s per job name, based on the last `per_job` history records."""
    means: dict[str, float] = {}
    for name, recs in by_name.items():
        durs = [
            float(r["duration_s"])
            for r in recs
            if isinstance(r.get("duration_s"), (int, float))
        ]
        if not durs:
            continue
        recent = durs[-per_job:]
        means[name] = sum(recent) / len(recent)
    return means


def quarantined_names(
    by_name: dict[str, list[dict[str, Any]]],
    threshold: int,
) -> set[str]:
    """Names whose last `threshold` consecutive history appearances all failed.

    threshold <= 0 disables quarantine entirely (empty set).
    """
    if threshold <= 0:
        return set()
    quarantined: set[str] = set()
    for name, recs in by_name.items():
        if len(recs) < threshold:
            continue
        last = recs[-threshold:]
        if all(r.get("status") == "fail" for r in last):
            quarantined.add(name)
    return quarantined


def _order_by_history(lines: list[str], means: dict[str, float]) -> list[str]:
    """Order plan lines by historical mean duration, slowest first.

    Jobs with no history sort after jobs with history, keeping their
    relative (original) order stable within each group.
    """
    def key(idx_line: tuple[int, str]) -> tuple[int, float, int]:
        idx, line = idx_line
        parts = line.split(EMIT_SEP, 2)
        name = parts[1] if len(parts) > 1 else ""
        mean = means.get(name)
        if mean is None:
            return (1, 0.0, idx)
        return (0, -mean, idx)

    indexed = list(enumerate(lines))
    indexed.sort(key=key)
    return [line for _, line in indexed]


def collect_emit_lines(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str],
    skip_origins: Optional[str],
    history_path: Optional[str] = None,
    quarantine_after: int = DEFAULT_QUARANTINE_AFTER,
    include_quarantined: bool = False,
    precheck_uptodate: Optional[dict[str, float]] = None,
    held_config: Optional[set[str]] = None,
    held_adhoc: Optional[set[str]] = None,
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
            # NOTE: do NOT seen_bulk.add(origin) here. A known tool has its
            # own specific update command, but other, untracked globals from
            # the same origin (e.g. other npm -g packages) still need the
            # origin's bulk update to run — so the bulk line must still be
            # able to emit later for this origin (once; dedup happens at the
            # `if origin in seen_bulk` check below when bulk actually emits).
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

    # Pinned/held jobs: a known tool name or bulk origin listed in config's
    # "hold" array (persistent) or the one-run HOLD= env (ad hoc) becomes a
    # synthetic "held" line instead of running. Applied before quarantine/
    # precheck so a hold always wins regardless of history/outdated state.
    # The cmd field carries the hold's source ("config" or "env") so the
    # shell can phrase its message accordingly.
    held_config = held_config or set()
    held_adhoc = held_adhoc or set()
    if held_config or held_adhoc:
        transformed_held: list[str] = []
        for line in lines:
            parts = line.split(EMIT_SEP, 3)
            kind = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            if kind in ("known", "bulk") and name in held_config:
                transformed_held.append(f"held{EMIT_SEP}{name}{EMIT_SEP}config{EMIT_SEP}")
            elif kind in ("known", "bulk") and name in held_adhoc:
                transformed_held.append(f"held{EMIT_SEP}{name}{EMIT_SEP}env{EMIT_SEP}")
            else:
                transformed_held.append(line)
        lines = transformed_held

    # Failure quarantine: replace known/bulk lines whose job name failed its
    # last `quarantine_after` consecutive history appearances with a
    # "quarantined" line (shell prints a warning and counts it as skipped).
    by_name = load_history_by_name(history_path)
    quarantined = set() if include_quarantined else quarantined_names(by_name, quarantine_after)
    if quarantined:
        transformed: list[str] = []
        for line in lines:
            parts = line.split(EMIT_SEP, 3)
            kind = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            if kind in ("known", "bulk") and name in quarantined:
                transformed.append(f"quarantined{EMIT_SEP}{name}{EMIT_SEP}{quarantine_after}{EMIT_SEP}")
            else:
                transformed.append(line)
        lines = transformed

    # Outdated pre-checks: a bulk origin whose `check` command reported
    # nothing to do is replaced with a synthetic "uptodate" line (shell
    # prints it as an instant ok, no update command runs). Applied after
    # quarantine so a quarantined origin still shows as quarantined, not
    # up to date, if both would otherwise apply.
    if precheck_uptodate:
        transformed2: list[str] = []
        for line in lines:
            parts = line.split(EMIT_SEP, 3)
            kind = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            if kind == "bulk" and name in precheck_uptodate:
                duration = precheck_uptodate[name]
                transformed2.append(f"uptodate{EMIT_SEP}{name}{EMIT_SEP}{duration}{EMIT_SEP}")
            else:
                transformed2.append(line)
        lines = transformed2

    # Slowest-first scheduling: order by historical mean duration (desc) so
    # the long pole (usually brew) starts first in a parallel run.
    means = historical_mean_durations(by_name)
    lines = _order_by_history(lines, means)

    return lines


def emit_lines(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str],
    skip_origins: Optional[str],
    history_path: Optional[str] = None,
    quarantine_after: int = DEFAULT_QUARANTINE_AFTER,
    include_quarantined: bool = False,
    precheck_uptodate: Optional[dict[str, float]] = None,
    held_config: Optional[set[str]] = None,
    held_adhoc: Optional[set[str]] = None,
) -> None:
    for line in collect_emit_lines(
        cache_path, cfg, only_origins, skip_origins,
        history_path, quarantine_after, include_quarantined, precheck_uptodate,
        held_config, held_adhoc,
    ):
        sys.stdout.write(line + "\n")


def emit_plan_json(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str],
    skip_origins: Optional[str],
    history_path: Optional[str] = None,
    quarantine_after: int = DEFAULT_QUARANTINE_AFTER,
    include_quarantined: bool = False,
    precheck_uptodate: Optional[dict[str, float]] = None,
    held_config: Optional[set[str]] = None,
    held_adhoc: Optional[set[str]] = None,
) -> None:
    plan: list[dict[str, str]] = []
    for line in collect_emit_lines(
        cache_path, cfg, only_origins, skip_origins,
        history_path, quarantine_after, include_quarantined, precheck_uptodate,
        held_config, held_adhoc,
    ):
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
            out = r.stdout or r.stderr
            if out:
                line = out.strip().split("\n")[0].strip()
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
        "manual": (),
        "dotnet": ("dotnet", "--version"),
        "krew": ("kubectl", "krew", "version"),
        "mise": ("mise", "--version"),
        "pipx": ("pipx", "--version"),
        "grok": ("grok", "--version"),
        "path": (),
    }
    if origin == "path":
        return "many tools (PATH scan)"
    if origin == "sdkman":
        try:
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
        except (OSError, subprocess.TimeoutExpired):
            return "?"
    cmd = plans.get(origin)
    if not cmd:
        return f"({origin})"
    return _probe_single(cmd)


def _load_cached_versions(cache_path: Optional[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (tool_name->version, origin->pm_version) from a cache file, if present.

    Lets the "before" snapshot reuse versions captured on the previous run
    instead of re-spawning `--version` probes for every tool.
    """
    versions: dict[str, str] = {}
    pm_versions: dict[str, str] = {}
    if not cache_path or not os.path.isfile(cache_path):
        return versions, pm_versions
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return versions, pm_versions
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name and item.get("version"):
            versions[str(name)] = str(item["version"])
        origin = item.get("origin")
        if origin and item.get("pm_version"):
            pm_versions[str(origin)] = str(item["pm_version"])
    return versions, pm_versions


# Bulk origins whose package-manager version we can resolve to a concrete
# binary on PATH (mirrors the command table in probe_bulk). Used to find a
# stat-able path for the mtime gate below; origins absent here (e.g. "path",
# "manual", "sdkman") always get re-probed since there's nothing cheap to
# gate on.
_BULK_ORIGIN_BINARY = {
    "brew": "brew", "npm": "npm", "cargo": "cargo", "gem": "gem", "pip": "pip3",
    "uv": "uv", "uv/pip": "uv", "uv/venv": "uv", "fnm": "fnm", "bun": "bun",
    "deno": "deno", "pyenv": "pyenv", "rbenv": "rbenv", "conda": "conda",
    "opencode": "opencode", "dotnet": "dotnet", "mise": "mise", "pipx": "pipx",
    "grok": "grok",
}


def _stat_mtime(path: Optional[str]) -> Optional[float]:
    if not path:
        return None
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _bulk_origin_binary_path(origin: str) -> Optional[str]:
    name = _BULK_ORIGIN_BINARY.get(origin)
    return shutil.which(name) if name else None


def snapshot_versions(
    lines: list[str],
    cache_path: Optional[str] = None,
    prior_snapshot_path: Optional[str] = None,
) -> dict[str, Any]:
    """Probe versions for every known/bulk job in `lines`.

    If `prior_snapshot_path` is given (the pre-run snapshot, which always
    records each job's resolved binary mtime under "mtimes"), a job whose
    binary mtime hasn't changed since then reuses the prior version string
    instead of spawning a new `--version` probe. Jobs whose binary can't be
    stat'd (or whose mtime changed) are probed fresh, same as before. This
    only affects the cheaper POST snapshot; behavior/format are unaffected
    otherwise (the extra "mtimes" key is additive, existing consumers only
    read "known"/"bulk").
    """
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
        if kind in ("skip", "quarantined", "held"):
            continue
        if kind == "known":
            known_tasks.append((name, "known"))
        elif kind in ("bulk", "uptodate") and name not in seen_bulk:
            seen_bulk.add(name)
            bulk_tasks.append((name, "bulk"))

    # Reuse versions captured on the previous run (avoids re-probing)
    cached_known, cached_bulk = _load_cached_versions(cache_path)

    prior: dict[str, Any] = {}
    if prior_snapshot_path and os.path.isfile(prior_snapshot_path):
        try:
            with open(prior_snapshot_path, encoding="utf-8") as f:
                prior = json.load(f)
        except (OSError, json.JSONDecodeError):
            prior = {}
    prior_mtimes: dict[str, float] = prior.get("mtimes", {}) if isinstance(prior, dict) else {}
    prior_known: dict[str, str] = prior.get("known", {}) if isinstance(prior, dict) else {}
    prior_bulk: dict[str, str] = prior.get("bulk", {}) if isinstance(prior, dict) else {}

    mtimes: dict[str, float] = {}

    # Probe versions in parallel with progress tracking
    def probe_task(task: tuple[str, str]) -> tuple[str, str, str]:
        name, kind = task
        if kind == "known":
            path = shutil.which(name)
            mtime = _stat_mtime(path)
            if mtime is not None:
                mtimes[name] = mtime
            if (
                mtime is not None
                and prior_mtimes.get(name) == mtime
                and name in prior_known
            ):
                return name, "known", prior_known[name]
            if name in cached_known:
                return name, "known", cached_known[name]
            return name, "known", probe_known(name)
        else:
            path = _bulk_origin_binary_path(name)
            mtime = _stat_mtime(path)
            if mtime is not None:
                mtimes[name] = mtime
            if (
                mtime is not None
                and prior_mtimes.get(name) == mtime
                and name in prior_bulk
            ):
                return name, "bulk", prior_bulk[name]
            if name in cached_bulk:
                return name, "bulk", cached_bulk[name]
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

    return {"known": known, "bulk": bulk, "mtimes": mtimes}


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


# Origins where individual tool entries make sense (exclude brew which is
# mostly system-level library binaries that aren't worth tracking individually)
_TRACKABLE_ORIGINS = frozenset({
    "npm", "cargo", "go", "gem", "pipx", "manual", "path",
    "uv", "uv/pip", "uv/venv", "fnm", "bun", "deno",
    "mise", "opencode", "grok", "conda", "dotnet", "krew",
})


def suggest_known(cache_path: str, cfg: dict[str, Any]) -> None:
    """Suggest tools covered by bulk but missing from the known list."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Cache file not found: {cache_path}. Run discovery scan first.")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cache file {cache_path}: {e}")
    tools = [t for t in data if "name" in t]
    known = set(cfg["known"].keys())
    bulk_origins = cfg["bulk"]

    # Group tools by origin that are covered by bulk but not in known
    by_origin: dict[str, list[dict]] = {}
    brew_count = 0
    for t in tools:
        name = t["name"]
        origin = t.get("origin", "?")
        if name in known:
            continue
        if origin not in bulk_origins:
            inferred = _infer_origin_from_symlink(name, origin)
            if not inferred or inferred not in bulk_origins:
                continue
            origin = inferred
        if origin == "brew":
            brew_count += 1
            continue
        by_origin.setdefault(origin, []).append(t)

    if not by_origin and brew_count == 0:
        print("All discovered tools are in the known list.", file=sys.stderr)
        return

    for origin in sorted(by_origin.keys()):
        items = sorted(by_origin[origin], key=lambda x: x["name"])
        print(f"  {origin} ({len(items)} tools):")
        for t in items:
            print(f'    "{t["name"]}": "UPDATE_COMMAND_HERE",')
        print()

    if brew_count > 0:
        print(f"  [brew: {brew_count} tools skipped — system-level packages, not user CLIs]")
        print()

    total = sum(len(v) for v in by_origin.values())
    print(f"Total: {total} tool(s) updated via bulk but missing from known list.")
    print()
    print("Copy entries above into ~/.config/update-all-clis/config.local.json")
    print('under the "known" section, replacing UPDATE_COMMAND_HERE.')
    print()


def suggest_known_count(cache_path: str, cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Return list of (name, origin) for bulk-covered tools not in known (no output).
    Excludes brew origin — too noisy for auto-tip."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    tools = [t for t in data if "name" in t]
    known = set(cfg["known"].keys())
    bulk_origins = cfg["bulk"]
    found: list[tuple[str, str]] = []
    for t in tools:
        name = t["name"]
        origin = t.get("origin", "?")
        if name in known:
            continue
        if origin == "brew":
            continue
        if origin not in bulk_origins:
            inferred = _infer_origin_from_symlink(name, origin)
            if not inferred or inferred not in bulk_origins:
                continue
            origin = inferred
        if origin == "brew":
            continue
        found.append((name, origin))
    return found


UNKNOWN_LOG_DEFAULT = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "update-all-clis",
    "unknown_tools.json",
)


def log_unknowns(cache_path: str, cfg: dict[str, Any], unknown_log_path: str) -> None:
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Cache file not found: {cache_path}. Run discovery scan first.")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cache file {cache_path}: {e}")
    tools = [t for t in data if "name" in t]
    meta = next((t for t in data if "scanned_at" in t), None)
    scanned_at = meta.get("scanned_at") if meta else None

    known = set(cfg["known"].keys())
    bulk_cmds = cfg["bulk"]
    bulk = {o for o, c in bulk_cmds.items() if c and str(c).strip()}

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
            existing_tools[name]["times_seen"] = existing_tools[name].get("times_seen", 0) + 1
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
    tmp_path = unknown_log_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    os.replace(tmp_path, unknown_log_path)


def report_unknown(unknown_log_path: str, min_times: int = 1) -> None:
    if not os.path.isfile(unknown_log_path):
        print("No unknown tools log found.", file=sys.stderr)
        return
    with open(unknown_log_path, encoding="utf-8") as f:
        data = json.load(f)
    tools = data.get("tools", {})

    unhandled = [t for t in tools.values() if t.get("times_seen", 0) >= min_times and not t.get("acknowledged")]
    acked = [t for t in tools.values() if t.get("acknowledged")]

    if not unhandled and not acked:
        print("No unknown tools recorded.")
        return

    if unhandled:
        unhandled.sort(key=lambda x: (-x.get("times_seen", 0), x["name"]))
        print("Tools with no update path (seen in recent scans):")
        print()
        for t in unhandled:
            flag = ""
            if t.get("times_seen", 0) >= 2:
                flag = f"  (run with --ack-unknown={t['name']} to dismiss)"
            print(f'  {t["name"]}  [origin: {t.get("origin", "?")}]  '
                  f'(seen {t.get("times_seen", 0)}x, last: {t.get("last_seen")}){flag}')
            print(f'    add to known: "{t["name"]}": "UPDATE_COMMAND_HERE",')
            print()
        print("Tip: Add entries above to ~/.config/update-all-clis/config.local.json")
        print("under the \"known\" section to give them an update path.")
        print()

    if acked:
        print("Acknowledged (dismissed from report):")
        for t in acked:
            print(f'  {t["name"]}  (seen {t.get("times_seen", 0)}x, last: {t.get("last_seen")})')


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


def diff_new_tools(prev_names_path: str, cache_path: str) -> list[str]:
    """Return tool names present in the cache but not in the previous-names file."""
    prev: set[str] = set()
    if os.path.isfile(prev_names_path):
        try:
            with open(prev_names_path, encoding="utf-8") as f:
                prev = {line.strip() for line in f if line.strip()}
        except OSError:
            prev = set()
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    current = {t["name"] for t in data if "name" in t}
    if not prev:
        return []
    return sorted(current - prev)


def _parse_history_result_line(line: str) -> Optional[dict[str, Any]]:
    """Parse one shell-emitted job-result line: kind\\x1ename\\x1ecmd\\x1eec\\x1estart\\x1eend."""
    parts = line.split(EMIT_SEP)
    if len(parts) < 6:
        return None
    kind, name, cmd, ec_s, start_s, end_s = parts[:6]
    try:
        ec = int(ec_s)
        start = float(start_s)
        end = float(end_s)
    except ValueError:
        return None
    return {"kind": kind, "name": name, "cmd": cmd, "ec": ec, "start": start, "end": end}


def history_append(
    history_path: str,
    run_id: str,
    result_lines: list[str],
    before: dict[str, Any],
    after: dict[str, Any],
    max_lines: int = HISTORY_MAX_LINES,
) -> int:
    """Append one JSONL record per executed (known/bulk) job to the history file.

    `result_lines` are shell-emitted "kind\\x1ename\\x1ecmd\\x1eec\\x1estart\\x1eend"
    strings for every job actually run this pass (skip/quarantined jobs are not
    included by the caller). Prunes the file to the most recent `max_lines` lines.
    Returns the number of records appended.

    "held" jobs are recorded too (unlike "quarantined", which isn't): they get
    `status: "held"` (a status distinct from "ok"/"fail", so they never count
    toward quarantine's consecutive-failure streak) plus `"held": true`, and
    since the job never actually ran, no version lookup is attempted.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    records: list[dict[str, Any]] = []
    for line in result_lines:
        line = line.strip()
        if not line:
            continue
        parsed = _parse_history_result_line(line)
        if not parsed or parsed["kind"] not in ("known", "bulk", "uptodate", "held"):
            continue
        name = parsed["name"]
        if parsed["kind"] == "held":
            records.append({
                "ts": ts,
                "run_id": run_id,
                "kind": "held",
                "name": name,
                "cmd": parsed["cmd"],
                "duration_s": round(parsed["end"] - parsed["start"], 3),
                "status": "held",
                "held": True,
                "version_before": "?",
                "version_after": "?",
            })
            continue
        # "uptodate" (pre-check skip) jobs are bulk origins under the hood;
        # look their versions up in the bulk section of before/after.
        section = "bulk" if parsed["kind"] == "uptodate" else parsed["kind"]
        records.append({
            "ts": ts,
            "run_id": run_id,
            "kind": parsed["kind"],
            "name": name,
            "cmd": parsed["cmd"],
            "duration_s": round(parsed["end"] - parsed["start"], 3),
            "status": "ok" if parsed["ec"] == 0 else "fail",
            "version_before": before.get(section, {}).get(name, "?"),
            "version_after": after.get(section, {}).get(name, "?"),
        })

    if not records:
        return 0

    hist_dir = os.path.dirname(history_path)
    if hist_dir:
        os.makedirs(hist_dir, exist_ok=True)

    existing: list[str] = []
    if os.path.isfile(history_path):
        try:
            with open(history_path, encoding="utf-8") as f:
                existing = f.read().splitlines()
        except OSError:
            existing = []

    combined = existing + [json.dumps(r) for r in records]
    if len(combined) > max_lines:
        combined = combined[-max_lines:]

    tmp_path = history_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(combined) + ("\n" if combined else ""))
    os.replace(tmp_path, history_path)
    return len(records)


def group_history_by_run(records: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group history records into runs, preserving file order (each run_id is contiguous)."""
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for r in records:
        rid = str(r.get("run_id", "?"))
        if groups and groups[-1][0] == rid:
            groups[-1][1].append(r)
        else:
            groups.append((rid, [r]))
    return groups


def format_history(history_path: str, n: int = 3) -> str:
    """Human-readable summary of the last `n` runs recorded in history.jsonl."""
    records = load_history_records(history_path)
    groups = group_history_by_run(records)
    if not groups:
        return "No run history recorded yet.\n"

    out: list[str] = []
    for run_id, recs in reversed(groups[-n:] if n > 0 else groups):
        ts = recs[0].get("ts", "?") if recs else "?"
        ok = sum(1 for r in recs if r.get("status") == "ok")
        fail = sum(1 for r in recs if r.get("status") == "fail")
        out.append(f"Run {run_id} ({ts}) — {ok} ok, {fail} failed")

        changed = [r for r in recs if r.get("version_before") != r.get("version_after")]
        if changed:
            out.append("  Changed:")
            for r in changed:
                out.append(f"    {r.get('name')}: {r.get('version_before')} → {r.get('version_after')}")

        failures = [r for r in recs if r.get("status") == "fail"]
        if failures:
            out.append("  Failed:")
            for r in failures:
                out.append(f"    {r.get('name')}")
        out.append("")

    return "\n".join(out).rstrip("\n") + "\n"


def _parse_version_tuple(v: Optional[str]) -> Optional[tuple[int, ...]]:
    """Best-effort leading dotted-integer sequence from a free-form version string.

    Tolerates a leading "v"/"V" and arbitrary trailing text (e.g.
    "ripgrep 15.1.0" won't parse — callers should pass just the version
    token; "v2.3.1" / "2.3.1-beta" / "2.1.139 (Claude Code)" all parse their
    leading numeric run). Returns None if no leading digits are found.
    """
    if not v:
        return None
    s = v.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    m = re.match(r"(\d+(?:\.\d+)*)", s)
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def leading_major(v: Optional[str]) -> Optional[int]:
    """The leading integer component of a version string, or None if unparseable."""
    t = _parse_version_tuple(v)
    return t[0] if t else None


def is_major_upgrade(before: Optional[str], after: Optional[str]) -> bool:
    """True if `after`'s leading integer component is greater than `before`'s.

    Both sides must parse to a usable leading integer; unparseable/"?"
    versions never count as a major jump (conservative — no false positives).
    """
    b = leading_major(before)
    a = leading_major(after)
    if b is None or a is None:
        return False
    return a > b


def format_run_summary(
    before: dict[str, Any],
    after: dict[str, Any],
    ok: int,
    fail: int,
    new_tools: Optional[list[str]] = None,
    quarantined: Optional[list[str]] = None,
    held: Optional[list[str]] = None,
) -> str:
    upgraded: list[str] = []
    unchanged: list[str] = []
    for section in ("known", "bulk"):
        names = set(before.get(section, {})) | set(after.get(section, {}))
        for name in sorted(names):
            b = before.get(section, {}).get(name, "?")
            a = after.get(section, {}).get(name, "?")
            if b == a:
                unchanged.append(name)
            else:
                marker = "  [MAJOR UPGRADE]" if is_major_upgrade(b, a) else ""
                upgraded.append(f"  {name}: {b} → {a}{marker}")

    lines_out: list[str] = [
        "update-all-clis",
        f"Steps: {ok} ok, {fail} failed",
        "",
        f"Upgraded ({len(upgraded)}):",
    ]
    lines_out.extend(upgraded if upgraded else ["  (none)"])

    lines_out.append("")
    new_tools = new_tools or []
    lines_out.append(f"New installs added for future runs ({len(new_tools)}):")
    if new_tools:
        lines_out.extend(f"  {name}" for name in sorted(new_tools))
    else:
        lines_out.append("  (none)")

    lines_out.append("")
    lines_out.append(f"Already up to date ({len(unchanged)}):")
    if unchanged:
        lines_out.append("  " + ", ".join(sorted(unchanged)))
    else:
        lines_out.append("  (none)")

    lines_out.append("")
    quarantined = quarantined or []
    lines_out.append(f"Quarantined, skipped this run ({len(quarantined)}):")
    if quarantined:
        lines_out.append("  " + ", ".join(sorted(quarantined)))
    else:
        lines_out.append("  (none)")

    lines_out.append("")
    held = held or []
    lines_out.append(f"Held (pinned in config), skipped this run ({len(held)}):")
    if held:
        lines_out.append("  " + ", ".join(sorted(held)))
    else:
        lines_out.append("  (none)")

    return "\n".join(lines_out) + "\n"


def notify_macos_dialog(
    before: dict[str, Any],
    after: dict[str, Any],
    ok: int,
    fail: int,
    new_tools: Optional[list[str]] = None,
    quarantined: Optional[list[str]] = None,
    held: Optional[list[str]] = None,
) -> None:
    if sys.platform != "darwin":
        return
    body = format_run_summary(before, after, ok, fail, new_tools, quarantined, held).rstrip("\n")
    if len(body) > 950:
        body = body[:947] + "\n…"
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(body)
        # Run the modal fully detached so the calling script never blocks.
        # `giving up after` ensures osascript never lingers indefinitely.
        # The wrapper removes the temp file after osascript finishes.
        path_osa = path.replace("\\", "\\\\").replace('"', '\\"')
        osa_args = [
            "osascript",
            "-e", f'set f to POSIX file "{path_osa}"',
            "-e", "set msg to read file f as Unicode text",
            "-e",
            'display dialog msg with title "update-all-clis" '
            'buttons {"OK"} default button "OK" giving up after 120',
        ]
        wrapper = " ".join(shlex.quote(a) for a in osa_args) + f" ; rm -f {shlex.quote(path)}"
        subprocess.Popen(
            ["bash", "-c", wrapper],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass


def notify_linux(
    before: dict[str, Any],
    after: dict[str, Any],
    ok: int,
    fail: int,
    new_tools: Optional[list[str]] = None,
    quarantined: Optional[list[str]] = None,
    held: Optional[list[str]] = None,
) -> None:
    if sys.platform == "linux" and shutil.which("notify-send"):
        body = format_run_summary(before, after, ok, fail, new_tools, quarantined, held).rstrip("\n")
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


def notify_diff(
    before: dict[str, Any],
    after: dict[str, Any],
    ok: int,
    fail: int,
    new_tools: Optional[list[str]] = None,
    quarantined: Optional[list[str]] = None,
    held: Optional[list[str]] = None,
) -> None:
    notify_macos_dialog(before, after, ok, fail, new_tools, quarantined, held)
    notify_linux(before, after, ok, fail, new_tools, quarantined, held)


def _load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_new_tools_arg(path: str) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


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


# Same exclusion rules as update_all_clis.sh's scan_dir(), kept in sync by
# hand (see the `case "$name"` block there).
_SCAN_EXCLUDE_NAMES = frozenset({
    "npm", "npx", "node", "python", "python3", "ruby", "perl", "lua",
    "bash", "zsh", "sh", "sh.dist", "npm-cli", "npx-cli",
    "corepack", "corepack.exe", "yarn", "yarn.js", "pnpm", "pnpm.js", "git",
})


def _scan_dir_entries(dir_path: str) -> list[str]:
    """Names of executable, non-hidden, non-excluded files directly in `dir_path`.

    Mirrors update_all_clis.sh's scan_dir(): only regular files (no
    subdirectories/symlinked dirs), executable, not dotfiles, not one of the
    shared runtime/vcs binaries every manager drags in, not `git-*`.
    """
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return []
    names: list[str] = []
    for name in entries:
        if name.startswith("."):
            continue
        if name in _SCAN_EXCLUDE_NAMES or name.startswith("git-"):
            continue
        full = os.path.join(dir_path, name)
        try:
            if not os.path.isfile(full) or not os.access(full, os.X_OK):
                continue
        except OSError:
            continue
        names.append(name)
    return names


def _sdkman_candidate_binaries(dir_path: str) -> list[str]:
    """Names of executables in `$dir/*/current/bin/*` (sdkman's layout)."""
    names: list[str] = []
    for cand in sorted(glob.glob(os.path.join(dir_path, "*", "current", "bin", "*"))):
        base = os.path.basename(cand)
        if base.startswith("."):
            continue
        try:
            if not os.path.isfile(cand) or not os.access(cand, os.X_OK):
                continue
        except OSError:
            continue
        names.append(base)
    return names


def _tree_scan_entries(dir_path: str) -> list[str]:
    """Names from every `$dir/*/bin` subdirectory (mirrors scan_tree())."""
    names: list[str] = []
    for sub in sorted(glob.glob(os.path.join(dir_path, "*", "bin"))):
        names.extend(_scan_dir_entries(sub))
    return names


def incremental_scan_merge(
    rows: list[tuple[str, str, str, bool]],
    cache_path: str,
    scanned_at: str,
    force: bool = False,
    extra_tools: Optional[list[tuple[str, str]]] = None,
) -> str:
    """Build the next cache.json, re-walking only directories that changed.

    `rows` is (dir, origin, mode, exists) for every directory the shell
    would otherwise scan directly, where mode is "dir" (flat, scan_dir),
    "tree" (one level of `*/bin` subdirs, scan_tree), or "sdkman"
    (`*/current/bin/*`). `exists` is whatever `[[ -d "$dir" ]]` found in the
    shell — a row with exists=False prunes any cached tools tagged to that
    directory (their source disappeared).

    A directory whose mtime matches what's stored in the cache's
    "dir_mtimes" record reuses the tools cached under that directory
    instead of re-listing it. `force=True` (--rescan) ignores stored
    mtimes and re-walks everything, refreshing all stored mtimes.

    Top-level directory mtime is sufficient for "dir" (adding/removing a
    file changes the parent dir's mtime on APFS/most filesystems) but NOT
    for deep changes inside a "tree"/"sdkman" node's subdirectories (e.g. a
    Homebrew Cellar upgrade that doesn't add/remove a top-level symlink).
    For those, only *new/removed* top-level entries are guaranteed to be
    noticed; that's judged acceptable since existing binary *names* don't
    change on an in-place upgrade, and `--rescan` remains available to force
    a full walk.
    """
    existing: list[Any] = []
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = []

    cached_tools = [t for t in existing if isinstance(t, dict) and "name" in t]
    mtimes_rec = next((t for t in existing if isinstance(t, dict) and "dir_mtimes" in t), None)
    old_mtimes: dict[str, float] = mtimes_rec.get("dir_mtimes", {}) if mtimes_rec else {}
    existing_versions = {t["name"]: t["version"] for t in cached_tools if "version" in t}

    cached_by_dir: dict[str, list[dict[str, Any]]] = {}
    for t in cached_tools:
        d = t.get("dir")
        if d:
            cached_by_dir.setdefault(d, []).append(t)

    out_tools: list[dict[str, Any]] = []
    new_mtimes: dict[str, float] = {}
    handled_dirs: set[str] = set()
    # Dedup by (name, origin) only — matching the old `sort -u` on "name|origin"
    # lines. The SAME (name, origin) can legitimately turn up from more than
    # one directory (e.g. a brew keg's opt/*/bin entry and a top-level
    # /opt/homebrew/bin symlink); only the first directory's tag is kept for
    # future mtime-gating, but only one final tool record is emitted.
    seen_keys: set[tuple[str, str]] = set()

    def emit(name: str, origin: str, dir_tag: Optional[str]) -> None:
        key = (name, origin)
        if key in seen_keys:
            return
        seen_keys.add(key)
        entry: dict[str, Any] = {"name": name, "origin": origin}
        if dir_tag:
            entry["dir"] = dir_tag
        if name in existing_versions:
            entry["version"] = existing_versions[name]
        out_tools.append(entry)

    for dir_path, origin, mode, exists in rows:
        handled_dirs.add(dir_path)
        if not exists:
            continue
        try:
            cur_mtime = os.stat(dir_path).st_mtime
        except OSError:
            continue
        old_mtime = old_mtimes.get(dir_path)
        if (not force) and old_mtime is not None and cur_mtime == old_mtime and dir_path in cached_by_dir:
            for t in cached_by_dir[dir_path]:
                emit(t["name"], origin, dir_path)
        else:
            if mode == "tree":
                names = _tree_scan_entries(dir_path)
            elif mode == "sdkman":
                names = _sdkman_candidate_binaries(dir_path)
            else:
                names = _scan_dir_entries(dir_path)
            for n in names:
                emit(n, origin, dir_path)
        new_mtimes[dir_path] = cur_mtime

    # Non-directory-gated entries the shell adds directly (currently just
    # the fnm sentinel: fnm's own binary lives under a version-manager
    # shim, not a plain bin dir worth mtime-tracking).
    for name, origin in (extra_tools or []):
        emit(name, origin, None)

    # Carry forward mtimes for directories not mentioned at all this run
    # (e.g. a manager whose whole resolution path is conditional and wasn't
    # even attempted, such as npm's dirs when npm itself isn't installed).
    for d, mt in old_mtimes.items():
        if d not in handled_dirs:
            new_mtimes[d] = mt

    # Carry forward cached tools whose directory wasn't touched this run,
    # and any tool with no "dir" tag at all (pre-migration cache entries,
    # or entries the shell adds directly without directory gating, e.g. fnm).
    for t in cached_tools:
        d = t.get("dir")
        if d is None or d not in handled_dirs:
            emit(t["name"], t.get("origin", "?"), d)

    out_tools.append({"scanned_at": scanned_at, "count": len(out_tools)})
    out_tools.append({"dir_mtimes": new_mtimes})
    return json.dumps(out_tools, indent=2)


def parse_scan_rows(rows_input: str) -> list[tuple[str, str, str, bool]]:
    """Parse "dir\\torigin\\tmode\\texists" lines (as written by the shell)."""
    rows: list[tuple[str, str, str, bool]] = []
    for line in rows_input.split("\n"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        dir_path, origin, mode, exists_s = parts[0], parts[1], parts[2], parts[3]
        rows.append((dir_path, origin, mode, exists_s == "1"))
    return rows


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
            result["valid"] = False
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
    
    timestamp = time.strftime("%Y%m%dT%H%M%S")
    backup_name = f"{os.path.basename(cache_path)}.{timestamp}.{os.getpid()}"
    backup_path = os.path.join(backup_dir, backup_name)
    
    shutil.copy2(cache_path, backup_path)
    logger.debug(f"Created backup: {backup_path}")
    _prune_backups(cache_path)
    return backup_path


def _prune_backups(cache_path: str, keep: int = 10) -> None:
    """Keep only the most recent `keep` backups for a cache file."""
    for old in list_backups(cache_path)[keep:]:
        try:
            os.unlink(old)
            logger.debug(f"Pruned old backup: {old}")
        except OSError:
            pass


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


def benchmark_operation(
    cache_path: str,
    cfg: dict[str, Any],
    base_path: Optional[str] = None,
    local_path: Optional[str] = None,
) -> dict[str, float]:
    """Benchmark key operations and return timing results."""
    results = {}
    
    # Benchmark config loading
    if base_path:
        start = time.time()
        load_merge(base_path, local_path)
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


# =============================================================================
# Doctor: read-only diagnostics over the existing cache + history + config.
# Each check is independent and failure-isolated (see doctor_report) so one
# crashing check never prevents the rest of the report from printing.
# =============================================================================

def doctor_broken_symlinks(dirs: list[str]) -> list[str]:
    """Symlinks in the given directories whose target no longer resolves."""
    broken: list[str] = []
    seen_dirs: set[str] = set()
    for d in dirs:
        if not d or d in seen_dirs:
            continue
        seen_dirs.add(d)
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            full = os.path.join(d, name)
            try:
                if os.path.islink(full) and not os.path.exists(full):
                    broken.append(full)
            except OSError:
                continue
    return sorted(broken)


def doctor_shadowed_duplicates(cache_path: str) -> list[dict[str, Any]]:
    """Binary names whose cache entries resolve to 2+ genuinely different files.

    A name discovered under several origins is normal — e.g. an npm global
    seen by both the npm query and the `$PATH` scan of `~/.npm-global/bin`
    resolves to the same real file and is NOT shadowing. Only names whose
    entries resolve (via realpath) to distinct existing files are reported:
    for those, which copy runs genuinely depends on `$PATH` order. Reports
    the distinct real paths and which absolute path currently wins on the
    live system's `$PATH` (via shutil.which).
    """
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    tools = [t for t in data if isinstance(t, dict) and "name" in t]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for t in tools:
        by_name.setdefault(t["name"], []).append(t)
    out: list[dict[str, Any]] = []
    for name in sorted(by_name):
        entries = by_name[name]
        if len(entries) < 2:
            continue
        origins: set[str] = set()
        real_paths: set[str] = set()
        for t in entries:
            origins.add(str(t.get("origin", "?")))
            d = t.get("dir")
            if not d:
                continue
            full = os.path.join(os.path.expanduser(str(d)), name)
            try:
                if os.path.exists(full):
                    real_paths.add(os.path.realpath(full))
            except OSError:
                continue
        if len(real_paths) < 2:
            continue
        out.append({
            "name": name,
            "origins": sorted(origins),
            "paths": sorted(real_paths),
            "winner_path": shutil.which(name),
        })
    return out


def doctor_chronic_failures(
    history_path: Optional[str],
    window: int = 10,
    min_failures: int = 3,
) -> list[dict[str, Any]]:
    """Jobs with >= `min_failures` failures in their last `window` history records.

    Surfaces failure-prone jobs even if they haven't (yet) hit the
    consecutive-failure quarantine threshold (e.g. failing intermittently
    rather than on every single run).
    """
    by_name = load_history_by_name(history_path)
    out: list[dict[str, Any]] = []
    for name in sorted(by_name):
        recent = by_name[name][-window:]
        fails = sum(1 for r in recent if r.get("status") == "fail")
        if fails >= min_failures:
            out.append({"name": name, "failures": fails, "checked": len(recent)})
    return out


def doctor_not_installed(cfg: dict[str, Any]) -> list[str]:
    """`known` entries with no binary on PATH — informational only.

    tool_config.json deliberately ships update commands for tools you
    *might* install; the updater silently skips absent ones, so these are
    not findings and don't affect the doctor exit status.
    """
    known = cfg.get("known", {}) or {}
    return [name for name in sorted(known) if not shutil.which(name)]


def doctor_config_issues(cfg: dict[str, Any]) -> list[str]:
    """Config-level issues: dangling `hold`/`check` entries."""
    issues: list[str] = []
    known = cfg.get("known", {}) or {}
    bulk = cfg.get("bulk", {}) or {}
    hold = cfg.get("hold", []) or []
    check = cfg.get("check", {}) or {}

    valid_targets = set(known) | set(bulk)
    for entry in sorted(normalize_hold_entries(hold)):
        if entry not in valid_targets:
            issues.append(f"hold entry '{entry}' matches no known tool or bulk origin")

    for origin in sorted(check):
        if origin not in bulk or not str(bulk.get(origin, "")).strip():
            issues.append(f"check entry for origin '{origin}' has no corresponding bulk command")

    return issues


# Mirrors the scan exclusions in update_all_clis.sh: system dirs the user
# can't (or shouldn't) modify, so broken symlinks there aren't actionable.
_DOCTOR_SYSTEM_DIRS = ("/usr/bin", "/bin", "/sbin", "/usr/sbin", "/usr/libexec",
                       "/run/current-system/sw/bin")
_DOCTOR_SYSTEM_PREFIXES = ("/System/", "/nix/")


def _doctor_dir_excluded(d: str) -> bool:
    return d in _DOCTOR_SYSTEM_DIRS or d.startswith(_DOCTOR_SYSTEM_PREFIXES)


def doctor_report(
    cache_path: str,
    cfg: dict[str, Any],
    history_path: Optional[str] = None,
    extra_dirs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run every doctor check, isolating failures so one crash doesn't kill the rest."""
    report: dict[str, Any] = {
        "cache_validation": {},
        "broken_symlinks": [],
        "shadowed_duplicates": [],
        "chronic_failures": [],
        "config_issues": [],
        "not_installed": [],
        "errors": [],
    }

    try:
        report["cache_validation"] = validate_cache(cache_path)
    except Exception as e:
        report["errors"].append(f"cache validation failed: {e}")

    try:
        dirs: set[str] = set(extra_dirs or [])
        if os.path.isfile(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                data = json.load(f)
            for t in data:
                if isinstance(t, dict) and t.get("dir"):
                    dirs.add(t["dir"])
        dirs.update(p for p in os.environ.get("PATH", "").split(os.pathsep) if p)
        report["broken_symlinks"] = doctor_broken_symlinks(
            sorted(d for d in dirs if not _doctor_dir_excluded(d)))
    except Exception as e:
        report["errors"].append(f"broken symlink check failed: {e}")

    try:
        report["shadowed_duplicates"] = doctor_shadowed_duplicates(cache_path)
    except Exception as e:
        report["errors"].append(f"shadowed duplicate check failed: {e}")

    try:
        report["chronic_failures"] = doctor_chronic_failures(history_path or default_history_path())
    except Exception as e:
        report["errors"].append(f"chronic failure check failed: {e}")

    try:
        report["config_issues"] = doctor_config_issues(cfg)
    except Exception as e:
        report["errors"].append(f"config issue check failed: {e}")

    try:
        report["not_installed"] = doctor_not_installed(cfg)
    except Exception as e:
        report["errors"].append(f"not-installed check failed: {e}")

    return report


def doctor_has_findings(report: dict[str, Any]) -> bool:
    # Informational sections don't count as findings: `not_installed`
    # (config catalogs tools you might install) and cache warnings
    # (duplicate names across origins are normal — see shadowed check).
    cv = report.get("cache_validation", {}) or {}
    return bool(
        (not cv.get("valid", True))
        or cv.get("errors")
        or report.get("broken_symlinks")
        or report.get("shadowed_duplicates")
        or report.get("chronic_failures")
        or report.get("config_issues")
        or report.get("errors")
    )


def format_doctor_report(report: dict[str, Any]) -> str:
    lines: list[str] = ["update-all-clis doctor report", "=" * 30, ""]

    cv = report.get("cache_validation", {}) or {}
    lines.append(f"Cache valid: {cv.get('valid')}")
    for w in cv.get("warnings", []) or []:
        lines.append(f"  warning: {w}")
    for e in cv.get("errors", []) or []:
        lines.append(f"  error: {e}")
    lines.append("")

    bs = report.get("broken_symlinks", []) or []
    lines.append(f"Broken symlinks ({len(bs)}):")
    if bs:
        lines.extend(f"  {p}" for p in bs)
    else:
        lines.append("  (none)")
    lines.append("")

    sd = report.get("shadowed_duplicates", []) or []
    lines.append(f"Shadowed duplicates ({len(sd)}):")
    if sd:
        for d in sd:
            lines.append(
                f"  {d['name']}  [origins: {', '.join(d['origins'])}]  "
                f"winner: {d.get('winner_path') or '?'}"
            )
            for p in d.get("paths", []) or []:
                lines.append(f"    - {p}")
    else:
        lines.append("  (none)")
    lines.append("")

    cf = report.get("chronic_failures", []) or []
    lines.append(f"Chronic failures ({len(cf)}):")
    if cf:
        for c in cf:
            lines.append(f"  {c['name']}: {c['failures']}/{c['checked']} recent runs failed")
    else:
        lines.append("  (none)")
    lines.append("")

    ci = report.get("config_issues", []) or []
    lines.append(f"Config issues ({len(ci)}):")
    if ci:
        lines.extend(f"  {issue}" for issue in ci)
    else:
        lines.append("  (none)")

    ni = report.get("not_installed", []) or []
    if ni:
        lines.append("")
        lines.append(f"Known but not installed ({len(ni)}, informational — these are skipped):")
        lines.append("  " + ", ".join(ni))

    errs = report.get("errors", []) or []
    if errs:
        lines.append("")
        lines.append(f"Check errors ({len(errs)}):")
        lines.extend(f"  {e}" for e in errs)

    return "\n".join(lines) + "\n"


# =============================================================================
# Changelog digest: best-effort, offline-safe release-notes lookup for tools
# whose version changed this run and have a "repos" (owner/repo) mapping.
# Pure helpers (tag-range matching, truncation, formatting) are unit-tested
# without network; fetch_github_releases is the only part that hits the
# network and is mocked in tests.
# =============================================================================

CHANGELOG_MAX_TOOLS = 5
CHANGELOG_BODY_LIMIT = 400
CHANGELOG_TOTAL_TIMEOUT = 10.0


def tag_to_version(tag: str) -> str:
    """Strip a leading "v"/"V" from a release tag (tolerant of untagged input)."""
    if not tag:
        return tag
    return tag[1:] if tag[:1] in ("v", "V") else tag


def tag_in_range(tag: str, before: Optional[str], after: Optional[str]) -> bool:
    """True if `tag`'s version falls in (before, after] — i.e. it's a release

    the update just moved past. Unparseable `tag`/`after` never match
    (conservative); a missing/unparseable `before` only requires tag <= after
    (can't rule out "too old" without a lower bound, so we don't try).
    """
    tag_t = _parse_version_tuple(tag_to_version(tag))
    after_t = _parse_version_tuple(after)
    if tag_t is None or after_t is None:
        return False
    if tag_t > after_t:
        return False
    before_t = _parse_version_tuple(before)
    if before_t is not None and tag_t <= before_t:
        return False
    return True


def truncate_changelog_body(body: Optional[str], limit: int = CHANGELOG_BODY_LIMIT) -> str:
    text = (body or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def format_changelog_section(entries: list[dict[str, Any]], capped: bool = False, cap: int = CHANGELOG_MAX_TOOLS) -> str:
    """Render matched release entries as a "Changelog highlights" section.

    `entries` is a list of {"name", "version_before", "version_after",
    "releases": [{"tag", "body"}, ...]}. Returns "" if there's nothing to show.
    """
    if not entries:
        return ""
    lines = ["Changelog highlights:"]
    for e in entries:
        lines.append(f"  {e['name']} ({e['version_before']} → {e['version_after']}):")
        for rel in e.get("releases", []):
            body = truncate_changelog_body(rel.get("body", ""))
            tag = rel.get("tag", "?")
            if body:
                lines.append(f"    [{tag}] {body}")
            else:
                lines.append(f"    [{tag}] (no release notes)")
    if capped:
        lines.append(f"  (capped at {cap} tools this run — rest omitted)")
    return "\n".join(lines) + "\n"


def fetch_github_releases(slug: str, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Best-effort GitHub releases lookup for `owner/repo`; [] on any failure.

    Prefers `gh api` (works with auth, higher rate limit) when the `gh`
    binary is available; falls back to an unauthenticated urllib request
    against the public REST API (60 req/hr limit).
    """
    if shutil.which("gh"):
        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{slug}/releases?per_page=10"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if r.returncode == 0 and r.stdout.strip():
                parsed = json.loads(r.stdout)
                if isinstance(parsed, list):
                    return parsed
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
            pass
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{slug}/releases?per_page=10",
            headers={"User-Agent": "update-all-clis", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
            if isinstance(parsed, list):
                return parsed
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError, TimeoutError):
        pass
    return []


def changed_tools_with_repos(
    before: dict[str, Any],
    after: dict[str, Any],
    repos: dict[str, str],
) -> list[tuple[str, str, str]]:
    """(name, version_before, version_after) for every changed tool with a repos mapping."""
    changed: list[tuple[str, str, str]] = []
    for section in ("known", "bulk"):
        b_map = before.get(section, {}) or {}
        a_map = after.get(section, {}) or {}
        for name in sorted(set(b_map) | set(a_map)):
            if name not in repos:
                continue
            bv, av = b_map.get(name, "?"), a_map.get(name, "?")
            if bv != av and bv not in ("?", "") and av not in ("?", ""):
                changed.append((name, bv, av))
    return changed


def build_changelog_digest(
    before: dict[str, Any],
    after: dict[str, Any],
    cfg: dict[str, Any],
    max_tools: int = CHANGELOG_MAX_TOOLS,
    total_timeout: float = CHANGELOG_TOTAL_TIMEOUT,
    fetch: Any = fetch_github_releases,
) -> str:
    """Build the "Changelog highlights" section for this run, or "" if nothing to show.

    Network calls (via `fetch`, defaulting to fetch_github_releases) are
    capped at `max_tools` per run and to a `total_timeout`-second wall clock
    budget; any single tool's failure (offline, rate-limited, no matching
    tag) just omits that tool rather than aborting the whole digest.
    """
    repos = cfg.get("repos", {}) or {}
    changed = changed_tools_with_repos(before, after, repos)
    if not changed:
        return ""

    capped = len(changed) > max_tools
    start = time.time()
    entries: list[dict[str, Any]] = []
    for name, bv, av in changed[:max_tools]:
        remaining = total_timeout - (time.time() - start)
        if remaining <= 0:
            break
        try:
            releases = fetch(repos[name], timeout=max(1.0, min(8.0, remaining)))
        except Exception:
            continue
        matched = [
            {"tag": rel.get("tag_name", "?"), "body": rel.get("body", "")}
            for rel in (releases or [])
            if isinstance(rel, dict) and tag_in_range(rel.get("tag_name", ""), bv, av)
        ]
        if matched:
            entries.append({"name": name, "version_before": bv, "version_after": av, "releases": matched})

    return format_changelog_section(entries, capped=capped, cap=max_tools)


def hold_lock(path: str, blocking: bool = True) -> int:
    """Acquire an exclusive lock on `path` via fcntl.flock and hold it until killed.

    Prints 'LOCKED' to stdout once acquired, or 'BUSY' (exit 2) when non-blocking
    and already held. The lock is released automatically when the process dies
    (the OS closes the fd). Works on macOS and Linux without the `flock` binary.
    """
    lock_dir = os.path.dirname(path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        f = open(path, "a", encoding="utf-8")
    except OSError as e:
        print(f"LOCK_ERROR {e}", file=sys.stderr)
        return 3
    try:
        fcntl.flock(f.fileno(), flags)
    except OSError:
        f.close()
        if not blocking:
            sys.stdout.write("BUSY\n")
            sys.stdout.flush()
            return 2
        print("LOCK_ERROR could not acquire lock", file=sys.stderr)
        return 3
    sys.stdout.write("LOCKED\n")
    sys.stdout.flush()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def _load_precheck_uptodate_env() -> Optional[dict[str, float]]:
    """Read the {origin: duration_s} map written by the shell's pre-check stage.

    Path is passed via UAC_PRECHECK_UPTODATE_FILE (a small JSON file) rather
    than raw JSON in an env var, to sidestep shell quoting entirely.
    """
    path = os.environ.get("UAC_PRECHECK_UPTODATE_FILE")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: lib_update_all_clis.py emit|emit-json|list-json|snapshot-versions|"
            "notify-diff|run-summary|new-tools|suggest|suggest-known|suggest-known-count|"
            "log-unknowns|report-unknown|ack-unknown|"
            "parse-npm-globals|convert-tools-array|update-cache-versions|validate-cache|debug-cache|"
            "health-check|backup|restore|list-backups|hold-lock|try-hold-lock|benchmark|"
            "hold-add|hold-remove|doctor|changelog|"
            "history|history-append …",
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
        results = benchmark_operation(cache_path, cfg, base, local or None)
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
                mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(b)))
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
    elif cmd in ("hold-lock", "try-hold-lock"):
        path = sys.argv[2] if len(sys.argv) > 2 else ""
        if not path:
            print(f"usage: lib_update_all_clis.py {cmd} <path>", file=sys.stderr)
            sys.exit(2)
        sys.exit(hold_lock(path, blocking=(cmd == "hold-lock")))
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
    elif cmd == "incremental-scan":
        if len(sys.argv) < 6:
            print(
                "usage: lib_update_all_clis.py incremental-scan <cache_path> <scanned_at> "
                "<force:0|1> <rows_file> [extra_tools_file]",
                file=sys.stderr,
            )
            sys.exit(2)
        cache_path = sys.argv[2]
        scanned_at = sys.argv[3]
        force = sys.argv[4] == "1"
        rows_file = sys.argv[5]
        extra_file = sys.argv[6] if len(sys.argv) > 6 else ""
        with open(rows_file, encoding="utf-8") as f:
            rows = parse_scan_rows(f.read())
        extra_tools: list[tuple[str, str]] = []
        if extra_file and os.path.isfile(extra_file):
            with open(extra_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "|" not in line:
                        continue
                    n, o = line.split("|", 1)
                    if n:
                        extra_tools.append((n, o))
        result = incremental_scan_merge(rows, cache_path, scanned_at, force, extra_tools)
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
            os.environ.get("UPDATE_ALL_CLIS_HISTORY_FILE") or default_history_path(),
            int(os.environ.get("UAC_QUARANTINE_AFTER") or DEFAULT_QUARANTINE_AFTER),
            os.environ.get("UAC_INCLUDE_QUARANTINED", "0") == "1",
            _load_precheck_uptodate_env(),
            normalize_hold_entries(cfg.get("hold")),
            _parse_csv(os.environ.get("HOLD")),
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
            os.environ.get("UPDATE_ALL_CLIS_HISTORY_FILE") or default_history_path(),
            int(os.environ.get("UAC_QUARANTINE_AFTER") or DEFAULT_QUARANTINE_AFTER),
            os.environ.get("UAC_INCLUDE_QUARANTINED", "0") == "1",
            _load_precheck_uptodate_env(),
            normalize_hold_entries(cfg.get("hold")),
            _parse_csv(os.environ.get("HOLD")),
        )
    elif cmd == "precheck":
        base = os.environ.get("CONFIG_FILE", "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        validate(cfg)
        result = run_prechecks(cfg, os.environ.get("ONLY_ORIGINS"), os.environ.get("SKIP_ORIGINS"))
        print(json.dumps(result))
    elif cmd == "precheck-candidates":
        base = os.environ.get("CONFIG_FILE", "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        validate(cfg)
        origins = precheck_candidate_origins(cfg, os.environ.get("ONLY_ORIGINS"), os.environ.get("SKIP_ORIGINS"))
        print(", ".join(origins))
    elif cmd == "list-json":
        cache_path = sys.argv[2]
        list_json(cache_path)
    elif cmd == "snapshot-versions":
        emit_path = sys.argv[2]
        cache_path = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
        prior_snapshot_path = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
        snap = snapshot_versions(_read_lines(emit_path), cache_path, prior_snapshot_path)
        print(json.dumps(snap))
    elif cmd == "notify-diff":
        before = _load_json(sys.argv[2])
        after = _load_json(sys.argv[3])
        new_tools = _load_new_tools_arg(sys.argv[6] if len(sys.argv) > 6 else "")
        quarantined = _load_new_tools_arg(sys.argv[7] if len(sys.argv) > 7 else "")
        held = _load_new_tools_arg(sys.argv[8] if len(sys.argv) > 8 else "")
        notify_diff(before, after, int(sys.argv[4]), int(sys.argv[5]), new_tools, quarantined, held)
    elif cmd == "run-summary":
        before = _load_json(sys.argv[2])
        after = _load_json(sys.argv[3])
        new_tools = _load_new_tools_arg(sys.argv[6] if len(sys.argv) > 6 else "")
        quarantined = _load_new_tools_arg(sys.argv[7] if len(sys.argv) > 7 else "")
        held = _load_new_tools_arg(sys.argv[8] if len(sys.argv) > 8 else "")
        sys.stdout.write(format_run_summary(before, after, int(sys.argv[4]), int(sys.argv[5]), new_tools, quarantined, held))
    elif cmd == "history":
        history_path = sys.argv[2] if len(sys.argv) > 2 else default_history_path()
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 3
        sys.stdout.write(format_history(history_path, n))
    elif cmd == "history-append":
        if len(sys.argv) < 5:
            print(
                "usage: lib_update_all_clis.py history-append <history_path> <run_id> "
                "<results_path> [before_json] [after_json]",
                file=sys.stderr,
            )
            sys.exit(2)
        history_path = sys.argv[2]
        run_id = sys.argv[3]
        results_path = sys.argv[4]
        before = _load_json(sys.argv[5]) if len(sys.argv) > 5 else {}
        after = _load_json(sys.argv[6]) if len(sys.argv) > 6 else {}
        result_lines = _read_lines(results_path)
        appended = history_append(history_path, run_id, result_lines, before, after)
        logger.debug(f"Appended {appended} history record(s) to {history_path}")
        sys.exit(0)
    elif cmd == "new-tools":
        prev_names_path = sys.argv[2] if len(sys.argv) > 2 else ""
        cache_path = sys.argv[3] if len(sys.argv) > 3 else ""
        print(json.dumps(diff_new_tools(prev_names_path, cache_path)))
    elif cmd == "suggest":
        cache_path = sys.argv[2]
        base = os.environ.get("CONFIG_FILE", sys.argv[3] if len(sys.argv) > 3 else "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        validate(cfg)
        suggest_config(cache_path, cfg)
    elif cmd == "suggest-known":
        cache_path = sys.argv[2]
        base = os.environ.get("CONFIG_FILE", sys.argv[3] if len(sys.argv) > 3 else "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        validate(cfg)
        suggest_known(cache_path, cfg)
    elif cmd == "suggest-known-count":
        cache_path = sys.argv[2]
        base = os.environ.get("CONFIG_FILE", sys.argv[3] if len(sys.argv) > 3 else "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        result = suggest_known_count(cache_path, cfg)
        print(json.dumps(result))
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
    elif cmd == "hold-add":
        if len(sys.argv) < 4:
            print("usage: lib_update_all_clis.py hold-add CONFIG_LOCAL_FILE name1,name2,...", file=sys.stderr)
            sys.exit(2)
        names = _parse_csv(sys.argv[3])
        if not names:
            print("No names given.", file=sys.stderr)
            sys.exit(2)
        hold = edit_local_hold(sys.argv[2], add=names)
        print(f"Held: {', '.join(sorted(names))}")
        print(f"hold list now ({len(hold)}): {', '.join(hold) if hold else '(empty)'}")
    elif cmd == "hold-remove":
        if len(sys.argv) < 4:
            print("usage: lib_update_all_clis.py hold-remove CONFIG_LOCAL_FILE name1,name2,...", file=sys.stderr)
            sys.exit(2)
        names = _parse_csv(sys.argv[3])
        if not names:
            print("No names given.", file=sys.stderr)
            sys.exit(2)
        hold = edit_local_hold(sys.argv[2], remove=names)
        print(f"Unheld: {', '.join(sorted(names))}")
        print(f"hold list now ({len(hold)}): {', '.join(hold) if hold else '(empty)'}")
    elif cmd == "doctor":
        cache_path = sys.argv[2] if len(sys.argv) > 2 else ""
        want_json = "--json" in sys.argv[3:]
        base = os.environ.get("CONFIG_FILE", "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        validate(cfg)
        history_path = os.environ.get("UPDATE_ALL_CLIS_HISTORY_FILE") or default_history_path()
        report = doctor_report(cache_path, cfg, history_path)
        if want_json:
            print(json.dumps(report, indent=2))
        else:
            print(format_doctor_report(report), end="")
        sys.exit(1 if doctor_has_findings(report) else 0)
    elif cmd == "changelog":
        if len(sys.argv) < 4:
            print("usage: lib_update_all_clis.py changelog BEFORE_JSON AFTER_JSON", file=sys.stderr)
            sys.exit(2)
        before = _load_json(sys.argv[2])
        after = _load_json(sys.argv[3])
        base = os.environ.get("CONFIG_FILE", "")
        local = os.environ.get("CONFIG_LOCAL_FILE", "")
        if not base:
            base = os.path.join(os.path.dirname(__file__), "tool_config.json")
        cfg = load_merge(base, local or None)
        sys.stdout.write(build_changelog_digest(before, after, cfg))
    else:
        print("unknown command", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
