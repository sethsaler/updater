# update-all-clis

> One script to discover and update every CLI and package manager on your system.

`update-all-clis` scans `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, npm global bins (including `~/.npm-global/bin`, `pnpm`, `yarn`, and nvm-installed packages), Homebrew Cellar, gem bins, Go tool bins, dotnet tools, krew plugins, mise shims, pipx venvs, `~/bin`, Wasmtime/Wasmer runtimes, and **all user-writable directories on `$PATH`** — plus single-CLI managers detected by presence alone (rustup, gcloud, mas, tlmgr) — then runs the right update command for each. Nothing is hardcoded about *what* you have installed.

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
| pipx (Python) | `pipx upgrade-all` |
| rustup (Rust toolchains) | `rustup update` |
| gcloud (Google Cloud SDK) | `gcloud components update --quiet` |
| mas (Mac App Store CLI) | `mas upgrade` |
| tlmgr (TeX Live) | `tlmgr update --self --all` |

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

A tool being in `known` never suppresses its origin's bulk update: e.g. `cline` (a `known` npm tool) gets its own `npm update -g cline`, but the origin's `npm update -g` bulk line **still runs once** to cover every *other* npm global that isn't individually tracked — those two are not redundant. (An earlier version of this script had a bug where the first `known` tool seen for an origin marked that origin's bulk update as already handled, so it silently never ran for origins — npm chief among them — that almost always have at least one `known` tool. Fixed; see the regression test `test_known_tool_does_not_suppress_origin_bulk`.)

### Origins with limited or environment-specific updates

- **rbenv** — Bulk update uses Homebrew when `rbenv` / `ruby-build` are installed as formulas. If you installed rbenv only from git, run `git -C "$(rbenv root)" pull` yourself when needed.
- **SDKMAN** — Runs `sdk selfupdate` (SDKMAN itself). Candidate SDK upgrades are not bulk-updated automatically.
- **pipx** — Discovered via `~/.local/pipx/venvs/*/bin`; bulk update is `pipx upgrade-all`. No-op (both discovery and bulk command) if pipx isn't installed.
- **rustup** — Rust *toolchains*, distinct from Cargo-installed binaries (which are handled by the existing `cargo` origin/bulk entry above). `rustup` itself isn't discovered from a bin directory; its presence is detected with `command -v rustup` and registered as a single synthetic tool, same as `fnm`. No-op if `rustup` isn't installed.
- **gcloud** — `gcloud components update --quiet`, detected the same way as `rustup` (`command -v gcloud`). **Caveat, not machine-verified**: if your `gcloud` was installed via a Homebrew cask or another package manager (rather than the Google-provided installer), `gcloud components update` typically errors out because that installation method doesn't own its own component manifest — the command itself reports this clearly, and the origin's `2>/dev/null || true` guard keeps that failure from breaking the rest of the run, but it also means the update silently does nothing in that case. If this applies to you, update `gcloud` via your package manager instead (e.g. `brew upgrade google-cloud-sdk`) and consider adding a `known` override in `config.local.json` for the `gcloud` binary name pointing at that.
- **mas** — Mac App Store CLI; `mas upgrade`, detected via `command -v mas`. No-op if not installed.
- **tlmgr** — TeX Live package manager; `tlmgr update --self --all`, detected via `command -v tlmgr`. **Caveat, not machine-verified**: on a typical system-wide TeX Live install, `tlmgr` needs `sudo` to write to the system tree. This script **never invokes `sudo`** (and never will), so on such installs this bulk command will fail with a permissions error rather than silently doing nothing — the failure is visible in the run summary/history like any other failed job. If your TeX Live install needs elevated permissions, run `sudo tlmgr update --self --all` yourself, or reinstall TeX Live to a user-writable prefix.
- **`path`** — **Enabled by default**. Binaries found under `$PATH` directories (excluding system dirs like `/usr/bin`, `/bin`, `/sbin`) get origin `path`. There is no default bulk update for `path`; add a `"path"` entry under `bulk` in [`config.local.json`](#configuration-merge-and-overrides) if you want a single command for all of them, or list tools under `known`. Use `--no-scan-path` to disable PATH scanning.

None of pipx, rustup, gcloud, mas, or tlmgr were installed on the machine this was built on; every entry above is guarded to be a silent no-op in both discovery and the update command when the corresponding tool isn't present, but only `pipx` (pre-existing) had its wiring exercised end-to-end there. `check` pre-check commands and `repos` changelog slugs were deliberately **not** added for gcloud/tlmgr (no reliable/verifiable check command; no meaningful public-release-notes repo for gcloud, and TeX Live isn't a single-repo GitHub project). `rustup` (`rust-lang/rustup`) and `mas` (`mas-cli/mas`) do have verified `repos` slugs for the changelog digest.

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
./update_all_clis.sh --rescan        # force fresh discovery scan (default behavior)
./update_all_clis.sh --dry-run       # show what would be updated
./update_all_clis.sh --no-scan       # use cached discovery only (skip scanning)
./update_all_clis.sh --json-summary  # after run, print one JSON line: {"ok":N,"failed":M}
./update_all_clis.sh --trace         # bash -x when running each update command
./update_all_clis.sh --no-scan-path  # skip scanning directories on $PATH
./update_all_clis.sh --parallel=8    # run up to 8 updates at once (default 8)
./update_all_clis.sh --job-timeout=900  # kill any single update stuck longer than N seconds (default 900; 0 disables)
./update_all_clis.sh --notify       # show the non-blocking desktop summary dialog
./update_all_clis.sh --only-origins=brew,npm
./update_all_clis.sh --skip-origins=gem
./update_all_clis.sh --validate-cache  # validate cache structure and show diagnostics (JSON)
./update_all_clis.sh --debug-cache     # show human-readable cache validation report
./update_all_clis.sh --suggest-known   # show tools updated via bulk but not in known list
./update_all_clis.sh --history         # show the last 3 runs (ok/fail, version changes, failures)
./update_all_clis.sh --history=10      # show the last 10 runs
./update_all_clis.sh --include-quarantined  # force-run tools/origins currently quarantined
./update_all_clis.sh --no-precheck    # always run every bulk update (skip outdated pre-checks)
./update_all_clis.sh --hold=claude,brew     # pin tools/origins (persists in config.local.json) and exit
./update_all_clis.sh --unhold=claude        # un-pin and exit
HOLD=claude ./update_all_clis.sh            # one-run ad hoc hold (not persisted)
./update_all_clis.sh --doctor         # read-only diagnostics report
./update_all_clis.sh --doctor --json  # same, as JSON
./update_all_clis.sh --changelog      # after a real run, best-effort release-notes digest
./update_all_clis.sh --self-update    # git pull --ff-only this checkout before planning, then re-exec once
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
- **Outdated pre-checks** — a bulk origin with a configured `check` command (see below) skips its update entirely when the check says nothing's outdated
- **mtime-gated post-run probing** — the post-update version snapshot only re-probes a tool/manager whose binary's mtime actually changed since the pre-run snapshot; everything else reuses the pre-run version string instead of spawning another `--version` call
- **Incremental discovery scan** — directories are only re-listed when their mtime changed since the last scan (see below); `--rescan` forces a full walk
- **Fewer `python3` spawns per run** — the per-job and single-instance locks used to each spawn a Python `fcntl.flock` coprocess; both are now a bash-native `mkdir` spin-lock (atomic on POSIX filesystems, no helper process, same stale-lock-steal and Ctrl+C-safe cleanup semantics). `--dry-run` also skips locking entirely now — it never mutates anything, so serializing "would-run" jobs against each other bought nothing but a lock round-trip per job.

Measured on this machine (warmed cache, `--no-scan`, ~96 planned jobs):

| | Before | After |
|---|---|---|
| `--dry-run` wall clock | ~11.7s | ~1.3s |
| `--dry-run` `python3` invocations | ~104 | ~7 |
| `--list` (repeat, warm cache) | ~1.7s | ~1.7s (unchanged — `--list` was already cheap; its `python3` calls are the incremental-scan/list-json ones, not per-job locks) |

The `--dry-run` numbers are the ones that moved: removing the per-job lock spawn (dry-run never locks now) and replacing the two remaining Python-coprocess locks with `mkdir` accounts for essentially all of it. `--list`'s call count (6-7) is unrelated to job locking and was already small.

Use `--validate-cache` and `--debug-cache` to diagnose cache health and performance.

#### Outdated pre-checks (`check`)

`tool_config.json` supports an optional top-level `"check"` object, mapping a **bulk origin** to a read-only shell command whose stdout decides whether that origin's (often expensive) bulk update is actually needed:

```json
"check": {
  "npm": "npm outdated -g --parseable",
  "brew": "brew update >/dev/null 2>&1 && brew outdated --quiet"
}
```

Semantics: the check command runs; if it **exits 0** and its stdout (after trimming whitespace) is **empty**, `[]`, or `{}`, the origin is treated as up to date and its bulk update is skipped this run. Any other outcome — non-zero exit, real output, a missing command, or an error — fails open and the update runs exactly as before. Skipped origins print `✓ <origin>: already up to date (pre-check)`, count as `ok` in the run summary, and are recorded in `history.jsonl` with `status: "ok"` and the check's own duration.

Checks run **concurrently**, before the plan executes, and only for **bulk origins** (known-tool commands are left alone in v1 — a known tool routed through the same manager as a pre-checked origin still runs its own command). `--dry-run` never executes checks (some, like brew's, refresh manager metadata as a side effect) — it only reports which origins would have been checked. Disable pre-checks entirely with `--no-precheck` or `UAC_NO_PRECHECK=1`.

Only `npm` and `brew` ship with a `check` command — both were verified by hand on a real machine. `gem outdated` was tried and rejected: it always prints noisy `Ignoring ...` lines and a long list of un-upgradable system gems, so its stdout is never actually empty even when every user-installed gem is current. `cargo` (needs the `cargo-install-update` subcommand, not installed here), `dotnet`, and `krew` were not verified (missing/not installed) and were left out rather than guessed at — a good next step for whoever picks this up on a machine that has them.

#### Incremental discovery scan

`full_scan()` used to re-walk 20+ directories on every run. It now persists each scanned directory's mtime in the cache (`dir_mtimes`); on the next run, a directory whose mtime hasn't changed reuses its previously-cached tools instead of being re-listed (adding/removing a file changes a directory's own mtime on APFS and most other filesystems, so new installs are still always picked up). Directories whose source disappeared have their cached tools pruned. `--rescan` ignores stored mtimes and forces a full walk, refreshing every stored mtime.

Two scan modes: a plain directory listing (most bin dirs), and a "tree" mode for one level of `*/bin` subdirectories (Homebrew's `opt/`, mise's `installs/`, uv's shared venvs) — for those, only the top-level directory's mtime is tracked, so an in-place upgrade that doesn't add/remove a top-level entry (formula symlink, install dir) won't retrigger a walk. That's an accepted trade-off: existing binary *names* don't change on an in-place upgrade either way, and `--rescan` remains available. `npm ls -g --json` (used to discover extra per-package `.bin` directories) still runs on every scan — gating it reliably needs its own signal, which was out of scope for this pass — but the actual filesystem walking it feeds into is gated the same as everything else.

### Configuration merge and overrides

- **`~/.config/update-all-clis/config.local.json`** (override path with `CONFIG_LOCAL_FILE`) — optional. If present, its `known`, `bulk`, `check`, and `repos` objects are **merged on top of** `tool_config.json` (local wins on key conflicts) and its `hold` array is **added to** (not replaced by) the base list, so you can add or override commands without editing the repo file.
- **`CACHE_TTL_HOURS`** — cache freshness in hours (default **0**, i.e. every run does a fresh discovery scan so new installs are always picked up). Set to e.g. `24` to reuse a cache newer than 24h (unless `--rescan`).
- **`ONLY_ORIGINS`** — comma-separated origins (and known tool names) to **restrict** what runs. When set, bulk updates run only for listed origins; known tools run only if their `origin` or `name` is listed.
- **`SKIP_ORIGINS`** — comma-separated origins to skip for bulk updates; known tools whose `origin` is listed are skipped.
- **`UPDATE_ALL_CLIS_HISTORY_FILE`** — path to the run-history JSONL file (default `~/.config/update-all-clis/history.jsonl`). See [Run history](#run-history).
- **`UAC_QUARANTINE_AFTER`** — consecutive-failure threshold before a job is quarantined (default **3**; `0` disables quarantine).
- **`UAC_INCLUDE_QUARANTINED`** — set to `1` to force quarantined jobs to run this pass (same as `--include-quarantined`).
- **`UAC_NO_PRECHECK`** — set to `1` to skip outdated pre-checks entirely (same as `--no-precheck`).
- **`HOLD`** — comma-separated one-run ad hoc hold (same as `--hold=` but non-persistent; see [Pin/hold tools](#pinhold-tools)).
- **`UPDATE_ALL_CLIS_CHANGELOG`** — set to `1` to enable the changelog digest (same as `--changelog`).

### Desktop summary dialog (opt-in, non-blocking)

On **macOS** (or Linux via `notify-send`), after a real update run (not `--dry-run`), the script can show a **summary** with ok/fail counts, known tools' versions before → after (best effort), and bulk origins' manager versions before → after.

The dialog is **opt-in and never blocks the terminal** — it is spawned fully detached, so your prompt returns instantly whether or not you've dismissed it. (Earlier versions popped a blocking modal after every interactive run, which hung the terminal; that no longer happens.)

- **Default:** no dialog.
- **`--notify`** or **`UPDATE_ALL_CLIS_NOTIFY=1`** — show the dialog (non-blocking).
- **`UPDATE_ALL_CLIS_NOTIFY=0`** or **`UPDATE_ALL_CLIS_NO_NOTIFY=1`** — never show it (set automatically for **LaunchAgent**/**systemd** schedules).

### Run history

Every real (non-`--dry-run`) run appends one JSON line per executed job to **`~/.config/update-all-clis/history.jsonl`** (override with `UPDATE_ALL_CLIS_HISTORY_FILE`). Each record captures the run id, timestamp, job kind (`known`/`bulk`/`uptodate`), name, command, duration, `ok`/`fail` status, and the version before/after (best effort). A pre-check skip (see [Outdated pre-checks](#outdated-pre-checks-check)) is recorded with kind `uptodate`, `status: "ok"`, and the check's own duration rather than a near-zero one. The file is pruned to the most recent ~2000 lines on every append, so it won't grow unbounded. `--dry-run` never writes to it.

```bash
./update_all_clis.sh --history        # last 3 runs: ok/fail counts, version changes, failures
./update_all_clis.sh --history=10     # last 10 runs
python3 lib_update_all_clis.py history ~/.config/update-all-clis/history.jsonl 5
```

**Slowest-first scheduling** — when building the parallel-run plan, jobs are ordered by their historical mean duration (last ~10 runs per job), descending, so the long pole (usually `brew`) kicks off first instead of last. Jobs with no history yet run after the known-slow ones, in their otherwise-stable original order.

**Failure quarantine** — a job (known tool or bulk origin) that failed its last **`UAC_QUARANTINE_AFTER`** (default **3**, `0` disables) consecutive appearances in history is skipped automatically, with a warning:

```
!! skipped (quarantined after 3 consecutive failures): sometool — run with --include-quarantined to retry
```

Force a quarantined job to run anyway with **`--include-quarantined`** (or `UAC_INCLUDE_QUARANTINED=1`); a subsequent success naturally clears the streak since quarantine state is derived purely from `history.jsonl` — there's no separate state file to reset. Quarantined jobs are also listed in the run summary (desktop dialog / `UPDATE_ALL_CLIS_SUMMARY_FILE`) so they stay visible even when skipped silently in a scheduled run.

### Pin/hold tools

`tool_config.json` (and `config.local.json`) support an optional top-level **`"hold"`** array — known tool names and/or bulk origins that are pinned: skipped on every run until removed. Local `config.local.json` entries **add to** (never replace) the base list.

```json
"hold": ["claude", "brew"]
```

An entry may also be written `"name:major"` (e.g. `"claude:major"`). In v1 this is accepted and treated identically to a plain hold — for most package managers there's no reliable way to know the *target* version before the update runs, so a true "block major upgrades only" hold isn't safe to implement at the planning stage. What v1 gives you instead is **major-jump flagging in the run summary**: any tool whose version actually changed with a bump in the leading integer component (e.g. `1.9.0 → 2.0.0`) is marked `[MAJOR UPGRADE]` in the desktop dialog / `UPDATE_ALL_CLIS_SUMMARY_FILE` output, so you notice it after the fact even without a pre-run block.

Two ways to manage the persistent hold list:

```bash
./update_all_clis.sh --hold=claude,brew   # add to hold (writes config.local.json) and exit
./update_all_clis.sh --unhold=brew        # remove from hold and exit
```

For a one-run, non-persistent hold, use `HOLD=` (like `SKIP=`, but visibly reported rather than silently skipped):

```bash
HOLD=claude ./update_all_clis.sh
```

**Hold vs. `SKIP`/`SKIP_ORIGINS`:** `SKIP` is a per-run, silent exclusion — it never appears in the run summary and isn't persisted. A held job is **persistent** (via config, until you `--unhold`) or explicitly one-run (`HOLD=`), and always shows up: the shell prints `!! held (config): <name> — remove from "hold" to resume updates` (or `!! held (env HOLD=): <name> — …` for the ad hoc form), a held job is recorded in `history.jsonl` with `status: "held"` and `"held": true` (never counted toward quarantine's failure streak), and it appears in its own "Held" section of the run summary.

### Doctor

`--doctor` runs a read-only diagnostics pass over the existing cache, history, and config — no updates are executed:

```bash
./update_all_clis.sh --doctor         # human-readable report
./update_all_clis.sh --doctor --json  # machine-readable report
```

Checks (each is independent — one crashing doesn't prevent the rest from reporting):

1. **Broken symlinks** — dead symlinks in every scanned bin directory (from the cache) plus every user-serviceable directory on `$PATH` (SIP/system dirs like `/usr/bin` and `/usr/sbin` are excluded — findings there wouldn't be actionable).
2. **Shadowed duplicates** — a binary name whose cache entries resolve (via realpath) to **2+ genuinely different files**, so which copy runs depends on `$PATH` order. The same file discovered through several origins (e.g. an npm global seen by both the npm query and the `$PATH` scan) is *not* reported. Intentional shadows — e.g. a wrapper shim in `~/.local/bin` that sets env vars and `exec`s the managed copy — can be acknowledged with a top-level **`"doctor_ignore"`** array (usually in `config.local.json`); ignored names are listed informationally and don't affect the exit code.
3. **Chronic failures** — jobs with 3+ failures in their last 10 `history.jsonl` records, surfaced even if they haven't (yet) hit the consecutive-failure quarantine threshold.
4. **Config issues** — `hold` entries matching nothing; `check` entries for an origin with no corresponding `bulk` command. (`known` entries for tools that aren't installed are **informational only** — the config deliberately catalogs tools you might install, and absent ones are skipped.)
5. **Cache health** — reuses `validate_cache()` (same as `--validate-cache`) rather than duplicating that logic. Cache *warnings* are informational; only cache errors count as findings.

Exit code is **0** with no findings, **1** if any check surfaced something actionable.

### Changelog digest

`tool_config.json` supports an optional top-level **`"repos"`** object mapping a tool or bulk-origin name to a GitHub `owner/repo` slug:

```json
"repos": {
  "gh": "cli/cli",
  "fzf": "junegunn/fzf"
}
```

With **`--changelog`** (or `UPDATE_ALL_CLIS_CHANGELOG=1`) on a **real** run (never `--dry-run`), after the update finishes the script fetches best-effort release notes for every tool whose version actually changed and has a `repos` mapping, matching releases whose tag falls in `(version_before, version_after]` (tolerant of a leading `v` on tags). Prefers `gh api` when the `gh` CLI is available (works with your existing auth, higher rate limit); otherwise falls back to an unauthenticated `https://api.github.com` request (60 req/hour limit) — capped at **5 tools per run** either way, noting when the cap was hit. Each release body is truncated to ~400 characters. The whole digest is capped at a **~10s** wall-clock budget; any single tool's failure (offline, rate-limited, no matching tag) just omits that tool rather than aborting the digest.

