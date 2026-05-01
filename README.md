# update-all-clis

> One script to discover and update every CLI and package manager on your system.

`update-all-clis` scans `~/.local/bin`, `~/.cargo/bin`, npm global bins (including `pnpm`, `yarn`, and nvm-installed packages), Homebrew Cellar, gem bins, Go tool bins, dotnet tools, krew plugins, mise shims, `~/bin`, Wasmtime/Wasmer runtimes, and **all user-writable directories on `$PATH`** — then runs the right update command for each. Nothing is hardcoded about *what* you have installed.

Ships with [`update_all_clis.sh`](update_all_clis.sh), [`tool_config.json`](tool_config.json), and [`lib_update_all_clis.py`](lib_update_all_clis.py) (merge, validation, and command planning).

## Supported update mechanisms

### Bulk (package-manager-level)

| Origin / Manager | Update command |
|---|---|
| npm global packages | `npm update -g` |
| Homebrew (macOS) | `brew update && brew upgrade` |
| Ruby Gems | `gem update --user-install` |
| Cargo (Rust) | `cargo install-update -a` |
| Conda | `conda update --all` |
| uv (Python) | `uv self update && uv tool upgrade --all` |
| Go tools | `go install golang.org/x/tools/gopls@latest` |
| .NET tools | `dotnet tool update --global` |
| Krew (kubectl plugins) | `kubectl krew upgrade` |
| Mise (version manager) | `mise self-update` |
| fnm (Node) | `fnm update` |
| Bun | `bun update` |
| Deno | `deno upgrade` |
| pyenv | `pyenv update` |
| rbenv | `brew upgrade rbenv ruby-build` (no-op if not installed via Homebrew; see below) |
| SDKMAN | `sdk selfupdate` (via `sdkman-init.sh`) |

### Known tools (individual commands)

