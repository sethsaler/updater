# Changelog

## 0.10.0

**Retries + auto-fix for failed updates, more reliable discovery**
- **Retry policy** — a real command failure (non-zero exit) is retried up to `--retries=N` times (`UAC_RETRIES`, default 1, 0 disables), waiting `--retry-delay=N` seconds between attempts (`UAC_RETRY_DELAY`, default 10). Timeouts are never retried — a watchdog kill means the job was stuck on something external, and rerunning it would just burn another timeout.
- **Failures unmasked** — 102 known-tool commands in `tool_config.json` ended in `|| true`, which normalized every real npm/brew/uv/cargo failure to exit 0 before the executor could see it — no retry, no fix, no failure count, ever. The terminal `|| true` is removed from all known commands (the `2>/dev/null` redirects stay); bulk commands keep their existing noise guards. A persistently failing tool is still silenced eventually by the existing quarantine (3 consecutive failures).
- **One-shot fix after retries** — when a job exhausts its retries and has a fix command, that repair runs once; success counts the job as ok (`✓ tool (fixed via: ...)`). Disable with `--no-fix` / `UAC_FIX=0`. Fix commands are auto-derived for known tools on recognized managers (npm → `npm install -g pkg@latest --force`, brew → `brew reinstall pkg`, uv tool → `uv tool install pkg --force --reinstall`, pipx → `pipx reinstall pkg`, cargo → `cargo install pkg --locked --force`, gem → `gem install pkg --user-install`; `go install` is already a reinstall and gets none), or set explicitly via a new optional top-level `"fix"` object in `tool_config.json` / `config.local.json` (keyed by tool or origin name; explicit always wins, and is the only way a bulk origin gets a fix). Schema updated.
- Both executors (shell and TUI) implement identical retry/fix semantics; the TUI gained `--retries` / `--retry-delay` / `--fix 0|1` and shows success notes (`succeeded on retry 1`, `fixed via reinstall`) in the dashboard, plain output, and job detail. `--dry-run` shows the repair that would run. The plan's emit-line format gained an optional fifth `fix_command` field (older four-field lines still parse); JSON plans include `"fix_command"` when nonempty.
- **Fix derivation is token-based, not regex** — an unrecognized flag that might take a value (`npm update -g --registry https://x pkg`) previously risked deriving a reinstall of the flag's *value*; derivation now tokenizes the command, allows only known valueless flags per manager, requires exactly one package token, and otherwise emits no auto-fix. A fix identical to the failing command (configured or derived, whitespace-insensitive) is suppressed — that's an extra retry, not a repair.
- **Discovery: new scan locations** — modern pipx (`~/.local/share/pipx/venvs`), pnpm (`~/Library/pnpm` on macOS; `~/.local/share/pnpm` and `~/.local/share/pnpm/bin` on Linux), and Yarn (`~/.yarn/bin`), each a silent no-op when absent; PATH duplicate-scan skip list updated to match.
- **pnpm and Yarn are first-class origins** — their global dirs were initially labeled origin `npm`, which routed their tools to `npm update -g` (which can't update them). New bulk origins: `pnpm` (`pnpm update -g`) and `yarn` (`yarn global upgrade`), with their own lock groups, version probing, and symlink-target inference (pnpm/yarn global trees contain `node_modules`, so they're checked before the generic node_modules → npm rule).
- **Discovery: cache-clobbering bug fixed** — a failed or empty scanner run used to overwrite the cache anyway; `full_scan` now only replaces the cache when the scan succeeds with nonempty output, otherwise the prior cache is preserved.
- **Discovery: broader symlink-target inference** — `_origin_from_target_path` now recognizes the install trees of uv, pipx, Homebrew, bun, deno, Volta, mise, pnpm, Yarn, npm (`node_modules`), Cargo, and dotnet, so binaries in generic bin dirs get routed to the right bulk update. The overly-broad `"cargo" in path` substring match was tightened to `.cargo/` so unrelated paths can't be misattributed.
- **Executor fixes** — the shell emit-line parser no longer smears a 3-field line's command into the lock-group field; a failed TUI fix reports the fix's own exit code (not the earlier update's) and clears per-attempt output so a fix failure can't display leftovers from a previous attempt; renderers show success notes (`succeeded on retry 1`, `fixed via: ...`).
- 45 new unit tests (259 total).

## 0.9.0

**Broader discovery: 9 new scan origins + `qwen` known tool**
- New scan directories: macOS Python user installs (`~/Library/Python/3.x/bin`, origin `pip`), Volta (`~/.volta/bin`), asdf (`~/.asdf/shims`), proto (`~/.proto/bin`), Rye (`~/.rye/shims`, `~/.local/share/rye/shims`), Foundry (`~/.foundry/bin`), aqua (`~/.aqua/bin`, `~/.local/share/aquaproj-aqua/bin`), and Mason/Neovim LSP (`~/.local/share/nvim/mason/bin`). Each is a silent no-op when the directory doesn't exist.
- New bulk origins with update commands: `pip` (upgrades pip/setuptools/wheel), `asdf` (`asdf update`), `proto` (`proto update`), `volta` (`volta update`), `rye` (`rye self update`), `foundry` (`foundryup`), `aqua` (`aqua update`). `mason` is discovery-only (no bulk update command).
- `qwen` added to the `known` list with `qwen update` (self-update, same pattern as claude/grok/kimi).
- PATH exclusion list updated so the new directories aren't double-counted by the `$PATH` scan.
- `probe_bulk`, `_BULK_ORIGIN_BINARY`, and `_TRACKABLE_ORIGINS` in `lib_update_all_clis.py` extended for all new origins.
- 12 new unit tests: bulk emit for each new origin, mason empty-cmd skip, lock-group mapping, qwen known emit, and qwen-doesn't-suppress-npm-bulk regression.

## 0.8.0

**Live TUI dashboard for the update run**
- On interactive terminals the update phase now renders a live dashboard: per-job rows with a braille spinner, status, elapsed time, and rolling last output line; a progress bar with ok/failed tallies; queued/finished jobs windowed to fit the screen. After the run closes, the usual summary sections print below it, so scrollback keeps the full record.
- Implemented as a new stdlib-only Python executor (`tui_update_all_clis.py`, installed next to the other files) that runs the identical plan with identical semantics — parallel cap, per-origin lock-group serialization (in-process, replacing the per-run mkdir/flock dance for TUI runs), per-job watchdog timeout with process-group kills, `/dev/null` stdin, exit-code conventions — and writes result records in the byte-exact format the bash executor's `*.result` files use. Everything before and after the update phase (discovery, prechecks, snapshots, run summary, history, notify, changelog) is untouched.
- **On by default** when stdout is a TTY; `--tui` forces on, `--no-tui` / `UAC_TUI=0` disables. Automatically off (previous plain output) for non-terminals (LaunchAgent/CI/pipes), `--dry-run`, `--quiet`, `--trace`, `NO_COLOR`, `TERM=dumb`, tiny terminals, or when the TUI file is missing — those paths run byte-identical to 0.7.1.
- `Ctrl+C` aborts gracefully from the dashboard: running jobs' process trees are killed, in-flight jobs are recorded as failed, the terminal is restored, and the post-run summary still prints.
- Failure exit-code fidelity: the runner keeps each command's real exit code for display (`failed (exit 3)`) while normalizing records to the shell's 0/1/3 convention, so a real `exit 3` can no longer alias the "skipped" sentinel in ok/fail counters.
- 33 new unit tests cover emit-line parsing, the executor (lock serialization, parallel cap, watchdog tree-kill, instant kinds, result format), and the frame renderer (width/height fitting, windowing).

## 0.7.1

**Hang protection: one stuck update can no longer stall the run**
- Per-job watchdog: every update command now runs under `UAC_JOB_TIMEOUT` (default 900s, `--job-timeout=N`, 0 disables). A job still running past the timeout has its whole process tree killed, is reported as timed out (with a hint that something — e.g. an open app blocking a cask upgrade — was probably holding it), and counts as failed while the rest of the run continues.
- stdin is now `/dev/null` for every update command, so anything that tries to prompt (sudo, npm questions, installer "close the app" prompts) reads EOF immediately instead of waiting forever — including in sequential (`--parallel=1`) runs, which previously inherited the terminal's stdin.
- Bounded lock waits: parallel jobs waiting on a same-package-manager `flock` now give up after the watchdog window (+60s slack; 1h when the watchdog is disabled) and proceed, matching the existing mkdir-fallback cap, instead of blocking indefinitely behind a wedged sibling.

**Discovery: npm CLIs installed into `~/.local/bin` are now routed correctly**
- When the npm global prefix is `~/.local`, npm globals (e.g. `pi`, `qwen`) land in `~/.local/bin` — the same directory scanned as origin `uv/pip` — and were previously misattributed to uv's bulk update. Symlink inference now also applies to uv-ish origins: a binary whose symlink resolves into `node_modules` is rerouted to the npm bulk update, so freshly installed npm CLIs are picked up by the discovery sweep and updated on the next run with no manual config.

## 0.7.0

Four rounds of work on top of 0.6.0; the script's `--version` was bumped to `0.7.0` partway through (incremental discovery scan) but this is the first changelog entry covering all of it.

**Run history, scheduling, and resilience**
- `history.jsonl` records one JSON line per executed job (kind, name, command, duration, ok/fail, version before/after); `--history[=N]` shows recent runs. Pruned to the most recent ~2000 lines.
- Slowest-first scheduling: jobs are ordered by historical mean duration (descending) so the long pole starts first in a parallel run.
- Failure quarantine: a job that failed its last `UAC_QUARANTINE_AFTER` (default 3) consecutive appearances is skipped automatically; `--include-quarantined` forces it to run anyway.

**Fewer wasted updates**
- `check` pre-checks (`tool_config.json`'s `"check"` section) let a bulk origin (npm, brew shipped by default) report "nothing outdated" and skip its own update entirely, recorded as a synthetic `uptodate` job. `--no-precheck` disables this.
- mtime-gated post-run version probing reuses the pre-run version string for anything whose binary mtime didn't change, instead of spawning another `--version` call for every tool on every run.
- Incremental discovery scan: each scanned directory's mtime is cached (`dir_mtimes`); an unchanged directory is never re-listed. Full-walk `--rescan` still available. Cut a warm `--list`/scan from ~5.4s to ~1.5s.

**Pin/hold, diagnostics, and changelogs**
- `"hold"` config array (+ `--hold=`/`--unhold=`/`HOLD=`) pins tools/origins to skip every run until removed; applied before quarantine/precheck. Held jobs get their own `held` emit-line kind and run-summary section.
- `--doctor` (+ `--doctor --json`): read-only report on broken symlinks, shadowed duplicates across origins, chronic failures, config issues, and cache health.
- `--changelog` (+ `UPDATE_ALL_CLIS_CHANGELOG=1`): best-effort GitHub release-notes digest for tools with a `"repos"` mapping whose version changed in a real run.
- `[MAJOR UPGRADE]` flagging in the run summary for any tool whose leading version component bumped.

**New origins**
- pipx, rustup, gcloud, mas, and tlmgr are now discovered/updated (each a silent no-op when the tool isn't installed). rustup/gcloud/mas/tlmgr are detected by `command -v` (single-CLI managers, not a directory of binaries), following the same pattern as the existing `fnm` entry. See the README for caveats on gcloud (cask/non-Google installs) and tlmgr (needs sudo for the system tree — this script never invokes sudo).

**Self-update**
- `--self-update` (+ `UPDATE_ALL_CLIS_SELF_UPDATE=1`): before planning, `git pull --ff-only` this script's own checkout and re-exec once if it changed. Fail-open on every error path (dirty tree, no network, diverged, not a git checkout, no origin remote) — never fails the run, never discards local changes (no stash/reset/checkout is ever run).

**Fewer `python3` spawns**
- Per-job and single-instance locks no longer spawn a Python `fcntl.flock` coprocess; both now use a bash-native `mkdir` spin-lock (atomic, no helper process, stale-lock steal after 10 minutes, cleaned up by the existing Ctrl+C-safe trap).
- `--dry-run` skips locking entirely (nothing mutates, so serializing "would-run" jobs bought nothing).
- Measured on a warmed cache: `--dry-run` python3 invocations dropped from ~104 to ~7, wall clock from ~11.7s to ~1.3s, for a ~96-job plan.

**Bug fixes**
- Fixed `collect_emit_lines` marking an origin's bulk update as already handled the moment it saw the *first* `known` tool for that origin — which meant origins like `npm` (almost always home to at least one individually-tracked tool) never actually ran their bulk update for the rest of their untracked globals. A known tool's own command and its origin's bulk command are independent; both now run. Added a regression test.

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
