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
#   --retries=N       Retry a failed update up to N times before giving up
#                     (default 1; 0 disables; also: UAC_RETRIES=N).
#                     Timeouts are never retried, only real failures.
#   --retry-delay=N   Seconds to wait between retries (default 10;
#                     also: UAC_RETRY_DELAY=N)
#   --no-fix          Don't attempt the one-shot fix (force-reinstall) after
#                     an update has failed all its retries (also: UAC_FIX=0).
#                     Fix commands are auto-derived (npm/brew/uv/pipx/cargo/gem
#                     reinstalls) or set per tool in config's "fix" object.
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
  #   --notify=on-failure  Show the dialog only when at least one update failed
  #                     (also: UPDATE_ALL_CLIS_NOTIFY=on-failure)
  #   --summary=MODE    Run-summary verbosity: full (default) or failures (leads
  #                     with failed jobs, collapses the up-to-date name list)
  #                     (also: UPDATE_ALL_CLIS_SUMMARY_MODE)
  #   --history[=N]     Show the last N runs from history.jsonl (default 3) and exit
  #   --insights        History analytics: slowest jobs, chronic failers, most-
  #                     frequently-updated tools, and suggestions; then exit
#   --include-quarantined  Force quarantined tools/origins to run this run
#                     (also: UAC_INCLUDE_QUARANTINED=1)
#                     (quarantine threshold: UAC_QUARANTINE_AFTER, default 3, 0 disables)
  #   --no-precheck     Skip outdated pre-checks; always run every bulk update and
  #                     every known tool (also: UAC_NO_PRECHECK=1)
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
  #                     All real runs execute via tui_update_all_clis.py — the
  #                     dashboard on terminals, identical plain output elsewhere.
  #                     UAC_EXECUTOR=bash forces the legacy bash executor, which
  #                     also still handles --dry-run and --trace.)
  #   --version         Print version and exit
# =============================================================================

UAC_VERSION="0.11.0"

set -uo pipefail

# Preserve the original argv for a self-update re-exec (arg parsing below
# consumes "$@" via shift, so it must be captured before that happens).
_UAC_ORIG_ARGS=("$@")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_SCRIPT="${LIB_SCRIPT:-$SCRIPT_DIR/lib_update_all_clis.py}"
TUI_SCRIPT="${TUI_SCRIPT:-$SCRIPT_DIR/tui_update_all_clis.py}"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/tool_config.json}"
CONFIG_LOCAL_FILE="${CONFIG_LOCAL_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/config.local.json}"
# Every lib subprocess reads these from the environment — export them here,
# before the earliest lib calls (full_scan's scan-dirs runs long before the
# main-stage export block below, which stays as a harmless duplicate).
export LIB_SCRIPT TUI_SCRIPT CONFIG_FILE CONFIG_LOCAL_FILE

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

# Retry-then-fix (Feature: fix failing packages): a failed update is retried
# up to UAC_RETRIES times (UAC_RETRY_DELAY seconds apart); if it still fails
# and a fix command exists (auto-derived force-reinstall, or config "fix"
# entry), that fix runs once and its success counts as ok. Timeouts are not
# retried — a wedged job would just burn the watchdog twice.
UAC_RETRIES="${UAC_RETRIES:-1}"
UAC_RETRY_DELAY="${UAC_RETRY_DELAY:-10}"
UAC_FIX="${UAC_FIX:-1}"

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
SUMMARY_MODE="${UPDATE_ALL_CLIS_SUMMARY_MODE:-full}"
REPORT_UNKNOWN=""; ACK_UNKNOWN=""; HEALTH_CHECK=""
SUGGEST_KNOWN=""; JSON_PLAN=""; VERBOSE=""; VALIDATE_CACHE=""; DEBUG_CACHE=""
HISTORY_MODE=""; HISTORY_N=3
INSIGHTS_MODE=""
INCLUDE_QUARANTINED="${UAC_INCLUDE_QUARANTINED:-}"
NO_PRECHECK="${UAC_NO_PRECHECK:-}"
HOLD_ADD=""; HOLD_REMOVE=""; DOCTOR_MODE=""
HOLD="${HOLD:-}"
CHANGELOG="${UPDATE_ALL_CLIS_CHANGELOG:-}"
SELF_UPDATE="${UPDATE_ALL_CLIS_SELF_UPDATE:-}"
# Live TUI dashboard for the update run: "auto" (on for interactive
# terminals), "1" (forced), "0" (disabled). Controls the Python executor's
# renderer; see run_updates_python.
TUI_MODE="${UAC_TUI:-auto}"
# Executor escape hatch: UAC_EXECUTOR=bash forces the legacy in-shell
# executor (which also still handles --dry-run and --trace).
UAC_EXECUTOR="${UAC_EXECUTOR:-}"

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
    --notify=on-failure) NOTIFY="on-failure"; shift ;;
    --summary=*)       SUMMARY_MODE="${1#*=}"; shift ;;
    --history)         HISTORY_MODE=1; shift ;;
    --history=*)       HISTORY_MODE=1; HISTORY_N="${1#*=}"; shift ;;
    --insights)        INSIGHTS_MODE=1; shift ;;
    --include-quarantined) INCLUDE_QUARANTINED=1; shift ;;
    --no-precheck)     NO_PRECHECK=1; shift ;;
    --job-timeout=*)   UAC_JOB_TIMEOUT="${1#*=}"; shift ;;
    --retries=*)       UAC_RETRIES="${1#*=}"; shift ;;
    --retry-delay=*)   UAC_RETRY_DELAY="${1#*=}"; shift ;;
    --no-fix)          UAC_FIX=0; shift ;;
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
case "$SUMMARY_MODE" in
  full|failures) ;;
  *) echo "Invalid --summary value (use full or failures): $SUMMARY_MODE" >&2; exit 1 ;;
