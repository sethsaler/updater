#!/usr/bin/env bash
# Send the latest update-all-clis run summary via Agent Mail (https://agentmail.to).
# Requires: npm install -g agentmail-cli, AGENTMAIL_API_KEY, and an inbox id.
#
# Usage:
#   export AGENTMAIL_API_KEY=am_us_xxx
#   export UPDATE_ALL_CLIS_AGENTMAIL_INBOX_ID=inb_xxx
#   export UPDATE_ALL_CLIS_EMAIL_TO=you@example.com
#   ./scripts/agentmail_send_update_summary.sh [path-to-summary.txt]
#
# Default summary path: ~/.config/update-all-clis/last-run-summary.txt

set -euo pipefail

SUMMARY_FILE="${1:-${UPDATE_ALL_CLIS_SUMMARY_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/last-run-summary.txt}}"
INBOX_ID="${UPDATE_ALL_CLIS_AGENTMAIL_INBOX_ID:-${AGENTMAIL_INBOX_ID:-}}"
TO_ADDR="${UPDATE_ALL_CLIS_EMAIL_TO:-}"

if [[ ! -f "$SUMMARY_FILE" ]]; then
  echo "No summary file at $SUMMARY_FILE — run update_all_clis.sh with UPDATE_ALL_CLIS_SUMMARY_FILE set first." >&2
  exit 1
fi

if [[ -z "$INBOX_ID" ]]; then
  echo "Set UPDATE_ALL_CLIS_AGENTMAIL_INBOX_ID (or AGENTMAIL_INBOX_ID) to your Agent Mail inbox id." >&2
  exit 1
fi

if [[ -z "$TO_ADDR" ]]; then
  echo "Set UPDATE_ALL_CLIS_EMAIL_TO to the recipient address." >&2
  exit 1
fi

if ! command -v agentmail >/dev/null 2>&1; then
  echo "agentmail CLI not found. Install with: npm install -g agentmail-cli" >&2
  exit 1
fi

if [[ -z "${AGENTMAIL_API_KEY:-}" ]]; then
  echo "Set AGENTMAIL_API_KEY (see https://docs.agentmail.to)." >&2
  exit 1
fi

SUBJECT="${UPDATE_ALL_CLIS_EMAIL_SUBJECT:-update-all-clis $(date -u +%Y-%m-%d)}"

# shellcheck disable=SC2016
agentmail inboxes:messages send \
  --inbox-id "$INBOX_ID" \
  --to "$TO_ADDR" \
  --subject "$SUBJECT" \
  --text "$(cat "$SUMMARY_FILE")"
