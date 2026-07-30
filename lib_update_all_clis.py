#!/usr/bin/env python3
"""Merge tool config, validate, emit update lines for update_all_clis.sh."""
from __future__ import annotations

import argparse
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
        for key in ("known", "bulk", "check", "repos", "fix"):
            if key in loc and isinstance(loc[key], dict):
                base.setdefault(key, {})
                base[key].update(loc[key])
                logger.debug(f"Merged {len(loc[key])} entries from local config {key}")
        # "hold" and "doctor_ignore" are flat lists, not dicts: local entries
        # ADD to (rather than replace) the base list, deduplicated, preserving
        # base order first.
        for key in ("hold", "doctor_ignore"):
            if key in loc and isinstance(loc[key], list):
                base_list = base.get(key, [])
                if not isinstance(base_list, list):
                    base_list = []
                merged = list(base_list)
                for entry in loc[key]:
                    if entry not in merged:
                        merged.append(entry)
                base[key] = merged
                logger.debug(f"Merged {key} list, now {len(merged)} entries")
        # "scan_dirs" is a list of {dir, origin, mode} objects: local entries
        # come FIRST so a local row for the same dir wins over the base one
        # (consistent with local-wins key conflicts above); dedupe by dir.
        if isinstance(loc.get("scan_dirs"), list):
            base_dirs = base.get("scan_dirs")
            if not isinstance(base_dirs, list):
                base_dirs = []
            merged_dirs: list[Any] = []
            seen_dirs: set[str] = set()
            for entry in list(loc["scan_dirs"]) + list(base_dirs):
                if not isinstance(entry, dict):
                    continue
                d = entry.get("dir")
                if not d or d in seen_dirs:
                    continue
                seen_dirs.add(d)
                merged_dirs.append(entry)
            base["scan_dirs"] = merged_dirs
            logger.debug(f"Merged scan_dirs, now {len(merged_dirs)} entries")
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

    # Validate optional "fix" mapping (tool/origin name -> repair command run
    # once after an update has failed all its retries; overrides the
    # auto-derived reinstall command).
    if "fix" in cfg:
        if not isinstance(cfg["fix"], dict):
            raise ValueError("'fix' must be an object mapping name to a fix command")
        for k, v in cfg["fix"].items():
            if not isinstance(k, str) or not k:
                raise ValueError(f"fix key must be a non-empty string, got {k!r}")
            if not isinstance(v, str):
                raise ValueError(f"fix.{k!r} must be a string command")

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

    # Validate optional "doctor_ignore" list (tool names whose shadowed-
    # duplicate findings are acknowledged as intentional, e.g. wrapper shims).
    if "doctor_ignore" in cfg:
        if not isinstance(cfg["doctor_ignore"], list):
            raise ValueError("'doctor_ignore' must be an array of strings")
        for entry in cfg["doctor_ignore"]:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(f"doctor_ignore entries must be non-empty strings, got {entry!r}")

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

    # Validate optional "scan_dirs" list (static discovery-scan directories;
    # the shell adds its own dynamic manager-derived rows on top).
    if "scan_dirs" in cfg:
        if not isinstance(cfg["scan_dirs"], list):
            raise ValueError("'scan_dirs' must be an array of {dir, origin, mode} objects")
        for entry in cfg["scan_dirs"]:
            if not isinstance(entry, dict):
                raise ValueError(f"scan_dirs entries must be objects, got {entry!r}")
            if not isinstance(entry.get("dir"), str) or not entry["dir"].strip():
                raise ValueError(f"scan_dirs entry needs a non-empty 'dir', got {entry!r}")
            if not isinstance(entry.get("origin"), str) or not entry["origin"].strip():
                raise ValueError(f"scan_dirs entry needs a non-empty 'origin', got {entry!r}")
            if "mode" in entry and entry["mode"] not in ("dir", "tree"):
                raise ValueError(
                    f"scan_dirs mode must be 'dir' or 'tree', got {entry['mode']!r}")