Output is appended as a "Changelog highlights" section to stdout and to `UPDATE_ALL_CLIS_SUMMARY_FILE` (if set) — **never** to the macOS dialog, since release notes can run to several KB.

Only tools with a **verified** repo slug ship in `tool_config.json` by default (`gh`, `fzf`, `rg`→ripgrep, `starship`, `uv`, `deno`, `bun`, `zoxide`, `eza`, `bat`, `fd`, `just`, `lazygit`, `yazi`, `atuin`, `mise`, `gemini`, `opencode`). Add more in `config.local.json`'s `"repos"` — local entries merge on top of (and can override) the base mapping.

### Self-update

`--self-update` (or `UPDATE_ALL_CLIS_SELF_UPDATE=1`) makes the script update its own checkout before planning anything:

```bash
./update_all_clis.sh --self-update
```

If `SCRIPT_DIR` (wherever `update_all_clis.sh` lives) is a git checkout with an `origin` remote, it runs `git -C "$SCRIPT_DIR" pull --ff-only` (capped at 15s — no `flock`/`gtimeout` dependency; a backgrounded pull is watched and killed if it overruns). If that changes `HEAD`, the script re-execs itself once with the same arguments, so the run that follows uses the freshly-pulled code, config, and library. A `UAC_SELF_UPDATED=1` marker prevents a re-exec loop.

