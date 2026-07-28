#!/usr/bin/env bash
# =============================================================================
# quit_office_apps.sh — gracefully quit Microsoft Office / Teams so msupdate can run
#
# Used as a Topgrade pre_command before the microsoft_office step.
#
# Usage:
#   ./quit_office_apps.sh              # quit only
#   ./quit_office_apps.sh --update     # quit, then run Microsoft AutoUpdate (msupdate)
#   ./quit_office_apps.sh --dry-run
#   QUIT_OFFICE_RELAUNCH=1 ./quit_office_apps.sh --update   # relaunch apps that were open
#
# Env:
#   QUIT_OFFICE_RELAUNCH=1   after a successful --update, reopen apps that were running
#   QUIT_OFFICE_FORCE=1      if graceful quit fails, kill -15 then kill -9
#   QUIT_OFFICE_WAIT=60      seconds to wait for apps to exit (default 60)
# =============================================================================

set -euo pipefail

DRY_RUN=0
DO_UPDATE=0
RELAUNCH="${QUIT_OFFICE_RELAUNCH:-0}"
FORCE="${QUIT_OFFICE_FORCE:-0}"
WAIT_SECS="${QUIT_OFFICE_WAIT:-60}"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --update) DO_UPDATE=1 ;;
    -h|--help)
      sed -n '2,20p' "$0" | tr -d '#'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Display name (for osascript "quit app") | process name patterns to detect
# Bundles match what MAU codes refer to (TEAMS21, MSWD2019, XCEL2019, …).
APPS=(
  "Microsoft Teams|MSTeams|Microsoft Teams"
  "Microsoft Word|Microsoft Word"
  "Microsoft Excel|Microsoft Excel"
  "Microsoft PowerPoint|Microsoft PowerPoint"
  "Microsoft Outlook|Microsoft Outlook"
  "Microsoft OneNote|Microsoft OneNote"
  "OneNote|OneNote"
)

MSUPDATE="/Library/Application Support/Microsoft/MAU2.0/Microsoft AutoUpdate.app/Contents/MacOS/msupdate"
STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/update-all-clis"
STATE_FILE="$STATE_DIR/office-apps-were-running.txt"

was_running=()

app_is_running() {
  local display="$1"
  shift
  local pat
  for pat in "$@"; do
    if pgrep -x "$pat" >/dev/null 2>&1; then
      return 0
    fi
    # Teams and some helpers don't always match -x
    if pgrep -f "/Applications/${pat}\\.app/" >/dev/null 2>&1; then
      return 0
    fi
  done
  # Fallback: System Events process name
  if osascript -e "tell application \"System Events\" to (name of processes) contains \"${display}\"" 2>/dev/null | grep -qi true; then
    return 0
  fi
  return 1
}

quit_app() {
  local display="$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] quit app \"$display\""
    return 0
  fi
  echo "  → quitting $display"
  # Prefer AppleEvent quit (saves docs / clean shutdown)
  osascript -e "tell application \"${display}\" to quit" 2>/dev/null || true
}

wait_until_gone() {
  local display="$1"
  shift
  local i=0
  while app_is_running "$display" "$@"; do
    if [[ $i -ge $WAIT_SECS ]]; then
      if [[ "$FORCE" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
        echo "  ! $display still running after ${WAIT_SECS}s — force kill"
        for pat in "$@"; do
          pkill -15 -x "$pat" 2>/dev/null || true
          pkill -15 -f "/Applications/${pat}\\.app/" 2>/dev/null || true
        done
        sleep 2
        for pat in "$@"; do
          pkill -9 -x "$pat" 2>/dev/null || true
          pkill -9 -f "/Applications/${pat}\\.app/" 2>/dev/null || true
        done
      else
        echo "  ! $display still running after ${WAIT_SECS}s (set QUIT_OFFICE_FORCE=1 to kill)" >&2
        return 1
      fi
      break
    fi
    sleep 1
    i=$((i + 1))
  done
  return 0
}

mkdir -p "$STATE_DIR"
: >"$STATE_FILE"

# Split "Display|pat1|pat2|..." into display + pats array
parse_app_entry() {
  # sets globals _display and _pats
  local entry="$1"
  _display="${entry%%|*}"
  local rest="${entry#*|}"
  _pats=()
  if [[ "$rest" == "$entry" ]]; then
    _pats=("$_display")
    return
  fi
  IFS='|' read -ra _pats <<<"$rest"
}

echo "==> Checking Microsoft Office / Teams apps"
any=0
for entry in "${APPS[@]}"; do
  parse_app_entry "$entry"
  if app_is_running "$_display" "${_pats[@]}"; then
    echo "  · open: $_display"
    was_running+=("$_display")
    echo "$_display" >>"$STATE_FILE"
    any=1
  fi
done

if [[ $any -eq 0 ]]; then
  echo "  (none running)"
else
  echo "==> Quitting open apps (graceful)"
  for display in "${was_running[@]}"; do
    quit_app "$display"
  done
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "==> [dry-run] skip wait / force-kill"
  else
    echo "==> Waiting up to ${WAIT_SECS}s for exit"
    failed_wait=0
    for entry in "${APPS[@]}"; do
      parse_app_entry "$entry"
      if printf '%s\n' "${was_running[@]+"${was_running[@]}"}" | grep -Fxq "$_display"; then
        if ! wait_until_gone "$_display" "${_pats[@]}"; then
          failed_wait=1
        fi
      fi
    done
    if [[ $failed_wait -eq 1 && "$DO_UPDATE" -eq 1 ]]; then
      echo "!! Some apps still open — msupdate will likely fail (App is Open)" >&2
    fi
  fi
fi

if [[ "$DO_UPDATE" -eq 1 ]]; then
  if [[ ! -x "$MSUPDATE" ]]; then
    echo "!! msupdate not found at: $MSUPDATE" >&2
    exit 1
  fi
  echo "==> Microsoft AutoUpdate"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] $MSUPDATE --install --wait 600"
  else
    # Same flags Topgrade uses
    "$MSUPDATE" --install --wait 600
  fi
fi

if [[ "$RELAUNCH" -eq 1 && "$DRY_RUN" -eq 0 && -s "$STATE_FILE" ]]; then
  echo "==> Relaunching apps that were open"
  while IFS= read -r display; do
    [[ -z "$display" ]] && continue
    echo "  → open -a \"$display\""
    open -a "$display" 2>/dev/null || true
  done <"$STATE_FILE"
fi

echo "==> Done"
exit 0
