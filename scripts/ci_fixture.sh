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
  {"name": "unconfigured-tool", "origin": "manual"},
  {"scanned_at": "2026-01-01T00:00:00Z", "count": 2}
]
EOF

HOME="$td"
export XDG_CONFIG_HOME="$td/.config"
export CONFIG_FILE="$ROOT/tool_config.json"
export LIB_SCRIPT="$ROOT/lib_update_all_clis.py"

"$ROOT/update_all_clis.sh" --dry-run --no-scan --quiet
"$ROOT/update_all_clis.sh" --json --no-scan | python3 -c "import json,sys; json.load(sys.stdin)"

"$ROOT/update_all_clis.sh" --json-plan --no-scan | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert 'plan' in d and isinstance(d['plan'], list)
"

python3 "$LIB_SCRIPT" suggest "$td/.config/update-all-clis/cache.json" >/dev/null || true

python3 -c "
import sys
sys.path.insert(0, '$ROOT')
from lib_update_all_clis import validate
try:
    validate({'known': 'x', 'bulk': {}})
except ValueError:
    sys.exit(0)
sys.exit(1)
"

python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -q

echo "ci_fixture: ok"