esac
export UPDATE_ALL_CLIS_SUMMARY_MODE="$SUMMARY_MODE"
if ! [[ "$UAC_RETRIES" =~ ^[0-9]+$ ]]; then
  echo "Invalid --retries / UAC_RETRIES value (use a non-negative integer, 0 disables): $UAC_RETRIES" >&2
  exit 1
fi
if ! [[ "$UAC_RETRY_DELAY" =~ ^[0-9]+$ ]]; then
  echo "Invalid --retry-delay / UAC_RETRY_DELAY value (use seconds): $UAC_RETRY_DELAY" >&2
  exit 1
fi

# -------------------------------------------------------------------
# Desktop summary is opt-in only so the terminal never blocks/hangs.
# Enable with --notify or UPDATE_ALL_CLIS_NOTIFY=1.
# Scheduled LaunchAgent/systemd set UPDATE_ALL_CLIS_NO_NOTIFY=1.
# --notify=on-failure / UPDATE_ALL_CLIS_NOTIFY=on-failure shows the dialog
# only when at least one update step failed.
# -------------------------------------------------------------------
_want_notify_popup() {
  [[ "${UPDATE_ALL_CLIS_NO_NOTIFY:-}" == "1" ]] && return 1
  case "${UPDATE_ALL_CLIS_NOTIFY:-}" in
    1) return 0 ;;
    0) return 1 ;;
    on-failure) (( UPDATE_FAIL > 0 )) && return 0; return 1 ;;
  esac
  case "$NOTIFY" in
    1) return 0 ;;
    on-failure) (( UPDATE_FAIL > 0 )) && return 0; return 1 ;;
  esac
  return 1
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
# Newline-joined "dir|origin" keys of every row registered so far, so a
# directory can't be scanned twice for the same origin (the same dir MAY
# appear under two different origins deliberately — e.g. /usr/local/bin as
# "manual" and, when brew isn't installed, "brew").
_SCAN_ROW_KEYS=$'\n'

