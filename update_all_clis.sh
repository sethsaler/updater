#!/usr/bin/env bash
# =============================================================================
# update-all-clis: Dynamic discovery + update all CLIs and package managers
#
# Usage: ./update_all_clis.sh [options]
#   --rescan          Force a fresh discovery scan (default behavior)
#   --no-scan         Use existing cache instead of scanning
#                     (set CACHE_TTL_HOURS=N to reuse a cache newer than N hours)
#   --skip=a,b        Skip known tools (overrides $SKIP)
#   --only-origins=   Only run bulk/known matching these origins or names
#   --skip-origins=   Skip bulk (and known) for these origins
#   --no-scan-path    Skip scanning directories on $PATH (origin: path)
#   --parallel=N      Run up to N updates concurrently (default 8)
#   --job-timeout=N   Kill any single update still running after N seconds
#                     (default 900; 0 disables; also: UAC_JOB_TIMEOUT=N).
#                     A killed job counts as failed; other updates continue.
#   --json-summary    Print JSON ok/failed counts on stdout after run
#   --list --json     Machine-readable tool list (with --list)
#   --report-unknown  Show tools discovered with no update path
#   --ack-unknown=X   Dismiss a tool from the unknown report
#   --suggest-known   Show tools updated via bulk but not in known list
#   --trace           Trace shell commands (bash -x)
#   --dry-run         Show commands without running
#   --json-plan       Print planned updates as JSON and exit
#   --notify          Show the desktop summary dialog (non-blocking, opt-in)
#                     (also: UPDATE_ALL_CLIS_NOTIFY=1; default is silent)
#   --history[=N]     Show the last N runs from history.jsonl (default 3) and exit
#   --include-quarantined  Force quarantined tools/origins to run this run
#                     (also: UAC_INCLUDE_QUARANTINED=1)
#                     (quarantine threshold: UAC_QUARANTINE_AFTER, default 3, 0 disables)
#   --no-precheck     Skip outdated pre-checks; always run every bulk update
#                     (also: UAC_NO_PRECHECK=1)
#   --hold=a,b        Add tools/origins to the persistent hold list (config.local.json) and exit
#   --unhold=a,b      Remove tools/origins from the persistent hold list and exit
#                     (one-run ad hoc hold: HOLD=a,b ./update_all_clis.sh)
#   --doctor          Read-only diagnostics: broken symlinks, shadowed duplicates,
#                     chronic failures, config issues, cache health (--doctor --json for JSON)
#   --changelog       After a real run, fetch best-effort release notes for tools that
#                     changed version and have a "repos" mapping (also: UPDATE_ALL_CLIS_CHANGELOG=1)
#   --self-update     Before planning, `git pull --ff-only` this script's own repo checkout
#                     and re-exec once if it updated (also: UPDATE_ALL_CLIS_SELF_UPDATE=1)
#                     Off by default; any failure (dirty tree, no network, diverged,
#                     not a git checkout) warns and continues — never fails the run.
#   --tui             Force the live TUI dashboard on for the update run
#   --no-tui          Force the live TUI dashboard off (plain log output)
#                     (default: on when stdout is an interactive terminal and
#                     tui_update_all_clis.py is present; also: UAC_TUI=1|0.
#                     Always off for --dry-run/--quiet/--trace/--list/JSON modes
#                     and non-terminals: LaunchAgent/CI runs are unaffected.)
#   --version         Print version and exit
# =============================================================================

UAC_VERSION="0.9.0"

set -uo pipefail

# Preserve the original argv for a self-update re-exec (arg parsing below
# consumes "$@" via shift, so it must be captured before that happens).
_UAC_ORIG_ARGS=("$@")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_SCRIPT="${LIB_SCRIPT:-$SCRIPT_DIR/lib_update_all_clis.py}"
TUI_SCRIPT="${TUI_SCRIPT:-$SCRIPT_DIR/tui_update_all_clis.py}"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/tool_config.json}"
CONFIG_LOCAL_FILE="${CONFIG_LOCAL_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/config.local.json}"

CACHE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/cache.json"
LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/logs"
UNKNOWN_LOG_FILE="${UNKNOWN_LOG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/unknown_tools.json}"
LOCK_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/locks"
HISTORY_FILE="${UPDATE_ALL_CLIS_HISTORY_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/history.jsonl}"

# Quarantine: a job (known tool or bulk origin) that failed its last N
# consecutive appearances in history.jsonl is skipped by default. 0 disables.
UAC_QUARANTINE_AFTER="${UAC_QUARANTINE_AFTER:-3}"

# Per-job watchdog: an update command still running after this many seconds
# is killed (whole process tree) and counted as failed, so one wedged update
# (e.g. a cask upgrade waiting on an open app) can't stall the rest of the
# run. 0 disables. Override per-run with --job-timeout=N.
UAC_JOB_TIMEOUT="${UAC_JOB_TIMEOUT:-900}"

# Default 0: every run does a fresh discovery scan so new installs are
# always picked up. Set CACHE_TTL_HOURS=N to reuse a recent cache instead.
CACHE_TTL_HOURS="${CACHE_TTL_HOURS:-0}"
CACHE_TTL_SECONDS=$((CACHE_TTL_HOURS * 3600))

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

# Color output configuration
if [[ -n "${NO_COLOR:-}" ]] || [[ "${TERM:-}" == "dumb" ]]; then
  GREEN='' YELLOW='' BLUE='' BOLD='' NC=''
fi

SKIP="${SKIP:-}"
ONLY_ORIGINS="${ONLY_ORIGINS:-}"
SKIP_ORIGINS="${SKIP_ORIGINS:-}"
QUIET=""; DRY_RUN=""; RESCAN=""; LIST_MODE=""; NO_SCAN=""
LIST_JSON=""; JSON_SUMMARY=""; TRACE=""
SCAN_PATH=1; NO_SCAN_PATH=""; PARALLEL_JOBS=8; NOTIFY=""
REPORT_UNKNOWN=""; ACK_UNKNOWN=""; HEALTH_CHECK=""
SUGGEST_KNOWN=""; JSON_PLAN=""; VERBOSE=""; VALIDATE_CACHE=""; DEBUG_CACHE=""
HISTORY_MODE=""; HISTORY_N=3
INCLUDE_QUARANTINED="${UAC_INCLUDE_QUARANTINED:-}"
NO_PRECHECK="${UAC_NO_PRECHECK:-}"
HOLD_ADD=""; HOLD_REMOVE=""; DOCTOR_MODE=""
HOLD="${HOLD:-}"
CHANGELOG="${UPDATE_ALL_CLIS_CHANGELOG:-}"
SELF_UPDATE="${UPDATE_ALL_CLIS_SELF_UPDATE:-}"
# Live TUI dashboard for the update run: "auto" (on for interactive
# terminals), "1" (forced), "0" (disabled). See _tui_wanted.
TUI_MODE="${UAC_TUI:-auto}"

# Background-job bookkeeping for the cleanup trap (parallel updates + locks).
_UAC_PIDS=()
# Job-result records (kind\x1ename\x1ecmd\x1eec\x1estart\x1eend) collected
# during this run, fed to `history-append` afterward (Feature: run history).
declare -a _UAC_RESULT_LINES=()
_UAC_SEP=$'\x1e'

