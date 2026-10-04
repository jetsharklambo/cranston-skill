#!/bin/bash
# gates/secure-bash-argv.sh - "Reuse the existing gate": the argv gate for a
# deployment whose dangerous-command path is already a 2FA wrapper that takes
# ONE SHELL STRING (the OpenClaw secure-bash). The engine speaks argv; this
# gate validates that argv to the gate contract and hands the wrapper a
# string composed only from the validated parts. One factor, one audit trail.
#
# Setup (once): create ~/.cranston-gate-secure-bash.env, chmod 600, containing:
#   SECURE_BASH="/path/to/the/deployment's/2fa-wrapper"
# Then set "approval_gate": "gates/secure-bash-argv.sh" in services.json, and
# point the deployment's dangerous-command detector at THIS file: it is the
# allowlist anchor, so it requires the remediation to live under
# $SELFHEAL_ROOT/remediations/ on BOTH paths, not only the automation one.
#
# Contract: references/approval-gates.md - argv pass-through: <script> plus at
# most ONE argument of shape [A-Za-z0-9][A-Za-z0-9._@:-]{0,63}; anything else
# exits 65 like a refusal. GATE_CODE (set by `approve-heal.py <key> <code>`)
# passes through the environment untouched - the wrapper may consume it.
# Env knobs: GATE_SECURE_BASH_ENV (env file path; approve-heal strips GATE_*
# from the child env, so it only matters when invoking the gate directly, e.g.
# in tests - the mode-600 file is the trust anchor).
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

# 2. Human approval path: forward to the deployment's own 2FA wrapper.
#    This gate is the deployment's allowlist anchor, so the remediations/
#    anchor holds here too - the engine's auto path is not the only one bounded.
under_remediations \
    || refuse "script is not under \$SELFHEAL_ROOT/remediations/ (SELFHEAL_ROOT=${SELFHEAL_ROOT:-unset}) - this gate is the allowlist anchor"

# Where the wrapper lives comes from the gate's own mode-600 file, never from
# whoever invoked us (approve-heal strips GATE_* from the environment).
ENVFILE="${GATE_SECURE_BASH_ENV:-$HOME/.cranston-gate-secure-bash.env}"
[ -f "$ENVFILE" ] || refuse "secure-bash gate env file missing: $ENVFILE"
PERMS=$(stat -c %a "$ENVFILE" 2>/dev/null || stat -f %Lp "$ENVFILE" 2>/dev/null)
case "$PERMS" in *00) ;; *) refuse "secure-bash gate env file $ENVFILE must be mode 600 (is $PERMS)" ;; esac
# shellcheck disable=SC1090
. "$ENVFILE"
[ -n "${SECURE_BASH:-}" ] || refuse "SECURE_BASH not set in $ENVFILE (path of the deployment's 2FA wrapper)"
[ -f "$SECURE_BASH" ] && [ -x "$SECURE_BASH" ] || refuse "SECURE_BASH is not an executable file: $SECURE_BASH"
SCRIPT="$1"; ARGSUMMARY="$*"   # re-pin after sourcing: the env file names the wrapper, never the command

# The wrapper takes ONE shell string and evals it. This is the single place in
# the gate family where a string is composed, and it is safe ONLY because of
# what reached this line: $SCRIPT is the resolved absolute path of an existing
# file under $SELFHEAL_ROOT/remediations/, restricted below to
# [A-Za-z0-9/._-] (no spaces, quotes or metacharacters), and $2 - when present -
# already matched [A-Za-z0-9][A-Za-z0-9._@:-]{0,63}, which admits no quotes,
# spaces, globs or metacharacters either. Nothing a finding or a chat message
# produced is in the string: the engine validated the argument, this gate
# re-validated it, and the path came from the admin's services.json.
case "$SCRIPT" in
    /*) ;;
    *) refuse "resolved script path is not absolute: $SCRIPT" ;;
esac
printf '%s\n' "$SCRIPT" | LC_ALL=C grep -Eq '^/[A-Za-z0-9/._-]+$' \
    || refuse "script path has characters unsafe for a shell string: $SCRIPT"
audit "GATE-FORWARDED $ARGSUMMARY via=$SECURE_BASH"
exec "$SECURE_BASH" "bash $SCRIPT${2:+ $2}"
