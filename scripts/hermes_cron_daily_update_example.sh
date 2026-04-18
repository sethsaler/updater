#!/usr/bin/env bash
# Example: daily CLI updates + summary file for Hermes cron or Agent Mail.
#
# 1) Run the updater and write a plain-text summary (same content as the macOS dialog,
#    but not truncated) to last-run-summary.txt:
#
#   export UPDATE_ALL_CLIS_SUMMARY_FILE="$HOME/.config/update-all-clis/last-run-summary.txt"
#   /path/to/update_all_clis.sh
#
# 2) Hermes built-in email (IMAP/SMTP): use Hermes cron with email delivery so the
#    agent emails you the digest. Requires `hermes gateway` running and Email configured
#    in ~/.hermes/.env (see https://hermes-agent.nousresearch.com/docs/user-guide/messaging/email).
#
#   hermes cron create "0 8 * * *" \
#     "Read the file at $HOME/.config/update-all-clis/last-run-summary.txt and email me a clear summary of what changed (tools and package managers). If the file is missing or empty, say so briefly." \
#     --name "Daily CLI update digest" \
#     --deliver email
#
#    Or point the prompt at the log instead:
#   hermes cron create "0 8 * * *" \
#     "Summarize the last update-all-clis run from \$HOME/.config/update-all-clis/logs/update-all-clis.log — what was updated and any failures." \
#     --name "Daily CLI update digest" \
#     --deliver email
#
# 3) Agent Mail (separate API): after the updater runs, send the summary file:
#
#   export AGENTMAIL_API_KEY=...
#   export UPDATE_ALL_CLIS_AGENTMAIL_INBOX_ID=inb_xxx
#   export UPDATE_ALL_CLIS_EMAIL_TO=you@example.com
#   /path/to/scripts/agentmail_send_update_summary.sh
#
# This script only demonstrates (1) when executed directly.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export UPDATE_ALL_CLIS_SUMMARY_FILE="${UPDATE_ALL_CLIS_SUMMARY_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/last-run-summary.txt}"
mkdir -p "$(dirname "$UPDATE_ALL_CLIS_SUMMARY_FILE")"
exec "$ROOT/update_all_clis.sh" "$@"
