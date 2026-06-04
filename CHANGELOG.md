# Changelog

## 0.6.0

- Performance improvements: reduced rate limiting delay from 0.1s to 0.01s, increased ThreadPoolExecutor workers from 8 to 16, reduced subprocess timeout from 15s to 5s
- Increased default parallel jobs from 4 to 8 for faster concurrent execution
- Fixed bash array unbound variable error in parallel execution
- Version caching: preserve tool versions across rescans to avoid redundant probing
- Cache validation and debugging: added `--validate-cache` and `--debug-cache` options for diagnostics
- Discovery improvements: fixed npm global discovery (added ~/.npm-global/bin scan), fixed bun discovery (added ~/.bun/bin scan)
- Enhanced npm prefix logic to scan both /bin and lib paths
- Added comprehensive unit tests for version caching functions and integration tests for discovery path coverage
- Added `lit` to known tools
- Improved parallel update execution with proper result tracking

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