_scan_row() {
  local dir="$1" origin="$2" mode="$3"
  case "$_SCAN_ROW_KEYS" in
    *$'\n'"${dir}|${origin}"$'\n'*) return 0 ;;
  esac
  _SCAN_ROW_KEYS+="${dir}|${origin}"$'\n'
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
  _SCAN_ROW_KEYS=$'\n'
  debug "Starting discovery scan (incremental: dirs whose mtime is unchanged are not re-listed)"

  # brew's resolved prefix rows register BEFORE the config's static rows:
  # on Intel Macs /usr/local/bin is both the brew bin dir (origin "brew")
  # and a static "manual" row, and brew must win that attribution (first
  # registered row wins; the old hardcoded order did the same).
  if command -v brew >/dev/null 2>&1; then
    local brew_prefix
    brew_prefix=$(brew --prefix 2>/dev/null || true)
    if [[ -n "$brew_prefix" ]]; then
      # Cellar/opt is scanned one level deep (each formula's own bin dir);
      # we only mtime-gate at the "opt" level (see incremental_scan_merge's
      # docstring for why that's an acceptable trade-off vs a full walk).
      _scan_row "$brew_prefix/opt" "brew" "tree"
      # The public bin dir lives at the resolved prefix (/opt/homebrew on
      # Apple Silicon, /usr/local on Intel, ~/.linuxbrew or
      # /home/linuxbrew/.linuxbrew on Linux) — derive it rather than
      # hardcoding one platform's path.
      _scan_row "$brew_prefix/bin" "brew" "dir"
    fi
  else
    # brew not on PATH: probe the standard install locations directly.
    _scan_row "/opt/homebrew/bin" "brew" "dir"
    _scan_row "/usr/local/bin" "brew" "dir"
    _scan_row "/home/linuxbrew/.linuxbrew/bin" "brew" "dir"
  fi

  # Static scan directories come from the merged config's "scan_dirs"
  # section (tool_config.json + config.local.json — users can extend
  # discovery without editing this script). Dynamic manager-derived rows
  # (npm/go prefixes, globs, sdkman, pnpm) are computed below.
  local _cdir _corigin _cmode
  while IFS=$'\t' read -r _cdir _corigin _cmode; do
    [[ -n "$_cdir" ]] || continue
    # Expand only a leading $HOME token — never eval config content.
    _cdir="${_cdir/#\$HOME/$HOME}"
    _scan_row "$_cdir" "$_corigin" "${_cmode:-dir}"
  done < <(python3 "$LIB_SCRIPT" scan-dirs 2>/dev/null)

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
  if command -v go >/dev/null 2>&1; then
    go_bin_dir="$(go env GOPATH 2>/dev/null)/bin"
    [[ -n "$go_bin_dir" ]] && _scan_row "$go_bin_dir" "go" "dir"
  fi
  [[ -n "${GOBIN:-}" ]] && _scan_row "$GOBIN" "go" "dir"

  if [[ -d "$HOME/.nvm/versions/node" ]]; then
    local nvm_bin
    for nvm_bin in "$HOME/.nvm/versions/node"/*/bin; do
      [[ -d "$nvm_bin" ]] && _scan_row "$nvm_bin" "npm" "dir"
    done
  fi

  # pipx: legacy (~/.local/pipx) and modern (~/.local/share/pipx) venv roots
  local pipx_root pipx_bin
  for pipx_root in "$HOME/.local/pipx/venvs" "$HOME/.local/share/pipx/venvs"; do
    [[ -d "$pipx_root" ]] || continue
    for pipx_bin in "$pipx_root"/*/bin; do
      [[ -d "$pipx_bin" ]] && _scan_row "$pipx_bin" "pipx" "dir"
    done
  done

  local gem_home
  gem_home=$(gem env home 2>/dev/null || true)
  [[ -n "$gem_home" ]] && _scan_row "$gem_home/bin" "gem" "dir"

  # sdkman's layout (candidates/*/current/bin/*) is gated at the top-level
  # "candidates" dir; adding/removing a candidate changes its mtime.
  _scan_row "$HOME/.sdkman/candidates" "sdkman" "sdkman"

  if [[ -n "${PNPM_HOME:-}" ]]; then
    _scan_row "$PNPM_HOME" "pnpm" "dir"
  else
    # pnpm's default global dir: ~/Library/pnpm on macOS, ~/.local/share/pnpm
    # on Linux. Scan both; a missing dir is pruned harmlessly.
    _scan_row "$HOME/Library/pnpm" "pnpm" "dir"
    _scan_row "$HOME/.local/share/pnpm/bin" "pnpm" "dir"
    _scan_row "$HOME/.local/share/pnpm" "pnpm" "dir"
  fi

  local _npm_packages="$HOME/.npm-packages/bin"
  if [[ -z "$npm_prefix" ]] || [[ "${npm_prefix}/lib/node_modules/.bin" != "$_npm_packages" ]]; then
    _scan_row "$_npm_packages" "npm" "dir"
  fi

  # macOS: pip install --user lands binaries in ~/Library/Python/3.x/bin
  if [[ "$(uname)" == "Darwin" ]]; then
    local _pyuser_bin
    for _pyuser_bin in "$HOME"/Library/Python/3.*/bin; do
      [[ -d "$_pyuser_bin" ]] && _scan_row "$_pyuser_bin" "pip" "dir"
    done
  fi

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
    local pdir _prow _skip
    IFS=':' read -ra _path_dirs <<< "${PATH:-}"
    for pdir in "${_path_dirs[@]}"; do
      [[ -n "$pdir" ]] || continue
      case "$pdir" in
        /usr/bin|/bin|/sbin|/usr/sbin|/usr/libexec|/System/*|/nix/*|/run/current-system/sw/bin) continue ;;
        "$HOME"/Library/Python/3.*/bin) continue ;;
      esac
      # Any directory already registered as a scan row — static config rows
      # AND dynamic manager-derived rows — is owned by that origin, so don't
      # double-count it under the generic "path" origin. (This list used to
      # be a second, manually-synced hardcoded copy of the scan dirs.)
      _skip=0
      for _prow in "${_SCAN_ROWS[@]:-}"; do
        [[ -z "$_prow" ]] && continue
        if [[ "${_prow%%$'\t'*}" == "${pdir%/}" ]]; then
          _skip=1
          break
        fi
      done
      (( _skip )) && continue
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
  # Only replace the cache when the scan actually succeeded and produced
  # output — an unconditional mv here used to clobber a good cache with an
  # empty file whenever the Python scanner failed, breaking discovery until
  # the next successful --rescan.
  if python3 "$LIB_SCRIPT" incremental-scan "$CACHE_FILE" "$scanned_at" "$force_flag" "$rows_file" "$extra_file" > "$tmpfile" 2>/dev/null && [[ -s "$tmpfile" ]]; then
    mv "$tmpfile" "$CACHE_FILE"
    debug "Cache written to: $CACHE_FILE"
  else
    rm -f "$tmpfile"
    if [[ -f "$CACHE_FILE" ]]; then
      warn "discovery scan failed — keeping the previous cache at $CACHE_FILE (run with UAC_DEBUG=1 to investigate)"
    else
      warn "discovery scan failed and no previous cache exists — no tools will be planned this run"
    fi
  fi
  rm -f "$rows_file" "$extra_file"
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
# One watchdog-guarded attempt. Sets _RUN_EC, _RUN_OUTPUT, _RUN_TIMED_OUT.
_run_cmd_once() {
  local cmd="$1"
  local ec=0
  local _outfile
  _RUN_TIMED_OUT=""
  _outfile=$(mktemp)
  if [[ -n "$TRACE" ]] && [[ -z "${SUPPRESS_TRACE:-}" ]]; then
    bash -x -c "$cmd" </dev/null >"$_outfile" 2>&1 &
  else
    bash -c "$cmd" </dev/null >"$_outfile" 2>&1 &
  fi
  local _cmd_pid=$!
  local _elapsed=0
  while kill -0 "$_cmd_pid" 2>/dev/null; do
    if (( UAC_JOB_TIMEOUT > 0 )) && (( _elapsed >= UAC_JOB_TIMEOUT )); then
      _RUN_TIMED_OUT=1
      _kill_tree "$_cmd_pid"
      break
    fi
    sleep 1
    _elapsed=$((_elapsed + 1))
  done
  wait "$_cmd_pid" 2>/dev/null || ec=$?
  _RUN_OUTPUT=$(<"$_outfile")
  rm -f "$_outfile"
  _RUN_EC=$ec
}

