# remediation.sh - shared skeleton for Cranston remediations. SOURCE this file.
#
# Every remediation follows the same shape (extracted from ten hand-copied
# cranston scripts): audit -> internal cap -> act -> verify -> result.
#
# Exit code convention:
#   0   fixed and verified
#   1   ran but failed / unverified
#   64  refused (bad usage, missing param, uncommissioned target)
#   75  refused by the internal rate cap (EX_TEMPFAIL - distinct from failure)
#
# The internal cap is a BACKSTOP: the engine has its own attempt caps, but a
# remediation never trusts its caller - it counts its own executions in the
# audit log and refuses past its budget even when invoked by hand.
#
# Portability: no GNU `date -d` (a v1 debt - it made every remediation
# Linux-only). Audit lines carry ts=<epoch>; the cap window is epoch math.
# bash 3.2+ compatible; no flock binary required.
#
# The engine exports SELFHEAL_AUDIT_LOG; standalone runs fall back to /tmp.

: "${SELFHEAL_AUDIT_LOG:=${TMPDIR:-/tmp}/selfheal-audit.log}"

rem_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

rem_audit() {
    mkdir -p "$(dirname "$SELFHEAL_AUDIT_LOG")" 2>/dev/null
    echo "$(rem_iso) ts=$(date +%s) $*" >> "$SELFHEAL_AUDIT_LOG"
}

# rem_begin <service-key> <action> - REQUIRED first call; writes the EXEC
# audit line the cap counts.
rem_begin() {
    REM_SERVICE="$1"
    REM_ACTION="${2:-$(basename "$0")}"
    rem_audit "EXEC service=$REM_SERVICE action=$REM_ACTION caller=${SELFHEAL_CALLER:-manual}"
}

# rem_cap <max> <window_hours> - refuse (exit 75) when this service has more
# than <max> EXEC lines inside the window (the line rem_begin just wrote
# counts, so call rem_begin first).
rem_cap() {
    local max="$1" win_h="$2" cutoff n
    cutoff=$(( $(date +%s) - win_h * 3600 ))
    n=$(awk -v c="$cutoff" -v tag=" EXEC service=$REM_SERVICE " '
        index($0, tag) {
            for (i = 1; i <= NF; i++)
                if ($i ~ /^ts=/) { t = substr($i, 4) + 0; if (t >= c) k++ }
        }
        END { print k + 0 }' "$SELFHEAL_AUDIT_LOG" 2>/dev/null)
    if [ "${n:-0}" -gt "$max" ]; then
        rem_audit "REFUSED service=$REM_SERVICE reason=internal-cap recent=$n max=$max window_h=$win_h"
        echo "REFUSED: internal rate cap ($n executions in ${win_h}h, max $max)"
        exit 75
    fi
}

# rem_refuse <message> - refuse for a non-cap reason (exit 64)
rem_refuse() {
    rem_audit "REFUSED service=${REM_SERVICE:-?} reason=$*"
    echo "REFUSED: $*"
    exit 64
}

# rem_verify <tries> <sleep_seconds> <cmd...> - poll until the command
# succeeds; 0 on success, 1 when the budget runs out. Pass the verification
# as argv, never as a string.
rem_verify() {
    local tries="$1" sleep_s="$2" i=1
    shift 2
    while [ "$i" -le "$tries" ]; do
        if "$@"; then return 0; fi
        sleep "$sleep_s"
        i=$((i + 1))
    done
    return 1
}

# rem_result <ok|fail> [detail...] - the RESULT audit line; echoes nothing
rem_result() {
    local st="$1"; shift || true
    rem_audit "RESULT service=$REM_SERVICE status=$st $*"
}

# rem_trap_restore <cmd...> - arm an EXIT/INT/TERM trap that re-asserts the
# safe state (e.g. outlet back ON) no matter how the script dies. The command
# is stored as argv; call rem_trap_clear once the safe state is confirmed if
# the trap should not fire on normal exit.
rem_trap_restore() {
    REM_RESTORE_CMD=("$@")
    trap '"${REM_RESTORE_CMD[@]}" >/dev/null 2>&1 || true' EXIT INT TERM
}

rem_trap_clear() {
    trap - EXIT INT TERM
}
