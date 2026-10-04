#!/bin/bash
# gates/TEMPLATE.sh - approval-gate skeleton. Copy, rename, implement step 2.
#
# The gate CONTRACT (full text: references/approval-gates.md):
#   - You receive the remediation argv as YOUR argv: $1 = script path,
#     $2 = optional single sanitized argument.
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
audit() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$AUDIT" 2>/dev/null || true; }
refuse() { audit "GATE-REFUSED reason=\"$1\" argv=$SCRIPT ${2:-}"; echo "gate: $1"; exit 65; }

[ -n "$SCRIPT" ] && [ -f "$SCRIPT" ] || refuse "no such remediation script: '${SCRIPT}'"

under_remediations() {
    [ -n "${SELFHEAL_ROOT:-}" ] || return 1
    local dir; dir="$(cd "$(dirname "$SCRIPT")" && pwd)/"
    case "$dir" in "${SELFHEAL_ROOT%/}/remediations/"*) return 0 ;; *) return 1 ;; esac
}

# 1. Engine auto path: allowlisted fail-safe fixes pass without interaction.
if [ "${SELFHEAL_AUTOMATION:-}" = "true" ] && under_remediations; then
    audit "GATE-AUTO-PASS $*"
    exec bash "$@"
fi

# 2. Human approval path: YOUR second-factor check goes here.
#    On success:  audit "GATE-APPROVED $*"; exec bash "$@"
#    On failure:  refuse "<why>"
refuse "TEMPLATE gate has no factor configured - copy this file and implement step 2"
