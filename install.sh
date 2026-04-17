#!/usr/bin/env bash
# =============================================================================
# install.sh — Install update-all-clis and optionally set up a LaunchAgent
# Usage: ./install.sh [--launchd]
# =============================================================================

set -e

INSTALL_DIR="${INSTALL_DIR:-$HOME/update-all-clis}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/.hermes/logs"

info() { echo -e "==> $*"; }
warn() { echo -e "!! $*"; }

install_script() {
  info "Installing to $INSTALL_DIR..."
  mkdir -p "$INSTALL_DIR"
  cp -r "$SCRIPT_DIR/scripts" "$INSTALL_DIR/"
  chmod +x "$INSTALL_DIR/scripts/update_all_clis.sh"
  info "Installed. Run with: $INSTALL_DIR/scripts/update_all_clis.sh"
}

setup_launchd() {
  local user="${USER:-$(whoami)}"
  local plist_src="$SCRIPT_DIR/launchd/com.YOURUSER.update-all-clis.plist"
  local plist_dst="$HOME/Library/LaunchAgents/com.${user}.update-all-clis.plist"
  local script_path="$INSTALL_DIR/scripts/update_all_clis.sh"

  info "Setting up LaunchAgent for daily 8 AM run..."

  # Create log directory
  mkdir -p "$LOG_DIR"

  # Generate the plist with correct paths
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$plist_dst" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.${user}.update-all-clis</string>
	<key>ProgramArguments</key>
	<array>
		<string>${script_path}</string>
	</array>
	<key>StartCalendarInterval</key>
	<dict>
		<key>Hour</key>
		<integer>8</integer>
		<key>Minute</key>
		<integer>0</integer>
	</dict>
	<key>StandardOutPath</key>
	<string>${LOG_DIR}/update-all-clis.log</string>
	<key>StandardErrorPath</key>
	<string>${LOG_DIR}/update-all-clis.err</string>
	<key>RunAtLoad</key>
	<false/>
	<key>KeepAlive</key>
	<false/>
</dict>
</plist>
EOF

  launchctl load "$plist_dst" 2>/dev/null || true
  info "LaunchAgent installed and loaded."
  info "  Runs daily at 8:00 AM"
  info "  Logs: $LOG_DIR/update-all-clis.log"
  info "  Manage: launchctl unload $plist_dst"
}

main() {
  install_script

  if [[ "${1:-}" == "--launchd" ]] || [[ "${1:-}" == "-l" ]]; then
    setup_launchd
  else
    echo ""
    echo "To also set up the daily 8 AM LaunchAgent, run:"
    echo "  ./install.sh --launchd"
  fi
}

main "$@"
