#!/usr/bin/env bash
# =============================================================================
# update-all-clis: Dynamic discovery + update all CLIs and package managers
#
# Usage: ./update_all_clis.sh [options]
#   --rescan          Force a fresh discovery scan
#   --no-scan         Use existing cache (even if older than TTL)
#   --skip=a,b        Skip known tools (overrides $SKIP)
#   --only-origins=   Only run bulk/known matching these origins or names
#   --skip-origins=   Skip bulk (and known) for these origins
#   --scan-path       Also scan directories on $PATH (origin: path)
#   --parallel=N      Run up to N updates concurrently (default 1)
#   --json-summary    Print JSON ok/failed counts on stdout after run
#   --list --json     Machine-readable tool list (with --list)
#   --trace           Trace shell commands (bash -x)
#   --dry-run         Show commands without running
#   --version         Print version and exit
# =============================================================================

UAC_VERSION="0.2.0"

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_SCRIPT="${LIB_SCRIPT:-$SCRIPT_DIR/lib_update_all_clis.py}"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/tool_config.json}"
CONFIG_LOCAL_FILE="${CONFIG_LOCAL_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/config.local.json}"

CACHE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/cache.json"
LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/logs"

CACHE_TTL_HOURS="${CACHE_TTL_HOURS:-24}"
CACHE_TTL_SECONDS=$((CACHE_TTL_HOURS * 3600))

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

SKIP="${SKIP:-}"
ONLY_ORIGINS="${ONLY_ORIGINS:-}"
SKIP_ORIGINS="${SKIP_ORIGINS:-}"
QUIET=""; DRY_RUN=""; RESCAN=""; LIST_MODE=""; NO_SCAN=""
LIST_JSON=""; JSON_SUMMARY=""; TRACE=""
SCAN_PATH=""; PARALLEL_JOBS=1

# -------------------------------------------------------------------
# Logging helpers
# -------------------------------------------------------------------
log()   { [[ -z "$QUIET" ]] && echo -e "$@"; }
info()  { log "${BOLD}==>${NC} $*"; }
ok()    { log "${GREEN}✓${NC} $*"; }
warn()  { log "${YELLOW}!!${NC} $*"; }

is_skipped() {
  [[ -z "$SKIP" ]] && return 1
  local name="$1"
  IFS=',' read -ra SKIP_ITEMS <<< "$SKIP"
  for item in "${SKIP_ITEMS[@]}"; do
    [[ "$name" == "$item" ]] && return 0
  done
  return 1
}

# -------------------------------------------------------------------
# Argument parsing
# -------------------------------------------------------------------
SKIP_CLI=""
ONLY_CLI=""
SKIP_ORIGINS_CLI=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip=*)          SKIP_CLI="${1#*=}"; shift ;;
    --only-origins=*)  ONLY_CLI="${1#*=}"; shift ;;
    --skip-origins=*)  SKIP_ORIGINS_CLI="${1#*=}"; shift ;;
    --parallel=*)      PARALLEL_JOBS="${1#*=}"; shift ;;
    --quiet|-q)        QUIET=1; shift ;;
    --dry-run|-n)      DRY_RUN=1; shift ;;
    --rescan|-r)       RESCAN=1; shift ;;
    --list|-l)         LIST_MODE=1; shift ;;
    --json)            LIST_JSON=1; shift ;;
    --no-scan)         NO_SCAN=1; shift ;;
    --json-summary)    JSON_SUMMARY=1; shift ;;
    --trace)           TRACE=1; shift ;;
    --scan-path)       SCAN_PATH=1; shift ;;
    --version|-V)      echo "update-all-clis $UAC_VERSION"; exit 0 ;;
    --help|-h)         grep "^# " "$0" | sed 's/^# //'; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Try --help for usage." >&2
      exit 1
      ;;
  esac
done

SKIP="${SKIP_CLI:-$SKIP}"
ONLY_ORIGINS="${ONLY_CLI:-$ONLY_ORIGINS}"
SKIP_ORIGINS="${SKIP_ORIGINS_CLI:-$SKIP_ORIGINS}"

[[ -n "$LIST_JSON" ]] && LIST_MODE=1

if ! [[ "$PARALLEL_JOBS" =~ ^[1-9][0-9]*$ ]] && ! [[ "$PARALLEL_JOBS" == "0" ]]; then
  echo "Invalid --parallel value (use a non-negative integer): $PARALLEL_JOBS" >&2
  exit 1
fi
if [[ "$PARALLEL_JOBS" == "0" ]]; then
  echo "--parallel must be at least 1" >&2
  exit 1
fi

