# update-all-clis

> One script to discover and update every CLI and package manager on your system.

`update-all-clis` scans `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, npm global bins (including `~/.npm-global/bin`, `pnpm`, `yarn`, and nvm-installed packages), Homebrew Cellar, gem bins, Go tool bins, dotnet tools, krew plugins, mise shims, `~/bin`, Wasmtime/Wasmer runtimes, and **all user-writable directories on `$PATH`** — then runs the right update command for each. Nothing is hardcoded about *what* you have installed.

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
|------|---------------|
| Cursor Agent | `agent update` |
| Atuin | `atuin update` |
| bat | `brew upgrade bat` |
| Browserbase (bb) | `npm update -g @browserbasehq/cli` |
| Browserbase Browse (browse) | `npm update -g @browserbasehq/browse-cli` |
| Claude Code | `claude update` |
| cline | `npm update -g cline` |
| Codex CLI | `npm update -g codex-cli` |
| Composio | `composio upgrade` |
| dev-browser | `npm update -g dev-browser` |
| Devin | `devin update` |
| ElevenLabs | `npm update -g @elevenlabs/cli` |
| ESPN PP CLI | `go install github.com/mvanhorn/printing-press-library/library/media-and-entertainment/espn/cmd/espn-pp-cli@latest` |
| expect-cli | `npm update -g expect-cli` |
| eza | `cargo install eza --locked` |
| fd | `brew upgrade fd` |
| FieldTheory | `npm update -g fieldtheory` |
| Firecrawl | `npm update -g firecrawl-cli` |
| Flight Goat PP CLI | `go install github.com/mvanhorn/printing-press-library/library/travel/flight-goat/cmd/flight-goat-pp-cli@latest` |
| fzf | `brew upgrade fzf` |
| Gemini CLI | `npm update -g @google/gemini-cli` |
| Genspark | `npm update -g @genspark/cli` |
| GitHub CLI (gh) | `gh auth refresh` / `gh upgrade` |
| Goose | `goose update` |
| Hermes | `hermes update` |
| just | `cargo install just --locked` |
| kanban | `npm update -g kanban` |
| Kimi | `kimi update` |
| lazygit | `brew upgrade lazygit` |
| mcp-remote | `npm update -g mcp-remote` |
| Mem0 | `npm update -g @mem0/cli` |
| mise | `mise self-update` |
| mmx | `npm update -g mmx-cli` |
| Movie Goat PP CLI | `go install github.com/mvanhorn/printing-press-library/library/media-and-entertainment/movie-goat/cmd/movie-goat-pp-cli@latest` |
| ntn | `ntn update` |
| Ollama | `ollama update` |
| 1Password CLI (op) | `op update` |
| OpenClaw | `npm update -g openclaw` |
| OpenCode | `opencode upgrade` |
| Printing Press | `go install github.com/mvanhorn/cli-printing-press/v4/cmd/printing-press@latest` |
| Readwise | `npm update -g @readwise/cli` |
| Recipe Goat PP CLI | `go install github.com/mvanhorn/printing-press-library/library/food-and-dining/recipe-goat/cmd/recipe-goat-pp-cli@latest` |
| ripgrep (rg) | `brew upgrade ripgrep` |
| Starship | `starship self-update` |
| TinyFish | `npm update -g tinyfish` |
| uv | `uv self update && uv tool upgrade --all` |
| Warp | `warp-cli update` |
| yazi | `brew upgrade yazi` |
| zoxide | `zoxide update` / `zoxide self-update` |

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
./update_all_clis.sh --parallel=8    # run up to 8 updates at once (default 8)
./update_all_clis.sh --only-origins=brew,npm
./update_all_clis.sh --skip-origins=gem
./update_all_clis.sh --validate-cache  # validate cache structure and show diagnostics (JSON)
./update_all_clis.sh --debug-cache     # show human-readable cache validation report
./update_all_clis.sh --suggest-known   # show tools updated via bulk but not in known list
SKIP=hermes,uv ./update_all_clis.sh
./update_all_clis.sh --skip=hermes,uv
QUIET=1 ./update_all_clis.sh
```

