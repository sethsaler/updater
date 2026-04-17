#!/usr/bin/env bash
# =============================================================================
# update-all-clis: Dynamic discovery + update all CLIs and package managers
#
# Auto-discovers everything installed on your system, determines how to update
# each tool, and runs the right command. Results are cached for speed.
#
# Usage: ./update_all_clis.sh [--rescan] [--skip=tool1,tool2] [--quiet] [--dry-run]
#   --rescan   Force a fresh discovery scan (default: use cache if < 24h old)
#   --skip=    Comma-separated list of tool names to skip
#   --quiet    Only show errors
#   --dry-run  Show what would be updated without running
#   --list     Show discovered tools and exit
# =============================================================================

CACHE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/cache.json"
LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/logs"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

SKIP=""; QUIET=""; DRY_RUN=""; RESCAN=""; LIST_MODE=""

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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip=*)    SKIP="${1#*=}"; shift ;;
    --quiet|-q)  QUIET=1; shift ;;
    --dry-run|-n) DRY_RUN=1; shift ;;
    --rescan|-r) RESCAN=1; shift ;;
    --list|-l)   LIST_MODE=1; shift ;;
    --help|-h)   grep "^# " "$0" | sed 's/^# //'; exit 0 ;;
    *)           shift ;;
  esac
done

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
# Ensure cache is current
# -------------------------------------------------------------------
ensure_cache() {
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
  local group="$1"; shift
  local cmd="$*"

  if [[ -n "$DRY_RUN" ]]; then
    log "  [dry-run] $cmd"
    return 0
  fi

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
# Self-update command for known standalone tools
# -------------------------------------------------------------------
self_update() {
  local name="$1"
  case "$name" in
    hermes)           echo "hermes update" ;;
    agent)            echo "agent update" ;;
    opencode)         echo "opencode upgrade" ;;
    uv)               echo "uv self update && uv tool update-all" ;;
    firecrawl)        echo "npm update -g firecrawl-cli" ;;
    mmx|mmx-cli)    echo "npm update -g mmx-cli" ;;
    codex|codex-cli) echo "npm update -g codex-cli" ;;
    dev-browser)      echo "npm update -g dev-browser" ;;
    tinyfish)         echo "npm update -g tinyfish" ;;
    cline)            echo "npm update -g cline" ;;
    ollama)           echo "ollama update 2>/dev/null || true" ;;
    warp)             echo "warp-cli update 2>/dev/null || true" ;;
    gh)               echo "gh auth refresh 2>/dev/null || gh upgrade 2>/dev/null || true" ;;
    goose)            echo "goose update 2>/dev/null || true" ;;
    kimi|kimi-cli)  echo "kimi update 2>/dev/null || true" ;;
    *)                echo "" ;;
  esac
}

