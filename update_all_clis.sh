#!/usr/bin/env bash
# =============================================================================
# update-all-clis: Update all your CLIs, package managers, and developer tools
# Usage: ./update_all_clis.sh [--skip=manager1,manager2] [--quiet]
#
# Auto-detects what's installed and only updates what it finds.
# Works on macOS and Linux. No root required (uses --user-install where needed).
# =============================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
BOLD='\033[1m'

SKIP="${SKIP:-}"; QUIET="${QUIET:-}"

log()   { [[ -z "$QUIET" ]] && echo -e "$@"; }
info()  { log "${BOLD}==>${NC} $*"; }
warn()  { log "${YELLOW}!! $*${NC}"; }
err()   { log "${RED}!! $*${NC}"; }
ok()    { log "${GREEN}✓ $*${NC}"; }
skip()  { [[ ",${SKIP}," == *",$1,"* ]]; }
has()   { command -v "$1" &>/dev/null; }

# Detect the package manager / tool and run its update command.
# Each entry: name|command|install_hint
run_tool() {
  local name="$1"; shift
  local cmd="$1"; shift

  if skip "$name"; then
    log "${BLUE}-- $name skipped (--skip)${NC}"
    return 0
  fi

  # Some tools need their package name (for npm) to be checked separately
  local pkg="${1:-}"
  if [[ -n "$pkg" ]]; then
    if ! has "$name" && ! npm ls -g "$pkg" &>/dev/null 2>&1; then
      log "${YELLOW}-- $name not found, skipping${NC}"
      return 0
    fi
  elif ! has "$name"; then
    log "${YELLOW}-- $name not found, skipping${NC}"
    return 0
  fi

  info "Updating $name..."
  local output ec=0
  output=$(eval "$cmd" 2>&1) || ec=$?

  if [[ $ec -eq 0 ]]; then
    ok "$name updated"
  else
    err "$name failed (exit $ec)"
    [[ -n "$QUIET" ]] || echo "$output" | grep -v "^npm warn" | head -3 | sed 's/^/   /'
  fi
}

# -------------------------------------------------------------------
# Package managers
# -------------------------------------------------------------------
update_managers() {
  log ""
  log "${BOLD}=== Package managers ===${NC}"

  # ---- npm (Node.js) ----
  run_tool "npm" "npm update -g --ignore-scripts 2>&1 | grep -v 'cline-4g5O6Ovb' | grep -v 'npm warn' || true"

  # ---- pip3 (Python) ----
  run_tool "pip3" "pip3 list --outdated --format=freeze 2>/dev/null | grep -v '^\-e' | cut -d'=' -f1 | xargs -r pip3 install -U"

  # ---- Homebrew ----
  if [[ "$(uname)" == "Darwin" ]]; then
    run_tool "brew" "brew update && brew upgrade"
  fi

  # ---- Ruby Gems ----
  # Silences verbose compile errors for old Ruby versions (macOS system Ruby 2.6)
  run_tool "gem" "gem update --user-install 2>&1 | grep -v -E 'BUILD_RUBY_PLATFORM|fiddle|compiling|make:|error:|Error:|warnings generated' || true"

  # ---- Cargo (Rust) ----
  run_tool "cargo" "cargo install-update -a 2>/dev/null || cargo update"

  # ---- Conda ----
  run_tool "conda" "conda update --all -y"
}

# -------------------------------------------------------------------
# Third-party CLIs and agents
# -------------------------------------------------------------------
update_tools() {
  log ""
  log "${BOLD}=== Third-party CLIs and agents ===${NC}"

  # ---- Hermes ----
  run_tool "hermes" "hermes update"

  # ---- Cursor Agent ----
  run_tool "agent" "agent update"

  # ---- OpenCode ----
  run_tool "opencode" "opencode upgrade"

  # ---- uv (Python package installer) ----
  run_tool "uv" "uv self update"

  # ---- npm global packages ----
  run_tool "firecrawl"    "npm update -g firecrawl-cli"
  run_tool "mmx"         "npm update -g mmx-cli"
  run_tool "codex"       "npm update -g codex-cli"
  run_tool "dev-browser" "npm update -g dev-browser"
  run_tool "tinyfish"    "npm update -g tinyfish"
  run_tool "cline"       "npm update -g cline"

  # ---- Tools that don't have built-in update commands ----
  for tool in goose kimi kimi-cli bb browse cursor; do
    if has "$tool"; then
      log "${YELLOW}-- $tool has no auto-update command, skipping${NC}"
    fi
  done
}

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
summary() {
  log ""
  log "${BOLD}=== Done! ===${NC}"
  log "Run 'brew cleanup' periodically to reclaim disk space."
  log "Run 'npm cache clean --force' if npm update has stale cache errors."
  log ""
  log "To skip tools on this run: SKIP=npm,brew ./update_all_clis.sh"
  log "Quiet mode (errors only):     QUIET=1 ./update_all_clis.sh"
}

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
main() {
  log "${BOLD}update-all-clis${NC} — updating all your CLIs and package managers"
  log ""

  update_managers
  update_tools
  summary
}

main "$@"