### Performance

The updater includes several performance optimizations:

- **Parallel execution** — runs 8 concurrent update jobs by default (adjustable with `--parallel=N`)
- **Version caching** — preserves tool versions across rescans to avoid redundant version probing
- **Rate limiting** — minimal delay between subprocess calls (0.01s default, configurable via `UAC_RATE_LIMIT_DELAY`)
- **Parallel version probing** — uses 16 workers for concurrent version checks
- **Fast failure detection** — 5-second timeout for unresponsive tools during version probing

Use `--validate-cache` and `--debug-cache` to diagnose cache health and performance.

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

### Suggest-known command for tracked-tool gaps

After a discovery run, use the `suggest-known` command to find tools that are updated via a bulk package manager (npm, cargo, go, etc.) but don't have dedicated entries in the `known` list:

```bash
python3 lib_update_all_clis.py suggest-known ~/.config/update-all-clis/cache.json
```

Or from the shell script:

```bash
./update_all_clis.sh --suggest-known
```

This outputs ready-to-paste `config.local.json` snippets for tools like `liteparse`, `roughdraft`, and others that are being handled by bulk updates but could benefit from individual tracking. The auto-tip also runs after each update, showing a brief summary if new candidates are found.

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

1. **Discovery scan** — walks 20+ known tool directories (`~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/.npm-global/bin`, npm globals, Homebrew, Go bins, dotnet tools, krew, mise, etc.) **and** scans user-writable directories on `$PATH` (skipping system dirs like `/usr/bin`, `/bin`), then writes `~/.config/update-all-clis/cache.json`.
2. **Version caching** — after each update run, tool versions are cached to speed up future runs. The cache preserves version information across rescans to avoid redundant version probing.
3. **Symlink inference** — if a binary in a generic directory (e.g., `~/.local/bin`) is a symlink into a package manager tree (e.g., `node_modules`), it's routed to that manager's bulk update.
4. **Cache** — reused until it is older than **`CACHE_TTL_HOURS`** (default 24h), unless you pass **`--rescan`**. A normal run performs **at most one** full scan.
5. **`--no-scan`** — uses the existing cache when possible (see main script help for edge cases).
6. **Deduplication** — one bulk command per origin (e.g. one `npm update -g` for all npm globals). Known tools get their own command when listed in merged config.
7. **Execution** — parallel by default (8 concurrent jobs); **`--parallel=N`** adjusts concurrency (tracing is disabled for parallel runs).

## Adding a new tool

Edit [`tool_config.json`](tool_config.json) or your **`config.local.json`**:

- **`known`** — tool name → command to run for that binary.
- **`bulk`** — origin label → one command per run for that origin.

[`lib_update_all_clis.py`](lib_update_all_clis.py) validates that both sections exist and that every command is a string.

After a discovery scan, run the **suggest** command to see which discovered tools aren't yet covered:

```bash
python3 lib_update_all_clis.py suggest ~/.config/update-all-clis/cache.json
```

## CLI version status

See [`CLI_LAST_UPDATED.md`](CLI_LAST_UPDATED.md) for per-tool versions (regenerate with `python3 scripts/generate_cli_report.py`).

<details>
<summary>Legacy inline table (may be stale)</summary>