def scan_dirs_config_rows(cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(dir, origin, mode) rows from the merged config's "scan_dirs" section.

    mode defaults to "dir". Dirs are emitted verbatim — the shell expands a
    leading $HOME itself (no eval). Duplicate dirs keep the first row
    (load_merge already ordered local-over-base)."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for entry in cfg.get("scan_dirs", []) or []:
        if not isinstance(entry, dict):
            continue
        d = str(entry.get("dir") or "").strip()
        origin = str(entry.get("origin") or "").strip()
        mode = str(entry.get("mode") or "dir").strip() or "dir"
        if not d or not origin or d in seen:
            continue
        seen.add(d)
        rows.append((d, origin, mode))
    return rows


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


def normalize_hold_entries_major(entries: Optional[list[str]]) -> set[str]:
    """Config `hold` list -> set of names carrying the ":major" suffix.

    v2 semantics: a "name:major" hold blocks only MAJOR upgrades — the
    resolve-major-holds stage compares the tool's installed version against
    the manager's latest and emits a held line only when the leading
    integer would jump. Anything it can't verify stays held (fail-safe).
    """
    if not entries:
        return set()
    out: set[str] = set()
    for e in entries:
        if isinstance(e, str) and e.strip().endswith(":major"):
            base = _hold_base_name(e.strip())
            if base:
                out.add(base)
    return out


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


# Auto-derived "fix" commands (Feature: fix-after-retries). When a known
# tool's update command fails all its retries, the executors run a one-shot
# repair — normally a force-reinstall at latest — derived from the update
# command itself. An explicit entry in config's "fix" object always wins.
# Only patterns whose repair is safe and idempotent are listed; anything
# unrecognized (e.g. a tool's own self-updater like `claude update`) gets no
# auto-fix, because there is no generic way to reinstall it.
#
# Matching is token-based (shlex), not regex: a flag that isn't in the
# manager's known-valueless allowlist might consume the next token as its
# value (`npm update -g --registry https://x pkg`), so rather than risk
# reinstalling a flag value as a "package", derivation bails and leaves
# repair to an explicit config "fix" entry.
_FIX_TEMPLATES = {
    "npm": "npm install -g {pkg}@latest --force",
    "brew": "brew reinstall {pkg}",
    "uv": "uv tool install {pkg} --force --reinstall",
    "pipx": "pipx reinstall {pkg}",
    "cargo": "cargo install {pkg} --locked --force",
    "gem": "gem install {pkg} --user-install",
}

# manager -> (accepted verb-token prefixes, flags known to take NO value)
_FIX_MATCHERS: list[tuple[str, tuple[tuple[str, ...], ...], frozenset[str]]] = [
    ("npm", (("npm", "update"), ("npm", "install"), ("npm", "i")),
     frozenset({"-g", "--global", "--no-fund", "--no-audit", "--silent",
                "--quiet", "-q", "--force", "-f", "--no-save"})),
    ("brew", (("brew", "upgrade"),),
     frozenset({"--cask", "--formula", "--quiet", "-q", "--greedy",
                "--force", "-f"})),
    ("uv", (("uv", "tool", "upgrade"),),
     frozenset({"--quiet", "-q", "--no-progress"})),
    ("pipx", (("pipx", "upgrade"),),
     frozenset({"--quiet", "-q", "--verbose"})),
    ("cargo", (("cargo", "install"),),
     frozenset({"--locked", "--force", "-f", "--quiet", "-q"})),
    ("gem", (("gem", "update"),),
     frozenset({"--user-install", "--quiet", "-q"})),
]

_FIX_PKG_RE = re.compile(r"@?[A-Za-z0-9][A-Za-z0-9._/@-]*\Z")


def _fix_tokens(cmd: str) -> Optional[list[str]]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def _same_command(a: str, b: str) -> bool:
    """Whitespace-insensitive command equality (shlex token lists)."""
    ta, tb = _fix_tokens(a), _fix_tokens(b)
    if ta is None or tb is None:
        return a.strip() == b.strip()
    return ta == tb

# Update commands that already ARE a from-scratch reinstall — rerunning them
# is the fix, so no separate fix command is derived.
_FIX_SELF_HEALING_RE = re.compile(r"^go\s+install\s")


def _cmd_head(cmd: str) -> str:
    """The first simple command in a shell string (before any operator).

    Config commands routinely carry suffixes like `2>/dev/null || true`;
    fix derivation only cares about the leading `npm update -g pkg` part.
    """
    head = re.split(r"\s*(?:\|\||&&|;|\|)\s*", cmd, maxsplit=1)[0]
    head = re.split(r"\s+(?:[012]?>>?|<)", head, maxsplit=1)[0]
    return head.strip()


def derive_fix_command(
    kind: str, name: str, cmd: str, fix_cfg: Optional[dict[str, str]] = None,
) -> str:
    """Repair command to run after `cmd` has failed all retries ("" = none).

    Explicit config ("fix" object, keyed by tool/origin name) always wins,
    for both known tools and bulk origins. Auto-derivation applies to known
    tools only: bulk commands (whole-manager sweeps like `npm update -g`)
    have no single package to reinstall.
    """
    if fix_cfg and name in fix_cfg:
        fix = fix_cfg[name].strip()
        # A configured fix identical to the failing command is just a
        # disguised extra retry, not a repair.
        return "" if _same_command(fix, cmd) else fix
    if kind != "known":
        return ""
    head = _cmd_head(cmd)
    if _FIX_SELF_HEALING_RE.match(head):
        return ""
    toks = _fix_tokens(head)
    if not toks:
        return ""
    for mgr, prefixes, valueless in _FIX_MATCHERS:
        prefix = next(
            (p for p in prefixes if tuple(toks[: len(p)]) == p), None)
        if prefix is None:
            continue
        rest = toks[len(prefix):]
        pkgs = []
        for tok in rest:
            if tok.startswith("-"):
                # `--flag=value` is self-contained; a bare unknown flag
                # might take the next token as its value — bail rather
                # than guess.
                if "=" in tok or tok in valueless:
                    continue
                return ""
            pkgs.append(tok)
        # Exactly one unambiguous package token, or no auto-fix.
        if len(pkgs) != 1 or not _FIX_PKG_RE.fullmatch(pkgs[0]):
            return ""
        pkg = pkgs[0]
        if mgr == "npm":
            if not any(t in ("-g", "--global") for t in rest):
                return ""
            # An npm spec that already pins a tag/version (`pkg@latest`)
            # would otherwise double up as `pkg@latest@latest`.
            if "@" in pkg[1:]:
                pkg = pkg[: pkg.rindex("@")]
        fix = _FIX_TEMPLATES[mgr].format(pkg=pkg)
        # Never emit a fix identical to the failing command.
        return "" if _same_command(fix, head) else fix
    return ""


def _infer_origin_from_symlink(name: str, origin: str) -> str | None:
    """If the binary is a symlink into a known package-manager tree, return that origin.

    Also handles uv-ish origins: when the npm global prefix is ~/.local, npm
    globals land in ~/.local/bin — the same directory scanned as "uv/pip" —
    so a freshly installed npm CLI (e.g. `pi`, `qwen`) would otherwise be
    misattributed to uv and ride the wrong bulk update. A real uv tool's
    symlink resolves into ~/.local/share/uv/tools (never node_modules), so
    rerouting only on a node_modules target is safe.
    """
    if origin in _UV_ORIGINS:
        path = shutil.which(name)
        if not path or not os.path.islink(path):
            return None
        target = os.path.realpath(path)
        if "node_modules" in target:
            return _origin_from_target_path(target) or "npm"
        return None
    if origin not in ("manual", "path", "?"):
        return None
    path = shutil.which(name)
    if not path:
        return None
    if not os.path.islink(path):
        return None
    target = os.path.realpath(path)
    return _origin_from_target_path(target)


def _origin_from_target_path(target: str) -> str | None:
    """Map a resolved binary path to the package manager that owns it.

    Checked in specificity order: more specific trees (uv tools, pipx venvs,
    Homebrew Cellar) before the generic substring patterns, so a freshly
    discovered binary in a scanned PATH directory rides the right bulk
    update instead of being reported as unknown.
    """
    if "/uv/tools/" in target or ".local/share/uv" in target:
        return "uv"
    if "pipx/venvs" in target or ".pipx" in target:
        return "pipx"
    if "/Cellar/" in target or "/homebrew/" in target.lower() or "/linuxbrew/" in target:
        return "brew"
    # pnpm and yarn global trees contain "node_modules" too — check them
    # before the generic node_modules → npm fallback.
    if "/pnpm/" in target or "Library/pnpm" in target or ".local/share/pnpm" in target:
        return "pnpm"
    if ".yarn/" in target or ".config/yarn/global" in target:
        return "yarn"
    if "node_modules" in target:
        return "npm"
    if ".bun/" in target:
        return "bun"
    if ".deno/" in target:
        return "deno"
    if ".volta/" in target:
        return "volta"
    if "/mise/installs/" in target or "/mise/shims/" in target:
        return "mise"
    if ".cargo/" in target:
        return "cargo"
    if ".dotnet" in target:
        return "dotnet"
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


def run_check_command(cmd: str, timeout: int = 60) -> tuple[bool, float, str]:
    """Run one `check` command; return (is_up_to_date, duration_s, stdout).

    Fails open: a missing binary, non-zero exit, or timeout is treated as
    "not up to date" (i.e. the real bulk update still runs). The raw stdout
    rides along so known-tool prechecks can reuse the manager's outdated
    list (npm/brew) instead of issuing per-tool lookups.
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
        return False, time.time() - start, ""
    duration = time.time() - start
    if r.returncode != 0:
        # npm outdated exits non-zero exactly when it has outdated packages
        # to report — the stdout is still the real, usable list.
        return False, duration, r.stdout or ""
    return _stdout_signals_uptodate(r.stdout), duration, r.stdout or ""


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


def run_prechecks_full(
    cfg: dict[str, Any],
    only_origins: Optional[str] = None,
    skip_origins: Optional[str] = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """Run all configured `check` commands concurrently.

    Returns ({origin: duration_s} for origins confirmed up to date,
    {origin: raw stdout} for every check that ran). Origins with no check,
    or whose check errors, are simply absent from the first map (fail open);
    their stdout is absent from the second.
    """
    candidates = _precheck_candidates(cfg, only_origins, skip_origins)
    if not candidates:
        return {}, {}
    uptodate: dict[str, float] = {}
    stdouts: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        future_to_origin = {
            executor.submit(run_check_command, cmd): origin for origin, cmd in candidates
        }
        for future in as_completed(future_to_origin):
            origin = future_to_origin[future]
            try:
                is_uptodate, duration, stdout = future.result()
            except Exception:
                continue
            stdouts[origin] = stdout
            if is_uptodate:
                uptodate[origin] = round(duration, 3)
    return uptodate, stdouts


def run_prechecks(
    cfg: dict[str, Any],
    only_origins: Optional[str] = None,
    skip_origins: Optional[str] = None,
) -> dict[str, float]:
    """{origin: duration_s} for origins confirmed up to date (see run_prechecks_full)."""
    uptodate, _ = run_prechecks_full(cfg, only_origins, skip_origins)
    return uptodate


# ---------------------------------------------------------------------------
# Known-tool outdated prechecks (v2): skip an individually-tracked tool's
# update command when it is already at the latest version — the known-tool
# counterpart of the bulk `check` prechecks the README's v1 deferred.
#
# How each supported manager decides "already current":
#   npm  — membership in `npm outdated -g --parseable` output, captured by
#          the bulk npm check this same run (no per-tool network calls).
#   brew — membership in `brew outdated --quiet` output, likewise.
#   uv   — installed version from one `uv tool list` call vs latest from the
#          PyPI JSON API (concurrent, 6h TTL cache, failures never cached).
#   cargo — installed version from one `cargo install --list` call vs latest
#          from the crates.io API (same cache/semantics as PyPI).
#
# A manager's captured list is only trusted when its bulk check actually
# produced usable output this run: either the origin was confirmed up to
# date (exit 0, empty list) or the list is non-empty (npm's exit-1-with-
# output case). A failed check (e.g. `brew update` offline) leaves an empty,
# untrusted list — fail open, every tool updates exactly as before. Any
# unrecognized command shape (self-updaters, multi-package, unknown flags)
# is left alone for the same reason.
# ---------------------------------------------------------------------------
_KNOWN_PRECHECK_MANAGERS = frozenset({"npm", "brew", "uv", "cargo"})


def _known_pkg_from_cmd(cmd: str) -> Optional[tuple[str, str]]:
    """(manager, package) for a known tool's update command, or None.

    Same tokenization discipline as fix derivation: recognized manager verb
    prefix, only known valueless flags, exactly one package token.
    """
    head = _cmd_head(cmd)
    toks = _fix_tokens(head)
    if not toks:
        return None
    for mgr, prefixes, valueless in _FIX_MATCHERS:
        if mgr not in _KNOWN_PRECHECK_MANAGERS:
            continue
        prefix = next(
            (p for p in prefixes if tuple(toks[: len(p)]) == p), None)
        if prefix is None:
            continue
        rest = toks[len(prefix):]
        pkgs = []
        for tok in rest:
            if tok.startswith("-"):
                if "=" in tok or tok in valueless:
                    continue
                return None
            pkgs.append(tok)
        if len(pkgs) != 1 or not _FIX_PKG_RE.fullmatch(pkgs[0]):
            return None
        pkg = pkgs[0]
        if mgr == "npm":
            if not any(t in ("-g", "--global") for t in rest):
                return None
            # Strip a pinned tag/version (`pkg@latest`) for list matching.
            if "@" in pkg[1:]:
                pkg = pkg[: pkg.rindex("@")]
        return mgr, pkg
    return None


def _npm_outdated_map(stdout: str) -> dict[str, str]:
    """{name: wanted_version} from `npm outdated -g --parseable` output.

    Lines look like `<dir>:<name>@<wanted>:<name>@<current>:...`; the name
    column is `@scope/pkg@1.2.3` for scoped packages. Unparseable lines just
    never match (fail open downstream).
    """
    out: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        fields = line.split(":")
        if len(fields) < 2 or not fields[1]:
            continue
        cand = fields[1]
        if "@" in cand[1:]:
            name, _, wanted = cand.rpartition("@")
            if name and wanted:
                out[name] = wanted
        elif cand:
            out[cand] = ""
    return out


def _npm_outdated_names(stdout: str) -> set[str]:
    """Package names from `npm outdated -g --parseable` output."""
    return set(_npm_outdated_map(stdout))


def _brew_outdated_names(stdout: str) -> set[str]:
    """Formula/cask names from `brew outdated --quiet` (one per line)."""
    return {ln.strip() for ln in (stdout or "").splitlines() if ln.strip()}


def _uv_installed_versions() -> dict[str, str]:
    """{package: installed_version} from one `uv tool list` call ({} on error)."""
    try:
        r = subprocess.run(
            ["uv", "tool", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^(\S+)\s+v(\S+)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


_NORM_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+){0,3})([-+.][0-9A-Za-z.-]+)?")


def _norm_version(v: Optional[str]) -> Optional[tuple[tuple[int, ...], str]]:
    """Normalized (numeric tuple, suffix) for equality comparison.

    `1.2` equals `1.2.0`; a leading `v` is ignored; pre-release/build
    suffixes compare as lowercase strings. None (unparseable) never equals.
    """
    if not v:
        return None
    m = _NORM_VERSION_RE.search(str(v))
    if not m:
        return None
    nums = tuple(int(p) for p in m.group(1).split("."))
    nums = nums + (0,) * (3 - len(nums))
    return nums, (m.group(2) or "").lower()


def _pypi_latest_version(pkg: str, timeout: int = 10) -> Optional[str]:
    """Latest release of `pkg` per the PyPI JSON API (None on any error —
    network, 404 for git-installed tools, rate limit: all fail open)."""
    url = f"https://pypi.org/pypi/{pkg}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "update-all-clis"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return None
    info = data.get("info") if isinstance(data, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    return str(version) if version else None


def _crates_latest_version(pkg: str, timeout: int = 10) -> Optional[str]:
    """Latest stable release of `pkg` per the crates.io API (None on any
    error — fail open, same as PyPI)."""
    url = f"https://crates.io/api/v1/crates/{pkg}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "update-all-clis (github.com/sethsaler/updater)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return None
    crate = data.get("crate") if isinstance(data, dict) else None
    if not isinstance(crate, dict):
        return None
    version = crate.get("max_stable_version") or crate.get("max_version")
    return str(version) if version else None


def _known_latest_cache_path(cache_path: Optional[str]) -> str:
    override = os.environ.get("UAC_KNOWN_LATEST_CACHE")
    if override:
        return override
    if cache_path:
        base = os.path.dirname(os.path.abspath(cache_path))
    else:
        base = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
            "update-all-clis",
        )
    return os.path.join(base, "known_latest_cache.json")


def _latest_versions_cached(
    pkgs: set[str],
    fetch_one: Any,
    cache_path: Optional[str],
    key_prefix: str,
) -> dict[str, str]:
    """{pkg: latest_version} from `fetch_one(pkg)`, TTL-cached on disk
    (default 6h, UAC_KNOWN_LATEST_TTL seconds). Cache keys are namespaced
    with `key_prefix` so PyPI and crates.io entries share one file without
    collisions. Lookup failures are cached as nulls so an unlisted package
    (e.g. a git-installed tool) doesn't cost a request on every run; nulls
    simply never satisfy the equality check (the update still runs — fail
    open)."""
    try:
        ttl = float(os.environ.get("UAC_KNOWN_LATEST_TTL", "21600") or "21600")
    except ValueError:
        ttl = 21600.0
    now = time.time()
    path = _known_latest_cache_path(cache_path)
    cached: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            cached = raw
    except (OSError, json.JSONDecodeError):
        cached = {}

    out: dict[str, str] = {}
    missing: list[str] = []
    for p in sorted(pkgs):
        ent = cached.get(f"{key_prefix}{p}")
        if (
            isinstance(ent, dict)
            and "version" in ent
            and now - float(ent.get("fetched_at", 0) or 0) < ttl
        ):
            if ent["version"]:
                out[p] = str(ent["version"])
        else:
            missing.append(p)

    if missing:
        fetched: dict[str, Optional[str]] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(missing))) as executor:
            future_to_pkg = {
                executor.submit(fetch_one, p): p for p in missing
            }
            for future in as_completed(future_to_pkg):
                p = future_to_pkg[future]
                try:
                    fetched[p] = future.result()
                except Exception:
                    fetched[p] = None
        for p, v in fetched.items():
            cached[f"{key_prefix}{p}"] = {"version": v, "fetched_at": now}
            if v:
                out[p] = v
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cached, f)
            os.replace(tmp, path)
        except OSError:
            pass
    return out


def _pypi_latest_versions(pkgs: set[str], cache_path: Optional[str]) -> dict[str, str]:
    return _latest_versions_cached(pkgs, _pypi_latest_version, cache_path, "pypi:")


def _crates_latest_versions(pkgs: set[str], cache_path: Optional[str]) -> dict[str, str]:
    return _latest_versions_cached(pkgs, _crates_latest_version, cache_path, "crates:")


def _cargo_installed_versions() -> dict[str, str]:
    """{package: installed_version} from one `cargo install --list` call
    ({} on error or unparseable output — fail open)."""
    try:
        r = subprocess.run(
            ["cargo", "install", "--list"],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        # Entries look like "eza v0.23.0:" followed by indented binary names.
        m = re.match(r"^(\S+)\s+v(\S+):\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _brew_info_version(pkg: str, timeout: int = 30) -> Optional[str]:
    """Latest available version of a brew formula/cask via `brew info`
    (None on any error — fail safe for :major holds)."""
    try:
        r = subprocess.run(
            ["brew", "info", "--json=v2", pkg],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    formulae = data.get("formulae") or []
    if formulae and isinstance(formulae[0], dict):
        stable = (formulae[0].get("versions") or {}).get("stable")
        if stable:
            return str(stable)
    casks = data.get("casks") or []
    if casks and isinstance(casks[0], dict) and casks[0].get("version"):
        return str(casks[0]["version"])
    return None


def _npm_view_version(pkg: str, timeout: int = 20) -> Optional[str]:
    """Latest registry version of an npm package via `npm view` (None on any
    error — fail safe). Only used for :major-hold decisions when the bulk
    npm check's outdated list isn't available this run."""
    try:
        r = subprocess.run(
            ["npm", "view", pkg, "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    line = r.stdout.strip().split("\n")[0].strip()
    return line or None


def _npm_latest_versions(pkgs: set[str]) -> dict[str, str]:
    """{pkg: latest_version} via per-package `npm view` (concurrent, no
    cache — :major holds are a handful of tools at most)."""
    if not pkgs:
        return {}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(pkgs))) as executor:
        future_to_pkg = {executor.submit(_npm_view_version, p): p for p in sorted(pkgs)}
        for future in as_completed(future_to_pkg):
            p = future_to_pkg[future]
            try:
                v = future.result()
            except Exception:
                v = None
            if v:
                out[p] = v
    return out


def _brew_latest_versions(pkgs: set[str]) -> dict[str, str]:
    """{pkg: latest_version} via per-package `brew info` (concurrent).

    Only used for :major-hold decisions (a handful of tools at most), so no
    cache — the bulk brew check's metadata refresh already ran this run.
    """
    if not pkgs:
        return {}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(pkgs))) as executor:
        future_to_pkg = {executor.submit(_brew_info_version, p): p for p in sorted(pkgs)}
        for future in as_completed(future_to_pkg):
            p = future_to_pkg[future]
            try:
                v = future.result()
            except Exception:
                v = None
            if v:
                out[p] = v
    return out


def resolve_major_holds(
    cache_path: str,
    cfg: dict[str, Any],
    adhoc_hold_csv: Optional[str] = None,
    uptodate_bulk: Optional[dict[str, float]] = None,
    check_stdouts: Optional[dict[str, str]] = None,
    probe_installed: Optional[Any] = None,
) -> dict[str, Any]:
    """Decide what each "name:major" hold should do this run.

    Returns {"block": {name: target_version}, "allow": [name], "unknown":
    [name]} — block = a major upgrade is pending (stay held, say what's
    coming); allow = no major jump (minor/patch or nothing at all — the
    update runs normally); unknown = the target couldn't be verified (a
    self-updater with no registry, a failed lookup, an unparseable version)
    and the hold stays, fail-safe.

    A name plainly held (same name without the suffix, either source) is
    excluded here — the plain hold already wins in emit.
    """
    uptodate_bulk = uptodate_bulk or {}
    check_stdouts = check_stdouts or {}
    probe = probe_installed or probe_version

    hold_list = cfg.get("hold", []) or []
    adhoc_list = [s for s in (adhoc_hold_csv or "").split(",") if s.strip()]
    config_major = normalize_hold_entries_major(hold_list)
    adhoc_major = normalize_hold_entries_major(adhoc_list)

    def _plain(entries: list[str]) -> set[str]:
        return {e.strip() for e in entries
                if isinstance(e, str) and e.strip() and not e.strip().endswith(":major")}

    plainly_held = _plain(hold_list) | _plain(adhoc_list)

    known = cfg.get("known", {}) or {}
    out: dict[str, Any] = {"block": {}, "allow": [], "unknown": []}

    # Batch per-manager targets first (concurrent where it matters).
    jobs: dict[str, tuple[str, str, str]] = {}  # name -> (source, mgr, pkg)
    for name in sorted(config_major | adhoc_major):
        if name in plainly_held:
            continue
        source = "config" if name in config_major else "env"
        ext = _known_pkg_from_cmd(known.get(name, "") or "")
        if ext is None:
            out["unknown"].append(name)
            continue
        jobs[name] = (source, ext[0], ext[1])

    npm_map: Optional[dict[str, str]] = None
    npm_view_latest: dict[str, str] = {}
    npm_pkgs = {pkg for _, mgr, pkg in jobs.values() if mgr == "npm"}
    if npm_pkgs:
        if "npm" in uptodate_bulk or check_stdouts.get("npm", "").strip():
            npm_map = _npm_outdated_map(check_stdouts.get("npm", ""))
        else:
            # No usable bulk-check signal (e.g. --no-precheck, or the npm
            # check failed): fall back to per-package registry lookups.
            npm_view_latest = _npm_latest_versions(npm_pkgs)
    brew_pkgs = {pkg for _, mgr, pkg in jobs.values() if mgr == "brew"}
    brew_latest = _brew_latest_versions(brew_pkgs) if brew_pkgs else {}
    uv_pkgs = {pkg for _, mgr, pkg in jobs.values() if mgr == "uv"}
    uv_latest = _pypi_latest_versions(uv_pkgs, cache_path) if uv_pkgs else {}
    cargo_pkgs = {pkg for _, mgr, pkg in jobs.values() if mgr == "cargo"}
    cargo_latest = _crates_latest_versions(cargo_pkgs, cache_path) if cargo_pkgs else {}

    for name, (source, mgr, pkg) in sorted(jobs.items()):
        latest: Optional[str] = None
        allow_without_compare = False
        if mgr == "npm":
            if npm_map is not None:
                if pkg not in npm_map:
                    allow_without_compare = True  # not outdated at all
                else:
                    latest = npm_map[pkg] or None
            else:
                latest = npm_view_latest.get(pkg)
        elif mgr == "brew":
            latest = brew_latest.get(pkg)
        elif mgr == "uv":
            latest = uv_latest.get(pkg)
        else:  # cargo
            latest = cargo_latest.get(pkg)

        if allow_without_compare:
            out["allow"].append(name)
            continue
        installed = probe(name)
        li, ll = leading_major(installed), leading_major(latest)
        if li is None or ll is None:
            out["unknown"].append(name)
        elif ll > li:
            out["block"][name] = {"source": source, "target": str(latest)}
        else:
            out["allow"].append(name)

    return out


def _known_precheck_jobs(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str],
    skip_origins: Optional[str],
    skip_names: Optional[set[str]],
) -> list[tuple[str, str, str]]:
    """(name, manager, package) triples eligible for a known-tool up-to-date
    check: discovered known tools with a recognizable update command, after
    the same origin only/skip filtering the emit loop applies (SKIP= names
    excluded so a `--skip`ed tool can never surface as an up-to-date ok).
    """
    only = _parse_csv(only_origins)
    skip = _parse_csv(skip_origins)
    skip_names = skip_names or set()
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    tools = [t for t in data if isinstance(t, dict) and "name" in t]
    known = cfg.get("known", {}) or {}

    jobs: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for t in tools:
        name = t["name"]
        if name in seen or name in skip_names or name not in known:
            continue
        seen.add(name)
        origin = str(t.get("origin", "?"))
        if origin in skip:
            continue
        if only and origin not in only and name not in only:
            continue
        cmd = known.get(name) or ""
        if not cmd.strip():
            continue
        ext = _known_pkg_from_cmd(cmd)
        if ext:
            jobs.append((name, ext[0], ext[1]))
    return jobs


def known_precheck_candidates(
    cache_path: str,
    cfg: dict[str, Any],
    only_origins: Optional[str] = None,
    skip_origins: Optional[str] = None,
    skip_names: Optional[set[str]] = None,
) -> list[str]:
    """Known tool names that WOULD be up-to-date-checked (no lookups run)."""
    return sorted(name for name, _, _ in _known_precheck_jobs(
        cache_path, cfg, only_origins, skip_origins, skip_names))


def run_known_prechecks(
    cache_path: str,
    cfg: dict[str, Any],
    uptodate_bulk: Optional[dict[str, float]] = None,
    check_stdouts: Optional[dict[str, str]] = None,
    only_origins: Optional[str] = None,
    skip_origins: Optional[str] = None,
    skip_names: Optional[set[str]] = None,
) -> dict[str, float]:
    """{known_tool_name: duration_s} for tools confirmed already at latest.

    Everything is fail-open: any manager whose freshness signal is missing
    or unusable leaves its tools alone.
    """
    uptodate_bulk = uptodate_bulk or {}
    check_stdouts = check_stdouts or {}
    jobs = _known_precheck_jobs(cache_path, cfg, only_origins, skip_origins, skip_names)
    if not jobs:
        return {}

    results: dict[str, float] = {}

    # npm / brew: membership in the bulk check's outdated list. Trust the
    # list only when the bulk check produced a usable signal this run (the
    # origin went fully up to date, or the list has real content).
    npm_trusted = "npm" in uptodate_bulk or bool(check_stdouts.get("npm", "").strip())
    brew_trusted = "brew" in uptodate_bulk or bool(check_stdouts.get("brew", "").strip())
    npm_outdated = _npm_outdated_names(check_stdouts.get("npm", "")) if npm_trusted else None
    brew_outdated = _brew_outdated_names(check_stdouts.get("brew", "")) if brew_trusted else None

    # Registry-compared managers: (installed-map fn, latest-map fn). Each
    # batch costs one local list call + concurrent registry lookups, and any
    # failure leaves installed/latest empty (fail open — tools update).
    uv_jobs: list[tuple[str, str]] = []
    cargo_jobs: list[tuple[str, str]] = []
    for name, mgr, pkg in jobs:
        if mgr == "npm":
            if npm_outdated is not None and pkg not in npm_outdated:
                results[name] = 0.0
        elif mgr == "brew":
            if brew_outdated is not None and pkg not in brew_outdated:
                results[name] = 0.0
        elif mgr == "cargo":
            cargo_jobs.append((name, pkg))
        else:  # uv
            uv_jobs.append((name, pkg))

    for batch, installed_fn, latest_fn in (
        (uv_jobs, _uv_installed_versions, _pypi_latest_versions),
        (cargo_jobs, _cargo_installed_versions, _crates_latest_versions),
    ):
        if not batch:
            continue
        phase_start = time.time()
        installed = installed_fn()
        latest = latest_fn({pkg for _, pkg in batch}, cache_path)
        phase = time.time() - phase_start
        per_tool = round(phase / len(batch), 3)
        for name, pkg in batch:
            iv = _norm_version(installed.get(pkg))
            lv = _norm_version(latest.get(pkg))
            if iv is not None and lv is not None and iv == lv:
                results[name] = per_tool

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
    precheck_uptodate_known: Optional[dict[str, float]] = None,
    held_config_major: Optional[set[str]] = None,
    held_adhoc_major: Optional[set[str]] = None,
    major_hold_blocks: Optional[dict[str, dict[str, str]]] = None,
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

    fix_cfg = cfg.get("fix") if isinstance(cfg.get("fix"), dict) else {}

    def write_line(kind: str, name: str, cmd: str, origin: str) -> None:
        lock = lock_group_for(origin, cmd, name)
        fix = derive_fix_command(kind, name, cmd, fix_cfg)
        lines.append(f"{kind}{EMIT_SEP}{name}{EMIT_SEP}{cmd}{EMIT_SEP}{lock}{EMIT_SEP}{fix}")

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
    # executors can phrase their message accordingly. A "name:major" hold
    # only becomes a held line when the resolve stage found a major upgrade
    # pending (cmd "major:<source>:<target>") or couldn't verify one
    # ("major:<source>:unknown", fail-safe); a verified no-major-jump run
    # lets the update through untouched.
    held_config = held_config or set()
    held_adhoc = held_adhoc or set()
    held_config_major = held_config_major or set()
    held_adhoc_major = held_adhoc_major or set()
    if held_config or held_adhoc or held_config_major or held_adhoc_major:
        transformed_held: list[str] = []
        for line in lines:
            parts = line.split(EMIT_SEP, 3)
            kind = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            if kind not in ("known", "bulk"):
                transformed_held.append(line)
                continue
            if name in held_config:
                transformed_held.append(f"held{EMIT_SEP}{name}{EMIT_SEP}config{EMIT_SEP}")
            elif name in held_adhoc:
                transformed_held.append(f"held{EMIT_SEP}{name}{EMIT_SEP}env{EMIT_SEP}")
            elif name in held_config_major or name in held_adhoc_major:
                source = "config" if name in held_config_major else "env"
                if major_hold_blocks is None:
                    # No resolve stage ran (dry-run): stay held, fail-safe.
                    target = "unknown"
                elif name in major_hold_blocks:
                    target = major_hold_blocks[name].get("target") or "unknown"
                else:
                    # Verified: no major jump pending — let the update run.
                    transformed_held.append(line)
                    continue
                transformed_held.append(
                    f"held{EMIT_SEP}{name}{EMIT_SEP}major:{source}:{target}{EMIT_SEP}")
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
    # nothing to do — or a known tool already at the latest version — is
    # replaced with a synthetic "uptodate" line (the executor prints it as
    # an instant ok, no update command runs). Applied after quarantine so a
    # quarantined job still shows as quarantined, not up to date, if both
    # would otherwise apply.
    if precheck_uptodate or precheck_uptodate_known:
        bulk_up = precheck_uptodate or {}
        known_up = precheck_uptodate_known or {}
        transformed2: list[str] = []
        for line in lines:
            parts = line.split(EMIT_SEP, 3)
            kind = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            duration: Optional[float] = None
            if kind == "bulk" and name in bulk_up:
                duration = bulk_up[name]
            elif kind == "known" and name in known_up:
                duration = known_up[name]
            if duration is not None:
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
    precheck_uptodate_known: Optional[dict[str, float]] = None,
    held_config_major: Optional[set[str]] = None,
    held_adhoc_major: Optional[set[str]] = None,
    major_hold_blocks: Optional[dict[str, dict[str, str]]] = None,
) -> None:
    for line in collect_emit_lines(
        cache_path, cfg, only_origins, skip_origins,
        history_path, quarantine_after, include_quarantined, precheck_uptodate,
        held_config, held_adhoc, precheck_uptodate_known,
        held_config_major, held_adhoc_major, major_hold_blocks,
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
    precheck_uptodate_known: Optional[dict[str, float]] = None,
    held_config_major: Optional[set[str]] = None,
    held_adhoc_major: Optional[set[str]] = None,
    major_hold_blocks: Optional[dict[str, dict[str, str]]] = None,
) -> None:
    plan: list[dict[str, str]] = []
    for line in collect_emit_lines(
        cache_path, cfg, only_origins, skip_origins,
        history_path, quarantine_after, include_quarantined, precheck_uptodate,
        held_config, held_adhoc, precheck_uptodate_known,
        held_config_major, held_adhoc_major, major_hold_blocks,
    ):
        parts = line.split(EMIT_SEP)
        if len(parts) < 3:
            continue
        kind, name, cmd = parts[0], parts[1], parts[2]
        lock = parts[3] if len(parts) > 3 else name
        fix = parts[4] if len(parts) > 4 else ""
        entry: dict[str, str] = {"type": kind, "name": name, "command": cmd}
        if lock:
            entry["lock_group"] = lock
        if fix:
            entry["fix_command"] = fix
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
        "asdf": ("asdf", "--version"),
        "proto": ("proto", "--version"),
        "volta": ("volta", "--version"),
        "rye": ("rye", "--version"),
        "foundry": ("forge", "--version"),
        "aqua": ("aqua", "--version"),
        "mason": (),
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
    "brew": "brew", "npm": "npm", "pnpm": "pnpm", "yarn": "yarn",
    "cargo": "cargo", "gem": "gem", "pip": "pip3",
    "uv": "uv", "uv/pip": "uv", "uv/venv": "uv", "fnm": "fnm", "bun": "bun",
    "deno": "deno", "pyenv": "pyenv", "rbenv": "rbenv", "conda": "conda",
    "opencode": "opencode", "dotnet": "dotnet", "mise": "mise", "pipx": "pipx",
    "grok": "grok", "asdf": "asdf", "proto": "proto", "volta": "volta",
    "rye": "rye", "foundry": "forge", "aqua": "aqua",
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
    "npm", "pnpm", "yarn", "cargo", "go", "gem", "pipx", "manual", "path",
    "uv", "uv/pip", "uv/venv", "fnm", "bun", "deno",
    "mise", "opencode", "grok", "conda", "dotnet", "krew",
    "pip", "asdf", "proto", "volta", "rye", "foundry", "aqua", "mason",
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
    failed: Optional[list[str]] = None,
    mode: str = "full",
) -> str:
    """Run summary text. mode="full" lists everything; mode="failures"
    leads with the failed jobs and collapses the (long) up-to-date name
    list to a count — for scheduled runs where only problems are news."""
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

    failed = failed or []
    lines_out: list[str] = [
        "update-all-clis",
        f"Steps: {ok} ok, {fail} failed",
    ]

    if mode == "failures":
        lines_out.append("")
        lines_out.append(f"Failed ({len(failed)}):")
        lines_out.append("  " + ", ".join(sorted(failed)) if failed else "  (none)")

    lines_out.append("")
    lines_out.append(f"Upgraded ({len(upgraded)}):")
    lines_out.extend(upgraded if upgraded else ["  (none)"])

    lines_out.append("")
    new_tools = new_tools or []
    lines_out.append(f"New installs added for future runs ({len(new_tools)}):")
    if new_tools:
        lines_out.extend(f"  {name}" for name in sorted(new_tools))
    else:
        lines_out.append("  (none)")

    lines_out.append("")
    if mode == "failures":
        lines_out.append(f"Already up to date: {len(unchanged)} (list omitted in failures mode)")
    else:
        lines_out.append(f"Already up to date ({len(unchanged)}):")
        lines_out.append("  " + ", ".join(sorted(unchanged)) if unchanged else "  (none)")

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


def _summary_mode_env() -> str:
    mode = os.environ.get("UPDATE_ALL_CLIS_SUMMARY_MODE", "full")
    return mode if mode in ("full", "failures") else "full"


def notify_macos_dialog(
    before: dict[str, Any],
    after: dict[str, Any],
    ok: int,
    fail: int,
    new_tools: Optional[list[str]] = None,
    quarantined: Optional[list[str]] = None,
    held: Optional[list[str]] = None,
    failed: Optional[list[str]] = None,
) -> None:
    if sys.platform != "darwin":
        return
    body = format_run_summary(
        before, after, ok, fail, new_tools, quarantined, held, failed,
        mode=_summary_mode_env(),
    ).rstrip("\n")
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
    failed: Optional[list[str]] = None,
) -> None:
    if sys.platform == "linux" and shutil.which("notify-send"):
        body = format_run_summary(
            before, after, ok, fail, new_tools, quarantined, held, failed,
            mode=_summary_mode_env(),
        ).rstrip("\n")
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
    failed: Optional[list[str]] = None,
) -> None:
    notify_macos_dialog(before, after, ok, fail, new_tools, quarantined, held, failed)
    notify_linux(before, after, ok, fail, new_tools, quarantined, held, failed)


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


def _count_dir_entries(dir_path: str) -> int:
    """Count visible, non-excluded directory entries without stat() calls.

    Mirrors the name filters in `_scan_dir_entries` so a change in the
    number of candidate entries reliably indicates the directory contents
    changed even when the directory's mtime has not advanced (e.g. several
    installs completed within the same filesystem timestamp tick).
    """
    try:
        with os.scandir(dir_path) as it:
            return sum(
                1
                for entry in it
                if not entry.name.startswith(".")
                and entry.name not in _SCAN_EXCLUDE_NAMES
                and not entry.name.startswith("git-")
            )
    except OSError:
        return 0


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


def _coerce_dir_stats(value: Any, mode: str) -> Optional[dict[str, Any]]:
    """Convert legacy dir_mtimes values or dicts into a stats dict."""
    if isinstance(value, dict):
        stats = dict(value)
        if "mtime" not in stats:
            return None
        return stats
    try:
        return {"mtime": float(value), "size": 0, "nlink": 0}
    except (TypeError, ValueError):
        return None


def _dir_stats(dir_path: str, mode: str) -> Optional[dict[str, Any]]:
    """Filesystem stats used to decide whether a directory needs re-listing."""
    try:
        st = os.stat(dir_path)
    except OSError:
        return None
    stats: dict[str, Any] = {"mtime": st.st_mtime, "size": st.st_size, "nlink": st.st_nlink}
    if mode == "dir":
        stats["count"] = _count_dir_entries(dir_path)
    return stats


def _stats_unchanged(cur: dict[str, Any], old: dict[str, Any], mode: str) -> bool:
    """Return True if every tracked stat (including entry count for dir mode) matches."""
    for key in ("mtime", "size", "nlink"):
        if cur.get(key) != old.get(key):
            return False
    if mode == "dir":
        old_count = old.get("count")
        if old_count is None:
            return False
        if cur.get("count") != old_count:
            return False
    return True


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

    A directory is skipped (its cached tools reused) only when every tracked
    stat is unchanged: mtime, size, and link count. For flat "dir"
    directories we also track a candidate-entry count; this catches new
    installs that complete within the same filesystem timestamp tick, where
    the directory mtime may not advance. `force=True` (--rescan) ignores
    stored stats and re-walks everything, refreshing all stored stats.

    Top-level directory stats are sufficient for "dir" and "tree"/"sdkman"
    additions/removals, but an in-place upgrade that doesn't add/remove a
    top-level entry (formula symlink, install dir) won't retrigger a walk.
    That's judged acceptable since existing binary *names* don't change on
    an in-place upgrade, and `--rescan` remains available to force a full
    walk.
    """
    existing: list[Any] = []
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = []

    cached_tools = [t for t in existing if isinstance(t, dict) and "name" in t]
    stats_rec = next(
        (t for t in existing if isinstance(t, dict) and ("dir_mtimes" in t or "dir_stats" in t)),
        None,
    )
    old_stats: dict[str, Any] = {}
    if stats_rec is not None:
        old_stats = stats_rec.get("dir_stats", stats_rec.get("dir_mtimes", {}))
    existing_versions = {t["name"]: t["version"] for t in cached_tools if "version" in t}

    cached_by_dir: dict[str, list[dict[str, Any]]] = {}
    for t in cached_tools:
        d = t.get("dir")
        if d:
            cached_by_dir.setdefault(d, []).append(t)

    out_tools: list[dict[str, Any]] = []
    new_stats: dict[str, Any] = {}
    handled_dirs: set[str] = set()
    # Dedup by (name, origin) only — matching the old `sort -u` on "name|origin"
    # lines. The SAME (name, origin) can legitimately turn up from more than
    # one directory (e.g. a brew keg's opt/*/bin entry and a top-level
    # /opt/homebrew/bin symlink); only the first directory's tag is kept for
    # future stat-gating, but only one final tool record is emitted.
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
        cur_stats = _dir_stats(dir_path, mode)
        if cur_stats is None:
            continue
        old = _coerce_dir_stats(old_stats.get(dir_path), mode)
        if (not force) and old is not None and _stats_unchanged(cur_stats, old, mode) and dir_path in cached_by_dir:
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
        new_stats[dir_path] = cur_stats

    # Non-directory-gated entries the shell adds directly (currently just
    # the fnm sentinel: fnm's own binary lives under a version-manager
    # shim, not a plain bin dir worth stat-tracking).
    for name, origin in (extra_tools or []):
        emit(name, origin, None)

    # Carry forward stats for directories not mentioned at all this run
    # (e.g. a manager whose whole resolution path is conditional and wasn't
    # even attempted, such as npm's dirs when npm itself isn't installed).
    for d, stats in old_stats.items():
        if d not in handled_dirs:
            new_stats[d] = stats

    # Carry forward cached tools whose directory wasn't touched this run,
    # and any tool with no "dir" tag at all (pre-migration cache entries,
    # or entries the shell adds directly without directory gating, e.g. fnm).
    for t in cached_tools:
        d = t.get("dir")
        if d is None or d not in handled_dirs:
            emit(t["name"], t.get("origin", "?"), d)

    out_tools.append({"scanned_at": scanned_at, "count": len(out_tools)})
    out_tools.append({"dir_mtimes": new_stats})
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