run_update() {
  local group="$1"
  local cmd="$2"
  local fix="${3:-}"

  if [[ -n "$DRY_RUN" ]]; then
    log "  [dry-run] $cmd"
    if [[ -n "$fix" ]] && [[ "$UAC_FIX" != "0" ]]; then
      log "  [dry-run]   (on failure, after $UAC_RETRIES retries: $fix)"
    fi
    return 0
  fi

  [[ -z "$QUIET" ]] && log "  ${BOLD}→${NC} $cmd"

  # Retry loop: real failures are retried up to UAC_RETRIES times; a
  # watchdog timeout is not (a wedged job would just wedge again and eat
  # another full UAC_JOB_TIMEOUT), and it skips the fix for the same reason.
  local attempt=0
  while :; do
    _run_cmd_once "$cmd"
    if [[ -n "$_RUN_TIMED_OUT" ]]; then
      warn "$group timed out after ${UAC_JOB_TIMEOUT}s and was killed — it was probably waiting on something (e.g. an open app blocking a cask upgrade, or a prompt). Other updates were not blocked."
      [[ -z "$QUIET" ]] && echo "$_RUN_OUTPUT" | tail -3 | sed 's/^/   /'
      return 1
    fi
    if [[ $_RUN_EC -eq 0 ]]; then
      if (( attempt > 0 )); then
        ok "$group (succeeded on retry $attempt)"
      else
        ok "$group"
      fi
      return 0
    fi
    if (( attempt < UAC_RETRIES )); then
      attempt=$((attempt + 1))
      warn "$group failed (exit $_RUN_EC) — retrying in ${UAC_RETRY_DELAY}s (retry $attempt/$UAC_RETRIES)"
      sleep "$UAC_RETRY_DELAY"
      continue
    fi
    break
  done

  warn "$group failed (exit $_RUN_EC)"
  [[ -z "$QUIET" ]] && echo "$_RUN_OUTPUT" | grep -v "^npm warn" | grep -v "^brew warn" | head -3 | sed 's/^/   /'

  # Retries exhausted: one-shot fix (force-reinstall). Its success counts
  # as ok — a reinstall at latest achieves what the update was trying to do.
  if [[ -n "$fix" ]] && [[ "$UAC_FIX" != "0" ]]; then
    info "Attempting fix for $group: $fix"
    _run_cmd_once "$fix"
    if [[ -z "$_RUN_TIMED_OUT" ]] && [[ $_RUN_EC -eq 0 ]]; then
      ok "$group (fixed via: $fix)"
      return 0
    fi
    warn "$group fix failed"
    [[ -z "$QUIET" ]] && echo "$_RUN_OUTPUT" | head -3 | sed 's/^/   /'
  fi
  return 1
}