# -------------------------------------------------------------------
# macOS: show summary dialog only for manual (TTY) runs unless overridden.
# Scheduled LaunchAgent sets UPDATE_ALL_CLIS_NO_NOTIFY=1 in the plist.
# -------------------------------------------------------------------
_want_notify_popup() {
  [[ "$(uname)" != "Darwin" ]] && return 1
  [[ "${UPDATE_ALL_CLIS_NO_NOTIFY:-}" == "1" ]] && return 1
  case "${UPDATE_ALL_CLIS_NOTIFY:-}" in
    1) return 0 ;;
    0) return 1 ;;
  esac
  [[ -t 1 ]]
}

# -------------------------------------------------------------------
# Glob-based directory scanner — pure bash, accumulates into TOOLS_RAW
# -------------------------------------------------------------------
TOOLS_RAW=""

scan_dir() {
  local dir="$1"; local origin="$2"
  [[ -d "$dir" ]] && [[ -r "$dir" ]] || return 0

  local bin
  for bin in "$dir"/*; do
    [[ -f "$bin" && -x "$bin" ]] || continue
    local name
    name="$(basename "$bin")"

    [[ "$name" != .* ]] || continue
    case "$name" in
      npm|npx|node|python|python3|ruby|perl|lua|bash|zsh|sh|sh.dist|npm-cli|npx-cli) continue ;;
      corepack|corepack.exe|yarn|yarn.js|pnpm|pnpm.js|git|git-*) continue ;;
    esac

    TOOLS_RAW="${TOOLS_RAW}${name}|${origin}"$'\n'
  done
}

scan_tree() {
  local dir="$1"; local origin="$2"
  [[ -d "$dir" ]] && [[ -r "$dir" ]] || return 0
  local subdir
  for subdir in "$dir"/*/bin; do
    [[ -d "$subdir" ]] && scan_dir "$subdir" "$origin"
  done
}