def doctor_prune_suggestions(cache_path: str, cfg: dict[str, Any]) -> list[str]:
    """Known-config entries whose tool is nowhere: not on PATH AND absent
    from the latest discovery scan. Purely informational (a pruning aid —
    the config deliberately catalogs tools you might install later), so
    these never count as doctor findings."""
    known = cfg.get("known", {}) or {}
    cached_names = set(cache_tool_names(cache_path))
    out = []
    for name in sorted(known):
        if name in cached_names:
            continue
        if shutil.which(name):
            continue
        out.append(name)
    return out


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
        "prune_suggestions": [],
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
        shadows = doctor_shadowed_duplicates(cache_path)
        ignore = set(cfg.get("doctor_ignore", []) or [])
        report["shadowed_duplicates"] = [s for s in shadows if s["name"] not in ignore]
        report["ignored_shadows"] = sorted(s["name"] for s in shadows if s["name"] in ignore)
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

    try:
        report["prune_suggestions"] = doctor_prune_suggestions(cache_path, cfg)
    except Exception as e:
        report["errors"].append(f"prune-suggestion check failed: {e}")

    return report


def doctor_has_findings(report: dict[str, Any]) -> bool:
    # Informational sections don't count as findings: `not_installed`
    # (config catalogs tools you might install), `prune_suggestions`
    # (an aid, not a problem), and cache warnings (duplicate names across
    # origins are normal — see shadowed check).
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

    ps = report.get("prune_suggestions", []) or []
    if ps:
        lines.append("")
        lines.append(
            f"Not seen anywhere ({len(ps)}, informational — not on PATH and "
            "absent from the last scan; review for pruning):")
        lines.append("  " + ", ".join(ps))

    ig = report.get("ignored_shadows", []) or []
    if ig:
        lines.append("")
        lines.append(f"Ignored shadows ({len(ig)}, via doctor_ignore): " + ", ".join(ig))

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


