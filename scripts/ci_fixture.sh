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

# Smoke test suggest-known (prune default), suggest-known-add, and suggest-known-count
python3 "$LIB_SCRIPT" suggest-known >/dev/null || true
python3 "$LIB_SCRIPT" suggest-known-prune >/dev/null || true
python3 "$LIB_SCRIPT" suggest-known --add "$td/.config/update-all-clis/cache.json" >/dev/null || true
python3 "$LIB_SCRIPT" suggest-known-count "$td/.config/update-all-clis/cache.json" >/dev/null || true
python3 "$LIB_SCRIPT" merge-pack "$ROOT/packs/ai-clis.json" "$td/.config/update-all-clis/config.local.json" >/dev/null

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

# Executor parity gate: the Python executor (single executor for all real
# runs) and the legacy bash executor must agree on records, tallies, and
# (at parallel=1) stdout for an identical plan.
bash "$ROOT/tests/executor_parity.sh"

# LaunchAgent plists must bake in a PATH: launchd/systemd jobs otherwise
# run with a minimal system PATH and can't find brew, npm, cargo, etc.
sed -n '/^write_plist_daily()/,/^}/p;/^write_plist_interval()/,/^}/p;/^write_plist_weekly()/,/^}/p' "$ROOT/install.sh" > "$td/plist_funcs.sh"
bash -c "
LOG_DIR='$td/logs'
source '$td/plist_funcs.sh'
write_plist_daily testuser '$td/t.plist' /fake/update_all_clis.sh '/opt/bin:/usr/bin:/bin'
write_plist_interval testuser '$td/t.plist.i' /fake/update_all_clis.sh 21600 '/opt/bin:/usr/bin:/bin'
write_plist_weekly testuser '$td/t.plist.w' /fake/update_all_clis.sh '/opt/bin:/usr/bin:/bin'
"
for p in "$td/t.plist" "$td/t.plist.i" "$td/t.plist.w"; do
  grep -q "<key>PATH</key>" "$p" || { echo "ci_fixture: $p missing PATH key"; exit 1; }
  grep -q "/opt/bin:/usr/bin:/bin" "$p" || { echo "ci_fixture: $p missing baked PATH"; exit 1; }
done
python3 -c "
import plistlib
for p in ('$td/t.plist', '$td/t.plist.i', '$td/t.plist.w'):
    with open(p, 'rb') as f:
        d = plistlib.load(f)
    assert d['EnvironmentVariables']['PATH'] == '/opt/bin:/usr/bin:/bin', p
print('plist PATH: ok')
"

echo "ci_fixture: ok"