# -------------------------------------------------------------------
# Run emit lines (skip lines do not count toward ok/fail)
# -------------------------------------------------------------------
_parse_emit_line() {
  local line="$1"
  EMIT_TYPE="${line%%$'\x1e'*}"
  local rest="${line#*$'\x1e'}"
  EMIT_NAME="${rest%%$'\x1e'*}"
  EMIT_CMD="" EMIT_LOCK="" EMIT_FIX=""
  # Fields 4 (lock group) and 5 (fix command) are optional: `${rest#*SEP}`
  # leaves rest unchanged when no separator remains, so guard each step or
  # a short line would smear its last field into the next variable.
  [[ "$rest" == *$'\x1e'* ]] || return 0
  rest="${rest#*$'\x1e'}"
  EMIT_CMD="${rest%%$'\x1e'*}"
  [[ "$rest" == *$'\x1e'* ]] || return 0
  rest="${rest#*$'\x1e'}"
  EMIT_LOCK="${rest%%$'\x1e'*}"
  [[ "$rest" == *$'\x1e'* ]] && EMIT_FIX="${rest#*$'\x1e'}"
  return 0
}

_run_one_emit_line_core() {
  local cmd_type="$1"
  local name="$2"
  local cmd="$3"
  local fix="${4:-}"
  case "$cmd_type" in
    skip) return 3 ;;
    quarantined)
      warn "skipped (quarantined after $cmd consecutive failures): $name — run with --include-quarantined to retry"
      return 3
      ;;
    held)
      case "$cmd" in
        env)
          warn "held (env HOLD=): $name — remove from HOLD= to resume this run only"
          ;;
        major:*)
          # cmd is major:<source>:<target|"unknown"> — a :major pin.
          local _majrest="${cmd#major:}"
          local _majsrc="${_majrest%%:*}"
          local _majtgt="${_majrest#*:}"
          if [[ "$_majtgt" == "unknown" ]]; then
            warn "held (:major pin, latest version unverified): $name — staying held (fail-safe); --unhold to force"
          elif [[ "$_majsrc" == "env" ]]; then
            warn "held (env HOLD=, major upgrade to $_majtgt blocked): $name — remove from HOLD= to allow"
          else
            warn "held (major upgrade to $_majtgt blocked): $name — remove the \":major\" hold to allow, or upgrade manually"
          fi
          ;;
        *)
          warn "held (config): $name — remove from \"hold\" to resume updates"
          ;;
      esac
      return 3
      ;;
    uptodate)
      ok "$name: already up to date (pre-check)"
      return 0
      ;;
    bulk)
      info "Updating all $name..."
      run_update "$name" "$cmd" "$fix"
      ;;
    known)
      if is_skipped "$name"; then
        log "${BLUE}-- $name skipped${NC}"
        return 3
      fi
      info "Updating $name..."
      run_update "$name" "$cmd" "$fix"
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
  local cmd_type name cmd lock_group fix
  _parse_emit_line "$line"
  cmd_type="$EMIT_TYPE"
  name="$EMIT_NAME"
  cmd="$EMIT_CMD"
  lock_group="${EMIT_LOCK:-$name}"
  fix="$EMIT_FIX"

  # Dry-run never mutates anything, so locking (which only exists to
  # serialize concurrent *writes* from the same package manager) is pointless
  # overhead there — every dry-run "job" is a near-instant echo of the
  # command it would run, so skip the lock round-trip entirely.
  if [[ -n "$DRY_RUN" ]]; then
    _run_one_emit_line_core "$cmd_type" "$name" "$cmd" "$fix"
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
        _run_one_emit_line_core "$cmd_type" "$name" "$cmd" "$fix"
      } 200>"$LOCK_DIR/${lock_group}.lock"
    else
      _run_with_mkdir_lock "$lock_group" "$cmd_type" "$name" "$cmd" "$fix"
      return $?
    fi
  else
    _run_one_emit_line_core "$cmd_type" "$name" "$cmd" "$fix"
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
# Python executor (tui_update_all_clis.py) — the single update executor.
#
# Every real (non-dry-run) update phase is delegated to
# tui_update_all_clis.py, which runs the exact same plan with the same
# semantics as the bash executor below (parallel cap, per-origin lock
# serialization, per-job watchdog, retry/fix policy, exit-code conventions)
# and writes result records in the same format run_updates_parallel's
# *.result files use. On an interactive terminal it renders the live
# dashboard; anywhere else its plain renderer prints the same lines the
# bash executor would. Everything before (discovery, prechecks, planning)
# and after (snapshots, run summary, history, notify, changelog) is
# unchanged.
#
# The bash executor remains for: --dry-run (prints "would run" lines only),
# --trace (bash -x is shell-only), and the UAC_EXECUTOR=bash escape hatch.
# -------------------------------------------------------------------
_bash_executor_wanted() {
  [[ -n "$TRACE" ]] && return 0                       # bash -x tracing is shell-only
  [[ "${UAC_EXECUTOR:-}" == "bash" ]] && return 0     # escape hatch
  [[ -f "$TUI_SCRIPT" ]] || return 0                  # runner not installed
  command -v python3 >/dev/null 2>&1 || return 0
  return 1
}

