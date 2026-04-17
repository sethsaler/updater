#!/usr/bin/env bash
# Minimal smoke test: fake HOME + cache, dry-run updates (no network).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$ROOT:$PATH"
td="$(mktemp -d)"
cleanup() { rm -rf "$td"; }
trap cleanup EXIT

mkdir -p "$td/.config/update-all-clis"
cat > "$td/.config/update-all-clis/cache.json" <<'EOF'
[
  {"name": "dummy-npm-tool", "origin": "npm"},
  {"scanned_at": "2026-01-01T00:00:00Z", "count": 1}
]
EOF

HOME="$td"
export XDG_CONFIG_HOME="$td/.config"
export CONFIG_FILE="$ROOT/tool_config.json"
export LIB_SCRIPT="$ROOT/lib_update_all_clis.py"

"$ROOT/update_all_clis.sh" --dry-run --no-scan --quiet
"$ROOT/update_all_clis.sh" --json | python3 -c "import json,sys; json.load(sys.stdin)"

echo "ci_fixture: ok"
