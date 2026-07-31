#!/usr/bin/env bash
# Executor parity test — the verification gate for the single-executor
# consolidation. Runs one identical plan through:
#   1. the legacy bash executor (run_updates_parallel, sourced via
#      UAC_SOURCE_ONLY), and
#   2. the Python executor (tui_update_all_clis.py --mode plain)
# and asserts they agree on: per-job exit codes, history-record fields,
# ok/fail tallies, lock-group serialization, and — at --parallel 1 — the
# exact stdout lines. Also asserts --quiet makes both fully silent.
#
# bash 3.2 compatible (no mapfile / wait -n / assoc arrays).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SEP=$'\x1e'
FAILURES=0

note() { printf '# %s\n' "$*"; }
fail() { printf 'not ok - %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
ok() { printf 'ok - %s\n' "$*"; }

# -------------------------------------------------------------------------
# Fake job scripts. Every script prints at least one line so failure-tail
# reporting is exercised (empty-output jobs are uninteresting here).
# -------------------------------------------------------------------------
mkdir -p "$WORK/bin"
cat > "$WORK/bin/flaky.sh" <<'EOF'
#!/usr/bin/env bash
# Fails until its marker file exists: first attempt fails, retry succeeds.
# The marker path comes from the environment so each executor/phase gets an
# independent marker (the two runs in a phase must both see a real retry).
echo "flaky: attempt"
m="${UAC_T_FLAKY:-/nonexistent-uac-t-flaky}"
if [[ -f "$m" ]]; then exit 0; fi
touch "$m"
exit 1
EOF
cat > "$WORK/bin/fail2.sh" <<'EOF'
#!/usr/bin/env bash
echo "fail2: line one"
echo "fail2: line two"
exit 1
EOF
cat > "$WORK/bin/fix.sh" <<'EOF'
#!/usr/bin/env bash
echo "fix: repaired"
exit 0
EOF
cat > "$WORK/bin/fixfail.sh" <<'EOF'
#!/usr/bin/env bash
echo "fix: nope"
exit 1
EOF
chmod +x "$WORK/bin/"*.sh

# -------------------------------------------------------------------------
# The plan: exercises ok, retry-success, fix-success, plain failure,
# fix failure, watchdog timeout, --skip, and every instant kind.
# -------------------------------------------------------------------------
PLAN="$WORK/plan.txt"
cat > "$PLAN" <<EOF
known${SEP}good${SEP}true${SEP}grp-good
known${SEP}flaky${SEP}$WORK/bin/flaky.sh${SEP}grp-flaky
known${SEP}badfix${SEP}$WORK/bin/fail2.sh${SEP}grp-badfix${SEP}$WORK/bin/fix.sh
known${SEP}nofix${SEP}$WORK/bin/fail2.sh${SEP}grp-nofix
known${SEP}fixfails${SEP}$WORK/bin/fail2.sh${SEP}grp-fixfails${SEP}$WORK/bin/fixfail.sh
known${SEP}slow${SEP}echo slow: starting; sleep 30${SEP}grp-slow
known${SEP}skiptool${SEP}true${SEP}grp-skiptool
skip${SEP}skipkind${SEP}${SEP}
held${SEP}heldcfg${SEP}config
held${SEP}heldenv${SEP}env
held${SEP}majheld${SEP}major:config:3.0.0
held${SEP}majenv${SEP}major:env:2.0.0
held${SEP}majunk${SEP}major:config:unknown
uptodate${SEP}utool${SEP}2
bulk${SEP}bulkok${SEP}true${SEP}grp-bulk
known${SEP}locka${SEP}echo a-start >> \$UAC_T_LOCKLOG; sleep 1; echo a-end >> \$UAC_T_LOCKLOG${SEP}shared
known${SEP}lockb${SEP}echo b-start >> \$UAC_T_LOCKLOG; sleep 1; echo b-end >> \$UAC_T_LOCKLOG${SEP}shared
EOF

# -------------------------------------------------------------------------
# Runner wrappers. Both take: <parallel> <quiet> <plan> <out> <results>
# <tallies>; results are normalized to the Python executor's format
# ("<ec>\x1e<record>", empty record for skip) in plan order.
# -------------------------------------------------------------------------
run_py_executor() {
  local par="$1" quiet="$2" plan="$3" out="$4" results="$5" tallies="$6" marker="$7"
  local -a extra=()
  [[ "$quiet" == "1" ]] && extra+=(--quiet)
  NO_COLOR=1 UAC_T_FLAKY="$marker" UAC_T_LOCKLOG="$marker.locklog" \
    python3 "$REPO_ROOT/tui_update_all_clis.py" \
    --emit-file "$plan" --results-file "$results" \
    --parallel "$par" --timeout 2 --retries 1 --retry-delay 0.1 --fix 1 \
    --mode plain --skip skiptool "${extra[@]+"${extra[@]}"}" > "$out"
  python3 - "$results" > "$tallies" <<'PYEOF'
import sys
ok = fail = 0
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.rstrip("\n")
    if not line:
        continue
    ec = line.split("\x1e", 1)[0]
    if ec == "0":
        ok += 1
    elif ec != "3":
        fail += 1
print(ok, fail)
PYEOF
}

run_bash_executor() {
  local par="$1" quiet="$2" plan="$3" out="$4" results="$5" tallies="$6" marker="$7"
  (
    set --
    export UAC_SOURCE_ONLY=1 NO_COLOR=1 UAC_T_FLAKY="$marker" UAC_T_LOCKLOG="$marker.locklog"
    cd "$REPO_ROOT" || exit 1
    # shellcheck disable=SC1091
    source ./update_all_clis.sh
    # These globals configure the sourced script's executor (shellcheck
    # can't see the usage through the dynamic source).
    # shellcheck disable=SC2034
    UAC_JOB_TIMEOUT=2 UAC_RETRIES=1 UAC_RETRY_DELAY=0.1 UAC_FIX=1
    # shellcheck disable=SC2034
    PARALLEL_JOBS="$par"
    # shellcheck disable=SC2034
    SKIP="skiptool"
    # shellcheck disable=SC2034
    LOCK_DIR="$WORK/locks-p$par-q$quiet"
    # shellcheck disable=SC2034
    [[ "$quiet" == "1" ]] && QUIET=1
    UPDATE_OK=0 UPDATE_FAIL=0
    _UAC_RESULT_LINES=()
    local -a _lines=()
    local _l
    while IFS= read -r _l || [[ -n "$_l" ]]; do
      [[ -n "$_l" ]] && _lines+=("$_l")
    done < "$plan"
    run_updates_parallel "$par" "${_lines[@]}" > "$out"
    : > "$results"
    # The bash executor ingests its per-job *.result files in lexicographic
    # glob order (1, 10, 11, ..., 2, ...) — reproduce that order exactly:
    # walk 1-based plan indices sorted as strings, synthesizing the
    # never-recorded skip lines in place.
    local _rec_idx=0 _idx _kind _rec _rest _ec
    local -a _idxs=()
    for (( _idx = 1; _idx <= ${#_lines[@]}; _idx++ )); do _idxs+=("$_idx"); done
    local _sorted_idxs
    _sorted_idxs=$(printf '%s\n' "${_idxs[@]}" | LC_ALL=C sort)
    while IFS= read -r _idx || [[ -n "$_idx" ]]; do
      [[ -z "$_idx" ]] && continue
      _kind="${_lines[$((_idx - 1))]%%"$SEP"*}"
      case "$_kind" in
        skip)
          # Instant kinds the bash executor never records; ec is always 3.
          printf '3%s\n' "$SEP" >> "$results"
          ;;
        *)
          _rec="${_UAC_RESULT_LINES[$_rec_idx]:-}"
          _rec_idx=$((_rec_idx + 1))
          # record = kind SEP name SEP cmd SEP ec SEP start SEP end
          _rest="${_rec#*"$SEP"}"; _rest="${_rest#*"$SEP"}"; _rest="${_rest#*"$SEP"}"
          _ec="${_rest%%"$SEP"*}"
          printf '%s%s%s\n' "$_ec" "$SEP" "$_rec" >> "$results"
          ;;
      esac
    done <<< "$_sorted_idxs"
    printf '%s %s\n' "$UPDATE_OK" "$UPDATE_FAIL" > "$tallies"
  )
}

# -------------------------------------------------------------------------
# Result-file comparison: row count, per-row ec + record fields (kind,
# name, cmd, recorded-ec). Wall-clock timestamps can never be equal across
# two runs, so they're checked as per-executor invariants instead:
# end >= start for every job, uptodate's synthesized duration is exactly
# its pre-check seconds, the timed-out job's duration brackets the 2s
# watchdog, and lock-group members serialize.
# -------------------------------------------------------------------------
compare_results() {  # <label> <py.results> <bash.results> <py.locklog> <bash.locklog>
  python3 - "$1" "$2" "$3" "$4" "$5" <<'PYEOF'
import sys
label, py_path, bash_path, py_log, bash_log = sys.argv[1:6]

def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            ec, _, rec = line.partition("\x1e")
            rows.append((ec, rec.split("\x1e") if rec else []))
    return rows

py, bash = load(py_path), load(bash_path)
errors = []
if len(py) != len(bash):
    errors.append(f"row count: py={len(py)} bash={len(bash)}")
else:
    for i, ((pec, pf), (bec, bf)) in enumerate(zip(py, bash)):
        name = pf[1] if len(pf) >= 2 else (bf[1] if len(bf) >= 2 else f"row{i}")
        if pec != bec:
            errors.append(f"row {i} ({name}): ec py={pec} bash={bec}")
        # kind/name/cmd/recorded-ec must be byte-identical.
        if pf[:4] != bf[:4]:
            errors.append(f"row {i} ({name}): record fields py={pf[:4]} bash={bf[:4]}")
        if pf and pf[3] != pec:
            errors.append(f"row {i} ({name}): line ec {pec} != recorded ec {pf[3]}")
        for side, f in (("py", pf), ("bash", bf)):
            if len(f) != 6:
                continue
            s, e = int(f[4]), int(f[5])
            if e < s:
                errors.append(f"row {i} ({name}): {side} inverted timestamps")
            if name == "utool" and e - s != 2:
                errors.append(f"row {i} ({name}): {side} duration {e-s}s != 2s pre-check")
            if name == "slow" and not (1 <= e - s <= 6):
                errors.append(f"row {i} ({name}): {side} duration {e-s}s outside 2s watchdog window")

# Lock-group serialization witness: each lock job appends start/end markers
# to a shared log as it executes. Serialized executions produce exactly one
# of the two sequential orders; overlapping executions interleave. (Either
# job may legitimately win the lock — the order itself is not asserted.)
SERIALIZED = (
    ["a-start", "a-end", "b-start", "b-end"],
    ["b-start", "b-end", "a-start", "a-end"],
)
for side, path in (("py", py_log), ("bash", bash_log)):
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        lines = []
    if lines not in SERIALIZED:
        errors.append(f"{side}: lock-group executions interleaved or missing: {lines}")

if errors:
    print(f"not ok - {label}")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
print(f"ok - {label}")
PYEOF
}

# =========================================================================
note "phase 1: parallel=1 — full parity (stdout byte-identical, records, tallies)"
run_py_executor   1 0 "$PLAN" "$WORK/py1.out"   "$WORK/py1.results"   "$WORK/py1.tally"   "$WORK/flaky.py1"
run_bash_executor 1 0 "$PLAN" "$WORK/bash1.out" "$WORK/bash1.results" "$WORK/bash1.tally" "$WORK/flaky.bash1"

if diff -u "$WORK/bash1.out" "$WORK/py1.out" > "$WORK/diff1.txt" 2>&1; then
  ok "stdout identical (parallel=1)"
else
  fail "stdout differs (parallel=1)"
  sed 's/^/  /' "$WORK/diff1.txt"
fi
# Record-file order is an artifact (bash ingests its *.result files in
# lexicographic glob order; history-append doesn't care — every record has
# its own timestamps). The contract is the multiset: sort both sides.
LC_ALL=C sort "$WORK/py1.results"   > "$WORK/py1.sorted"
LC_ALL=C sort "$WORK/bash1.results" > "$WORK/bash1.sorted"
compare_results "records agree (parallel=1)" "$WORK/py1.sorted" "$WORK/bash1.sorted" \
  "$WORK/flaky.py1.locklog" "$WORK/flaky.bash1.locklog" || FAILURES=$((FAILURES + 1))
if diff "$WORK/py1.tally" "$WORK/bash1.tally" >/dev/null 2>&1; then
  ok "ok/fail tallies agree: $(cat "$WORK/py1.tally")"
else
  fail "tallies differ: py=$(cat "$WORK/py1.tally") bash=$(cat "$WORK/bash1.tally")"
fi

# Expected tallies as a sanity anchor: good, flaky, badfix, utool, bulkok,
# locka, lockb ok; nofix, fixfails, slow failed; skips/held not counted.
if [[ "$(cat "$WORK/py1.tally")" == "7 3" ]]; then
  ok "tallies are 7 ok / 3 failed as expected"
else
  fail "unexpected tallies: $(cat "$WORK/py1.tally") (expected '7 3')"
fi

# =========================================================================
note "phase 2: quiet — both executors fully silent, results unchanged"
run_py_executor   1 1 "$PLAN" "$WORK/py2.out"   "$WORK/py2.results"   "$WORK/py2.tally"   "$WORK/flaky.py2"
run_bash_executor 1 1 "$PLAN" "$WORK/bash2.out" "$WORK/bash2.results" "$WORK/bash2.tally" "$WORK/flaky.bash2"

if [[ ! -s "$WORK/py2.out" ]]; then
  ok "python executor silent under --quiet"
else
  fail "python executor printed under --quiet"
  sed 's/^/  /' "$WORK/py2.out"
fi
if [[ ! -s "$WORK/bash2.out" ]]; then
  ok "bash executor silent under QUIET=1"
else
  fail "bash executor printed under QUIET=1"
  sed 's/^/  /' "$WORK/bash2.out"
fi
LC_ALL=C sort "$WORK/py2.results"   > "$WORK/py2.sorted"
LC_ALL=C sort "$WORK/bash2.results" > "$WORK/bash2.sorted"
compare_results "records agree (quiet)" "$WORK/py2.sorted" "$WORK/bash2.sorted" \
  "$WORK/flaky.py2.locklog" "$WORK/flaky.bash2.locklog" || FAILURES=$((FAILURES + 1))

# =========================================================================
note "phase 3: parallel=4 — same record multiset and tallies (stdout order not compared)"
run_py_executor   4 0 "$PLAN" "$WORK/py4.out"   "$WORK/py4.results"   "$WORK/py4.tally"   "$WORK/flaky.py4"
run_bash_executor 4 0 "$PLAN" "$WORK/bash4.out" "$WORK/bash4.results" "$WORK/bash4.tally" "$WORK/flaky.bash4"

LC_ALL=C sort "$WORK/py4.results"   > "$WORK/py4.sorted"
LC_ALL=C sort "$WORK/bash4.results" > "$WORK/bash4.sorted"
compare_results "records agree (parallel=4, sorted)" "$WORK/py4.sorted" "$WORK/bash4.sorted" \
  "$WORK/flaky.py4.locklog" "$WORK/flaky.bash4.locklog" || FAILURES=$((FAILURES + 1))
if diff "$WORK/py4.tally" "$WORK/bash4.tally" >/dev/null 2>&1; then
  ok "ok/fail tallies agree (parallel=4)"
else
  fail "tallies differ (parallel=4): py=$(cat "$WORK/py4.tally") bash=$(cat "$WORK/bash4.tally")"
fi

# =========================================================================
if (( FAILURES > 0 )); then
  note "$FAILURES parity check(s) failed"
  exit 1
fi
note "executor parity: ok"
