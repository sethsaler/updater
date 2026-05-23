#!/usr/bin/env python3
"""Merge tool config, validate, emit update lines for update_all_clis.sh."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Optional

_UV_ORIGINS = frozenset({"uv", "uv/pip", "uv/venv"})
EMIT_SEP = "\x1e"


def load_merge(base_path: str, local_path: Optional[str]) -> dict:
    with open(base_path, encoding="utf-8") as f:
        base = json.load(f)
    if local_path and os.path.isfile(local_path):
        with open(local_path, encoding="utf-8") as f:
            loc = json.load(f)
        for key in ("known", "bulk"):
            if key in loc and isinstance(loc[key], dict):
                base.setdefault(key, {})
                base[key].update(loc[key])
    return base


def validate(cfg: dict) -> None:
    if not isinstance(cfg.get("known"), dict) or not isinstance(cfg.get("bulk"), dict):
        raise ValueError("config must contain 'known' and 'bulk' objects")
    for section in ("known", "bulk"):
        for k, v in cfg[section].items():
            if not isinstance(v, str):
                raise ValueError(f"{section}.{k!r} must be a string command")


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
    cfg: dict,
    only_origins: Optional[str],
    skip_origins: Optional[str],
) -> list[str]:
    only = _parse_csv(only_origins)
    skip = _parse_csv(skip_origins)

    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)

    tools = [t for t in data if "name" in t]
    self_cmd = cfg["known"]
    bulk_origins = cfg["bulk"]
    known_names = set(self_cmd.keys())
    seen_names: set[str] = set()
    seen_bulk: set[str] = set()
    lines: list[str] = []

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

        if name in known_names:
            if name in seen_names:
                continue
            if origin in skip:
                continue
            if not origin_allowed_for_known(origin, name):
                continue
            cmd = self_cmd[name]
            if not cmd or not cmd.strip():
                seen_names.add(name)
                continue
            seen_names.add(name)
            write_line("known", name, cmd, origin)
            seen_bulk.add(origin)
            continue

        if name in seen_names:
            continue

        inferred = _infer_origin_from_symlink(name, origin)
        if inferred:
            origin = inferred

        if origin in bulk_origins and origin not in seen_bulk:
            if not should_emit_bulk(origin):
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

        if origin in bulk_origins:
            seen_names.add(name)
            continue

        seen_names.add(name)
        lines.append(f"skip{EMIT_SEP}{name}{EMIT_SEP}{EMIT_SEP}")

    return lines


def emit_lines(
    cache_path: str,
    cfg: dict,
    only_origins: Optional[str],
    skip_origins: Optional[str],
) -> None:
    for line in collect_emit_lines(cache_path, cfg, only_origins, skip_origins):
        sys.stdout.write(line + "\n")


def emit_plan_json(
    cache_path: str,
    cfg: dict,
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
    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
    tools = [t for t in data if "name" in t]
    meta = next((t for t in data if "scanned_at" in t), None)
    out = {
        "tools": tools,
        "count": len(tools),
        "scanned_at": meta.get("scanned_at") if meta else None,
    }
    print(json.dumps(out, indent=2))


def probe_version(name: str) -> str:
    """Best-effort version string for a CLI on PATH."""
    if not shutil.which(name):
        return "?"
    for args in ((name, "--version"), (name, "-V"), (name, "version")):
        try:
            r = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                timeout=15,
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
    known: dict[str, str] = {}
    bulk: dict[str, str] = {}
    seen_bulk: set[str] = set()
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
            known[name] = probe_known(name)
        elif kind == "bulk" and name not in seen_bulk:
            seen_bulk.add(name)
            bulk[name] = probe_bulk(name)
    return {"known": known, "bulk": bulk}


def suggest_config(cache_path: str, cfg: dict) -> None:
    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
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


def log_unknowns(cache_path: str, cfg: dict, unknown_log_path: str) -> None:
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


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: lib_update_all_clis.py emit|emit-json|list-json|snapshot-versions|"
            "notify-diff|run-summary|suggest|log-unknowns|report-unknown|ack-unknown …",
            file=sys.stderr,
        )
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "emit":
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
