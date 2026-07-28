#!/usr/bin/env bash
# =============================================================================
# update_ai_clis.sh — thin residual updater for tools package managers miss
#
# Companion to Topgrade (see migration/topgrade.toml + migration/README.md).
# Does NOT discover the filesystem. Only runs commands for binaries that exist.
#
# Usage:
#   ./update_ai_clis.sh
#   ./update_ai_clis.sh --dry-run
#   ./update_ai_clis.sh --only=claude,hermes
#   ./update_ai_clis.sh --skip=ollama,go
#   ./update_ai_clis.sh --no-go
# =============================================================================

set -euo pipefail

DRY_RUN=0
NO_GO=0
ONLY=""
SKIP=""

usage() {
  sed -n '2,16p' "$0" | tr -d '#'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage ;;
    --dry-run) DRY_RUN=1 ;;
    --no-go) NO_GO=1 ;;
    --only=*) ONLY="${arg#--only=}" ;;
    --skip=*) SKIP="${arg#--skip=}" ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# name|command  (command may contain spaces; split on first |)
# Self-updaters only — brew/npm/uv/cargo tools belong to Topgrade.
# Omitted on purpose when Topgrade already has a first-class step (as of topgrade 17.x):
#   claude, opencode, uv, bun, cursor-agent, npm/pnpm, brew, gem, …
SELF_UPDATERS=(
  "agent|agent update"
  "atuin|atuin update"
  "composio|composio upgrade"
  "devin|devin update"
  "goose|goose update"
  "grok|grok update"
  "hermes|hermes update"
  "kimi|kimi update"
  "mimo|mimo upgrade"
  "ntn|ntn update"
  "ollama|ollama update"
  "op|op update"
  "qwen|qwen update"
  "starship|starship self-update"
  "warp-cli|warp-cli update"
  "zoxide|zoxide update 2>/dev/null || zoxide self-update"
)

# name|go install module@latest
# Full module paths only (bare `go install foo@latest` is usually wrong).
GO_TOOLS=(
  "espn-pp-cli|go install github.com/mvanhorn/printing-press-library/library/media-and-entertainment/espn/cmd/espn-pp-cli@latest"
  "flight-goat-pp-cli|go install github.com/mvanhorn/printing-press-library/library/travel/flight-goat/cmd/flight-goat-pp-cli@latest"
  "movie-goat-pp-cli|go install github.com/mvanhorn/printing-press-library/library/media-and-entertainment/movie-goat/cmd/movie-goat-pp-cli@latest"
  "recipe-goat-pp-cli|go install github.com/mvanhorn/printing-press-library/library/food-and-dining/recipe-goat/cmd/recipe-goat-pp-cli@latest"
  "printing-press|go install github.com/mvanhorn/cli-printing-press/v4/cmd/printing-press@latest"
  "gopls|go install golang.org/x/tools/gopls@latest"
  "goimports|go install golang.org/x/tools/cmd/goimports@latest"
)

ok=0
failed=0
skipped=0

in_csv() {
  # $1=name $2=csv
  local name="$1" csv="$2" item
  [[ -z "$csv" ]] && return 1
  IFS=',' read -ra items <<< "$csv"
  for item in "${items[@]}"; do
    item="${item// /}"
    [[ "$item" == "$name" ]] && return 0
  done
  return 1
}

should_run() {
  local name="$1"
  if [[ -n "$ONLY" ]] && ! in_csv "$name" "$ONLY"; then
    return 1
  fi
  if in_csv "$name" "$SKIP"; then
    return 1
  fi
  # --skip=go skips the whole go group via name "go" checked by caller
  return 0
}

run_one() {
  local name="$1"
  local cmd="$2"
  local bin="$name"

  # warp-cli binary name vs friendly "warp"
  case "$name" in
    warp-cli) bin="warp-cli" ;;
  esac

  if ! should_run "$name"; then
    return 0
  fi

  if ! command -v "$bin" >/dev/null 2>&1; then
    printf '  · %-18s skipped (not installed)\n' "$name"
    skipped=$((skipped + 1))
    return 0
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '  · %-18s [dry-run] %s\n' "$name" "$cmd"
    ok=$((ok + 1))
    return 0
  fi

  printf '  → %-18s %s\n' "$name" "$cmd"
  # stdin closed so prompts cannot hang a scheduled run
  set +e
  bash -c "$cmd" </dev/null
  local rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    printf '  ✓ %-18s ok\n' "$name"
    ok=$((ok + 1))
  else
    printf '  ✗ %-18s failed (exit %s)\n' "$name" "$rc"
    failed=$((failed + 1))
  fi
}

echo "==> Self-updating CLIs"
for entry in "${SELF_UPDATERS[@]}"; do
  name="${entry%%|*}"
  cmd="${entry#*|}"
  run_one "$name" "$cmd"
done

run_go_section=1
if [[ "$NO_GO" -eq 1 ]] || in_csv "go" "$SKIP"; then
  run_go_section=0
fi
# If --only is set, only enter the go section when "go" or a go tool name is listed.
if [[ -n "$ONLY" ]] && ! in_csv "go" "$ONLY"; then
  run_go_section=0
  for entry in "${GO_TOOLS[@]}"; do
    if in_csv "${entry%%|*}" "$ONLY"; then
      run_go_section=1
      break
    fi
  done
fi

if [[ "$run_go_section" -eq 1 ]]; then
  if command -v go >/dev/null 2>&1; then
    echo "==> Go tools (go install @latest)"
    for entry in "${GO_TOOLS[@]}"; do
      name="${entry%%|*}"
      cmd="${entry#*|}"
      run_one "$name" "$cmd"
    done
  else
    echo "==> Go tools skipped (go not on PATH)"
  fi
fi

echo ""
echo "Summary: ok=$ok failed=$failed skipped=$skipped"
if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
exit 0
