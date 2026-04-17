# update-all-clis

> One script to discover and update every CLI and package manager on your system.

`update-all-clis` scans your `$PATH`, `~/.local/bin`, `~/.cargo/bin`, `~/.npm-global/bin`, Homebrew Cellar, gem bins, and more — dynamically figuring out which package manager installed each tool, then running the right update command for each. Nothing is hardcoded about *what* you have installed.

## Supported update mechanisms

| Origin / Manager | Update command |
|---|---|
| npm global packages | `npm update -g` |
| pip3 | `pip3 install -U --outdated` |
| Homebrew (macOS) | `brew update && brew upgrade` |
| Ruby Gems | `gem update --user-install` |
| Cargo (Rust) | `cargo install-update -a` |
| Conda | `conda update --all` |
| uv (Python) | `uv self update && uv tool update-all` |
| fnm (Node) | `fnm update` |
| Bun | `bun update` |
| Hermes | `hermes update` |
| Cursor Agent | `agent update` |
| OpenCode | `opencode upgrade` |
| GitHub CLI | `gh auth refresh` / `gh upgrade` |
| Ollama | `ollama update` |
| Goose | `goose update` |
| Kimi | `kimi update` |
| Warp | `warp-cli update` |
| Firecrawl | `npm update -g firecrawl-cli` |
| mmx | `npm update -g mmx-cli` |
| Codex | `npm update -g codex-cli` |
| dev-browser | `npm update -g dev-browser` |
| TinyFish | `npm update -g tinyfish` |
| cline | `npm update -g cline` |

Tools not found on your system are silently skipped. Unknown tools are skipped silently. The script never updates things it can't update.

## Installation

```bash
git clone https://github.com/YOUR_GITHUB_USER/updater.git
cd updater
./install.sh
```

Or run directly:

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USER/updater/main/update_all_clis.sh | bash
```

## Usage

```bash
./update_all_clis.sh              # discover + update everything
./update_all_clis.sh --list       # show discovered tools and exit
./update_all_clis.sh --rescan     # force fresh discovery scan
./update_all_clis.sh --dry-run    # show what would be updated
SKIP=hermes,uv ./update_all_clis.sh   # skip specific tools
QUIET=1 ./update_all_clis.sh          # errors only
```

## How it works

1. **Discovery scan** — walks known tool directories (`~/.local/bin`, `~/.cargo/bin`, npm global bins, Homebrew Cellar, gem bins, etc.) and builds a JSON cache at `~/.config/update-all-clis/cache.json`
2. **Cache** — reused for 24 hours; rescan with `--rescan` to pick up newly installed tools
3. **Deduplication** — if you have 30 npm global packages, one `npm update -g` handles all of them (not 30 separate commands). Known standalone tools (hermes, agent, opencode, etc.) get their own self-update command each.
4. **Execution** — runs each unique update command in sequence

## Adding a new tool

Add it to the `KNOWN` dict and `SELF_CMD` map in the Python block near the bottom of the script:

```python
'newtool': 'newtool self-update',
```

If it's installed via a package manager (npm, cargo, brew, etc.), it will be auto-discovered and updated with the bulk command for its origin.

## Requirements

- Bash 3.2+ (macOS default bash works)
- Python 3 (for JSON cache management)
- Git

## Scheduling

### macOS LaunchAgent

```bash
./install.sh --launchd
```

The installer will prompt you to choose a schedule:

```
How often should the updater run?

  [1] Daily at 8:00 AM   (default)
  [2] Every 6 hours
  [3] Every 12 hours
  [4] Weekly (Sunday at 8:00 AM)
  [5] Manual — just install, no schedule
```

### Linux (cron)

```bash
0 8 * * * /path/to/update_all_clis.sh >> ~/.logs/update-all-clis.log 2>&1
```

## License

MIT