# -------------------------------------------------------------------
# Logging helpers
# -------------------------------------------------------------------
log()   { [[ -z "$QUIET" ]] && echo -e "$@"; }
info()  { log "${BOLD}==>${NC} $*"; }
ok()    { log "${GREEN}✓${NC} $*"; }
warn()  { log "${YELLOW}!!${NC} $*"; }
debug() { [[ -n "$VERBOSE" ]] && echo -e "${BLUE}[DEBUG]${NC} $*" >&2; }

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
# Cleanup: kill background update jobs + release locks on exit/interrupt
# -------------------------------------------------------------------
_kill_tree() {
  # Recursively kill a process and all its descendants (pgrep is available
  # on both macOS and Linux). Ensures in-flight brew/npm/cargo updates
  # don't survive a Ctrl+C as orphans.
  local parent="$1"
  [[ "$parent" =~ ^[0-9]+$ ]] || return 0
  local child
  while IFS= read -r child; do
    [[ -n "$child" ]] && _kill_tree "$child"
  done < <(pgrep -P "$parent" 2>/dev/null)
  kill "$parent" 2>/dev/null || true
}

# shellcheck disable=SC2317,SC2329  # invoked indirectly via the EXIT/INT/TERM trap
_cleanup() {
  # Stop background update subshells and any commands they spawned.
  local _pid
  for _pid in "${_UAC_PIDS[@]:-}"; do
    [[ -n "$_pid" ]] && _kill_tree "$_pid"
  done
  # Catch any background children not tracked in _UAC_PIDS.
  pkill -P $$ 2>/dev/null || true
  # Remove the lock directory: releases the single-instance run.lockdir and
  # any per-origin job lockdirs (mkdir-based; see _run_with_mkdir_lock).
  if [[ -d "$LOCK_DIR" ]]; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
  fi
}
trap _cleanup EXIT INT TERM

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
    --report-unknown)  REPORT_UNKNOWN=1; shift ;;
    --ack-unknown=*)   ACK_UNKNOWN="${1#*=}"; shift ;;
    --trace)           TRACE=1; shift ;;
    --scan-path)       SCAN_PATH=1; shift ;;
    --no-scan-path)    NO_SCAN_PATH=1; shift ;;
    --json-plan)       JSON_PLAN=1; shift ;;
    --verbose|-v)      VERBOSE=1; shift ;;
    --no-color)        export NO_COLOR=1; GREEN='' YELLOW='' BLUE='' BOLD='' NC=''; shift ;;
    --health-check)    HEALTH_CHECK=1; shift ;;
    --validate-cache)  VALIDATE_CACHE=1; shift ;;
    --debug-cache)     DEBUG_CACHE=1; shift ;;
    --suggest-known)   SUGGEST_KNOWN=1; shift ;;
    --notify)          NOTIFY=1; shift ;;
    --history)         HISTORY_MODE=1; shift ;;
    --history=*)       HISTORY_MODE=1; HISTORY_N="${1#*=}"; shift ;;
    --include-quarantined) INCLUDE_QUARANTINED=1; shift ;;
    --no-precheck)     NO_PRECHECK=1; shift ;;
    --job-timeout=*)   UAC_JOB_TIMEOUT="${1#*=}"; shift ;;
    --hold=*)          HOLD_ADD="${1#*=}"; shift ;;
    --unhold=*)        HOLD_REMOVE="${1#*=}"; shift ;;
    --doctor)          DOCTOR_MODE=1; shift ;;
    --changelog)       CHANGELOG=1; shift ;;
    --self-update)     SELF_UPDATE=1; shift ;;
    --tui)             TUI_MODE="1"; shift ;;
    --no-tui)          TUI_MODE="0"; shift ;;
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
if ! [[ "$UAC_JOB_TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "Invalid --job-timeout / UAC_JOB_TIMEOUT value (use seconds, 0 disables): $UAC_JOB_TIMEOUT" >&2
  exit 1
fi

# -------------------------------------------------------------------
# Desktop summary is opt-in only so the terminal never blocks/hangs.
# Enable with --notify or UPDATE_ALL_CLIS_NOTIFY=1.
# Scheduled LaunchAgent/systemd set UPDATE_ALL_CLIS_NO_NOTIFY=1.
# -------------------------------------------------------------------
_want_notify_popup() {
  [[ "${UPDATE_ALL_CLIS_NO_NOTIFY:-}" == "1" ]] && return 1
  case "${UPDATE_ALL_CLIS_NOTIFY:-}" in
    1) return 0 ;;
    0) return 1 ;;
  esac
  [[ -n "$NOTIFY" ]]
}

# -------------------------------------------------------------------
# Directory scan planning — incremental by default (see plan-scan-rows
# below): rather than list every directory's contents ourselves in bash,
# we record (dir, origin, mode, exists) rows and hand them to Python, which
# skips re-listing any directory whose mtime hasn't changed since the last
# scan (reusing that directory's previously cached tools instead). This is
# what makes repeated `--list`/runs with nothing newly installed cheap.
# `scan_dir`/`scan_tree` below are kept only as the (rare) direct-append
# path for entries that aren't worth directory-gating (single-CLI managers
# like fnm, rustup, gcloud, mas, tlmgr — a command-existence check, not a
# directory of binaries).
# -------------------------------------------------------------------
declare -a TOOLS_ARRAY=()
declare -a _SCAN_ROWS=()

_scan_row() {
  local dir="$1" origin="$2" mode="$3"
  local exists=0
  [[ -d "$dir" ]] && exists=1
  _SCAN_ROWS+=("${dir}"$'\t'"${origin}"$'\t'"${mode}"$'\t'"${exists}")
}

