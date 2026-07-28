# Migration: Topgrade + thin residual

This folder is a **drop-in alternative** to running the full `update-all-clis` stack
for day-to-day updates. It keeps coverage of your real long tail (AI / self-updating
CLIs and a few `go install` tools) without the discovery engine, TUI, quarantine, or
~8k LOC of orchestration.

You can run this **alongside** the existing script while you evaluate it. Nothing here
uninstalls or disables `update_all_clis.sh`.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  topgrade                                               │
│  brew · npm · pnpm · yarn · cargo · rustup · pipx ·     │
│  uv · bun · deno · mise · mas · gem · gcloud · …        │
└───────────────────────────┬─────────────────────────────┘
                            │ post_command (optional)
                            ▼
┌─────────────────────────────────────────────────────────┐
│  update_ai_clis.sh   (this folder)                      │
│  only tools bulk managers cannot update:                │
│  claude · hermes · opencode · agent · go-install CLIs … │
└─────────────────────────────────────────────────────────┘
```

| Layer | Owns | Source of truth |
|-------|------|-----------------|
| **Topgrade** | Every real package manager | `topgrade.toml` |
| **Residual script** | Self-updaters + explicit `go install` | `update_ai_clis.sh` |
| **Not covered on purpose** | Random `$PATH` binaries with no manager | Install via brew/uv/npm or add one line to the residual |

## Microsoft Office / Teams ("App is Open")

Topgrade's built-in `microsoft_office` step runs `msupdate`, which **refuses to
update while Teams / Word / Excel / PowerPoint / Outlook / OneNote are open**.

Topgrade cannot quit apps by itself. This migration ships
[`quit_office_apps.sh`](quit_office_apps.sh), wired as a custom command:

1. Gracefully quits those apps (`osascript` quit)
2. Waits for them to exit
3. Runs `msupdate --install --wait 600`

```bash
# standalone
./migration/quit_office_apps.sh --dry-run
./migration/quit_office_apps.sh --update

# relaunch whatever was open after update
QUIT_OFFICE_RELAUNCH=1 ./migration/quit_office_apps.sh --update

# if an app ignores quit
QUIT_OFFICE_FORCE=1 ./migration/quit_office_apps.sh --update
```

The built-in `microsoft_office` step stays **disabled** so msupdate is not run twice.

## Quick start

### 1. Install Topgrade

```bash
brew install topgrade
```

### 2. Install the Topgrade config

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}"
cp migration/topgrade.toml "${XDG_CONFIG_HOME:-$HOME/.config}/topgrade.toml"
```

Edit the residual path if this repo does not live at the default shown in the file:

```toml
[post_commands]
"AI / self-updating CLIs" = "/ABS/PATH/to/updater/migration/update_ai_clis.sh"
```

Or leave `post_commands` commented out and run the residual yourself.

### 3. Make the residual executable

```bash
chmod +x migration/update_ai_clis.sh
```

### 4. Dry-run both layers

```bash
topgrade --dry-run
./migration/update_ai_clis.sh --dry-run
```

### 5. Real run

```bash
topgrade
# if post_commands is disabled:
./migration/update_ai_clis.sh
```

## What moved where

Mapped from root `tool_config.json` (`known` + `bulk`).

### Handled by Topgrade (do not duplicate)

| Origin / mechanism | Topgrade step | Was in this repo as |
|--------------------|---------------|---------------------|
| Homebrew formulas + casks | `brew` | `bulk.brew`, many `brew upgrade X` knowns |
| npm globals | `node` / npm | `bulk.npm`, all `npm update -g …` knowns |
| pnpm / yarn globals | custom or node ecosystem | `bulk.pnpm`, `bulk.yarn` |
| Cargo installed bins | `cargo` | `bulk.cargo`, `eza` / `just` knowns |
| rustup toolchains | `rustup` | `bulk.rustup` |
| uv self + tools | `uv` | `bulk.uv*`, all `uv tool upgrade …` knowns |
| pipx | `pipx` | `bulk.pipx` |
| bun / deno | `bun` / `deno` | `bulk.bun`, `bulk.deno` |
| mise | `mise` | `bulk.mise`, `known.mise` |
| gem (user) | `gem` | `bulk.gem` |
| gcloud components | `gcloud` | `bulk.gcloud` |
| Mac App Store (`mas`) | `mas` | `bulk.mas` |
| conda | `conda` | `bulk.conda` |
| asdf / sdkman / etc. | when detected | matching `bulk.*` |

