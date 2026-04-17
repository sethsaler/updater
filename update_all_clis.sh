#!/usr/bin/env bash
# =============================================================================
# update-all-clis: Dynamic discovery + update all CLIs and package managers
#
# Auto-discovers everything installed on your system, determines how to update
# each tool, and runs the right command. Results are cached for speed.
#
# Usage: ./update_all_clis.sh [--rescan] [--no-scan] [--skip=tool1,tool2] [--quiet] [--dry-run]
#   --rescan   Force a fresh discovery scan (default: use cache if < 24h old; wins over --no-scan)
#   --no-scan  Use existing cache even if older than 24h (fails if no cache)
#   --skip=    Comma-separated list of tool names to skip (overrides $SKIP)
#   --quiet    Only show errors
#   --dry-run  Show what would be updated without running
#   --list     Show discovered tools and exit
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/tool_config.json}"

CACHE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/cache.json"
LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/logs"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

SKIP="${SKIP:-}"
QUIET=""; DRY_RUN=""; RESCAN=""; LIST_MODE=""; NO_SCAN=""

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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip=*)     SKIP_CLI="${1#*=}"; shift ;;
    --quiet|-q)   QUIET=1; shift ;;
    --dry-run|-n) DRY_RUN=1; shift ;;
    --rescan|-r)  RESCAN=1; shift ;;
    --list|-l)    LIST_MODE=1; shift ;;
    --no-scan)    NO_SCAN=1; shift ;;
    --help|-h)    grep "^# " "$0" | sed 's/^# //'; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Try --help for usage." >&2
      exit 1
      ;;
  esac
done

SKIP="${SKIP_CLI:-$SKIP}"

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

    # Filter noise
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

  # npm global
  local npm_prefix
  npm_prefix=$(npm config get prefix 2>/dev/null || true)
  if [[ -n "$npm_prefix" ]] && [[ -d "$npm_prefix/lib/node_modules/.bin" ]]; then
    scan_dir "$npm_prefix/lib/node_modules/.bin" "npm"
  elif [[ -d "$HOME/.npm-global/lib/node_modules/.bin" ]]; then
    scan_dir "$HOME/.npm-global/lib/node_modules/.bin" "npm"
  fi

  # Homebrew Cellar
  if [[ "$(uname)" == "Darwin" ]]; then
    local brew_prefix
    brew_prefix=$(brew --prefix 2>/dev/null || true)
    if [[ -n "$brew_prefix" ]] && [[ -d "$brew_prefix/opt" ]]; then
      scan_tree "$brew_prefix/opt" "brew"
    fi
    [[ -d "/opt/homebrew/bin" ]] && scan_dir "/opt/homebrew/bin" "brew"
  fi

  # User gem bins
  local gem_home
  gem_home=$(gem env home 2>/dev/null || true)
  if [[ -n "$gem_home" ]] && [[ -d "$gem_home/bin" ]]; then
    scan_dir "$gem_home/bin" "gem"
  fi

  # SDKMAN candidates
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

  # uv venvs
  [[ -d "$HOME/.local/share/venv" ]] && scan_tree "$HOME/.local/share/venv" "uv/venv"

  # /usr/local/bin
  [[ -d "/usr/local/bin" ]] && scan_dir "/usr/local/bin" "manual"

  # ~/.opencode/bin — opencode
  [[ -d "$HOME/.opencode/bin" ]] && scan_dir "$HOME/.opencode/bin" "opencode"

  # fnm marker
  [[ -d "$HOME/.fnm" ]] && TOOLS_RAW="${TOOLS_RAW}fnm|fnm"$'\n'

  # Write cache via Python (avoids bash subshell issues)
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
    modified=$(stat -f "%m" "$CACHE_FILE" 2>/dev/null || stat -c "%Y" "$CACHE_FILE" 2>/dev/null)
    local now
    now=$(date +%s)
    cache_age=$((now - modified))
  fi

  if [[ -f "$CACHE_FILE" ]] && ((cache_age < 86400)) && [[ -z "$RESCAN" ]]; then
    return 0
  fi

  info "Discovering installed tools..."
  full_scan
}

# -------------------------------------------------------------------
# Run an update command
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
  output=$(eval "$cmd" 2>&1) || ec=$?

  if [[ $ec -eq 0 ]]; then
    ok "$group"
  else
    warn "$group failed (exit $ec)"
    [[ -z "$QUIET" ]] && echo "$output" | grep -v "^npm warn" | grep -v "^brew warn" | head -3 | sed 's/^/   /'
  fi
}

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
main() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Missing config: $CONFIG_FILE" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$CACHE_FILE")"
  mkdir -p "$LOG_DIR"

  log "${BOLD}update-all-clis${NC} — dynamic discovery and update"
  log ""

  ensure_cache

  if [[ -n "$LIST_MODE" ]]; then
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

  # Python reads cache + tool_config.json, deduplicates by group, emits shell commands.
  # Each line: "cmd_type|name|command"
  export CONFIG_FILE
  python3 -c "
import json, os, sys

config_path = os.environ['CONFIG_FILE']
with open(config_path) as f:
    cfg = json.load(f)
SELF_CMD = cfg['known']
BULK_ORIGINS = cfg['bulk']
KNOWN = set(SELF_CMD.keys())

with open('$CACHE_FILE') as f:
    data = json.load(f)

tools = [t for t in data if 'name' in t]
seen_bulk = set()
for t in tools:
    name   = t['name']
    origin = t.get('origin', '?')

    if name in KNOWN:
        cmd = SELF_CMD[name]
        sys.stdout.write(f'known|{name}|{cmd}\n')
        seen_bulk.add(origin)
        continue

    if origin in BULK_ORIGINS and origin not in seen_bulk:
        seen_bulk.add(origin)
        sys.stdout.write(f'bulk|{origin}|{BULK_ORIGINS[origin]}\n')
        continue

    sys.stdout.write(f'skip|{name}|\n')
" 2>/dev/null | \
  while IFS='|' read -r cmd_type name cmd; do

    case "$cmd_type" in
      skip) continue ;;
      bulk)
        info "Updating all $name..."
        run_update "$name" "$cmd"
        ;;
      known)
        if is_skipped "$name"; then
          log "${BLUE}-- $name skipped${NC}"
          continue
        fi
        info "Updating $name..."
        run_update "$name" "$cmd"
        ;;
    esac
  done

  log ""
  log "${BOLD}=== Done! ===${NC}"
  log "Cache: $CACHE_FILE"
  log "Run './update_all_clis.sh --rescan' to force a fresh discovery scan."
  log "Run './update_all_clis.sh --list' to see all discovered tools."
}

main
