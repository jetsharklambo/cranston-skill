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
# Setup: start the verifier on the second device, then create
# ~/.cranston-gate-totp.env, chmod 600, containing:
#   GATE_TOTP_URL="http://<second-device>:8766"
# The file is the trust anchor: approve-heal scrubs every GATE_* variable from
# the environment it hands the gate, so a caller cannot point the gate at a
# verifier of their own. (Override the file's path with GATE_TOTP_ENV only
# when invoking the gate directly, e.g. in tests.)
# Contract: references/approval-gates.md - argv pass-through: <script> plus at
# most ONE argument of shape [A-Za-z0-9][A-Za-z0-9._@:-]{0,63}; anything else
# exits 65 like a refusal. The tty prompt names the FULL script path.
set -u

AUDIT="${SELFHEAL_AUDIT_LOG:-/dev/null}"
SCRIPT="${1:-}"
# Audit/prompt text. Until the argv is validated below this is the caller's raw
# input, control characters stripped, so a refused argument cannot forge audit lines.
ARGSUMMARY="$(printf '%s' "${1:-?} ${2:-}" | LC_ALL=C tr -d '\000-\037\177')"
audit() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$AUDIT" 2>/dev/null || true; }
refuse() { audit "GATE-REFUSED reason=\"$1\" argv=$ARGSUMMARY"; echo "gate: $1"; exit 65; }

# Argv shape (defense in depth - the engine checks the same, never trust the
# caller): <script> plus at most ONE [A-Za-z0-9][A-Za-z0-9._@:-]{0,63}.
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
if [ "${SELFHEAL_AUTOMATION:-}" = "true" ] && under_remediations; then
    audit "GATE-AUTO-PASS $ARGSUMMARY"
    exec bash "$@"
fi

# The verifier's address comes from the gate's own mode-600 file, never from
# whoever invoked us (approve-heal strips GATE_* from the environment).
ENVFILE="${GATE_TOTP_ENV:-$HOME/.cranston-gate-totp.env}"
if [ -f "$ENVFILE" ]; then
    PERMS=$(stat -c %a "$ENVFILE" 2>/dev/null || stat -f %Lp "$ENVFILE" 2>/dev/null)
    case "$PERMS" in *00) ;; *) refuse "totp gate env file $ENVFILE must be mode 600 (is $PERMS)" ;; esac
    # shellcheck disable=SC1090
    . "$ENVFILE"
fi
[ -n "${GATE_TOTP_URL:-}" ] || refuse "GATE_TOTP_URL not set - put it in $ENVFILE (where is the verifier device?)"

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
