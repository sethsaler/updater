#!/usr/bin/env python3
"""Merge tool config, validate, emit update lines for update_all_clis.sh."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Optional


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


def emit_lines(
    cache_path: str,
    cfg: dict,
    only_origins: Optional[str],
    skip_origins: Optional[str],
) -> None:
    only = _parse_csv(only_origins)
    skip = _parse_csv(skip_origins)

    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)

    tools = [t for t in data if "name" in t]
    self_cmd = cfg["known"]
    bulk_origins = cfg["bulk"]
    known = set(self_cmd.keys())
    seen_bulk: set[str] = set()

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

    for t in tools:
        name = t["name"]
        origin = t.get("origin", "?")

        if name in known:
            if origin in skip:
                continue
            if not origin_allowed_for_known(origin, name):
                continue
            cmd = self_cmd[name]
            sys.stdout.write(f"known|{name}|{cmd}\n")
            seen_bulk.add(origin)
            continue

        if origin in bulk_origins and origin not in seen_bulk:
            if not should_emit_bulk(origin):
                continue
            seen_bulk.add(origin)
            sys.stdout.write(f"bulk|{origin}|{bulk_origins[origin]}\n")
            continue

        sys.stdout.write(f"skip|{name}|\n")


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


def probe_known(name: str) -> str:
    import shutil

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


def snapshot_versions(lines: list[str]) -> dict[str, dict[str, str]]:
    known: dict[str, str] = {}
    bulk: dict[str, str] = {}
    seen_bulk: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        kind, name, _ = parts[0], parts[1], parts[2]
        if kind == "skip":
            continue
        if kind == "known":
            known[name] = probe_known(name)
        elif kind == "bulk" and name not in seen_bulk:
            seen_bulk.add(name)
            bulk[name] = probe_bulk(name)
    return {"known": known, "bulk": bulk}


def notify_macos_dialog(before: dict[str, Any], after: dict[str, Any], ok: int, fail: int) -> None:
    if sys.platform != "darwin":
        return
    lines_out: list[str] = [f"Summary: {ok} ok, {fail} failed", ""]
    lines_out.append("Known tools:")
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
    body = "\n".join(lines_out)
    if len(body) > 950:
        body = body[:947] + "\n…"
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: lib_update_all_clis.py emit|list-json|snapshot-versions|notify-diff …",
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
    elif cmd == "list-json":
        cache_path = sys.argv[2]
        list_json(cache_path)
    elif cmd == "snapshot-versions":
        emit_path = sys.argv[2]
        with open(emit_path, encoding="utf-8") as f:
            snap = snapshot_versions(f.read().splitlines())
        print(json.dumps(snap))
    elif cmd == "notify-diff":
        before = json.load(open(sys.argv[2], encoding="utf-8"))
        after = json.load(open(sys.argv[3], encoding="utf-8"))
        notify_macos_dialog(before, after, int(sys.argv[4]), int(sys.argv[5]))
    else:
        print("unknown command", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