# -------------------------------------------------------------------
# Full filesystem scan
# -------------------------------------------------------------------
full_scan() {
  TOOLS_RAW=""

  scan_dir "$HOME/.local/bin"                  "uv/pip"
  scan_dir "$HOME/.cargo/bin"                  "cargo"
  scan_dir "$HOME/.deno/bin"                  "deno"
  scan_dir "$HOME/.bun/install/cache/bin"      "bun"
  scan_dir "$HOME/.rbenv/shims"               "rbenv"
  scan_dir "$HOME/.pyenv/shims"               "pyenv"

  local npm_prefix
  npm_prefix=$(npm config get prefix 2>/dev/null || true)
  if [[ -n "$npm_prefix" ]] && [[ -d "$npm_prefix/lib/node_modules/.bin" ]]; then
    scan_dir "$npm_prefix/lib/node_modules/.bin" "npm"
  elif [[ -d "$HOME/.npm-global/lib/node_modules/.bin" ]]; then
    scan_dir "$HOME/.npm-global/lib/node_modules/.bin" "npm"
  fi

  if [[ "$(uname)" == "Darwin" ]]; then
    local brew_prefix
    brew_prefix=$(brew --prefix 2>/dev/null || true)
    if [[ -n "$brew_prefix" ]] && [[ -d "$brew_prefix/opt" ]]; then
      scan_tree "$brew_prefix/opt" "brew"
    fi
    [[ -d "/opt/homebrew/bin" ]] && scan_dir "/opt/homebrew/bin" "brew"
  fi

  local gem_home
  gem_home=$(gem env home 2>/dev/null || true)
  if [[ -n "$gem_home" ]] && [[ -d "$gem_home/bin" ]]; then
    scan_dir "$gem_home/bin" "gem"
  fi

  if [[ -d "$HOME/.sdkman/candidates" ]]; then
    local cand
    for cand in "$HOME/.sdkman/candidates"/*/current/bin/*; do
      [[ -f "$cand" && -x "$cand" ]] || continue
      local name
      name="$(basename "$cand")"
      [[ "$name" != .* ]] || continue
      TOOLS_RAW="${TOOLS_RAW}${name}|sdkman"$'\n'
    done
  fi

  [[ -d "$HOME/.local/share/venv" ]] && scan_tree "$HOME/.local/share/venv" "uv/venv"

  [[ -d "/usr/local/bin" ]] && scan_dir "/usr/local/bin" "manual"

  [[ -d "$HOME/.opencode/bin" ]] && scan_dir "$HOME/.opencode/bin" "opencode"

  [[ -d "$HOME/.fnm" ]] && TOOLS_RAW="${TOOLS_RAW}fnm|fnm"$'\n'

  if [[ -n "$SCAN_PATH" ]]; then
    local pdir
    IFS=':' read -ra _path_dirs <<< "${PATH:-}"
    for pdir in "${_path_dirs[@]}"; do
      [[ -n "$pdir" ]] || continue
      [[ -d "$pdir" ]] && scan_dir "$pdir" "path"
    done
  fi

  local tmpfile="${CACHE_FILE}.tmp.$$"
  local scanned_at
  scanned_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  mkdir -p "$(dirname "$CACHE_FILE")"
  mkdir -p "$LOG_DIR"

  echo "$TOOLS_RAW" | grep -v '^$' | sort -u | python3 -c "
import sys, json
lines = [l.strip() for l in sys.stdin if '|' in l]
tools = []
for line in lines:
    parts = line.split('|', 1)
    if len(parts) == 2:
        tools.append({'name': parts[0], 'origin': parts[1]})
tools.append({'scanned_at': '$scanned_at', 'count': len(tools)})
with open('$tmpfile', 'w') as f:
    json.dump(tools, f, indent=2)
" 2>/dev/null

  mv "$tmpfile" "$CACHE_FILE"
}

# -------------------------------------------------------------------
# Ensure cache is current (at most one full_scan per invocation)
# -------------------------------------------------------------------
ensure_cache() {
  if [[ -n "$NO_SCAN" ]] && [[ -z "$RESCAN" ]]; then
    if [[ -f "$CACHE_FILE" ]]; then
      info "Using cached discovery (--no-scan)."
      return 0
    fi
    warn "No cache at $CACHE_FILE — running discovery scan."
    info "Discovering installed tools..."
    full_scan
    return 0
  fi

  local cache_age=99999
  if [[ -f "$CACHE_FILE" ]]; then
    local modified
    if [[ "$(uname)" == "Darwin" ]]; then
      modified=$(stat -f "%m" "$CACHE_FILE" 2>/dev/null)
    else
      modified=$(stat -c "%Y" "$CACHE_FILE" 2>/dev/null)
    fi
    local now
    now=$(date +%s)
    if [[ -n "${modified:-}" ]] && [[ "$modified" =~ ^[0-9]+$ ]]; then
      cache_age=$((now - modified))
    fi
  fi

  if [[ -f "$CACHE_FILE" ]] && ((cache_age < CACHE_TTL_SECONDS)) && [[ -z "$RESCAN" ]]; then
    return 0
  fi

  info "Discovering installed tools..."
  full_scan
}

# -------------------------------------------------------------------
# Run an update command (bash -c instead of eval)
# -------------------------------------------------------------------
run_update() {
  local group="$1"
  local cmd="$2"

  if [[ -n "$DRY_RUN" ]]; then
    log "  [dry-run] $cmd"
    return 0
  fi

  [[ -z "$QUIET" ]] && log "  ${BOLD}→${NC} $cmd"

  local output ec=0
  if [[ -n "$TRACE" ]] && [[ -z "${SUPPRESS_TRACE:-}" ]]; then
    output=$(bash -x -c "$cmd" 2>&1) || ec=$?
  else
    output=$(bash -c "$cmd" 2>&1) || ec=$?
  fi

  if [[ $ec -eq 0 ]]; then
    ok "$group"
  else
    warn "$group failed (exit $ec)"
    [[ -z "$QUIET" ]] && echo "$output" | grep -v "^npm warn" | grep -v "^brew warn" | head -3 | sed 's/^/   /'
    return 1
  fi
  return 0
}

# -------------------------------------------------------------------
# Run emit lines (skip lines do not count toward ok/fail)
# -------------------------------------------------------------------
_run_one_emit_line() {
  local line="$1"
  local cmd_type name cmd
  IFS='|' read -r cmd_type name cmd <<< "$line"
  case "$cmd_type" in
    skip) return 3 ;;
    bulk)
      info "Updating all $name..."
      run_update "$name" "$cmd"
      ;;
    known)
      if is_skipped "$name"; then
        log "${BLUE}-- $name skipped${NC}"
        return 3
      fi
      info "Updating $name..."
      run_update "$name" "$cmd"
      ;;
  esac
}

run_updates_sequential() {
  local line
  for line in "$@"; do
    [[ -z "$line" ]] && continue
    _run_one_emit_line "$line"
    local ec=$?
    case "$ec" in
      0) ((UPDATE_OK++)) || true ;;
      3) ;;
      *) ((UPDATE_FAIL++)) || true ;;
    esac
  done
}

run_updates_parallel() {
  local max="$1"
  shift
  local pids=()
  local line
  local ec

  for line in "$@"; do
    [[ -z "$line" ]] && continue
    while (( ${#pids[@]} >= max )); do
      wait "${pids[0]}"
      ec=$?
      case "$ec" in
        0) ((UPDATE_OK++)) || true ;;
        3) ;;
        *) ((UPDATE_FAIL++)) || true ;;
      esac
      pids=("${pids[@]:1}")
    done
    (
      SUPPRESS_TRACE=1
      _run_one_emit_line "$line"
    ) &
    pids+=($!)
  done
  for _pid in "${pids[@]}"; do
    wait "$_pid"
    ec=$?
    case "$ec" in
      0) ((UPDATE_OK++)) || true ;;
      3) ;;
      *) ((UPDATE_FAIL++)) || true ;;
    esac
  done
}

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
main() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Missing config: $CONFIG_FILE" >&2
    exit 1
  fi
  if [[ ! -f "$LIB_SCRIPT" ]]; then
    echo "Missing library: $LIB_SCRIPT (install update-all-clis from the repo or copy lib_update_all_clis.py next to this script)" >&2
    exit 1
  fi

  UPDATE_OK=0
  UPDATE_FAIL=0

  # Machine-readable list must be the only thing on stdout
  [[ -n "$LIST_JSON" ]] && QUIET=1

  mkdir -p "$(dirname "$CACHE_FILE")"
  mkdir -p "$LOG_DIR"

  log "${BOLD}update-all-clis${NC} — dynamic discovery and update"
  log ""

  ensure_cache

  if [[ -n "$LIST_MODE" ]]; then
    if [[ -n "$LIST_JSON" ]]; then
      python3 "$LIB_SCRIPT" list-json "$CACHE_FILE"
      exit 0
    fi
    log "${BOLD}Discovered tools:${NC}"
    python3 -c "
import json, sys
with open('$CACHE_FILE') as f:
    data = json.load(f)
tools = sorted([t for t in data if 'name' in t], key=lambda x: x['name'])
meta = next((t for t in data if 'scanned_at' in t), None)
for t in tools:
    print(f\"  {t['name']}  [{t['origin']}]\")
print(f\"\nTotal: {len(tools)} tools  |  Scanned: {meta['scanned_at'] if meta else '?'}\")
" 2>/dev/null
    exit 0
  fi

  log ""
  log "${BOLD}=== Running updates ===${NC}"
  log ""

  export CONFIG_FILE
  export CONFIG_LOCAL_FILE
  export ONLY_ORIGINS
  export SKIP_ORIGINS

  local emit_tmp
  emit_tmp=$(mktemp)
  local -a lines=()
  if ! python3 "$LIB_SCRIPT" emit "$CACHE_FILE" > "$emit_tmp" 2>&1; then
    cat "$emit_tmp" >&2
    rm -f "$emit_tmp"
    exit 1
  fi
  while IFS= read -r line; do
    lines+=("$line")
  done < "$emit_tmp"
  rm -f "$emit_tmp"

  local _emit_snap="" _before_snap="" _after_snap=""
  if [[ -z "$DRY_RUN" ]] && { _want_notify_popup || [[ -n "${UPDATE_ALL_CLIS_SUMMARY_FILE:-}" ]]; }; then
    _emit_snap=$(mktemp)
    _before_snap=$(mktemp)
    _after_snap=$(mktemp)
    printf '%s\n' "${lines[@]}" > "$_emit_snap"
    python3 "$LIB_SCRIPT" snapshot-versions "$_emit_snap" > "$_before_snap" 2>/dev/null || true
  fi

  if (( PARALLEL_JOBS < 2 )); then
    run_updates_sequential "${lines[@]}"
  else
    run_updates_parallel "$PARALLEL_JOBS" "${lines[@]}"
  fi

  if [[ -n "$_emit_snap" ]]; then
    python3 "$LIB_SCRIPT" snapshot-versions "$_emit_snap" > "$_after_snap" 2>/dev/null || true
    if _want_notify_popup; then
      python3 "$LIB_SCRIPT" notify-diff "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" 2>/dev/null || true
    fi
    if [[ -n "${UPDATE_ALL_CLIS_SUMMARY_FILE:-}" ]]; then
      python3 "$LIB_SCRIPT" run-summary "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" > "${UPDATE_ALL_CLIS_SUMMARY_FILE}" 2>/dev/null || true
    fi
    rm -f "$_emit_snap" "$_before_snap" "$_after_snap"
  fi

  log ""
  log "${BOLD}=== Done! ===${NC}"
  log "Summary: ${UPDATE_OK} ok, ${UPDATE_FAIL} failed"
  log "Cache: $CACHE_FILE"
  log "Run './update_all_clis.sh --rescan' to force a fresh discovery scan."
  log "Run './update_all_clis.sh --list' to see all discovered tools."

  if [[ -n "$JSON_SUMMARY" ]]; then
    python3 -c "import json; print(json.dumps({'ok': $UPDATE_OK, 'failed': $UPDATE_FAIL}))"
  fi

  if [[ -n "$DRY_RUN" ]]; then
    exit 0
  fi
  if (( UPDATE_FAIL > 0 )); then
    exit 1
  fi
  exit 0
}

main