def _load_precheck_file() -> Optional[dict[str, Any]]:
    """Read the JSON file written by the shell's pre-check stage.

    Path is passed via UAC_PRECHECK_UPTODATE_FILE (a small JSON file) rather
    than raw JSON in an env var, to sidestep shell quoting entirely. Two
    formats are accepted: the current {"bulk": {...}, "known": {...},
    "stdout": {...}} shape, and the original flat {origin: duration_s} map
    (treated as bulk-only).
    """
    path = os.environ.get("UAC_PRECHECK_UPTODATE_FILE")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_precheck_uptodate_env() -> Optional[dict[str, float]]:
    """The bulk {origin: duration_s} up-to-date map from the pre-check file."""
    data = _load_precheck_file()
    if not data:
        return None
    if "bulk" in data or "known" in data:
        bulk = data.get("bulk")
        return bulk if isinstance(bulk, dict) and bulk else None
    # Legacy flat {origin: duration} format.
    return data or None


def _load_precheck_known_env() -> Optional[dict[str, float]]:
    """The known-tool {name: duration_s} up-to-date map from the pre-check file."""
    data = _load_precheck_file()
    if not data:
        return None
    known = data.get("known")
    return known if isinstance(known, dict) and known else None


def _load_major_holds_env() -> Optional[dict[str, dict[str, str]]]:
    """The {name: {"source","target"}} major-upgrade blocks written by the
    shell's resolve-major-holds stage (UAC_MAJOR_HOLDS_FILE). None when the
    stage never ran (e.g. --dry-run) — emit treats that as fail-safe held.
    """
    path = os.environ.get("UAC_MAJOR_HOLDS_FILE")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("block")
    return block if isinstance(block, dict) else {}


