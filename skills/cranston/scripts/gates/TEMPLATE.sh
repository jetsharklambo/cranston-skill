#!/bin/bash
# gates/TEMPLATE.sh - approval-gate skeleton. Copy, rename, implement step 2.
#
# The gate CONTRACT (full text: references/approval-gates.md):
#   - You receive the remediation argv as YOUR argv: $1 = script path,
#     $2 = optional single sanitized argument.
#   - ARGV SHAPE - check it yourself, never trust the caller: refuse unless
#     $# is 1 or 2, and unless $2 (when present) matches
#     [A-Za-z0-9][A-Za-z0-9._@:-]{0,63} - letters, digits, . _ @ : -, no
#     leading '-', no slash, no whitespace, at most 64 characters. The engine
#     and approve-heal enforce the same rule; the gate is the backstop, and
#     the check sits ABOVE the auto-pass branch so the auto path is bounded.
#   - Show the approver and the audit log the FULL resolved script path, never
#     the basename: "restart.sh" could be /tmp/restart.sh.
#   - APPROVE  -> exec the remediation ("exec bash "$@"" - never eval, never
#     compose a shell string from anything a finding produced).
#   - REFUSE   -> exit 65 with a one-line reason on stdout.
#   - Budget: ~120s for any human interaction (approve-heal allows 300s total
#     including the remediation; the engine's auto path allows 180s and must
#     never block on a human).
#   - Callers you must distinguish (by env, set by the engine/approve-heal):
#       engine auto path : SELFHEAL_AUTOMATION=true, SELFHEAL_CALLER=selfheal.py
#       human approval   : SELFHEAL_CALLER=approve-heal (+ GATE_CODE when the
#                          approval reply carried a code)
#     Auto-class remediations are fail-safe by design (ON-only templates) and
#     MUST pass non-interactively, or every routine self-heal blocks on a human.
set -u

AUDIT="${SELFHEAL_AUDIT_LOG:-/dev/null}"
SCRIPT="${1:-}"
# What the audit line and the human prompt show. Until the argv is validated
# below this is the caller's raw input, control characters stripped, so a
# refused argument cannot forge audit lines.
ARGSUMMARY="$(printf '%s' "${1:-?} ${2:-}" | LC_ALL=C tr -d '\000-\037\177')"
audit() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$AUDIT" 2>/dev/null || true; }
refuse() { audit "GATE-REFUSED reason=\"$1\" argv=$ARGSUMMARY"; echo "gate: $1"; exit 65; }

# 0. Argv shape (defense in depth - the engine checks the same, never trust
#    the caller): <script> plus at most ONE [A-Za-z0-9][A-Za-z0-9._@:-]{0,63}.
valid_arg() {
    case "$1" in ""|*[[:space:]]*) return 1 ;; esac   # one line, or grep -q matches the first
    printf '%s\n' "$1" | LC_ALL=C grep -Eq '^[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}$'
}
[ $# -eq 1 ] || [ $# -eq 2 ] || refuse "expected <script> [one arg], got $# arguments"
[ $# -eq 1 ] || valid_arg "$2" || refuse "malformed remediation argument"
[ -n "$SCRIPT" ] && [ -f "$SCRIPT" ] || refuse "no such remediation script: '${SCRIPT}'"
# Full resolved path: an approver shown "restart.sh" cannot tell
# /opt/cranston/remediations/restart.sh from /tmp/restart.sh.
DIR="$(cd "$(dirname "$SCRIPT")" 2>/dev/null && pwd)" && [ -n "$DIR" ] \
    || refuse "cannot resolve the directory of '${SCRIPT}'"
SCRIPT="$DIR/$(basename "$SCRIPT")"
set -- "$SCRIPT" ${2:+"$2"}      # from here on "$@" is the validated, resolved argv
ARGSUMMARY="$*"

under_remediations() {
    [ -n "${SELFHEAL_ROOT:-}" ] || return 1
    local dir; dir="$(cd "$(dirname "$SCRIPT")" && pwd)/"
    case "$dir" in "${SELFHEAL_ROOT%/}/remediations/"*) return 0 ;; *) return 1 ;; esac
}

# 1. Engine auto path: allowlisted fail-safe fixes pass without interaction.
if [ "${SELFHEAL_AUTOMATION:-}" = "true" ] && under_remediations; then
    audit "GATE-AUTO-PASS $ARGSUMMARY"
    exec bash "$@"
fi

# 2. Human approval path: YOUR second-factor check goes here. Show the admin
#    $ARGSUMMARY (full path + arg), never a basename.
#    On success:  audit "GATE-APPROVED $ARGSUMMARY"; exec bash "$@"
#    On failure:  refuse "<why>"
refuse "TEMPLATE gate has no factor configured - copy this file and implement step 2"
