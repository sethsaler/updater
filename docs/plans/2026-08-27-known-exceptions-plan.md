---
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
date: 2026-08-27
---

# Known shrinks to exceptions - Plan

Seeded from ce-ideate: improvement ideas for update-all-clis (top-ranked survivor).

## Goal Capsule

**Objective:** Reduce `tool_config.json` maintenance by shipping `known` as exceptions only — self-updaters, explicit `go install` module paths, vendor reinstallers, and policy overrides — while bulk origins remain the catalog for manager-covered tools.

**Product authority:** Discovery + bulk is source of truth for what exists and how manager-owned tools update. `known` is for what bulk cannot express or where per-tool policy/commands are required.

**Open blockers:** Per-package bulk jobs (ideation idea #4) are desirable before pruning so npm/brew/uv-covered tools retain per-tool retry/fix/changelog without duplicate `known` entries.

## Problem

- ~120 `known` entries duplicate what bulk already does (`cline` gets its own `npm update -g cline` *and* the npm bulk sweep).
- `suggest-known` grows the inventory (`UPDATE_COMMAND_HERE` copy-paste), opposite of hygiene.
- Recent releases spent significant effort fixing broken per-tool commands that bulk would have covered anyway.
- [migration/README.md](../migration/README.md) already omits brew/npm/uv duplicates from the residual; the full stack has not adopted that model.

## Requirements

### R1 — Shipped `known` is residual-shaped

Base `tool_config.json` `known` MUST contain only:

1. **Self-updaters** — tools whose update is not the package manager (`claude update`, `hermes update`, …).
2. **Explicit reinstalls** — `go install` with full module path, vendor `curl | bash` patterns (bun-style).
3. **Policy overrides** — `:major` holds, custom commands bulk cannot express, `config.local.json`-documented one-offs worth sharing as examples.

Base `known` MUST NOT contain entries whose update is fully covered by a non-empty bulk origin command for the same manager (e.g. `brew upgrade bat`, `npm update -g cline`).

### R2 — Invert `suggest-known` to pruning

A new or replaced command (e.g. `suggest-known-prune` or `--suggest-known=prune`) MUST list `known` entries that are redundant with bulk coverage on the current machine's discovery cache.

Output MUST recommend removal (or moving to an optional overlay pack), not addition of `UPDATE_COMMAND_HERE` lines.

Existing `suggest-known` growth behavior MAY remain behind an explicit flag for users who still want it.

### R3 — Overlay packs via merge

Optional named packs (e.g. `packs/ai-clis.json` with only self-updaters and go-install paths) MAY ship in-repo.

Install/merge flow MUST layer packs onto `config.local.json` without requiring edits to base `tool_config.json`.

Packs MUST NOT reintroduce bulk-covered brew/npm/uv duplicates.

### R4 — Behavior preserved for exceptions

For tools remaining in `known`:

- Per-tool retry, fix, changelog (`repos`), and `:major` resolution MUST continue to work unchanged.
- `known` MUST still never suppress origin bulk (regression test `test_known_tool_does_not_suppress_origin_bulk` stays green).

For tools removed from `known` but covered by bulk:

- They MUST still update via bulk emit lines.
- Per-tool changelog/fix/retry applies at bulk granularity until per-package bulk (separate work) lands.

### R5 — Doctor alignment

Doctor prune suggestions for stale `known` (binary absent from PATH and cache) MUST remain informational.

Doctor SHOULD gain a section (informational, non-failing exit) listing `known` entries redundant with bulk on the latest cache — parallel to R2 but always visible in `--doctor`.

## Non-goals

- Structured `{manager, package, intent}` schema migration (ideation #3) — separate plan; not required for this slice.
- Removing bulk `|| true` from npm loop or other verified origins — separate honesty/per-package work.
- Changing Topgrade migration path or making it the default install.

## Acceptance examples

1. After pruning, `tool_config.json` `known` has no `brew upgrade fd`-style entries; `fd` still updates when discovered under brew origin and bulk runs.
2. `./update_all_clis.sh --suggest-known` (or prune mode) lists `cline` as redundant if npm bulk covers it and `cline` is still in `known`.
3. User with `packs/ai-clis.json` merged locally gets self-updater known entries without forking the repo.
4. `test_known_tool_does_not_suppress_origin_bulk` passes; no regression in emit plan counts for npm bulk when one npm known remains.

## Success criteria

- Base `known` count drops materially (target: roughly residual-sized, on the order of tens not ~120).
- No increase in chronic "broken known command" commits for bulk-covered tools.
- README and CHANGELOG document the exceptions-only model and overlay packs.

## Outstanding questions

- Whether to delete redundant knowns in one breaking release or ship prune suggestions first and prune in a follow-up.
- Whether `repos` changelog slugs move with pruned tools (lose per-tool digest) or attach to bulk origin only.