run_updates_python() {
  local _emit_file _results_file _rc=0
  _emit_file=$(mktemp)
  _results_file=$(mktemp)
  printf '%s\n' "$@" > "$_emit_file"
  local _fix="1" _mode="auto" _quiet=""
  [[ "$UAC_FIX" == "0" ]] && _fix="0"
  [[ "$TUI_MODE" == "0" ]] && _mode="plain"
  [[ "$TUI_MODE" == "1" ]] && _mode="live"
  # Quiet suppresses every executor message in the shell (log/info/ok/warn
  # all no-op), so it maps to the plain renderer's --quiet — never a
  # dashboard the user asked not to see output from.
  if [[ -n "$QUIET" ]]; then
    _mode="plain"
    _quiet="1"
  fi
  python3 "$TUI_SCRIPT" \
    --emit-file "$_emit_file" \
    --results-file "$_results_file" \
    --parallel "$PARALLEL_JOBS" \
    --timeout "$UAC_JOB_TIMEOUT" \
    --retries "$UAC_RETRIES" \
    --retry-delay "$UAC_RETRY_DELAY" \
    --fix "$_fix" \
    --mode "$_mode" \
    ${_quiet:+--quiet} \
    --skip "$SKIP" \
    --version-string "$UAC_VERSION" || _rc=$?
  # 130 = interrupted (Ctrl+C): the runner already reported it; don't warn.
  if (( _rc != 0 && _rc != 130 )); then
    warn "update runner exited with status $_rc — results may be incomplete"
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

  if [[ -n "$INSIGHTS_MODE" ]]; then
    python3 "$LIB_SCRIPT" insights "$HISTORY_FILE"
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
    python3 "$LIB_SCRIPT" cache-names "$CACHE_FILE" > "$_prev_names_snap" 2>/dev/null || true
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
    python3 "$LIB_SCRIPT" list-human "$CACHE_FILE" 2>/dev/null
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
    local _precheck_would _precheck_would_known
    _precheck_would=$(python3 "$LIB_SCRIPT" precheck-candidates 2>/dev/null || true)
    [[ -n "$_precheck_would" ]] && info "Would pre-check (dry-run, not executed): $_precheck_would"
    _precheck_would_known=$(UAC_PRECHECK_SKIP="$SKIP" python3 "$LIB_SCRIPT" precheck-known-candidates "$CACHE_FILE" 2>/dev/null || true)
    [[ -n "$_precheck_would_known" ]] && info "Would pre-check known tools (dry-run, not executed): $_precheck_would_known"
  elif [[ -n "$NO_PRECHECK" ]]; then
    debug "Pre-checks disabled (--no-precheck)"
  else
    info "Pre-checking for outdated packages..."
    python3 "$LIB_SCRIPT" precheck > "$_precheck_file" 2>/dev/null || echo "{}" > "$_precheck_file"
    # Second stage: known tools already at the latest version (reuses the
    # bulk checks' captured outdated lists for npm/brew; uv checks PyPI).
    # Fail-open like stage one: any error keeps the stage-one file.
    local _precheck_known_tmp
    _precheck_known_tmp=$(mktemp)
    if UAC_PRECHECK_SKIP="$SKIP" python3 "$LIB_SCRIPT" precheck-known "$CACHE_FILE" > "$_precheck_known_tmp" 2>/dev/null && [[ -s "$_precheck_known_tmp" ]]; then
      mv "$_precheck_known_tmp" "$_precheck_file"
    else
      rm -f "$_precheck_known_tmp"
    fi
    # "✓ x: already up to date (pre-check)" lines print later, when the run
    # loop processes each synthetic "uptodate" emit-line.
  fi
  export UAC_PRECHECK_UPTODATE_FILE="$_precheck_file"

  # Resolve "name:major" holds: a :major-pinned tool blocks only MAJOR
  # upgrades. This stage compares installed vs latest (registry lookups for
  # a handful of pinned tools at most) and writes {block:{name:target}}.
  # Not a pre-check (it decides holds, not skips), so --no-precheck does
  # not disable it. --dry-run runs no lookups; emit then treats every
  # :major hold as fail-safe held (v1 behavior).
  local _major_holds_file
  _major_holds_file=$(mktemp)
  if [[ -z "$DRY_RUN" ]]; then
    python3 "$LIB_SCRIPT" resolve-major-holds "$CACHE_FILE" > "$_major_holds_file" 2>/dev/null \
      || echo '{"block":{},"allow":[],"unknown":[]}' > "$_major_holds_file"
    export UAC_MAJOR_HOLDS_FILE="$_major_holds_file"
  else
    rm -f "$_major_holds_file"
    unset UAC_MAJOR_HOLDS_FILE
  fi

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
  } | python3 "$LIB_SCRIPT" lines-to-json > "$_quarantined_snap" 2>/dev/null || echo "[]" > "$_quarantined_snap"

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
  } | python3 "$LIB_SCRIPT" lines-to-json > "$_held_snap" 2>/dev/null || echo "[]" > "$_held_snap"

  # Failed names are collected after the run (from the executor's result
  # records) into _failed_snap — see below; both summaries consume it.

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

  if [[ -n "$DRY_RUN" ]]; then
    # Dry-run only prints "would run" lines — no locking, no subprocesses
    # beyond echos; deterministic plan order beats parallel subshells here.
    run_updates_sequential "${lines[@]:-}"
  elif _bash_executor_wanted; then
    if (( PARALLEL_JOBS < 2 )); then
      run_updates_sequential "${lines[@]:-}"
    else
      run_updates_parallel "$PARALLEL_JOBS" "${lines[@]:-}"
    fi
  else
    run_updates_python "${lines[@]:-}"
  fi

  if [[ -n "$_emit_snap" ]]; then
    # "" = no cache reuse; "$_before_snap" = mtime gate, reuse pre-run
    # version for any tool whose binary mtime hasn't changed since then.
    python3 "$LIB_SCRIPT" snapshot-versions "$_emit_snap" "" "$_before_snap" > "$_after_snap" 2>/dev/null || true
    # Failed job names for the summary's Failed section (result records are
    # kind\x1ename\x1ecmd\x1eec\x1estart\x1eend; ec 0=ok, 3=skipped).
    local _failed_snap
    _failed_snap=$(mktemp)
    {
      local _rline _rec_ec
      for _rline in "${_UAC_RESULT_LINES[@]:-}"; do
        [[ -z "$_rline" ]] && continue
        local _rest="${_rline#*"${_UAC_SEP}"}"
        local _rname="${_rest%%"${_UAC_SEP}"*}"
        _rest="${_rest#*"${_UAC_SEP}"}"
        _rest="${_rest#*"${_UAC_SEP}"}"
        _rec_ec="${_rest%%"${_UAC_SEP}"*}"
        [[ "$_rec_ec" != "0" && "$_rec_ec" != "3" ]] && printf '%s\n' "$_rname"
      done
    } | python3 "$LIB_SCRIPT" lines-to-json > "$_failed_snap" 2>/dev/null || echo "[]" > "$_failed_snap"
    # Terminal version-change list (before → after). Same text as the
    # desktop/email summary so every run surfaces what actually moved.
    local _summary_out=""
    _summary_out=$(python3 "$LIB_SCRIPT" run-summary "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" "$_new_tools_snap" "$_quarantined_snap" "$_held_snap" "$_failed_snap" 2>/dev/null || true)
    if [[ -n "$_summary_out" ]] && [[ -z "$QUIET" ]]; then
      log ""
      log "${BOLD}=== Packages updated ===${NC}"
      # Skip the leading "update-all-clis" / "Steps: …" header lines — those
      # are already covered by the run's own Done summary below.
      printf '%s\n' "$_summary_out" | tail -n +3 | while IFS= read -r _sline || [[ -n "$_sline" ]]; do
        log "$_sline"
      done
    fi
    if _want_notify_popup; then
      python3 "$LIB_SCRIPT" notify-diff "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" "$_new_tools_snap" "$_quarantined_snap" "$_held_snap" "$_failed_snap" 2>/dev/null || true
    fi
    if [[ -n "${UPDATE_ALL_CLIS_SUMMARY_FILE:-}" ]]; then
      if [[ -n "$_summary_out" ]]; then
        printf '%s' "$_summary_out" > "${UPDATE_ALL_CLIS_SUMMARY_FILE}"
      else
        python3 "$LIB_SCRIPT" run-summary "$_before_snap" "$_after_snap" "$UPDATE_OK" "$UPDATE_FAIL" "$_new_tools_snap" "$_quarantined_snap" "$_held_snap" "$_failed_snap" > "${UPDATE_ALL_CLIS_SUMMARY_FILE}" 2>/dev/null || true
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
  [[ -n "${_major_holds_file:-}" ]] && rm -f "$_major_holds_file"
  [[ -n "${_failed_snap:-}" ]] && rm -f "$_failed_snap"

  log ""
  log "${BOLD}=== Done! ===${NC}"
  log "Summary: ${UPDATE_OK} ok, ${UPDATE_FAIL} failed"

  # Auto-tip: bulk-covered tools missing from known list
  if [[ -z "$DRY_RUN" ]]; then
    local _known_summary _known_count _known_sample
    _known_summary=$(python3 "$LIB_SCRIPT" suggest-known-summary "$CACHE_FILE" 2>/dev/null || printf '0\t\n')
    _known_count="${_known_summary%%$'\t'*}"
    _known_sample="${_known_summary#*$'\t'}"
    if [[ "$_known_count" =~ ^[0-9]+$ ]] && [[ "$_known_count" -gt 0 ]]; then
      warn "$_known_count tools updated via bulk but not individually tracked (e.g., $_known_sample)"
      log "  Run './update_all_clis.sh --suggest-known' to see all candidates."
    fi
  fi

  # Auto-tip: discovered tools with no update path at all
  if [[ -z "$DRY_RUN" ]] && [[ -f "$UNKNOWN_LOG_FILE" ]]; then
    local _unknown_info
    _unknown_info=$(python3 "$LIB_SCRIPT" unknown-summary "$UNKNOWN_LOG_FILE" 2>/dev/null || echo "0")
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
    python3 "$LIB_SCRIPT" json-summary "$UPDATE_OK" "$UPDATE_FAIL"
  fi

  if [[ -n "$DRY_RUN" ]]; then
    exit 0
  fi
  if (( UPDATE_FAIL > 0 )); then
    exit 1
  fi
  exit 0
}

# UAC_SOURCE_ONLY lets test harnesses source this file for its functions
# (the executor parity test) without kicking off a full update run.
if [[ -z "${UAC_SOURCE_ONLY:-}" ]]; then
  main "$@"
fi
