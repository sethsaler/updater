# Changelog

## 0.5.0

- Align `tool_config.json` with README: `brew update`, Go install commands, `codex`/`mmx` aliases.
- Discovery: Go `GOPATH`/`GOBIN` bins, conda, nvm, pipx, Linux Homebrew.
- Parallel runs use per–package-manager `flock` locks to avoid npm/brew races.
- `--json-plan` prints the update plan as JSON.
- Linux desktop notifications via `notify-send` when stdout is a TTY.
- `./install.sh --systemd` for scheduled runs on Linux.
- Python unit tests and expanded CI fixture.
- JSON Schema for config files; shared `probe_version` helper.

## 0.4.0

- Unknown tools log, `--report-unknown`, `--ack-unknown`.
- macOS summary dialog and Agent Mail integration scripts.