# ---------------------------------------------------------------------------
# Subcommand handlers (dispatched by main()'s argparse subparsers). The CLI
# surface — subcommand names, positional args, env vars, exit codes — is
# unchanged from the original if/elif dispatch; update_all_clis.sh's
# callsites depend on it.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Small output helpers — these back lib subcommands that replaced inline
# `python3 -c` snippets in update_all_clis.sh (same logic, now testable).
# ---------------------------------------------------------------------------
def cache_tool_names(cache_path: str) -> list[str]:
    """Every discovered tool's name, one per line's worth, in cache order."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [str(t["name"]) for t in data if isinstance(t, dict) and "name" in t]


def format_tool_list(cache_path: str) -> str:
    """The human-readable --list output: sorted tools + scanned_at footer."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    tools = sorted(
        (t for t in data if isinstance(t, dict) and "name" in t),
        key=lambda x: x["name"],
    )
    meta = next((t for t in data if isinstance(t, dict) and "scanned_at" in t), None)
    lines = [f"  {t['name']}  [{t.get('origin', '?')}]" for t in tools]
    scanned = meta.get("scanned_at") if meta else "?"
    lines.append(f"\nTotal: {len(tools)} tools  |  Scanned: {scanned}")
    return "\n".join(lines)


def lines_to_json(stdin_text: str) -> str:
    """JSON array of stdin's non-empty stripped lines (snapshot plumbing)."""
    return json.dumps([ln.strip() for ln in stdin_text.splitlines() if ln.strip()])