# -------------------------------------------------------------------
# Bulk update command for a package-manager group
# -------------------------------------------------------------------
bulk_update() {
  local origin="$1"
  case "$origin" in
    npm)                echo "npm update -g --ignore-scripts 2>&1 | grep -v 'cline-4g5O6Ovb' | grep -v 'npm warn' || true" ;;
    cargo)              echo "cargo install-update -a 2>/dev/null || cargo update" ;;
    gem)                echo "gem update --user-install 2>&1 | grep -v -E 'BUILD_RUBY_PLATFORM|fiddle|compiling|make:|error:|Error:|warnings generated' || true" ;;
    brew)               echo "brew update && brew upgrade" ;;
    conda)              echo "conda update --all -y" ;;
    uv|uv/pip|uv/venv) echo "uv self update && uv tool update-all" ;;
    fnm)                echo "fnm update 2>/dev/null || true" ;;
    bun)                echo "bun update 2>/dev/null || true" ;;
    *)                  echo "" ;;
  esac
}

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
main() {
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

  info "Scanning for newly installed tools..."
  full_scan

  log ""
  log "${BOLD}=== Running updates ===${NC}"
  log ""

  # Python reads cache, deduplicates by group, and emits shell commands.
  # Each line: "cmd_type|name|command"
  #   cmd_type = "known"   -> standalone known tool with self-update
  #   cmd_type = "bulk"    -> bulk update for a group
  #   cmd_type = "skip"    -> unknown standalone, skip silently
  python3 -c "
import json, sys

with open('$CACHE_FILE') as f:
    data = json.load(f)

tools = [t for t in data if 'name' in t]
KNOWN = {
    'hermes','agent','opencode','uv','firecrawl','mmx','mmx-cli',
    'codex','codex-cli','dev-browser','tinyfish','cline',
    'ollama','warp','gh','goose','kimi','kimi-cli',
}
SELF_CMD = {
    'hermes':      'hermes update',
    'agent':       'agent update',
    'opencode':    'opencode upgrade',
    'uv':          'uv self update && uv tool update-all',
    'firecrawl':   'npm update -g firecrawl-cli',
    'mmx':         'npm update -g mmx-cli',
    'mmx-cli':     'npm update -g mmx-cli',
    'codex':       'npm update -g codex-cli',
    'codex-cli':   'npm update -g codex-cli',
    'dev-browser': 'npm update -g dev-browser',
    'tinyfish':    'npm update -g tinyfish',
    'cline':       'npm update -g cline',
    'ollama':      'ollama update 2>/dev/null || true',
    'warp':        'warp-cli update 2>/dev/null || true',
    'gh':          'gh auth refresh 2>/dev/null || gh upgrade 2>/dev/null || true',
    'goose':       'goose update 2>/dev/null || true',
    'kimi':        'kimi update 2>/dev/null || true',
    'kimi-cli':    'kimi update 2>/dev/null || true',
}
BULK_ORIGINS = {
    'npm':         'npm update -g --ignore-scripts 2>&1 | grep -v \"cline-4g5O6Ovb\" | grep -v \"npm warn\" || true',
    'cargo':       'cargo install-update -a 2>/dev/null || cargo update',
    'gem':         'gem update --user-install 2>&1 | grep -v -E \"BUILD_RUBY_PLATFORM|fiddle|compiling|make:|error:|Error:|warnings generated\" || true',
    'brew':        'brew update && brew upgrade',
    'manual':      'brew upgrade 2>/dev/null || true',
    'conda':       'conda update --all -y',
    'uv':          'uv self update && uv tool update-all',
    'uv/pip':      'uv self update && uv tool update-all',
    'uv/venv':     'uv self update && uv tool update-all',
    'fnm':         'fnm update 2>/dev/null || true',
    'bun':         'bun update 2>/dev/null || true',
}
# Track which bulk origins we've already emitted
seen_bulk = set()
for t in tools:
    name   = t['name']
    origin = t.get('origin', '?')

    # 1. Known standalone tools — always handle these
    if name in KNOWN:
        cmd = SELF_CMD[name]
        sys.stdout.write(f'known|{name}|{cmd}\n')
        # Also mark this tool's origin as handled so we don't emit a bulk group for it
        seen_bulk.add(origin)
        continue

    # 2. Package-manager bulk groups — emit once per origin
    if origin in BULK_ORIGINS and origin not in seen_bulk:
        seen_bulk.add(origin)
        sys.stdout.write(f'bulk|{origin}|{BULK_ORIGINS[origin]}\n')
        continue

    # 3. Tools with no update mechanism — skip silently
    sys.stdout.write(f'skip|{name}|\n')
" 2>/dev/null | \
  while IFS='|' read -r cmd_type name cmd; do

    case "$cmd_type" in
      skip) continue ;;
      bulk)
        info "Updating all $name..."
        run_update "$name" $cmd
        ;;
      known)
        if is_skipped "$name"; then
          log "${BLUE}-- $name skipped${NC}"
          continue
        fi
        info "Updating $name..."
        run_update "$name" $cmd
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