**Off by default**, and fail-open in every way that matters — none of the following ever fails the run, they just print a one-line warning (or, for the boring "nothing to pull" case, nothing at all) and continue on the current checkout:

- no `git` binary
- `SCRIPT_DIR` isn't a git checkout
- no `origin` remote configured
- `git pull --ff-only` fails for **any** reason — a dirty working tree with conflicting local changes, no network, or diverged history all surface as a non-zero exit from `git pull`, which is treated identically: warn and continue. **The script never runs `git stash`, `git reset`, `git checkout --`, or anything else that could discard uncommitted local changes** — if `--ff-only` can't fast-forward cleanly, it simply doesn't, and neither does this wrapper.
- pull takes longer than the timeout — killed and treated as a failure, same fail-open path

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
3. **Symlink inference** — if a binary in a generic directory (e.g., `~/.local/bin`) is a symlink into a package manager tree (e.g., `node_modules`), it's routed to that manager's bulk update. This includes binaries scanned under uv-ish origins (`uv`, `uv/pip`, `uv/venv`): when the npm global prefix is `~/.local`, npm CLIs land in `~/.local/bin` alongside uv tools, and a `node_modules` symlink target reroutes them to the npm bulk update so they're never misattributed to uv.
4. **Cache** — by default every run performs a fresh discovery scan (**`CACHE_TTL_HOURS=0`**) so newly installed tools are always found. Set `CACHE_TTL_HOURS=N` to reuse a cache newer than N hours. A normal run performs **at most one** full scan.
5. **`--no-scan`** — uses the existing cache when possible (see main script help for edge cases).
6. **Deduplication** — one bulk command per origin (e.g. one `npm update -g` for all npm globals). Known tools get their own command when listed in merged config.
7. **Execution** — parallel by default (8 concurrent jobs); **`--parallel=N`** adjusts concurrency (tracing is disabled for parallel runs). Every update command runs with **stdin redirected to `/dev/null`** (a command that tries to prompt reads EOF instead of waiting forever) and under a **per-job watchdog** (`--job-timeout=N` / `UAC_JOB_TIMEOUT`, default 900s, 0 disables): a job still running past the timeout has its whole process tree killed and is counted as failed — e.g. a `brew upgrade` cask waiting on an open app can't stall the rest of the run. Jobs waiting on a same-manager lock are bounded too (watchdog + 60s slack), so a wedged sibling never blocks the queue indefinitely.
8. **Concurrency safety** — a **single-instance lock** (`mkdir`-based, with stale-lock detection) prevents overlapping runs (e.g. a LaunchAgent run and a manual run) from racing on the cache. Parallel updates of the same package manager are serialized the same way — no `flock` binary or helper process needed on macOS, and no busy-waiting (a lock older than the stale cap is assumed abandoned and reclaimed rather than waited on forever). `--dry-run` skips locking entirely since it never mutates anything. **Ctrl+C** cleanly stops in-flight update jobs instead of orphaning `brew`/`npm`/`cargo`, and its `EXIT`/`INT`/`TERM` trap removes the whole lock directory so a crash never leaves a stale lock behind for long.

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

See [`CLI_LAST_UPDATED.md`](CLI_LAST_UPDATED.md) for per-tool versions (regenerate with `python3 scripts/generate_cli_report.py`) — that file is the current source of truth, not the table below.

<details>
<summary>⚠️ Stale snapshot — do not treat as current. Kept only for history; see <code>CLI_LAST_UPDATED.md</code> above instead.</summary>

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

</details>

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
