#!/usr/bin/env python3
"""Generate CLI_LAST_UPDATED.md — last-modified time + version for every known CLI."""
import json
import os
import sys
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from lib_update_all_clis import probe_version  # noqa: E402

CONFIG_PATH = os.path.join(REPO_ROOT, "tool_config.json")
OUTPUT_PATH = os.path.join(REPO_ROOT, "CLI_LAST_UPDATED.md")

with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = json.load(f)

rows = []
for name in sorted(cfg["known"].keys()):
    path = __import__("shutil").which(name)
    if not path:
        rows.append((name, "not installed", ""))
        continue

    mtime = os.path.getmtime(path)
    mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    ver = probe_version(name)
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

with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"Written: {OUTPUT_PATH}")
