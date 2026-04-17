# update-all-clis

> One script to discover and update every CLI and package manager on your system.

`update-all-clis` scans your `$PATH`, `~/.local/bin`, `~/.cargo/bin`, `~/.npm-global/bin`, Homebrew Cellar, gem bins, and more — dynamically figuring out which package manager installed each tool, then running the right update command for each. Nothing is hardcoded about *what* you have installed.

## Supported update mechanisms

| Origin / Manager | Update command |
|---|---|
| npm global packages | `npm update -g` |
| Homebrew (macOS) | `brew update && brew upgrade` |
| Ruby Gems | `gem update --user-install` |
| Cargo (Rust) | `cargo install-update -a` |
| Conda | `conda update --all` |
| uv (Python) | `uv self update && uv tool update-all` |
| fnm (Node) | `fnm update` |
| Bun | `bun update` |
| Deno | `deno upgrade` |
| pyenv | `pyenv update` |
| rbenv | `brew upgrade rbenv ruby-build` (no-op if not installed via Homebrew; see below) |
| SDKMAN | `sdk selfupdate` (via `sdkman-init.sh`) |
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

### Origins with limited or environment-specific updates

- **rbenv** — Bulk update uses Homebrew when `rbenv` / `ruby-build` are installed as formulas. If you installed rbenv only from git, run `git -C "$(rbenv root)" pull` yourself when needed.
- **SDKMAN** — Runs `sdk selfupdate` (SDKMAN itself). Candidate SDK upgrades are not bulk-updated automatically.

## Installation

```bash
git clone https://github.com/sethsaler/updater.git
cd updater
./install.sh
```

Or run directly:

```bash
curl -fsSL https://raw.githubusercontent.com/sethsaler/updater/main/update_all_clis.sh -o update_all_clis.sh
curl -fsSL https://raw.githubusercontent.com/sethsaler/updater/main/tool_config.json -o tool_config.json
chmod +x update_all_clis.sh
```

Keep `tool_config.json` next to `update_all_clis.sh` (the script loads it from the same directory by default). Override with `CONFIG_FILE=/path/to/tool_config.json` if needed.

## Usage

```bash
./update_all_clis.sh                 # discover (if needed) + update everything
./update_all_clis.sh --list          # show discovered tools and exit
./update_all_clis.sh --rescan       # force fresh discovery scan
./update_all_clis.sh --dry-run      # show what would be updated
./update_all_clis.sh --no-scan      # use cached discovery only (even if older than 24h)
SKIP=hermes,uv ./update_all_clis.sh  # skip tools (environment)
./update_all_clis.sh --skip=hermes,uv  # same, via flag (overrides $SKIP if both set)
QUIET=1 ./update_all_clis.sh        # errors only
```

## How it works

1. **Discovery scan** — walks known tool directories (`~/.local/bin`, `~/.cargo/bin`, npm global bins, Homebrew Cellar, gem bins, etc.) and builds a JSON cache at `~/.config/update-all-clis/cache.json`
2. **Cache** — reused for **24 hours**: if the cache file exists and is newer than 24 hours, discovery is skipped (unless you pass `--rescan`). A normal run performs **at most one** full scan.
3. **`--no-scan`** — skips discovery entirely and uses the existing cache file (useful for offline runs or when you trust the last scan). If there is no cache, a scan runs anyway.
4. **Deduplication** — if you have 30 npm global packages, one `npm update -g` handles all of them (not 30 separate commands). Known standalone tools (hermes, agent, opencode, etc.) get their own self-update command each.
5. **Execution** — runs each unique update command in sequence

## Adding a new tool

Edit [`tool_config.json`](tool_config.json) in the repo (or next to the installed script):

- **`known`** — tool name → command to update that binary when it appears in the scan.
- **`bulk`** — origin label (e.g. `npm`, `brew`) → one command run once per run for all tools from that origin.

If a tool is installed via a package manager (npm, cargo, brew, etc.), it will usually be auto-discovered and updated with the bulk command for its origin.

## Requirements

- Bash 3.2+ (macOS default bash works)
- Python 3 (for JSON cache management and command planning)
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

Scheduled runs log to **`~/.config/update-all-clis/logs/`** (`update-all-clis.log` and `.err`).

### Linux (cron)

```bash
0 8 * * * /path/to/update_all_clis.sh >> ~/.config/update-all-clis/logs/update-all-clis.log 2>&1
```

## License

MIT
