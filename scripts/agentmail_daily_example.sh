#!/usr/bin/env bash
# Example: run update-all-clis on a schedule, then email the digest with Agent Mail.
# No Hermes or other agent required — only the official agentmail CLI.
#
# One-time setup:
#   npm install -g agentmail-cli
#   export AGENTMAIL_API_KEY=am_us_xxx          # https://docs.agentmail.to
#   export UPDATE_ALL_CLIS_AGENTMAIL_INBOX_ID=inb_xxx
#   export UPDATE_ALL_CLIS_EMAIL_TO=you@example.com
#
# Daily cron (8:00) — structured summary (recommended):
#   0 8 * * * UPDATE_ALL_CLIS_SUMMARY_FILE="$HOME/.config/update-all-clis/last-run-summary.txt" /path/to/update_all_clis.sh >> "$HOME/.config/update-all-clis/logs/update-all-clis.log" 2>&1 && /path/to/scripts/agentmail_send_update_summary.sh
#
# Same but attach the raw log as the email body instead:
#   export UPDATE_ALL_CLIS_AGENTMAIL_TEXT_FILE="$HOME/.config/update-all-clis/logs/update-all-clis.log"
#   ... && /path/to/scripts/agentmail_send_update_summary.sh
#
# This script only runs the updater with a summary file path (same as a typical cron line).

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export UPDATE_ALL_CLIS_SUMMARY_FILE="${UPDATE_ALL_CLIS_SUMMARY_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/last-run-summary.txt}"
mkdir -p "$(dirname "$UPDATE_ALL_CLIS_SUMMARY_FILE")"
exec "$ROOT/update_all_clis.sh" "$@"