Individual known entries like `bat`, `fd`, `fzf`, `cline`, `firecrawl`, `gemini`,
`browser`, `kimi-cli`, … are **not** listed in the residual: their manager bulk
update already covers them.

### Residual only (Topgrade does not know these)

Self-updaters and tools whose update is not “the package manager”:

| Binary | Command |
|--------|---------|
| `agent` | `agent update` |
| `atuin` | `atuin update` |
| `composio` | `composio upgrade` |
| `devin` | `devin update` |
| `goose` | `goose update` |
| `grok` | `grok update` |
| `hermes` | `hermes update` |
| `kimi` | `kimi update` |
| `mimo` | `mimo upgrade` |
| `ntn` | `ntn update` |
| `ollama` | `ollama update` |
| `op` | `op update` |
| `qwen` | `qwen update` |
| `starship` | `starship self-update` |
| `warp` / `warp-cli` | `warp-cli update` |
| `zoxide` | `zoxide update` / `zoxide self-update` |

Topgrade 17+ already runs `claude update` and `opencode upgrade` as built-in steps, so those are **not** in the residual (avoids double updates when using `post_commands`).

Plus optional **go install @latest** tools (Topgrade will not reinstall these for you):

| Binary | Module |
|--------|--------|
| `espn-pp-cli` | `github.com/mvanhorn/printing-press-library/.../espn-pp-cli@latest` |
| `flight-goat-pp-cli` | `.../flight-goat-pp-cli@latest` |
| `movie-goat-pp-cli` | `.../movie-goat-pp-cli@latest` |
| `recipe-goat-pp-cli` | `.../recipe-goat-pp-cli@latest` |
| `printing-press` | `github.com/mvanhorn/cli-printing-press/v4/cmd/printing-press@latest` |
| `gopls` | `golang.org/x/tools/gopls@latest` |
| `goimports` | `golang.org/x/tools/cmd/goimports@latest` |

Bare `go install name@latest` entries without a full module path (`bumblebee`, `flora`)
are left out of the residual on purpose — fix the module path in the script when you
know it, or install those via brew/mise instead.

### Intentionally dropped

| Old behavior | Why |
|--------------|-----|
| Full filesystem discovery scan | You should install tools through a manager; mystery PATH bins are a smell |
| Per-tool brew/npm/uv known duplicates | Redundant with bulk manager updates |
| `gh auth refresh \|\| gh upgrade` | Auth refresh is not an update; use `brew upgrade gh` |
| TUI / quarantine / retry-fix / doctor / changelog | Product features of the big script; re-add only if you miss them |
| `tlmgr` with no sudo | Still needs a manual elevated run on system TeX |

## Scheduling

Replace or complement the existing LaunchAgent:

```bash
# Example: weekly Topgrade (includes residual if post_commands is set)
# crontab -e
0 8 * * 0 /opt/homebrew/bin/topgrade --no-retry >"$HOME/.config/update-all-clis/logs/topgrade.log" 2>&1
```

Or keep launchd but point it at `topgrade` instead of `update_all_clis.sh`.

## Rollback

```bash
# stop using Topgrade post_command; go back to:
~/update-all-clis/update_all_clis.sh
# or from this repo:
./update_all_clis.sh
```

No data migration is required. The big script’s cache under
`~/.config/update-all-clis/` is simply unused by this path.

## When to keep the full `update-all-clis` stack

Stay on the main script if you regularly need:

- live parallel TUI with per-job output
- failure quarantine + auto-derived reinstall fixes
- run history / version before→after / changelog digest
- doctor (broken symlinks, shadowed bins)
- discovery of tools you did not declare

Otherwise the hybrid here is the better default: **less code, same managers, explicit
long tail.**

## Customizing the residual

Edit the arrays at the top of `update_ai_clis.sh`. Skip missing binaries automatically
(`command -v`). Use:

```bash
./migration/update_ai_clis.sh --dry-run
./migration/update_ai_clis.sh --only=claude,hermes
./migration/update_ai_clis.sh --skip=ollama,go
./migration/update_ai_clis.sh --no-go          # self-updaters only
```