| CLI | Last Modified | Version |
|-----|---------------|---------|
| agent | 2026-05-09 15:34:16 | 2026.05.09-0afadcc |
| atuin | not installed |  |
| bat | not installed |  |
| bb | 2026-03-20 13:37:52 | 0.5.7 |
| browse | 2026-03-20 13:37:52 | 0.6.0 |
| claude | 2026-05-11 19:12:44 | 2.1.139 (Claude Code) |
| cline | 2026-05-13 13:52:21 | 3.0.2 |
| codex | 2026-05-13 13:52:21 | codex-cli 0.130.0 |
| codex-cli | not installed |  |
| composio | 2026-05-13 13:52:33 | 0.2.22 |
| dev-browser | 2026-05-13 13:52:21 | ? |
| devin | 2026-05-12 18:06:50 | devin 2026.5.6-7 (fb272f6) |
| elevenlabs | 2026-05-13 13:52:21 | 0.5.2 |
| espn-pp-cli | 2026-05-08 11:53:58 | espn-pp-cli 1.0.0 |
| expect-cli | 2026-05-13 13:52:24 | 0.1.3 |
| eza | not installed |  |
| fd | not installed |  |
| fieldtheory | 2026-05-13 13:52:25 | 1.3.19 |
| firecrawl | 2026-05-13 13:53:03 | 1.16.2 |
| flight-goat-pp-cli | 2026-05-08 11:54:04 | flight-goat-pp-cli 1.0.0 |
| fzf | 2026-04-26 05:04:37 | 0.72.0 (Homebrew) |
| gemini | 2026-05-13 13:52:33 | 0.42.0 |
| genspark | 2026-05-13 13:52:22 | 1.0.15 |
| gh | 2026-04-28 06:16:54 | gh version 2.92.0 (2026-04-28) |
| goose | 2025-01-28 00:19:13 | 1.0.0 |
| hermes | 2026-05-13 13:53:07 | Hermes Agent v0.13.0 (2026.5.7) |
| just | not installed |  |
| kanban | 2026-05-13 13:52:28 | 0.1.68 |
| kimi | 2026-05-13 13:54:28 | kimi, version 1.43.0 |
| kimi-cli | 2026-05-13 13:54:28 | kimi, version 1.43.0 |
| lazygit | not installed |  |
| mcp-remote | 2026-05-13 13:52:23 | ? |
| mem0 | 2026-05-13 13:52:23 | ◆ Mem0 CLI v0.2.4 |
| mise | not installed |  |
| mmx | 2026-05-13 13:53:06 | mmx 1.0.13 |
| mmx-cli | not installed |  |
| movie-goat-pp-cli | 2026-05-08 11:54:10 | movie-goat-pp-cli 1.0.0 |
| ntn | 2026-05-13 13:45:31 | ntn 0.13.2 |
| ollama | not installed |  |
| op | 2026-04-16 17:24:41 | 2.34.0 |
| openclaw | 2026-05-13 13:52:37 | OpenClaw 2026.5.7 (eeef486) |
| opencode | 2026-05-10 21:47:45 | 1.14.48 |
| printing-press | 2026-05-08 11:55:00 | printing-press 4.0.6 |
| readwise | 2026-05-13 13:52:22 | 0.5.6 |
| recipe-goat-pp-cli | 2026-05-08 11:54:15 | recipe-goat-pp-cli 1.0.0 |
| rg | 2026-02-26 13:19:47 | ripgrep 15.1.0 |
| starship | not installed |  |
| tinyfish | 2026-05-13 13:52:23 | 0.1.6 |
| uv | 2026-05-12 13:40:40 | uv 0.11.14 (3fdfdc7d4 2026-05-12 aarch64-apple-darwin) |
| warp | not installed |  |
| yazi | not installed |  |
| zoxide | not installed |  |

Last modified is the file timestamp of the binary on disk (when it was last installed or updated). Regenerate with `python3 scripts/generate_cli_report.py`.

## Requirements

- Bash 3.2+ (macOS default bash works)
- Python 3.6+ (for `lib_update_all_clis.py` and cache handling)
- Git

## Testing

The project includes a comprehensive test suite:

```bash
python3 -m unittest tests/test_lib_update_all_clis.py -v
```

Tests cover:
- Configuration loading and merging
- Validation logic
- Version caching functionality
- Discovery path handling
- Cache update operations

All tests should pass before making changes to the core logic.

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