| Tool | Update command |
|---|---|
| Hermes | `hermes update` |
| Cursor Agent | `agent update` |
| OpenCode | `opencode upgrade` |
| uv (self + tools) | `uv self update && uv tool upgrade --all` |
| GitHub CLI | `gh auth refresh` / `gh upgrade` |
| Ollama | `ollama update` |
| Goose | `goose update` |
| Kimi | `kimi update` |
| Warp | `warp-cli update` |
| Mise | `mise self-update` |
| Starship | `starship self-update` |
| Zoxide | `zoxide update` / `zoxide self-update` |
| Atuin | `atuin update` |
| Just | `cargo install just --locked` |
| fzf | `brew upgrade fzf` |
| lazygit | `brew upgrade lazygit` |
| bat | `brew upgrade bat` |
| eza | `cargo install eza --locked` |
| ripgrep | `brew upgrade ripgrep` |
| fd | `brew upgrade fd` |
| yazi | `brew upgrade yazi` |
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
- **`path`** — **Enabled by default**. Binaries found under `$PATH` directories (excluding system dirs like `/usr/bin`, `/bin`, `/sbin`) get origin `path`. There is no default bulk update for `path`; add a `"path"` entry under `bulk` in [`config.local.json`](#configuration-merge-and-overrides) if you want a single command for all of them, or list tools under `known`. Use `--no-scan-path` to disable PATH scanning.

## Installation

```bash
git clone https://github.com/sethsaler/updater.git
cd updater
./install.sh
```

Or run directly (download all three files into the same directory):

```bash
curl -fsSL https://raw.githubusercontent.com/sethsaler/updater/main/update_all_clis.sh -o update_all_clis.sh
curl -fsSL https://raw.githubusercontent.com/sethsaler/updater/main/tool_config.json -o tool_config.json
curl -fsSL https://raw.githubusercontent.com/sethsaler/updater/main/lib_update_all_clis.py -o lib_update_all_clis.py
chmod +x update_all_clis.sh
```

Override paths with `CONFIG_FILE`, `LIB_SCRIPT`, or `CONFIG_LOCAL_FILE` if you keep files elsewhere.

## Usage

```bash
./update_all_clis.sh --version
./update_all_clis.sh                 # discover (if needed) + update everything
./update_all_clis.sh --list          # show discovered tools and exit
./update_all_clis.sh --json          # JSON list of tools (implies --list)
./update_all_clis.sh --rescan        # force fresh discovery scan
./update_all_clis.sh --dry-run       # show what would be updated
./update_all_clis.sh --no-scan       # use cached discovery only (even if older than TTL)
./update_all_clis.sh --json-summary  # after run, print one JSON line: {"ok":N,"failed":M}
./update_all_clis.sh --trace         # bash -x when running each update command
./update_all_clis.sh --no-scan-path  # skip scanning directories on $PATH
./update_all_clis.sh --parallel=4    # run up to 4 updates at once (default 1)
./update_all_clis.sh --only-origins=brew,npm
./update_all_clis.sh --skip-origins=gem
SKIP=hermes,uv ./update_all_clis.sh
./update_all_clis.sh --skip=hermes,uv
QUIET=1 ./update_all_clis.sh
```

### Configuration merge and overrides

- **`~/.config/update-all-clis/config.local.json`** (override path with `CONFIG_LOCAL_FILE`) — optional. If present, its `known` and `bulk` objects are **merged on top of** `tool_config.json` so you can add or override commands without editing the repo file.
- **`CACHE_TTL_HOURS`** — cache freshness in hours (default **24**). Discovery runs again when the cache is older than this (unless `--no-scan` or a fresh cache is still valid).
- **`ONLY_ORIGINS`** — comma-separated origins (and known tool names) to **restrict** what runs. When set, bulk updates run only for listed origins; known tools run only if their `origin` or `name` is listed.
- **`SKIP_ORIGINS`** — comma-separated origins to skip for bulk updates; known tools whose `origin` is listed are skipped.

### macOS summary dialog (manual runs)

On **macOS**, after a real update run (not `--dry-run`), the script can show a **modal dialog** with a short summary: ok/fail counts, **known tools** with version strings before → after (best effort via `--version` / `-V`), and **bulk origins** with a manager/environment line before → after (e.g. `brew --version`, `npm --version`).

- **Default:** dialog runs when **stdout is a TTY** (interactive Terminal / iTerm). **No dialog** when stdout is not a TTY (LaunchAgent, cron, background jobs).
- **`UPDATE_ALL_CLIS_NO_NOTIFY=1`** — never show the dialog (set automatically for **LaunchAgent** plists generated by [`install.sh`](install.sh)).
- **`UPDATE_ALL_CLIS_NOTIFY=1`** — always try to show the dialog (even without a TTY).
- **`UPDATE_ALL_CLIS_NOTIFY=0`** — never show the dialog.

### Suggest command for unknown tools

After a discovery run, use the `suggest` command to find tools that were discovered but have no update command configured:

```bash
python3 lib_update_all_clis.py suggest ~/.config/update-all-clis/cache.json
```

This outputs a ready-to-paste `config.local.json` snippet with every discovered tool that isn't yet covered by `known` or `bulk` entries. Fill in the update commands and save to `~/.config/update-all-clis/config.local.json`.

### Email via Agent Mail CLI

You do not need another agent: install the [Agent Mail CLI](https://www.npmjs.com/package/agentmail-cli), set `AGENTMAIL_API_KEY` and an inbox id, then after each run send the digest with [`scripts/agentmail_send_update_summary.sh`](scripts/agentmail_send_update_summary.sh). Cron-friendly examples are in [`scripts/agentmail_daily_example.sh`](scripts/agentmail_daily_example.sh).

**What to email**

- **Structured summary (recommended)** — set **`UPDATE_ALL_CLIS_SUMMARY_FILE`** so each real run writes a **full plain-text summary** (known tools + bulk origins, before → after; same idea as the macOS dialog, not truncated):

```bash
export UPDATE_ALL_CLIS_SUMMARY_FILE="$HOME/.config/update-all-clis/last-run-summary.txt"
./update_all_clis.sh
# then: scripts/agentmail_send_update_summary.sh
```

- **Raw log** — if you redirect stdout/stderr to `~/.config/update-all-clis/logs/update-all-clis.log`, point the mailer at that file:

```bash
export UPDATE_ALL_CLIS_AGENTMAIL_TEXT_FILE="$HOME/.config/update-all-clis/logs/update-all-clis.log"
scripts/agentmail_send_update_summary.sh
```

Version lines are **best effort**; some tools do not expose a parseable version or may report `?`.

### Exit status

- **0** — all executed update steps succeeded, or **`--dry-run`** (always success).
- **1** — at least one update step failed, or invalid arguments, or missing `tool_config.json` / `lib_update_all_clis.py`.

## How it works

1. **Discovery scan** — walks 20+ known tool directories (`~/.local/bin`, `~/.cargo/bin`, npm globals, Homebrew, Go bins, dotnet tools, krew, mise, etc.) **and** scans user-writable directories on `$PATH` (skipping system dirs like `/usr/bin`, `/bin`), then writes `~/.config/update-all-clis/cache.json`.
2. **Symlink inference** — if a binary in a generic directory (e.g., `~/.local/bin`) is a symlink into a package manager tree (e.g., `node_modules`), it's routed to that manager's bulk update.
3. **Cache** — reused until it is older than **`CACHE_TTL_HOURS`** (default 24h), unless you pass **`--rescan`**. A normal run performs **at most one** full scan.
4. **`--no-scan`** — uses the existing cache when possible (see main script help for edge cases).
5. **Deduplication** — one bulk command per origin (e.g. one `npm update -g` for all npm globals). Known tools get their own command when listed in merged config.
6. **Execution** — sequential by default; **`--parallel=N`** runs multiple update steps concurrently (tracing is disabled for parallel runs).

## Adding a new tool

Edit [`tool_config.json`](tool_config.json) or your **`config.local.json`**:

- **`known`** — tool name → command to run for that binary.
- **`bulk`** — origin label → one command per run for that origin.

[`lib_update_all_clis.py`](lib_update_all_clis.py) validates that both sections exist and that every command is a string.

After a discovery scan, run the **suggest** command to see which discovered tools aren't yet covered:

```bash
python3 lib_update_all_clis.py suggest ~/.config/update-all-clis/cache.json
```

## Requirements

- Bash 3.2+ (macOS default bash works)
- Python 3.6+ (for `lib_update_all_clis.py` and cache handling)
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
