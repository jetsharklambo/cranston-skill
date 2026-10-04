#!/bin/bash
# gates/gate-totp-remote.sh - Tier 2 approval gate: off-box TOTP verification.
#
# The TOTP secret lives on a SECOND device running gates/gate-totp-server.py
# (any always-on box: the HA Pi, a NAS, an old phone under Termux). This box -
# the one the agent lives on - only ever sees 6-digit codes, so the agent can
# never mint its own approvals. That separation is the whole point; running
# the verifier on THIS box reduces the gate to theater.
#
# Code sources, in order: $GATE_CODE (set by `approve-heal.py <key> <code>`
# when the admin's chat reply carried one), else an interactive prompt when a
# tty is present, else refuse.
#
# Setup: start the verifier on the second device, then set here (or in the
# cron/env of the caller): GATE_TOTP_URL="http://<second-device>:8766".
# Contract: references/approval-gates.md (argv pass-through, exit 65 refusal).
set -u

AUDIT="${SELFHEAL_AUDIT_LOG:-/dev/null}"
SCRIPT="${1:-}"
ARGSUMMARY="$(basename "${SCRIPT:-?}") ${2:-}"
audit() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$AUDIT" 2>/dev/null || true; }
refuse() { audit "GATE-REFUSED reason=\"$1\" argv=$ARGSUMMARY"; echo "gate: $1"; exit 65; }

[ -n "$SCRIPT" ] && [ -f "$SCRIPT" ] || refuse "no such remediation script: '${SCRIPT}'"

under_remediations() {
    [ -n "${SELFHEAL_ROOT:-}" ] || return 1
    local dir; dir="$(cd "$(dirname "$SCRIPT")" && pwd)/"
    case "$dir" in "${SELFHEAL_ROOT%/}/remediations/"*) return 0 ;; *) return 1 ;; esac
}
if [ "${SELFHEAL_AUTOMATION:-}" = "true" ] && under_remediations; then
    audit "GATE-AUTO-PASS $ARGSUMMARY"
    exec bash "$@"
fi

[ -n "${GATE_TOTP_URL:-}" ] || refuse "GATE_TOTP_URL not set (where is the verifier device?)"

CODE="${GATE_CODE:-}"
if [ -z "$CODE" ] && [ -t 0 ]; then
    printf "TOTP code for %s: " "$ARGSUMMARY" > /dev/tty
    read -r CODE < /dev/tty
fi
case "$CODE" in
    [0-9][0-9][0-9][0-9][0-9][0-9]|[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    "") refuse "no code supplied (pass it with the approval, e.g. 'heal <key> <code>')" ;;
    *)  refuse "malformed code (expect 6-8 digits)" ;;
esac

RESP=$(curl -s -m 10 -X POST "${GATE_TOTP_URL%/}/verify" \
    -H "Content-Type: application/json" \
    -d "{\"code\": \"${CODE}\"}")
if printf '%s' "$RESP" | grep -q '"ok": *true'; then
    audit "GATE-APPROVED (totp) argv=$ARGSUMMARY"
    exec bash "$@"
fi
refuse "verifier rejected the code (${RESP:-no response}) - failing closed"
