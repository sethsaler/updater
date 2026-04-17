# update-all-clis

> One script to update every package manager and third-party CLI on your system.

`update-all-clis` auto-detects what you have installed and runs the right update command for each — npm, pip, brew, gem, cargo, hermes, agent, opencode, uv, firecrawl, mmx, and more. No configuration needed.

## Supported tools

### Package managers
| Manager | Update command | macOS | Linux |
|---------|---------------|-------|-------|
| npm | `npm update -g` | ✓ | ✓ |
| pip3 | `pip3 install -U --outdated` | ✓ | ✓ |
| Homebrew | `brew update && brew upgrade` | ✓ | — |
| Ruby Gems | `gem update --user-install` | ✓ | ✓ |
| Cargo | `cargo install-update -a` | ✓ | ✓ |
| Conda | `conda update --all` | ✓ | ✓ |

### Third-party CLIs and agents
| Tool | Update command |
|------|---------------|
| Hermes | `hermes update` |
| Cursor Agent | `agent update` |
| OpenCode | `opencode upgrade` |
| uv | `uv self update` |
| Firecrawl | `npm update -g firecrawl-cli` |
| mmx | `npm update -g mmx-cli` |
| Codex | `npm update -g codex-cli` |
| dev-browser | `npm update -g dev-browser` |
| TinyFish | `npm update -g tinyfish` |
| cline | `npm update -g cline` |

Tools not found on your system are silently skipped.

## Installation

### One-liner (recommended)
```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USER/update-all-clis/main/scripts/update_all_clis.sh | bash
```

Or download and run manually:
```bash
git clone https://github.com/YOUR_GITHUB_USER/update-all-clis.git
cd update-all-clis
./scripts/update_all_clis.sh
```

## Usage

```bash
./scripts/update_all_clis.sh
```

**Skip specific tools:**
```bash
SKIP=npm,brew ./scripts/update_all_clis.sh
```

**Quiet mode (errors only):**
```bash
QUIET=1 ./scripts/update_all_clis.sh
```

## Scheduled runs (LaunchAgent / cron)

### macOS LaunchAgent (daily at 8 AM)

Copy the plist template and load it:
```bash
cp launchd/com.sethsaler.update-all-clis.plist ~/Library/LaunchAgents/
# Edit the plist to replace the path with your actual path
launchctl load ~/Library/LaunchAgents/com.sethsaler.update-all-clis.plist
```

### Linux (cron)

Add to your crontab (`crontab -e`):
```
0 8 * * * /path/to/update-all-clis/scripts/update_all_clis.sh >> ~/.logs/update-all-clis.log 2>&1
```

## Adding new tools

The script is organized in two sections: `update_managers()` and `update_tools()`. To add a new tool:

```bash
run_tool "tool-name" "update-command"
```

If the binary name differs from the package name (like npm globals), pass it as a third arg:
```bash
run_tool "my-tool" "npm update -g my-tool-package" "my-tool"
```

## Requirements

- Bash 3.2+ (macOS default bash works)
- Git (to clone)
- Any of the supported tools above

## Contributing

Issues and PRs welcome. Please test changes with `SKIP=...` to avoid running actual updates during development.

## License

MIT