# -------------------------------------------------------------------
# Full filesystem scan (incremental unless --rescan / RESCAN is set)
# -------------------------------------------------------------------
full_scan() {
  TOOLS_ARRAY=()
  _SCAN_ROWS=()
  debug "Starting discovery scan (incremental: dirs whose mtime is unchanged are not re-listed)"

  _scan_row "$HOME/.local/bin"             "uv/pip" "dir"
  _scan_row "$HOME/.cargo/bin"             "cargo"  "dir"
  _scan_row "$HOME/.deno/bin"              "deno"   "dir"
  _scan_row "$HOME/.bun/bin"               "bun"    "dir"
  _scan_row "$HOME/.bun/install/cache/bin" "bun"    "dir"
  _scan_row "$HOME/.rbenv/shims"           "rbenv"  "dir"
  _scan_row "$HOME/.pyenv/shims"           "pyenv"  "dir"

  # Combine npm calls into single subprocess for efficiency. `npm ls -g
  # --json` itself still runs every scan (it's a manager query, not a
  # filesystem walk we can mtime-gate cheaply); the directories it and npm's
  # fixed locations resolve to ARE mtime-gated below like everything else.
  local npm_info
  npm_info=$(npm config get prefix 2>/dev/null; npm root -g 2>/dev/null; npm ls -g --depth=0 --json 2>/dev/null || true)
  local npm_prefix npm_root npm_globals
  npm_prefix=$(echo "$npm_info" | head -1)
  npm_root=$(echo "$npm_info" | head -2 | tail -1)
  npm_globals=$(echo "$npm_info" | tail -n +3)

  if [[ -n "$npm_prefix" ]]; then
    _scan_row "$npm_prefix/bin" "npm" "dir"
    _scan_row "$npm_prefix/lib/node_modules/.bin" "npm" "dir"
  fi
  _scan_row "$HOME/.npm-global/lib/node_modules/.bin" "npm" "dir"
  _scan_row "$HOME/.npm-global/bin" "npm" "dir"

  if [[ -n "$npm_root" ]]; then
    local _npm_bin_dir="$npm_root/.bin"
    local _prefix_bin_dir=""
    [[ -n "$npm_prefix" ]] && _prefix_bin_dir="$npm_prefix/lib/node_modules/.bin"
    if [[ "$_npm_bin_dir" != "$_prefix_bin_dir" ]]; then
      _scan_row "$_npm_bin_dir" "npm" "dir"
    fi
  fi

  if [[ -n "$npm_globals" ]]; then
    local npm_global_dir
    npm_global_dir=$(echo "$npm_globals" | python3 "$LIB_SCRIPT" parse-npm-globals 2>/dev/null)
    if [[ -n "$npm_global_dir" ]]; then
      IFS='|' read -ra pkg_dirs <<< "$npm_global_dir"
      for pkg_dir in "${pkg_dirs[@]}"; do
        [[ -n "$pkg_dir" ]] || continue
        local bin_dir="${pkg_dir}/.bin"
        case "$bin_dir" in
          "$HOME/.npm-global/lib/node_modules/.bin"|"$npm_prefix/lib/node_modules/.bin"|"$npm_root/.bin") continue ;;
        esac
        _scan_row "$bin_dir" "npm" "dir"
      done
    fi
  fi

  local go_bin_dir=""
  if command -v brew >/dev/null 2>&1; then
    local brew_prefix
    brew_prefix=$(brew --prefix 2>/dev/null || true)
    if [[ -n "$brew_prefix" ]]; then
      # Cellar/opt is scanned one level deep (each formula's own bin dir);
      # we only mtime-gate at the "opt" level (see incremental_scan_merge's
      # docstring for why that's an acceptable trade-off vs a full walk).
      _scan_row "$brew_prefix/opt" "brew" "tree"
    fi
  fi
  _scan_row "/opt/homebrew/bin" "brew" "dir"
  _scan_row "/home/linuxbrew/.linuxbrew/bin" "brew" "dir"

  if command -v go >/dev/null 2>&1; then
    go_bin_dir="$(go env GOPATH 2>/dev/null)/bin"
    [[ -n "$go_bin_dir" ]] && _scan_row "$go_bin_dir" "go" "dir"
  fi
  [[ -n "${GOBIN:-}" ]] && _scan_row "$GOBIN" "go" "dir"
  _scan_row "$HOME/go/bin" "go" "dir"

  local conda_base
  for conda_base in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" "$HOME/miniforge3" "$HOME/micromamba"; do
    _scan_row "$conda_base/bin" "conda" "dir"
  done

  if [[ -d "$HOME/.nvm/versions/node" ]]; then
    local nvm_bin
    for nvm_bin in "$HOME/.nvm/versions/node"/*/bin; do
      [[ -d "$nvm_bin" ]] && _scan_row "$nvm_bin" "npm" "dir"
    done
  fi

  if [[ -d "$HOME/.local/pipx/venvs" ]]; then
    local pipx_bin
    for pipx_bin in "$HOME/.local/pipx/venvs"/*/bin; do
      [[ -d "$pipx_bin" ]] && _scan_row "$pipx_bin" "pipx" "dir"
    done
  fi

  local gem_home
  gem_home=$(gem env home 2>/dev/null || true)
  [[ -n "$gem_home" ]] && _scan_row "$gem_home/bin" "gem" "dir"

  # sdkman's layout (candidates/*/current/bin/*) is gated at the top-level
  # "candidates" dir; adding/removing a candidate changes its mtime.
  _scan_row "$HOME/.sdkman/candidates" "sdkman" "sdkman"

  _scan_row "$HOME/.local/share/venv" "uv/venv" "tree"

  _scan_row "/usr/local/bin" "manual" "dir"

  _scan_row "$HOME/.opencode/bin" "opencode" "dir"

  _scan_row "$HOME/.grok/bin" "grok" "dir"

  _scan_row "$HOME/bin" "manual" "dir"

  if [[ -n "${PNPM_HOME:-}" ]]; then
    _scan_row "$PNPM_HOME" "npm" "dir"
  else
    _scan_row "$HOME/.local/share/pnpm/bin" "npm" "dir"
  fi

  local _npm_packages="$HOME/.npm-packages/bin"
  if [[ -z "$npm_prefix" ]] || [[ "${npm_prefix}/lib/node_modules/.bin" != "$_npm_packages" ]]; then
    _scan_row "$_npm_packages" "npm" "dir"
  fi

  _scan_row "$HOME/.config/yarn/global/node_modules/.bin" "npm" "dir"

  _scan_row "$HOME/.dotnet/tools" "dotnet" "dir"

  _scan_row "$HOME/.krew/bin" "krew" "dir"

  _scan_row "$HOME/.local/share/mise/shims" "mise" "dir"
  _scan_row "$HOME/.local/share/mise/installs" "mise" "tree"

  _scan_row "/opt/local/bin" "manual" "dir"
  _scan_row "$HOME/.wasmtime/bin" "manual" "dir"
  _scan_row "$HOME/.wasmer/bin" "manual" "dir"

  # macOS: pip install --user lands binaries in ~/Library/Python/3.x/bin
  if [[ "$(uname)" == "Darwin" ]]; then
    local _pyuser_bin
    for _pyuser_bin in "$HOME"/Library/Python/3.*/bin; do
      [[ -d "$_pyuser_bin" ]] && _scan_row "$_pyuser_bin" "pip" "dir"
    done
  fi

  # Version managers and tool directories not covered above
  _scan_row "$HOME/.volta/bin" "volta" "dir"
  _scan_row "$HOME/.asdf/shims" "asdf" "dir"
  _scan_row "$HOME/.proto/bin" "proto" "dir"
  _scan_row "$HOME/.rye/shims" "rye" "dir"
  _scan_row "$HOME/.local/share/rye/shims" "rye" "dir"
  _scan_row "$HOME/.foundry/bin" "foundry" "dir"
  _scan_row "$HOME/.aqua/bin" "aqua" "dir"
  _scan_row "$HOME/.local/share/aquaproj-aqua/bin" "aqua" "dir"
  _scan_row "$HOME/.local/share/nvim/mason/bin" "mason" "dir"

  [[ -d "$HOME/.fnm" ]] && TOOLS_ARRAY+=("fnm|fnm")

  # rustup/gcloud/mas/tlmgr are single system-wide CLIs, not directories of
  # installed binaries, so (like fnm above) they're a direct TOOLS_ARRAY
  # append gated on command existence rather than a scanned/mtime-gated dir.
  # Each is a silent no-op on machines where the tool isn't installed.
  command -v rustup >/dev/null 2>&1 && TOOLS_ARRAY+=("rustup|rustup")
  command -v gcloud >/dev/null 2>&1 && TOOLS_ARRAY+=("gcloud|gcloud")
  command -v mas >/dev/null 2>&1 && TOOLS_ARRAY+=("mas|mas")
  command -v tlmgr >/dev/null 2>&1 && TOOLS_ARRAY+=("tlmgr|tlmgr")

  if [[ -n "$SCAN_PATH" ]] && [[ -z "$NO_SCAN_PATH" ]]; then
    local pdir
    IFS=':' read -ra _path_dirs <<< "${PATH:-}"
    for pdir in "${_path_dirs[@]}"; do
      [[ -n "$pdir" ]] || continue
      case "$pdir" in
        /usr/bin|/bin|/sbin|/usr/sbin|/usr/libexec|/System/*|/nix/*|/run/current-system/sw/bin) continue ;;
        "$HOME/bin"|"$HOME/.local/bin"|"$HOME/.cargo/bin"|"$HOME/.deno/bin"|"$HOME/.bun/bin"|"$HOME/.bun/install/cache/bin"|"$HOME/.rbenv/shims"|"$HOME/.pyenv/shims"|"$HOME/.opencode/bin"|"$HOME/.grok/bin"|"/opt/homebrew/bin"|"/home/linuxbrew/.linuxbrew/bin"|"/usr/local/bin"|"$HOME/go/bin"|"$HOME/.volta/bin"|"$HOME/.asdf/shims"|"$HOME/.proto/bin"|"$HOME/.rye/shims"|"$HOME/.local/share/rye/shims"|"$HOME/.foundry/bin"|"$HOME/.aqua/bin"|"$HOME/.local/share/aquaproj-aqua/bin"|"$HOME/.local/share/nvim/mason/bin"|"$HOME/.dotnet/tools"|"$HOME/.krew/bin"|"$HOME/.local/share/mise/shims"|"$HOME/.wasmtime/bin"|"$HOME/.wasmer/bin") continue ;;
        "$HOME"/Library/Python/3.*/bin) continue ;;
      esac
      [[ -n "${go_bin_dir:-}" ]] && [[ "$pdir" == "$go_bin_dir" ]] && continue
      [[ -n "${GOBIN:-}" ]] && [[ "$pdir" == "$GOBIN" ]] && continue
      _scan_row "$pdir" "path" "dir"
    done
  fi

  local scanned_at
  scanned_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  mkdir -p "$(dirname "$CACHE_FILE")"
  mkdir -p "$LOG_DIR"

  local rows_file extra_file tmpfile
  rows_file=$(mktemp)
  extra_file=$(mktemp)
  tmpfile="${CACHE_FILE}.tmp.$$"
  # Guard against `set -u` treating a zero-element array expansion as an
  # unbound variable (TOOLS_ARRAY is usually empty now that directory
  # scanning goes through _SCAN_ROWS instead of direct appends).
  : > "$rows_file"
  (( ${#_SCAN_ROWS[@]} > 0 )) && printf '%s\n' "${_SCAN_ROWS[@]}" > "$rows_file"
  : > "$extra_file"
  (( ${#TOOLS_ARRAY[@]} > 0 )) && printf '%s\n' "${TOOLS_ARRAY[@]}" > "$extra_file"

  local force_flag=""
  [[ -n "$RESCAN" ]] && force_flag="1"

  debug "Planning scan over ${#_SCAN_ROWS[@]} directories (force=$force_flag)"
  python3 "$LIB_SCRIPT" incremental-scan "$CACHE_FILE" "$scanned_at" "$force_flag" "$rows_file" "$extra_file" > "$tmpfile" 2>/dev/null
  mv "$tmpfile" "$CACHE_FILE"
  rm -f "$rows_file" "$extra_file"
  debug "Cache written to: $CACHE_FILE"
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
    # Use single stat call with cross-platform syntax
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
# Run an update command (bash -c instead of eval).
#
# Two hang protections, so one stuck update can never stall the run:
#   1. stdin is /dev/null — an update that tries to prompt (sudo, npm
#      questions, a cask installer asking to close a running app) reads
#      EOF instead of waiting forever on input.
#   2. A per-job watchdog: any update still running after UAC_JOB_TIMEOUT
#      seconds (default 900) has its whole process tree killed and is
#      counted as a failure; the rest of the run continues normally.
#      Tune with UAC_JOB_TIMEOUT=N or --job-timeout=N (0 disables).
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
  local _outfile
  _outfile=$(mktemp)
  if [[ -n "$TRACE" ]] && [[ -z "${SUPPRESS_TRACE:-}" ]]; then
    bash -x -c "$cmd" </dev/null >"$_outfile" 2>&1 &
  else
    bash -c "$cmd" </dev/null >"$_outfile" 2>&1 &
  fi
  local _cmd_pid=$!
  local _elapsed=0 _timed_out=""
  while kill -0 "$_cmd_pid" 2>/dev/null; do
    if (( UAC_JOB_TIMEOUT > 0 )) && (( _elapsed >= UAC_JOB_TIMEOUT )); then
      _timed_out=1
      _kill_tree "$_cmd_pid"
      break
    fi
    sleep 1
    _elapsed=$((_elapsed + 1))
  done
  wait "$_cmd_pid" 2>/dev/null || ec=$?
  output=$(<"$_outfile")
  rm -f "$_outfile"

  if [[ -n "$_timed_out" ]]; then
    warn "$group timed out after ${UAC_JOB_TIMEOUT}s and was killed — it was probably waiting on something (e.g. an open app blocking a cask upgrade, or a prompt). Other updates were not blocked."
    [[ -z "$QUIET" ]] && echo "$output" | tail -3 | sed 's/^/   /'
    return 1
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
_parse_emit_line() {
  local line="$1"
  local rest="${line#*$'\x1e'}"
  EMIT_NAME="${rest%%$'\x1e'*}"
  rest="${rest#*$'\x1e'}"
  EMIT_CMD="${rest%%$'\x1e'*}"
  EMIT_LOCK="${rest#*$'\x1e'}"
  EMIT_TYPE="${line%%$'\x1e'*}"
}

_run_one_emit_line_core() {
  local cmd_type="$1"
  local name="$2"
  local cmd="$3"
  case "$cmd_type" in
    skip) return 3 ;;
    quarantined)
      warn "skipped (quarantined after $cmd consecutive failures): $name — run with --include-quarantined to retry"
      return 3
      ;;
    held)
      if [[ "$cmd" == "env" ]]; then
        warn "held (env HOLD=): $name — remove from HOLD= to resume this run only"
      else
        warn "held (config): $name — remove from \"hold\" to resume updates"
      fi
      return 3
      ;;
    uptodate)
      ok "$name: already up to date (pre-check)"
      return 0
      ;;
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

# Acquire a per-origin lock with a bash-native `mkdir` spin-lock (mkdir is
# atomic on POSIX filesystems, so this needs no `flock` binary and no helper
# process/python spawn per job — unlike the old fcntl.flock coprocess this
# replaced). Jobs sharing a lock_group (same package manager) serialize;
# a lockdir older than the stale cap is assumed abandoned (crashed/killed
# holder) and stolen rather than waited on forever. On interrupt, the
# top-level `_cleanup` trap's `rm -rf "$LOCK_DIR"` sweeps up any lockdir left
# behind mid-critical-section, so no extra bookkeeping is needed here.
_UAC_LOCK_STALE_SECS=600
_lockdir_age() {
  local d="$1" mtime now
  if [[ "$(uname)" == "Darwin" ]]; then
    mtime=$(stat -f "%m" "$d" 2>/dev/null)
  else
    mtime=$(stat -c "%Y" "$d" 2>/dev/null)
  fi
  [[ -z "$mtime" ]] && { echo 0; return; }
  now=$(date +%s)
  echo $((now - mtime))
}

_run_with_mkdir_lock() {
  local lock_group="$1"; shift
  local lock_dir="$LOCK_DIR/${lock_group}.lockdir"
  local _waited=0 _ec=0
  until mkdir "$lock_dir" 2>/dev/null; do
    if [[ -d "$lock_dir" ]] && (( $(_lockdir_age "$lock_dir") > _UAC_LOCK_STALE_SECS )); then
      rmdir "$lock_dir" 2>/dev/null || true
      continue
    fi
    sleep 0.2
    _waited=$((_waited + 1))
    (( _waited > 1500 )) && break
  done
  _run_one_emit_line_core "$@" || _ec=$?
  rmdir "$lock_dir" 2>/dev/null || true
  return $_ec
}

_run_one_emit_line() {
  local line="$1"
  local cmd_type name cmd lock_group
  _parse_emit_line "$line"
  cmd_type="$EMIT_TYPE"
  name="$EMIT_NAME"
  cmd="$EMIT_CMD"
  lock_group="${EMIT_LOCK:-$name}"

  # Dry-run never mutates anything, so locking (which only exists to
  # serialize concurrent *writes* from the same package manager) is pointless
  # overhead there — every dry-run "job" is a near-instant echo of the
  # command it would run, so skip the lock round-trip entirely.
  if [[ -n "$DRY_RUN" ]]; then
    _run_one_emit_line_core "$cmd_type" "$name" "$cmd"
    return $?
  fi

  if (( PARALLEL_JOBS >= 2 )) && [[ "$cmd_type" != "skip" ]] && [[ -n "$lock_group" ]]; then
    mkdir -p "$LOCK_DIR"
    if command -v flock >/dev/null 2>&1; then
      # Bounded wait: if a sibling job sharing this lock is wedged, don't
      # block behind it forever. The bound tracks the per-job watchdog
      # (which kills a wedged holder anyway) plus slack; when the watchdog
      # is disabled, fall back to a 1-hour cap. On lock timeout, proceed
      # without the lock — same behavior as the mkdir fallback's cap.
      local _lock_wait
      if (( UAC_JOB_TIMEOUT > 0 )); then
        _lock_wait=$((UAC_JOB_TIMEOUT + 60))
      else
        _lock_wait=3600
      fi
      { flock -x -w "$_lock_wait" 200 || warn "lock '$lock_group' busy after ${_lock_wait}s; running $name without it"
        _run_one_emit_line_core "$cmd_type" "$name" "$cmd"
      } 200>"$LOCK_DIR/${lock_group}.lock"
    else
      _run_with_mkdir_lock "$lock_group" "$cmd_type" "$name" "$cmd"
      return $?
    fi
  else
    _run_one_emit_line_core "$cmd_type" "$name" "$cmd"
  fi
}

run_updates_sequential() {
  local line
  for line in "$@"; do
    [[ -z "$line" ]] && continue
    _parse_emit_line "$line"
    local _res_type="$EMIT_TYPE" _res_name="$EMIT_NAME" _res_cmd="$EMIT_CMD"
    local _start _end
    _start=$(date +%s)
    _run_one_emit_line "$line"
    local ec=$?
    _end=$(date +%s)
    if [[ "$_res_type" == "uptodate" ]]; then
      # _res_cmd carries the pre-check's own duration (whole seconds); use
      # that for history instead of this near-instant synthetic "run".
      local _dur_int="${_res_cmd%%.*}"
      [[ "$_dur_int" =~ ^[0-9]+$ ]] && _start=$((_end - _dur_int))
    fi
    if [[ "$_res_type" == "known" || "$_res_type" == "bulk" || "$_res_type" == "uptodate" || "$_res_type" == "held" ]]; then
      _UAC_RESULT_LINES+=("${_res_type}${_UAC_SEP}${_res_name}${_UAC_SEP}${_res_cmd}${_UAC_SEP}${ec}${_UAC_SEP}${_start}${_UAC_SEP}${_end}")
    fi
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
  local result_dir
  local result_idx=0
  result_dir=$(mktemp -d)
  _UAC_PIDS=()

  for line in "$@"; do
    [[ -z "$line" ]] && continue
    result_idx=$((result_idx + 1))
    while (( ${#pids[@]} >= max )); do
      # Wait for any child to complete using wait -n if available
      if wait -n 2>/dev/null; then
        :
      else
        # wait -n not supported, wait for first PID
        wait "${pids[0]}" 2>/dev/null || true
      fi
      # Remove completed PIDs from array
      local new_pids=()
      for pid in "${pids[@]:-}"; do
        if kill -0 "$pid" 2>/dev/null; then
          new_pids+=("$pid")
        fi
      done
      if [[ ${#new_pids[@]} -gt 0 ]]; then
        pids=("${new_pids[@]}")
      else
        pids=()
      fi
    done
    (
      local result_file="$result_dir/$result_idx.result"
      SUPPRESS_TRACE=1
      _parse_emit_line "$line"
      local _res_type="$EMIT_TYPE" _res_name="$EMIT_NAME" _res_cmd="$EMIT_CMD"
      local _start _end _ec _dur_int
      _start=$(date +%s)
      _run_one_emit_line "$line"
      _ec=$?
      _end=$(date +%s)
      if [[ "$_res_type" == "uptodate" ]]; then
        _dur_int="${_res_cmd%%.*}"
        [[ "$_dur_int" =~ ^[0-9]+$ ]] && _start=$((_end - _dur_int))
      fi
      {
        echo "$_ec"
        if [[ "$_res_type" == "known" || "$_res_type" == "bulk" || "$_res_type" == "uptodate" || "$_res_type" == "held" ]]; then
          printf '%s\n' "${_res_type}${_UAC_SEP}${_res_name}${_UAC_SEP}${_res_cmd}${_UAC_SEP}${_ec}${_UAC_SEP}${_start}${_UAC_SEP}${_end}"
        fi
      } > "$result_file"
    ) &
    pids+=($!)
    _UAC_PIDS+=("$!")
  done
  # Wait for all remaining processes
  for _pid in "${pids[@]:-}"; do
    wait "$_pid" 2>/dev/null || true
  done
  _UAC_PIDS=()
  # Count results from files (line 1 = exit code; line 2, if present, is the
  # kind/name/cmd/ec/start/end record for history-append).
  for result_file in "$result_dir"/*.result; do
    [[ -f "$result_file" ]] || continue
    local ec
    ec=$(sed -n '1p' "$result_file")
    case "$ec" in
      0) ((UPDATE_OK++)) || true ;;
      3) ;;
      *) ((UPDATE_FAIL++)) || true ;;
    esac
    local _rec
    _rec=$(sed -n '2p' "$result_file")
    [[ -n "$_rec" ]] && _UAC_RESULT_LINES+=("$_rec")
  done
  rm -rf "$result_dir"
}

# -------------------------------------------------------------------
# Live TUI executor (Feature: live dashboard).
#
# When active, the update phase is delegated to tui_update_all_clis.py,
# which runs the exact same plan with the same semantics (parallel cap,
# per-origin lock serialization, per-job watchdog, exit-code conventions)
# and writes result records in the same format run_updates_parallel's
# *.result files use. Everything before (discovery, prechecks, planning)
# and after (snapshots, run summary, history, notify, changelog) is
# unchanged.
# -------------------------------------------------------------------
_tui_wanted() {
  [[ "$TUI_MODE" == "0" ]] && return 1
  # These modes own stdout in ways a full-screen dashboard would break.
  [[ -n "$DRY_RUN" || -n "$QUIET" || -n "$TRACE" ]] && return 1
  [[ -n "${NO_COLOR:-}" || "${TERM:-}" == "dumb" ]] && return 1
  [[ -f "$TUI_SCRIPT" ]] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  if [[ "$TUI_MODE" == "1" ]]; then return 0; fi
  [[ "$TUI_MODE" == "auto" && -t 1 ]]
}

run_updates_tui() {
  local _emit_file _results_file _rc=0
  _emit_file=$(mktemp)
  _results_file=$(mktemp)
  printf '%s\n' "$@" > "$_emit_file"
  python3 "$TUI_SCRIPT" \
    --emit-file "$_emit_file" \
    --results-file "$_results_file" \
    --parallel "$PARALLEL_JOBS" \
    --timeout "$UAC_JOB_TIMEOUT" \
    --skip "$SKIP" \
    --version-string "$UAC_VERSION" || _rc=$?
  # 130 = interrupted (Ctrl+C): the runner already reported it; don't warn.
  if (( _rc != 0 && _rc != 130 )); then
    warn "TUI runner exited with status $_rc — results may be incomplete"
  fi
  # Ingest results exactly like run_updates_parallel ingests *.result files:
  # each line is "<ec>\x1e<record>" (record empty for skip/quarantined).
  local _rline _ec _rec
  while IFS= read -r _rline || [[ -n "$_rline" ]]; do
    [[ -z "$_rline" ]] && continue
    _ec="${_rline%%"${_UAC_SEP}"*}"
    _rec="${_rline#*"${_UAC_SEP}"}"
    [[ "$_ec" =~ ^[0-9]+$ ]] || continue
    case "$_ec" in
      0) ((UPDATE_OK++)) || true ;;
      3) ;;
      *) ((UPDATE_FAIL++)) || true ;;
    esac
    [[ -n "$_rec" ]] && _UAC_RESULT_LINES+=("$_rec")
  done < "$_results_file"
  rm -f "$_emit_file" "$_results_file"
}

# -------------------------------------------------------------------
# Self-update: `git pull --ff-only` this script's own checkout, then
# re-exec once so the run that follows uses the freshly-pulled code.
# Off by default (--self-update / UPDATE_ALL_CLIS_SELF_UPDATE=1). Every
# failure mode here (dirty tree, no network, diverged history, not a git
# checkout, no `origin` remote, no `git` binary) is fail-open: print a
# one-line warning and let the run continue on the current checkout.
# -------------------------------------------------------------------
_git_pull_with_timeout() {
  # No portable `timeout`/`gtimeout` guarantee on macOS, so watch a
  # backgrounded `git pull` ourselves and kill it if it runs too long.
  local repo="$1" timeout_secs="$2"
  local out_file pid waited=0 ec=0
  out_file=$(mktemp)
  ( git -C "$repo" pull --ff-only > "$out_file" 2>&1 ) &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if (( waited >= timeout_secs )); then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      echo "timed out after ${timeout_secs}s" >> "$out_file"
      cat "$out_file"
      rm -f "$out_file"
      return 124
    fi
  done
  wait "$pid" 2>/dev/null || ec=$?
  cat "$out_file"
  rm -f "$out_file"
  return "$ec"
}

_self_update() {
  [[ -z "$SELF_UPDATE" ]] && return 0
  if [[ -n "${UAC_SELF_UPDATED:-}" ]]; then
    debug "self-update: already re-exec'd once this run; skipping to avoid a loop"
    return 0
  fi
  if ! command -v git >/dev/null 2>&1; then
    warn "self-update: git not found; skipping"
    return 0
  fi
  if ! git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    debug "self-update: $SCRIPT_DIR is not a git checkout; skipping"
    return 0
  fi
  if ! git -C "$SCRIPT_DIR" remote get-url origin >/dev/null 2>&1; then
    warn "self-update: no 'origin' remote configured for $SCRIPT_DIR; skipping"
    return 0
  fi
  local _before _after _pull_out _ec=0
  _before=$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || true)
  _pull_out=$(_git_pull_with_timeout "$SCRIPT_DIR" 15) || _ec=$?
  if (( _ec != 0 )); then
    warn "self-update: git pull --ff-only failed (dirty tree, no network, or diverged history) — continuing with the current checkout: $(echo "$_pull_out" | head -1)"
    return 0
  fi
  _after=$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || true)
  if [[ -n "$_after" ]] && [[ "$_before" != "$_after" ]]; then
    info "self-update: pulled new changes ($_before -> $_after); re-executing..."
    UAC_SELF_UPDATED=1 exec "$0" "${_UAC_ORIG_ARGS[@]}"
  else
    debug "self-update: already up to date"
  fi
  return 0
}

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
main() {
  _self_update

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

  # Machine-readable output must be the only thing on stdout
  [[ -n "$LIST_JSON" || -n "$JSON_PLAN" ]] && QUIET=1

  mkdir -p "$(dirname "$CACHE_FILE")"
  mkdir -p "$(dirname "$UNKNOWN_LOG_FILE")"
  mkdir -p "$LOG_DIR"

  log "${BOLD}update-all-clis${NC} — dynamic discovery and update"
  log ""

  if [[ -n "$HEALTH_CHECK" ]]; then
    python3 "$LIB_SCRIPT" health-check
    exit $?
  fi

  if [[ -n "$VALIDATE_CACHE" ]]; then
    python3 "$LIB_SCRIPT" validate-cache "$CACHE_FILE"
    exit $?
  fi

  if [[ -n "$DEBUG_CACHE" ]]; then
    python3 "$LIB_SCRIPT" debug-cache "$CACHE_FILE"
    exit 0
  fi

  if [[ -n "$REPORT_UNKNOWN" ]]; then
    python3 "$LIB_SCRIPT" report-unknown "$UNKNOWN_LOG_FILE"
    exit 0
  fi

  if [[ -n "$ACK_UNKNOWN" ]]; then
    python3 "$LIB_SCRIPT" ack-unknown "$UNKNOWN_LOG_FILE" "$ACK_UNKNOWN"
    exit 0
  fi

  if [[ -n "$SUGGEST_KNOWN" ]]; then
    export CONFIG_FILE
    export CONFIG_LOCAL_FILE
    python3 "$LIB_SCRIPT" suggest-known "$CACHE_FILE"
    exit 0
  fi

  if [[ -n "$HISTORY_MODE" ]]; then
    python3 "$LIB_SCRIPT" history "$HISTORY_FILE" "$HISTORY_N"
    exit 0
  fi

  if [[ -n "$HOLD_ADD" ]]; then
    python3 "$LIB_SCRIPT" hold-add "$CONFIG_LOCAL_FILE" "$HOLD_ADD"
    exit $?
  fi

  if [[ -n "$HOLD_REMOVE" ]]; then
    python3 "$LIB_SCRIPT" hold-remove "$CONFIG_LOCAL_FILE" "$HOLD_REMOVE"
    exit $?
  fi

  if [[ -n "$DOCTOR_MODE" ]]; then
    ensure_cache
    export CONFIG_FILE
    export CONFIG_LOCAL_FILE
    export UPDATE_ALL_CLIS_HISTORY_FILE="$HISTORY_FILE"
    if [[ -n "$LIST_JSON" ]]; then
      python3 "$LIB_SCRIPT" doctor "$CACHE_FILE" --json
    else
      python3 "$LIB_SCRIPT" doctor "$CACHE_FILE"
    fi
    exit $?
  fi

  # Single-instance lock for anything that scans/writes the cache or runs
  # updates (read-only commands above already exited). Avoids overlapping runs
  # (LaunchAgent + manual, or two terminals) clobbering each other's cache.
  # A plain `mkdir` is atomic on POSIX filesystems, so this needs no helper
  # process (the old approach spawned a python fcntl.flock coprocess and kept
  # it alive for the whole run just to hold one lock). Non-blocking with a
  # stale-lock steal: a lockdir older than the stale cap means a previous run
  # crashed without cleaning up, so we reclaim it instead of refusing forever.
  # Held until cleanup's `rm -rf "$LOCK_DIR"` removes it on exit.
  mkdir -p "$LOCK_DIR"
  local _run_lockdir="$LOCK_DIR/run.lockdir"
  if ! mkdir "$_run_lockdir" 2>/dev/null; then
    if [[ -d "$_run_lockdir" ]] && (( $(_lockdir_age "$_run_lockdir") > _UAC_LOCK_STALE_SECS )); then
      rmdir "$_run_lockdir" 2>/dev/null || true
      mkdir "$_run_lockdir" 2>/dev/null || true
    fi
  fi
  if [[ ! -d "$_run_lockdir" ]]; then
    warn "another update-all-clis run is in progress; exiting"
    exit 0
  fi

  if [[ -n "$JSON_PLAN" ]]; then
    ensure_cache
    export CONFIG_FILE
    export CONFIG_LOCAL_FILE
    export ONLY_ORIGINS
    export SKIP_ORIGINS
    export UPDATE_ALL_CLIS_HISTORY_FILE="$HISTORY_FILE"
    export UAC_QUARANTINE_AFTER
    export UAC_INCLUDE_QUARANTINED="$INCLUDE_QUARANTINED"
    export HOLD
    python3 "$LIB_SCRIPT" emit-json "$CACHE_FILE"
    exit 0
  fi

  local _prev_names_snap
  _prev_names_snap=$(mktemp)
  if [[ -f "$CACHE_FILE" ]]; then
    python3 -c "
import json
for t in json.load(open('$CACHE_FILE')):
    if 'name' in t:
        print(t['name'])
" > "$_prev_names_snap" 2>/dev/null || true
  fi

  ensure_cache

  local _new_tools_snap
  _new_tools_snap=$(mktemp)
  python3 "$LIB_SCRIPT" new-tools "$_prev_names_snap" "$CACHE_FILE" > "$_new_tools_snap" 2>/dev/null || echo "[]" > "$_new_tools_snap"
  rm -f "$_prev_names_snap"

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
  export UPDATE_ALL_CLIS_HISTORY_FILE="$HISTORY_FILE"
  export UAC_QUARANTINE_AFTER
  export UAC_INCLUDE_QUARANTINED="$INCLUDE_QUARANTINED"
  export HOLD

  # -----------------------------------------------------------------
  # Outdated pre-checks: for bulk origins with a configured `check`
  # command (tool_config.json's "check" section), run it first; an
  # origin whose check says nothing is outdated skips its (expensive)
  # bulk update entirely this run. Concurrent, read-only, fail-open.
  # --dry-run never executes checks (some, like brew's, mutate state);
  # it only reports which origins would have been checked.
  # -----------------------------------------------------------------
  local _precheck_file
  _precheck_file=$(mktemp)
  echo "{}" > "$_precheck_file"
  if [[ -n "$DRY_RUN" ]]; then
    local _precheck_would
    _precheck_would=$(python3 "$LIB_SCRIPT" precheck-candidates 2>/dev/null || true)
    [[ -n "$_precheck_would" ]] && info "Would pre-check (dry-run, not executed): $_precheck_would"
  elif [[ -n "$NO_PRECHECK" ]]; then
    debug "Pre-checks disabled (--no-precheck)"
  else
    info "Pre-checking bulk origins for outdated packages..."
    python3 "$LIB_SCRIPT" precheck > "$_precheck_file" 2>/dev/null || echo "{}" > "$_precheck_file"
    # Per-origin "✓ x: already up to date (pre-check)" lines print later,
    # when the run loop processes each synthetic "uptodate" emit-line.
  fi
  export UAC_PRECHECK_UPTODATE_FILE="$_precheck_file"

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

  # Collect quarantined names from the plan for the run summary (jobs skipped
  # this run because they failed their last $UAC_QUARANTINE_AFTER attempts).
  local _quarantined_snap
  _quarantined_snap=$(mktemp)
  {
    local _qline
    for _qline in "${lines[@]:-}"; do
      [[ -z "$_qline" ]] && continue
      _parse_emit_line "$_qline"
      [[ "$EMIT_TYPE" == "quarantined" ]] && printf '%s\n' "$EMIT_NAME"
    done
  } | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" > "$_quarantined_snap" 2>/dev/null || echo "[]" > "$_quarantined_snap"

  # Collect held names too (jobs pinned via the "hold" config or HOLD= env).
  local _held_snap
  _held_snap=$(mktemp)
  {
    local _hline
    for _hline in "${lines[@]:-}"; do
      [[ -z "$_hline" ]] && continue
      _parse_emit_line "$_hline"
      [[ "$EMIT_TYPE" == "held" ]] && printf '%s\n' "$EMIT_NAME"
    done
  } | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" > "$_held_snap" 2>/dev/null || echo "[]" > "$_held_snap"

  log "${BOLD}=== Logging unknown tools ===${NC}"
  export UNKNOWN_LOG_FILE
  python3 "$LIB_SCRIPT" log-unknowns "$CACHE_FILE" 2>/dev/null || true

  # Run id shared by every history record from this run.
  local RUN_ID
  RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"

  local _emit_snap="" _before_snap="" _after_snap=""
  if [[ -z "$DRY_RUN" ]]; then
    # Pre/post version snapshots are needed both for the desktop/email
    # summary AND for history.jsonl's version_before/version_after fields,
    # so (unlike before) we always take them on a real run, not just when
    # --notify/UPDATE_ALL_CLIS_SUMMARY_FILE are set.
    _emit_snap=$(mktemp)
    _before_snap=$(mktemp)
    _after_snap=$(mktemp)
    printf '%s\n' "${lines[@]:-}" > "$_emit_snap"
    # "before" reuses versions cached on the previous run (no subprocess spawns);
    # "after" probes fresh to capture what changed.
    python3 "$LIB_SCRIPT" snapshot-versions "$_emit_snap" "$CACHE_FILE" > "$_before_snap" 2>/dev/null || true
  fi

  if _tui_wanted; then
    run_updates_tui "${lines[@]:-}"
  elif (( PARALLEL_JOBS < 2 )); then
    run_updates_sequential "${lines[@]:-}"
  else
    run_updates_parallel "$PARALLEL_JOBS" "${lines[@]:-}"
  fi

  if [[ -n "$_emit_snap" ]]; then
    # "" = no cache reuse; "$_before_snap" = mtime gate, reuse pre-run
    # version for any tool whose binary mtime hasn't changed since then.
    python3 "$LIB_SCRIPT" snapshot-versions "$_emit_snap" "" "$_before_snap" > "$_after_snap" 2>/dev/null || true
    # Terminal version-change list (before → after). Same text as the
    # desktop/email summary so every run surfaces what actually moved.
    local _summary_out=""
    _summary_out=$(python3 "$LIB_SCRIPT" run-summary "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" "$_new_tools_snap" "$_quarantined_snap" "$_held_snap" 2>/dev/null || true)
    if [[ -n "$_summary_out" ]] && [[ -z "$QUIET" ]]; then
      log ""
      log "${BOLD}=== Packages updated ===${NC}"
      # Skip the leading "update-all-clis" / "Steps: …" header — those are
      # already covered by the run's own Done summary below.
      printf '%s\n' "$_summary_out" | awk 'BEGIN{skip=1} /^Upgraded /{skip=0} !skip{print}' | while IFS= read -r _sline || [[ -n "$_sline" ]]; do
        log "$_sline"
      done
    fi
    if _want_notify_popup; then
      python3 "$LIB_SCRIPT" notify-diff "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" "$_new_tools_snap" "$_quarantined_snap" "$_held_snap" 2>/dev/null || true
    fi
    if [[ -n "${UPDATE_ALL_CLIS_SUMMARY_FILE:-}" ]]; then
      if [[ -n "$_summary_out" ]]; then
        printf '%s' "$_summary_out" > "${UPDATE_ALL_CLIS_SUMMARY_FILE}"
      else
        python3 "$LIB_SCRIPT" run-summary "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" "$_new_tools_snap" "$_quarantined_snap" "$_held_snap" > "${UPDATE_ALL_CLIS_SUMMARY_FILE}" 2>/dev/null || true
      fi
    fi
    # Update cache with new version information
    python3 "$LIB_SCRIPT" update-cache-versions "$CACHE_FILE" < "$_after_snap" 2>/dev/null || true

    # Append this run's job results to history.jsonl (never on --dry-run).
    if (( ${#_UAC_RESULT_LINES[@]} > 0 )); then
      local _results_snap
      _results_snap=$(mktemp)
      printf '%s\n' "${_UAC_RESULT_LINES[@]}" > "$_results_snap"
      python3 "$LIB_SCRIPT" history-append "$HISTORY_FILE" "$RUN_ID" "$_results_snap" "$_before_snap" "$_after_snap" 2>/dev/null || true
      rm -f "$_results_snap"
    fi

    # Changelog digest (best-effort, offline-safe): only on --changelog /
    # UPDATE_ALL_CLIS_CHANGELOG=1, never on --dry-run (handled above by this
    # whole block being inside `if [[ -z "$DRY_RUN" ]]`-gated snapshotting).
    # Bodies can be multi-KB; printed to stdout/summary file, never the
    # macOS dialog (notify-diff above never sees it).
    if [[ -n "$CHANGELOG" ]]; then
      local _changelog_out
      _changelog_out=$(python3 "$LIB_SCRIPT" changelog "$_before_snap" "$_after_snap" 2>/dev/null || true)
      if [[ -n "$_changelog_out" ]]; then
        log ""
        log "$_changelog_out"
        if [[ -n "${UPDATE_ALL_CLIS_SUMMARY_FILE:-}" ]]; then
          { echo ""; echo "$_changelog_out"; } >> "${UPDATE_ALL_CLIS_SUMMARY_FILE}" 2>/dev/null || true
        fi
      fi
    fi
    rm -f "$_emit_snap" "$_before_snap" "$_after_snap"
  fi
  rm -f "$_new_tools_snap" "$_quarantined_snap" "$_held_snap" "$_precheck_file"

  log ""
  log "${BOLD}=== Done! ===${NC}"
  log "Summary: ${UPDATE_OK} ok, ${UPDATE_FAIL} failed"

  # Auto-tip: bulk-covered tools missing from known list
  if [[ -z "$DRY_RUN" ]]; then
    local _known_candidates
    _known_candidates=$(export CONFIG_FILE CONFIG_LOCAL_FILE; python3 "$LIB_SCRIPT" suggest-known-count "$CACHE_FILE" 2>/dev/null || echo "[]")
    local _known_count
    _known_count=$(echo "$_known_candidates" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "0")
    if [[ "$_known_count" -gt 0 ]]; then
      local _known_sample
      _known_sample=$(echo "$_known_candidates" | python3 -c "import json,sys; d=json.load(sys.stdin); print(', '.join(x[0] for x in d[:3]))" 2>/dev/null || true)
      warn "$_known_count tools updated via bulk but not individually tracked (e.g., $_known_sample)"
      log "  Run './update_all_clis.sh --suggest-known' to see all candidates."
    fi
  fi

  # Auto-tip: discovered tools with no update path at all
  if [[ -z "$DRY_RUN" ]] && [[ -f "$UNKNOWN_LOG_FILE" ]]; then
    local _unknown_info
    _unknown_info=$(python3 -c "
import json
d = json.load(open('$UNKNOWN_LOG_FILE'))
tools = [t['name'] for t in d.get('tools', {}).values() if not t.get('acknowledged')]
print(len(tools))
print(', '.join(sorted(tools)[:5]))
" 2>/dev/null || echo "0")
    local _unknown_count _unknown_sample
    _unknown_count=$(echo "$_unknown_info" | head -1)
    _unknown_sample=$(echo "$_unknown_info" | tail -1)
    if [[ "$_unknown_count" =~ ^[0-9]+$ ]] && [[ "$_unknown_count" -gt 0 ]]; then
      warn "$_unknown_count discovered tools have no update path (e.g., $_unknown_sample)"
      log "  Run './update_all_clis.sh --report-unknown' to review and add them."
    fi
  fi

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