def unknown_log_summary(unknown_log_path: str, sample_size: int = 5) -> tuple[int, str]:
    """(unacknowledged count, comma sample) from the unknown-tools log."""
    try:
        with open(unknown_log_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0, ""
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, dict):
        return 0, ""
    names = sorted(
        str(t.get("name") or key)
        for key, t in tools.items()
        if isinstance(t, dict) and not t.get("acknowledged")
    )
    return len(names), ", ".join(names[:sample_size])


# ---------------------------------------------------------------------------
# --insights: history analytics (slowest jobs, chronic failers, most-
# frequently-updated tools, and actionable suggestions).
# ---------------------------------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def build_insights(history_path: Optional[str], top_n: int = 10) -> dict[str, Any]:
    """Analyze history.jsonl. All numbers come straight from recorded job
    records (kind/name/duration_s/status/version_before/version_after)."""
    records = load_history_records(history_path)
    by_name = load_history_by_name(history_path)
    means = historical_mean_durations(by_name)

    slowest = [
        {"name": name, "mean_s": round(mean, 1),
         "runs": len([r for r in by_name[name] if isinstance(r.get("duration_s"), (int, float))])}
        for name, mean in sorted(means.items(), key=lambda kv: -kv[1])[:top_n]
    ]

    failers = []
    for name, recs in sorted(by_name.items()):
        counted = [r for r in recs if r.get("status") in ("ok", "fail")]
        fails = sum(1 for r in counted if r.get("status") == "fail")
        if len(counted) >= 3 and fails:
            failers.append({
                "name": name, "failed": fails, "total": len(counted),
                "rate": round(fails / len(counted), 3),
            })
    failers.sort(key=lambda f: (-f["rate"], -f["failed"], f["name"]))
    failers = failers[:top_n]

    frequent = []
    for name, recs in sorted(by_name.items()):
        changed = sum(
            1 for r in recs
            if r.get("version_before") and r.get("version_after")
            and r["version_before"] != r["version_after"]
        )
        if changed:
            frequent.append({"name": name, "changed_runs": changed})
    frequent.sort(key=lambda f: (-f["changed_runs"], f["name"]))
    frequent = frequent[:top_n]

    suggestions: list[str] = []
    for f in failers:
        if f["rate"] >= 0.5:
            suggestions.append(
                f"{f['name']} fails {f['failed']}/{f['total']} runs "
                f"({int(f['rate'] * 100)}%) — investigate, or --hold it to stop the noise")
    if slowest and slowest[0]["mean_s"] >= 60:
        suggestions.append(
            f"{slowest[0]['name']} is the long pole "
            f"(~{_fmt_duration(slowest[0]['mean_s'])} mean) — slowest-first "
            "scheduling already starts it first")
    if frequent and frequent[0]["changed_runs"] >= 5:
        suggestions.append(
            f"{frequent[0]['name']} changes nearly every run "
            f"({frequent[0]['changed_runs']} version bumps on record) — "
            "expect regular [MAJOR UPGRADE]-style churn")

    return {
        "records": len(records),
        "jobs_tracked": len(by_name),
        "slowest": slowest,
        "chronic_failures": failers,
        "frequently_updated": frequent,
        "suggestions": suggestions,
    }


def format_insights(report: dict[str, Any]) -> str:
    lines = [
        f"update-all-clis insights — {report.get('records', 0)} history records "
        f"({report.get('jobs_tracked', 0)} jobs tracked)",
        "",
    ]
    slowest = report.get("slowest", [])
    lines.append(f"Slowest jobs (mean of last ~10 runs each, top {len(slowest)}):")
    if slowest:
        for s in slowest:
            lines.append(f"  {s['name']:<28} {_fmt_duration(s['mean_s']):>7} mean ({s['runs']} runs)")
    else:
        lines.append("  (none — no duration data yet)")
    lines.append("")

    failers = report.get("chronic_failures", [])
    lines.append(f"Chronic failure rates ({len(failers)}):")
    if failers:
        for f in failers:
            lines.append(f"  {f['name']:<28} {f['failed']}/{f['total']} failed ({int(f['rate'] * 100)}%)")
    else:
        lines.append("  (none)")
    lines.append("")

    frequent = report.get("frequently_updated", [])
    lines.append(f"Most frequently updated ({len(frequent)}):")
    if frequent:
        for f in frequent:
            lines.append(f"  {f['name']:<28} version changed in {f['changed_runs']} runs")
    else:
        lines.append("  (none)")
    lines.append("")

    suggestions = report.get("suggestions", [])
    lines.append(f"Suggestions ({len(suggestions)}):")
    if suggestions:
        lines.extend(f"  - {s}" for s in suggestions)
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _cfg_from_env(do_validate: bool = True) -> dict[str, Any]:
    """Merged config from CONFIG_FILE/CONFIG_LOCAL_FILE env (default base:
    the tool_config.json next to this file)."""
    base = os.environ.get("CONFIG_FILE", "") or os.path.join(
        os.path.dirname(__file__), "tool_config.json")
    local = os.environ.get("CONFIG_LOCAL_FILE", "")
    cfg = load_merge(base, local or None)
    if do_validate:
        validate(cfg)
    return cfg


