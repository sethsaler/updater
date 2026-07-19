#!/usr/bin/env bash
# =============================================================================
# install.sh — Install update-all-clis and optionally set up a LaunchAgent
# Usage: ./install.sh [--launchd]
#
# Options:
#   --launchd        Set up a macOS LaunchAgent (interactive frequency prompt)
#   --dir <path>     Custom install directory (default: ~/update-all-clis)
# =============================================================================

set -eu

INSTALL_DIR="${INSTALL_DIR:-$HOME/update-all-clis}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/logs"

info() { echo -e "==> $*"; }
warn() { echo -e "!! $*"; }

install_script() {
  info "Installing to $INSTALL_DIR..."
  mkdir -p "$INSTALL_DIR"
  cp "$SCRIPT_DIR/update_all_clis.sh" "$INSTALL_DIR/"
  cp "$SCRIPT_DIR/tool_config.json" "$INSTALL_DIR/"
  cp "$SCRIPT_DIR/lib_update_all_clis.py" "$INSTALL_DIR/"
  cp "$SCRIPT_DIR/tui_update_all_clis.py" "$INSTALL_DIR/"
  chmod +x "$INSTALL_DIR/update_all_clis.sh"
  if [[ -d "$SCRIPT_DIR/scripts" ]]; then
    mkdir -p "$INSTALL_DIR/scripts"
    for f in "$SCRIPT_DIR/scripts"/*.sh; do
      [[ -f "$f" ]] || continue
      cp "$f" "$INSTALL_DIR/scripts/"
      chmod +x "$INSTALL_DIR/scripts/$(basename "$f")"
    done
  fi
  info "Installed. Run with: $INSTALL_DIR/update_all_clis.sh"
}

ask_frequency() {
  echo ""
  echo "How often should the updater run?"
  echo ""
  echo "  [1] Daily at 8:00 AM   (default)"
  echo "  [2] Every 6 hours"
  echo "  [3] Every 12 hours"
  echo "  [4] Weekly (Sunday at 8:00 AM)"
  echo "  [5] Manual — just install, no schedule"
  echo ""
  read -r -p "Enter choice [1-5, default 1]: " choice
  choice="${choice:-1}"
  echo ""
}

parse_frequency() {
  local choice="$1"
  case "$choice" in
    2) SCHEDULE_TYPE="interval"; INTERVAL_SEC=21600 ;;  # 6 hours
    3) SCHEDULE_TYPE="interval"; INTERVAL_SEC=43200 ;;  # 12 hours
    4) SCHEDULE_TYPE="weekly" ;;
    5) echo "Skipping LaunchAgent setup."; return 1 ;;
    *) SCHEDULE_TYPE="daily" ;;
  esac
  return 0
}

write_plist_daily() {
  local user="$1"
  local plist_dst="$2"
  local script_path="$3"
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
	<key>EnvironmentVariables</key>
	<dict>
		<key>UPDATE_ALL_CLIS_NO_NOTIFY</key>
		<string>1</string>
	</dict>
</dict>
</plist>
EOF
}

write_plist_interval() {
  local user="$1"
  local plist_dst="$2"
  local script_path="$3"
  local sec="$4"
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
	<key>StartInterval</key>
	<integer>${sec}</integer>
	<key>StandardOutPath</key>
	<string>${LOG_DIR}/update-all-clis.log</string>
	<key>StandardErrorPath</key>
	<string>${LOG_DIR}/update-all-clis.err</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<false/>
	<key>EnvironmentVariables</key>
	<dict>
		<key>UPDATE_ALL_CLIS_NO_NOTIFY</key>
		<string>1</string>
	</dict>
</dict>
</plist>
EOF
}

write_plist_weekly() {
  local user="$1"
  local plist_dst="$2"
  local script_path="$3"
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
		<key>Weekday</key>
		<integer>0</integer>
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
	<key>EnvironmentVariables</key>
	<dict>
		<key>UPDATE_ALL_CLIS_NO_NOTIFY</key>
		<string>1</string>
	</dict>
</dict>
</plist>
EOF
}

setup_launchd() {
  local user="${USER:-$(whoami)}"
  local plist_dst="$HOME/Library/LaunchAgents/com.${user}.update-all-clis.plist"
  local script_path="$INSTALL_DIR/update_all_clis.sh"

  mkdir -p "$LOG_DIR"
  mkdir -p "$HOME/Library/LaunchAgents"

  local SCHEDULE_TYPE="daily"
  local INTERVAL_SEC=0

  ask_frequency
  parse_frequency "$choice" || return 0

  case "$SCHEDULE_TYPE" in
    daily)
      info "Scheduling: daily at 8:00 AM"
      write_plist_daily "$user" "$plist_dst" "$script_path"
      ;;
    interval)
      local hours=$((INTERVAL_SEC / 3600))
      info "Scheduling: every ${hours} hours"
      write_plist_interval "$user" "$plist_dst" "$script_path" "$INTERVAL_SEC"
      ;;
    weekly)
      info "Scheduling: weekly on Sunday at 8:00 AM"
      write_plist_weekly "$user" "$plist_dst" "$script_path"
      ;;
  esac

  launchctl load "$plist_dst" 2>/dev/null || true
  info "LaunchAgent installed and loaded."
  info "  Label: com.${user}.update-all-clis"
  info "  Logs: $LOG_DIR/update-all-clis.log"
  info ""
  info "Manage with:"
  info "  launchctl unload $plist_dst   # pause"
  info "  launchctl load    $plist_dst   # resume"
  info "  launchctl remove  $plist_dst   # remove entirely"
}

setup_systemd() {
  local script_path="$INSTALL_DIR/update_all_clis.sh"
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local service_file="$unit_dir/update-all-clis.service"
  local timer_file="$unit_dir/update-all-clis.timer"

  mkdir -p "$LOG_DIR"
  mkdir -p "$unit_dir"

  cat > "$service_file" << EOF
[Unit]
Description=update-all-clis — update installed CLIs and package managers

[Service]
Type=oneshot
Environment=UPDATE_ALL_CLIS_NO_NOTIFY=1
Environment=UPDATE_ALL_CLIS_SUMMARY_FILE=${XDG_CONFIG_HOME:-$HOME/.config}/update-all-clis/last-run-summary.txt
ExecStart=${script_path}
StandardOutput=append:${LOG_DIR}/update-all-clis.log
StandardError=append:${LOG_DIR}/update-all-clis.err

[Install]
WantedBy=default.target
EOF

  cat > "$timer_file" << EOF
[Unit]
Description=Daily update-all-clis run at 8:00 AM

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now update-all-clis.timer 2>/dev/null || true
    info "systemd user timer installed (daily 8:00 AM)."
    info "  systemctl --user status update-all-clis.timer"
    info "  journalctl --user -u update-all-clis.service"
  else
    warn "systemctl not found — unit files written to $unit_dir"
    warn "Enable with: systemctl --user enable --now update-all-clis.timer"
  fi
}

usage() {
  echo "Usage: $0 [--launchd] [--systemd] [--dir <path>]"
  echo ""
  echo "  --launchd    Set up a macOS LaunchAgent (interactive)"
  echo "  --systemd    Set up a Linux systemd user timer (daily 8:00 AM)"
  echo "  --dir <path> Install to a custom directory"
  exit 1
}

main() {
  local do_launchd=false
  local do_systemd=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --launchd|-l) do_launchd=true; shift ;;
      --systemd|-s) do_systemd=true; shift ;;
      --dir) INSTALL_DIR="$2"; shift 2 ;;
      --help|-h) usage ;;
      *)
        echo "Unknown option: $1" >&2
        usage
        ;;
    esac
  done

  install_script

  if $do_launchd; then
    if [[ "$(uname)" != "Darwin" ]]; then
      warn "LaunchAgent is macOS-only. skipping."
    else
      setup_launchd
    fi
  fi

  if $do_systemd; then
    if [[ "$(uname)" != "Linux" ]]; then
      warn "systemd timer is Linux-only. skipping."
    else
      setup_systemd
    fi
  fi

  if ! $do_launchd && ! $do_systemd; then
    echo ""
    echo "To schedule automatic runs:"
    echo "  macOS:  ./install.sh --launchd"
    echo "  Linux:  ./install.sh --systemd"
  fi
}

main "$@"
