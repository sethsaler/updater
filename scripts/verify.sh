#!/usr/bin/env bash
# Run the same checks locally and in CI; no real updates are executed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Verification must never inherit the updater's opt-in git pull/re-exec mode.
# Any nonempty value (including "0") enables it in update_all_clis.sh.
export UPDATE_ALL_CLIS_SELF_UPDATE=''
export UPDATE_ALL_CLIS_NOTIFY=0

require() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Missing prerequisite: %s (see README Testing)\n' "$1" >&2
    exit 1
  }
}

check_shell() {
  require shellcheck
  shellcheck ./*.sh scripts/*.sh migration/*.sh tests/*.sh
}

check_python() {
  require ruff
  ruff check --select E9,F ./*.py tests/ scripts/
}

check_config() {
  require python3
  # Preserve the existing lightweight configuration-shape check.
  python3 - <<'PY'
import json

with open("tool_config.json") as f:
    cfg = json.load(f)
with open("tool_config.schema.json") as f:
    schema = json.load(f)
assert set(schema["required"]).issubset(cfg.keys())
assert isinstance(cfg["known"], dict)
assert isinstance(cfg["bulk"], dict)
for section in ("known", "bulk"):
    for key, value in cfg[section].items():
        assert isinstance(value, str), f"{section}.{key} must be string"
print("config schema: ok")
PY
}

check_fixture() {
  require python3
  bash scripts/ci_fixture.sh
}

check_unit() {
  require python3
  python3 -m unittest discover -s tests -p 'test_*.py' -q
}

check_native() (
  if [[ "$(uname -s)" != Darwin ]]; then
    echo 'The native discovery check requires macOS.' >&2
    exit 1
  fi
  require python3
  # Keep real macOS discovery paths but isolate generated cache/config files.
  td="$(mktemp -d)"
  trap 'rm -rf "$td"' EXIT
  XDG_CONFIG_HOME="$td" CONFIG_FILE="$ROOT/tool_config.json" \
    CONFIG_LOCAL_FILE="$td/config.local.json" \
    UPDATE_ALL_CLIS_SELF_UPDATE='' UPDATE_ALL_CLIS_NOTIFY=0 \
    bash update_all_clis.sh --dry-run --quiet
)

if [[ "$#" -gt 1 ]]; then
  echo 'Usage: bash scripts/verify.sh [full|shellcheck|ruff|config-schema|unit|fixture|native]' >&2
  exit 2
fi
case "${1:-full}" in
  shellcheck) check_shell ;;
  ruff) check_python ;;
  config-schema) check_config ;;
  unit) check_unit ;;
  fixture) check_fixture ;;
  native) check_native ;;
  full)
    require python3
    require shellcheck
    require ruff
    check_shell
    check_python
    check_config
    check_fixture
    if [[ "$(uname -s)" == Darwin ]]; then
      check_native
    else
      echo 'Portable checks passed. Native macOS discovery was not run on this platform.'
    fi
    ;;
  *)
    echo 'Usage: bash scripts/verify.sh [full|shellcheck|ruff|config-schema|unit|fixture|native]' >&2
    exit 2
    ;;
esac