def _cfg_with_legacy_base(args_base: str, do_validate: bool = True) -> dict[str, Any]:
    """Like _cfg_from_env but allows the legacy positional base-config path
    (used by the suggest/log-unknowns subcommands)."""
    base = (os.environ.get("CONFIG_FILE") or args_base
            or os.path.join(os.path.dirname(__file__), "tool_config.json"))
    local = os.environ.get("CONFIG_LOCAL_FILE", "")
    cfg = load_merge(base, local or None)
    if do_validate:
        validate(cfg)
    return cfg


def _emit_kwargs(cfg: dict[str, Any]) -> dict[str, Any]:
    """Shared emit/emit-json argument bundle: origin filters, history,
    quarantine, holds (plain + :major), and both pre-check maps."""
    hold_env_list = [s for s in os.environ.get("HOLD", "").split(",")]
    return {
        "only_origins": os.environ.get("ONLY_ORIGINS"),
        "skip_origins": os.environ.get("SKIP_ORIGINS"),
        "history_path": os.environ.get("UPDATE_ALL_CLIS_HISTORY_FILE") or default_history_path(),
        "quarantine_after": int(os.environ.get("UAC_QUARANTINE_AFTER") or DEFAULT_QUARANTINE_AFTER),
        "include_quarantined": os.environ.get("UAC_INCLUDE_QUARANTINED", "0") == "1",
        "precheck_uptodate": _load_precheck_uptodate_env(),
        "held_config": normalize_hold_entries(cfg.get("hold")) - normalize_hold_entries_major(cfg.get("hold")),
        "held_adhoc": _parse_csv(os.environ.get("HOLD")) - normalize_hold_entries_major(hold_env_list),
        "precheck_uptodate_known": _load_precheck_known_env(),
        "held_config_major": normalize_hold_entries_major(cfg.get("hold")),
        "held_adhoc_major": normalize_hold_entries_major(hold_env_list),
        "major_hold_blocks": _load_major_holds_env(),
    }


def _precheck_stage_data() -> tuple[dict[str, Any], dict[str, Any]]:
    """(bulk map, stdout map) from the pre-check stage's file."""
    data = _load_precheck_file() or {}
    bulk_up = data.get("bulk") if isinstance(data.get("bulk"), dict) else {}
    stdouts = data.get("stdout") if isinstance(data.get("stdout"), dict) else {}
    return bulk_up, stdouts


def _cmd_benchmark(args: argparse.Namespace) -> int:
    base = os.environ.get("CONFIG_FILE", "") or os.path.join(
        os.path.dirname(__file__), "tool_config.json")
    local = os.environ.get("CONFIG_LOCAL_FILE", "")
    cfg = load_merge(base, local or None)
    results = benchmark_operation(args.cache_path or "", cfg, base, local or None)
    print(json.dumps(results, indent=2))
    return 0


def _cmd_health_check(args: argparse.Namespace) -> int:
    result = health_check()
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "healthy" else 1


def _cmd_backup(args: argparse.Namespace) -> int:
    backup_path = create_backup(args.cache_path)
    if backup_path:
        print(f"Backup created: {backup_path}")
    else:
        print("No backup created (cache file not found)")
    return 0


def _cmd_list_backups(args: argparse.Namespace) -> int:
    backups = list_backups(args.cache_path)
    if backups:
        print(f"Found {len(backups)} backup(s):")
        for b in backups:
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(b)))
            print(f"  {b} (modified: {mtime})")
    else:
        print("No backups found")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    return 0 if restore_backup(args.cache_path, args.backup_path) else 1


def _cmd_parse_npm_globals(args: argparse.Namespace) -> int:
    print(parse_npm_globals_json(sys.stdin.read()))
    return 0


def _cmd_convert_tools_array(args: argparse.Namespace) -> int:
    result = convert_tools_array_to_json(
        sys.stdin.read(), args.scanned_at, args.existing_cache or None)
    print(result)
    return 0


def _cmd_incremental_scan(args: argparse.Namespace) -> int:
    with open(args.rows_file, encoding="utf-8") as f:
        rows = parse_scan_rows(f.read())
    extra_tools: list[tuple[str, str]] = []
    if args.extra_tools_file and os.path.isfile(args.extra_tools_file):
        with open(args.extra_tools_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "|" not in line:
                    continue
                n, o = line.split("|", 1)
                if n:
                    extra_tools.append((n, o))
    print(incremental_scan_merge(rows, args.cache_path, args.scanned_at,
                                 args.force == "1", extra_tools))
    return 0


def _cmd_update_cache_versions(args: argparse.Namespace) -> int:
    versions_input = sys.stdin.read()
    versions = json.loads(versions_input) if versions_input.strip() else {}
    update_cache_versions(args.cache_path, versions)
    return 0


def _cmd_validate_cache(args: argparse.Namespace) -> int:
    result = validate_cache(args.cache_path)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


def _cmd_debug_cache(args: argparse.Namespace) -> int:
    debug_cache(args.cache_path)
    return 0


def _cmd_emit(args: argparse.Namespace) -> int:
    cfg = _cfg_from_env()
    emit_lines(args.cache_path, cfg, **_emit_kwargs(cfg))
    return 0


def _cmd_emit_json(args: argparse.Namespace) -> int:
    cfg = _cfg_from_env()
    emit_plan_json(args.cache_path, cfg, **_emit_kwargs(cfg))
    return 0


def _cmd_precheck(args: argparse.Namespace) -> int:
    cfg = _cfg_from_env()
    bulk_up, stdouts = run_prechecks_full(
        cfg, os.environ.get("ONLY_ORIGINS"), os.environ.get("SKIP_ORIGINS"))
    # The per-origin stdout rides along so the precheck-known stage can
    # reuse the manager's outdated list (npm/brew) instead of issuing
    # per-tool lookups.
    print(json.dumps({"bulk": bulk_up, "known": {}, "stdout": stdouts}))
    return 0


def _cmd_precheck_known(args: argparse.Namespace) -> int:
    # Second pre-check stage: known tools already at the latest version.
    # Reads the file precheck wrote (path in UAC_PRECHECK_UPTODATE_FILE),
    # fills its "known" map, and prints the full updated JSON.
    cfg = _cfg_from_env()
    bulk_up, stdouts = _precheck_stage_data()
    known_up = run_known_prechecks(
        args.cache_path, cfg, bulk_up, stdouts,
        os.environ.get("ONLY_ORIGINS"), os.environ.get("SKIP_ORIGINS"),
        _parse_csv(os.environ.get("UAC_PRECHECK_SKIP")),
    )
    print(json.dumps({"bulk": bulk_up, "known": known_up, "stdout": stdouts}))
    return 0


def _cmd_precheck_known_candidates(args: argparse.Namespace) -> int:
    # Dry-run support: known tool names that WOULD be considered for an
    # up-to-date check this run (no lookups performed).
    cfg = _cfg_from_env()
    names = known_precheck_candidates(
        args.cache_path, cfg,
        os.environ.get("ONLY_ORIGINS"), os.environ.get("SKIP_ORIGINS"),
        _parse_csv(os.environ.get("UAC_PRECHECK_SKIP")),
    )
    print(", ".join(names))
    return 0


def _cmd_precheck_candidates(args: argparse.Namespace) -> int:
    cfg = _cfg_from_env()
    origins = precheck_candidate_origins(
        cfg, os.environ.get("ONLY_ORIGINS"), os.environ.get("SKIP_ORIGINS"))
    print(", ".join(origins))
    return 0


def _cmd_scan_dirs(args: argparse.Namespace) -> int:
    # Static discovery-scan directories from the merged config, as TSV rows
    # ("dir\torigin\tmode") for the shell's full_scan. Dirs are printed
    # verbatim; the shell expands a leading $HOME (no eval).
    cfg = _cfg_from_env()
    for d, origin, mode in scan_dirs_config_rows(cfg):
        sys.stdout.write(f"{d}\t{origin}\t{mode}\n")
    return 0


def _cmd_resolve_major_holds(args: argparse.Namespace) -> int:
    # Decide what each "name:major" hold does this run: block (a major
    # upgrade is pending), allow (no major jump — update runs), or unknown
    # (can't verify — stays held, fail-safe). Reuses the pre-check file's
    # captured manager output where available.
    cfg = _cfg_from_env()
    bulk_up, stdouts = _precheck_stage_data()
    result = resolve_major_holds(args.cache_path, cfg, os.environ.get("HOLD"), bulk_up, stdouts)
    print(json.dumps(result))
    return 0


def _cmd_list_json(args: argparse.Namespace) -> int:
    list_json(args.cache_path)
    return 0


def _cmd_snapshot_versions(args: argparse.Namespace) -> int:
    snap = snapshot_versions(
        _read_lines(args.emit_path),
        args.cache_path or None,
        args.prior_snapshot_path or None,
    )
    print(json.dumps(snap))
    return 0


def _cmd_notify_diff(args: argparse.Namespace) -> int:
    notify_diff(
        _load_json(args.before), _load_json(args.after),
        int(args.ok), int(args.fail),
        _load_new_tools_arg(args.new_tools),
        _load_new_tools_arg(args.quarantined),
        _load_new_tools_arg(args.held),
        _load_new_tools_arg(args.failed),
    )
    return 0


def _cmd_run_summary(args: argparse.Namespace) -> int:
    sys.stdout.write(format_run_summary(
        _load_json(args.before), _load_json(args.after),
        int(args.ok), int(args.fail),
        _load_new_tools_arg(args.new_tools),
        _load_new_tools_arg(args.quarantined),
        _load_new_tools_arg(args.held),
        _load_new_tools_arg(args.failed),
        mode=_summary_mode_env(),
    ))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    history_path = args.history_path or default_history_path()
    sys.stdout.write(format_history(history_path, args.n))
    return 0


def _cmd_history_append(args: argparse.Namespace) -> int:
    before = _load_json(args.before_json) if args.before_json else {}
    after = _load_json(args.after_json) if args.after_json else {}
    appended = history_append(
        args.history_path, args.run_id, _read_lines(args.results_path), before, after)
    logger.debug(f"Appended {appended} history record(s) to {args.history_path}")
    return 0


def _cmd_new_tools(args: argparse.Namespace) -> int:
    print(json.dumps(diff_new_tools(args.prev_names_path or "", args.cache_path or "")))
    return 0


def _cmd_suggest(args: argparse.Namespace) -> int:
    suggest_config(args.cache_path, _cfg_with_legacy_base(args.base_config or ""))
    return 0


def _cmd_suggest_known(args: argparse.Namespace) -> int:
    suggest_known(args.cache_path, _cfg_with_legacy_base(args.base_config or ""))
    return 0


def _cmd_suggest_known_count(args: argparse.Namespace) -> int:
    cfg = _cfg_with_legacy_base(args.base_config or "", do_validate=False)
    print(json.dumps(suggest_known_count(args.cache_path, cfg)))
    return 0


def _cmd_log_unknowns(args: argparse.Namespace) -> int:
    cfg = _cfg_with_legacy_base(args.base_config or "")
    log_unknowns(args.cache_path, cfg, os.environ.get("UNKNOWN_LOG_FILE", UNKNOWN_LOG_DEFAULT))
    return 0


def _cmd_report_unknown(args: argparse.Namespace) -> int:
    report_unknown(args.unknown_log or UNKNOWN_LOG_DEFAULT, args.min_times)
    return 0


def _cmd_ack_unknown(args: argparse.Namespace) -> int:
    ack_unknown(args.unknown_log, args.tool_name)
    return 0


def _cmd_hold_edit(args: argparse.Namespace, add: bool) -> int:
    names = _parse_csv(args.names)
    if not names:
        print("No names given.", file=sys.stderr)
        return 2
    hold = edit_local_hold(args.config_local_file, add=names if add else None,
                           remove=None if add else names)
    verb = "Held" if add else "Unheld"
    print(f"{verb}: {', '.join(sorted(names))}")
    print(f"hold list now ({len(hold)}): {', '.join(hold) if hold else '(empty)'}")
    return 0


def _cmd_hold_add(args: argparse.Namespace) -> int:
    return _cmd_hold_edit(args, add=True)


def _cmd_hold_remove(args: argparse.Namespace) -> int:
    return _cmd_hold_edit(args, add=False)


def _cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _cfg_from_env()
    history_path = os.environ.get("UPDATE_ALL_CLIS_HISTORY_FILE") or default_history_path()
    report = doctor_report(args.cache_path, cfg, history_path)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_doctor_report(report), end="")
    return 1 if doctor_has_findings(report) else 0


