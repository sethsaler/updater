#!/usr/bin/env bash
# Manager sweeps used by the default tool_config.json.
set -eu

update_cargo() {
  local installed
  installed=$(cargo install --list)
  if [[ -z "$installed" ]]; then
    echo 'No Cargo-installed packages.'
    return
  fi

  # Cargo can resolve plugins from its install root even with a minimal
  # PATH. A missing plugin must never fall back to updating a project lockfile.
  if ! cargo install-update --version >/dev/null 2>&1; then
    echo 'Updating Cargo-installed packages requires cargo-update. Install it with cargo install cargo-update --locked.' >&2
    return 1
  fi
  cargo install-update --all --git
}

update_bun() {
  local installed result=0
  # A Homebrew/npm-owned runtime is upgraded by its package manager. -ef
  # compares inodes so a PATH symlink to the vendor install works as well.
  if [[ "$(command -v bun)" -ef "${BUN_INSTALL:-$HOME/.bun}/bin/bun" ]]; then
    bun upgrade || result=$?
  fi
  installed=$(bun pm ls -g)
  if [[ "$installed" == *' (0 installed)' ]]; then
    echo 'No Bun global packages.'
  else
    bun update -g || result=$?
  fi
  return "$result"
}

fast_forward_checkout() {
  local label="$1" app="$2" slug="$3" required_branch="${4:-}" branch remote url changes upstream
  if [[ ! -d "$app/.git" || -L "$app" ]]; then
    echo "$label update requires its dedicated installed checkout." >&2
    return 1
  fi
  branch=$(git -C "$app" symbolic-ref --short HEAD)
  remote=$(git -C "$app" config --get "branch.$branch.remote")
  url=$(git -C "$app" remote get-url "$remote")
  case "${url%.git}" in
    "https://github.com/$slug"|"git@github.com:$slug"|"ssh://git@github.com/$slug") ;;
    *) echo "$label checkout tracks a different repository." >&2; return 1 ;;
  esac
  if [[ -n "$required_branch" ]]; then
    upstream=$(git -C "$app" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')
    if [[ "$branch" != "$required_branch" || "$upstream" != "$remote/$required_branch" ]]; then
      echo "$label checkout does not track its release branch." >&2
      return 1
    fi
  fi
  changes=$(git -C "$app" status --porcelain)
  if [[ -n "$changes" ]]; then
    echo "$label checkout has local changes. Deferred its update." >&2
    return 1
  fi
  GIT_TERMINAL_PROMPT=0 git -C "$app" pull --ff-only --no-autostash
}

nexus_dependencies_current() {
  # The installed npm receipt is written at the end of reification. Compare
  # it and the actual packages to the vendor lock even if an earlier update
  # already advanced the checkout before npm finished.
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
def read(path):
    return json.loads((root / path).read_text())

try:
    lock = read("package-lock.json")["packages"]
    installed = read("node_modules/.package-lock.json")["packages"]
    manifest = read("package.json")
    if manifest.get("dependencies", {}) != lock[""].get("dependencies", {}):
        sys.exit(1)
    for path, spec in lock.items():
        if not path or spec.get("dev") or (spec.get("optional") and path not in installed):
            continue
        actual = installed[path]
        if any(actual.get(field) != spec.get(field) for field in ("version", "resolved", "integrity")):
            sys.exit(1)
        if read(Path(path) / "package.json")["version"] != spec["version"]:
            sys.exit(1)
except (KeyError, OSError, ValueError, TypeError):
    sys.exit(1)
PY
}

update_nexus() {
  local app="$HOME/.nexus/cli" setup="$HOME/.nexus/cli/packages/setup-cli"
  fast_forward_checkout Nexus "$app" fw-ai/fireconnect main
  if ! nexus_dependencies_current "$setup"; then
    # Nexus's postinstall finalizer migrates provider credentials and harness
    # settings. Only install the release's locked runtime dependencies here.
    (cd "$setup" && NEXUS_SKIP_POSTINSTALL_FINALIZE=1 npm ci --omit=dev --no-fund --no-audit --ignore-scripts)
    nexus_dependencies_current "$setup" || { echo 'Nexus runtime dependencies remain incomplete.' >&2; return 1; }
  fi
}

update_claude_science() {
  local output result
  if output=$(claude-science update </dev/null 2>&1); then
    printf '%s\n' "$output"
    return 0
  else
    result=$?
  fi
  if grep -Fxq 'update: not signed in to claude.ai' <<<"$output"; then
    echo 'Deferred claude-science: sign in to the Claude Science web UI to enable release checks.'
    return 0
  fi
  printf '%s\n' "$output" >&2
  return "$result"
}

update_mimo() {
  local installed latest
  if [[ "$(command -v mimo)" -ef "$HOME/.mimocode/bin/mimo" ]]; then
    installed=$(mimo --version)
    latest=$(curl -fsSL --retry 1 --max-time 25 https://mimocode.cnbj1.mi-fds.com/mimocode/mimocode/releases/latest)
    latest=${latest%$'\r'}
    if [[ ! $latest =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo 'MiMo returned an invalid stable release version.' >&2
      return 1
    fi
    if [[ "$installed" == "$latest" ]]; then
      echo "MiMo $installed is current."
      return 0
    fi
  fi
  mimo upgrade </dev/null
}

case "${1:-}" in
  cargo) update_cargo ;;
  bun) update_bun ;;
  headlong) fast_forward_checkout Headlong "${HEADLONG_HOME:-$HOME/.headlong}/app" laude-institute/headlong ;;
  nexus) update_nexus ;;
  claude-science) update_claude_science ;;
  mimo) update_mimo ;;
  *) echo "Usage: $0 {cargo|bun|headlong|nexus|claude-science|mimo}" >&2; exit 2 ;;
esac
