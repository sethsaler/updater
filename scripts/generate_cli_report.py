#!/usr/bin/env python3
"""Generate CLI_LAST_UPDATED.md — last-modified time + version for every known CLI."""
import json
import os
import shutil
import subprocess
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "tool_config.json")
OUTPUT_PATH = os.path.join(REPO_ROOT, "CLI_LAST_UPDATED.md")

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

rows = []
for name in sorted(cfg["known"].keys()):
    path = shutil.which(name)
    if not path:
        rows.append((name, "not installed", ""))
        continue

    mtime = os.path.getmtime(path)
    mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

    ver = "?"
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
                ver = r.stdout.strip().split("\n")[0].strip()[:120]
                break
        except Exception:
            pass

    rows.append((name, mtime_str, ver))

lines = [
    "# CLI Last Updated\n",
    f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
    "Shows file modification time and version for each CLI in `tool_config.json`.\n",
    "| CLI | Last Modified | Version |",
    "|-----|---------------|---------|",
]
for name, mtime, ver in rows:
    lines.append(f"| {name} | {mtime} | {ver} |")
lines.append("")

with open(OUTPUT_PATH, "w") as f:
    f.write("\n".join(lines))

print(f"Written: {OUTPUT_PATH}")