def _cmd_changelog(args: argparse.Namespace) -> int:
    cfg = _cfg_from_env(do_validate=False)
    sys.stdout.write(build_changelog_digest(_load_json(args.before), _load_json(args.after), cfg))
    return 0


def _cmd_cache_names(args: argparse.Namespace) -> int:
    for name in cache_tool_names(args.cache_path):
        print(name)
    return 0


def _cmd_list_human(args: argparse.Namespace) -> int:
    out = format_tool_list(args.cache_path)
    if out:
        print(out)
    return 0


def _cmd_lines_to_json(args: argparse.Namespace) -> int:
    print(lines_to_json(sys.stdin.read()))
    return 0


def _cmd_suggest_known_summary(args: argparse.Namespace) -> int:
    # One-line "count<TAB>sample" for the shell's post-run auto-tip.
    cfg = _cfg_with_legacy_base(args.base_config or "", do_validate=False)
    candidates = suggest_known_count(args.cache_path, cfg)
    sample = ", ".join(str(c[0]) for c in candidates[:3] if c)
    print(f"{len(candidates)}\t{sample}")
    return 0


def _cmd_unknown_summary(args: argparse.Namespace) -> int:
    count, sample = unknown_log_summary(args.unknown_log)
    print(count)
    print(sample)
    return 0


def _cmd_json_summary(args: argparse.Namespace) -> int:
    print(json.dumps({"ok": int(args.ok), "failed": int(args.fail)}))
    return 0


def _cmd_insights(args: argparse.Namespace) -> int:
    history_path = args.history_path or (
        os.environ.get("UPDATE_ALL_CLIS_HISTORY_FILE") or default_history_path())
    report = build_insights(history_path, top_n=args.top)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        sys.stdout.write(format_insights(report))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lib_update_all_clis.py",
        description="update-all-clis library subcommands (invoked by update_all_clis.sh).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _p(name: str, func: Any, **kwargs: Any) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, **kwargs)
        sp.set_defaults(func=func)
        return sp

    p = _p("benchmark", _cmd_benchmark); p.add_argument("cache_path", nargs="?", default="")
    _p("health-check", _cmd_health_check)
    p = _p("backup", _cmd_backup); p.add_argument("cache_path")
    p = _p("list-backups", _cmd_list_backups); p.add_argument("cache_path")
    p = _p("restore", _cmd_restore)
    p.add_argument("cache_path"); p.add_argument("backup_path")
    _p("parse-npm-globals", _cmd_parse_npm_globals)
    p = _p("convert-tools-array", _cmd_convert_tools_array)
    p.add_argument("scanned_at", nargs="?", default="")
    p.add_argument("existing_cache", nargs="?", default="")
    p = _p("incremental-scan", _cmd_incremental_scan)
    p.add_argument("cache_path"); p.add_argument("scanned_at")
    p.add_argument("force"); p.add_argument("rows_file")
    p.add_argument("extra_tools_file", nargs="?", default="")
    p = _p("update-cache-versions", _cmd_update_cache_versions); p.add_argument("cache_path")
    p = _p("validate-cache", _cmd_validate_cache); p.add_argument("cache_path")
    p = _p("debug-cache", _cmd_debug_cache); p.add_argument("cache_path")
    p = _p("emit", _cmd_emit); p.add_argument("cache_path")
    p = _p("emit-json", _cmd_emit_json); p.add_argument("cache_path")
    _p("precheck", _cmd_precheck)
    p = _p("precheck-known", _cmd_precheck_known); p.add_argument("cache_path")
    p = _p("precheck-known-candidates", _cmd_precheck_known_candidates)
    p.add_argument("cache_path")
    _p("precheck-candidates", _cmd_precheck_candidates)
    _p("scan-dirs", _cmd_scan_dirs)
    p = _p("resolve-major-holds", _cmd_resolve_major_holds); p.add_argument("cache_path")
    p = _p("list-json", _cmd_list_json); p.add_argument("cache_path")
    p = _p("snapshot-versions", _cmd_snapshot_versions)
    p.add_argument("emit_path"); p.add_argument("cache_path", nargs="?", default="")
    p.add_argument("prior_snapshot_path", nargs="?", default="")
    for name, func in (("notify-diff", _cmd_notify_diff), ("run-summary", _cmd_run_summary)):
        p = _p(name, func)
        p.add_argument("before"); p.add_argument("after")
        p.add_argument("ok"); p.add_argument("fail")
        p.add_argument("new_tools", nargs="?", default="")
        p.add_argument("quarantined", nargs="?", default="")
        p.add_argument("held", nargs="?", default="")
        p.add_argument("failed", nargs="?", default="")
    p = _p("history", _cmd_history)
    p.add_argument("history_path", nargs="?", default="")
    p.add_argument("n", nargs="?", type=int, default=3)
    p = _p("history-append", _cmd_history_append)
    p.add_argument("history_path"); p.add_argument("run_id"); p.add_argument("results_path")
    p.add_argument("before_json", nargs="?", default="")
    p.add_argument("after_json", nargs="?", default="")
    p = _p("new-tools", _cmd_new_tools)
    p.add_argument("prev_names_path", nargs="?", default="")
    p.add_argument("cache_path", nargs="?", default="")
    for name, func in (("suggest", _cmd_suggest), ("suggest-known", _cmd_suggest_known),
                       ("suggest-known-count", _cmd_suggest_known_count),
                       ("log-unknowns", _cmd_log_unknowns)):
        p = _p(name, func)
        p.add_argument("cache_path")
        p.add_argument("base_config", nargs="?", default="")
    p = _p("report-unknown", _cmd_report_unknown)
    p.add_argument("unknown_log", nargs="?", default="")
    p.add_argument("min_times", nargs="?", type=int, default=1)
    p = _p("ack-unknown", _cmd_ack_unknown)
    p.add_argument("unknown_log"); p.add_argument("tool_name")
    p = _p("hold-add", _cmd_hold_add)
    p.add_argument("config_local_file"); p.add_argument("names")
    p = _p("hold-remove", _cmd_hold_remove)
    p.add_argument("config_local_file"); p.add_argument("names")
    p = _p("doctor", _cmd_doctor)
    p.add_argument("cache_path", nargs="?", default="")
    p.add_argument("--json", action="store_true")
    p = _p("changelog", _cmd_changelog)
    p.add_argument("before"); p.add_argument("after")
    p = _p("cache-names", _cmd_cache_names); p.add_argument("cache_path")
    p = _p("list-human", _cmd_list_human); p.add_argument("cache_path")
    _p("lines-to-json", _cmd_lines_to_json)
    p = _p("suggest-known-summary", _cmd_suggest_known_summary)
    p.add_argument("cache_path")
    p.add_argument("base_config", nargs="?", default="")
    p = _p("unknown-summary", _cmd_unknown_summary); p.add_argument("unknown_log")
    p = _p("json-summary", _cmd_json_summary)
    p.add_argument("ok"); p.add_argument("fail")
    p = _p("insights", _cmd_insights)
    p.add_argument("history_path", nargs="?", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument("--top", type=int, default=10)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
