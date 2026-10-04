#!/bin/bash
# gates/gate-telegram-confirm.sh - Tier 1 approval gate: chat confirmation.
#
# Proves possession of the admin's phone WITHOUT trusting the agent: this gate
# talks to the Telegram Bot API ITSELF - it sends a one-time nonce to the
# pinned chat and polls for a reply containing it. An agent relaying chat
# cannot fake the approval, because the gate reads the Bot API directly.
#
# Setup (once): create ~/.cranston-gate-telegram.env, chmod 600, containing:
#   GATE_TG_BOT_TOKEN="123456:ABC..."   # a bot in the admin's chat
#   GATE_TG_CHAT_ID="123456789"         # the ADMIN's numeric chat id (pinned)
# Then set "approval_gate": "gates/gate-telegram-confirm.sh" in services.json.
#
# Contract: references/approval-gates.md (argv pass-through, exit 65 refusal).
# Env knobs: GATE_TELEGRAM_ENV (creds file), GATE_TIMEOUT (seconds, default
# 120), GATE_TG_API (test override for the Bot API base URL).
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

ENVFILE="${GATE_TELEGRAM_ENV:-$HOME/.cranston-gate-telegram.env}"
[ -f "$ENVFILE" ] || refuse "telegram gate env file missing: $ENVFILE"
# mode must be private: the token authorizes sending as the bot
PERMS=$(stat -c %a "$ENVFILE" 2>/dev/null || stat -f %Lp "$ENVFILE" 2>/dev/null)
case "$PERMS" in *00) ;; *) refuse "telegram gate env file $ENVFILE must be mode 600 (is $PERMS)" ;; esac
# shellcheck disable=SC1090
. "$ENVFILE"
[ -n "${GATE_TG_BOT_TOKEN:-}" ] && [ -n "${GATE_TG_CHAT_ID:-}" ] \
    || refuse "GATE_TG_BOT_TOKEN / GATE_TG_CHAT_ID not set in $ENVFILE"

API="${GATE_TG_API:-https://api.telegram.org}/bot${GATE_TG_BOT_TOKEN}"
TIMEOUT="${GATE_TIMEOUT:-120}"
NONCE=$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')

# Only consider replies NEWER than now: fetch the current max update_id first.
OFFSET=$(curl -s -m 10 "$API/getUpdates" | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin).get("result", [])
    print(max((u.get("update_id", 0) for u in r), default=0) + 1)
except Exception:
    print(0)')

SENT=$(curl -s -m 10 -X POST "$API/sendMessage" \
    --data-urlencode "chat_id=${GATE_TG_CHAT_ID}" \
    --data-urlencode "text=🔐 Approve remediation: ${ARGSUMMARY}?
Reply within ${TIMEOUT}s with:  approve ${NONCE}
Reply 'deny ${NONCE}' (or ignore) to refuse.")
echo "$SENT" | grep -q '"ok": *true' || refuse "could not send the confirmation prompt (Bot API unreachable?)"
audit "GATE-PROMPTED nonce=$NONCE argv=$ARGSUMMARY"

DEADLINE=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    VERDICT=$(curl -s -m 10 "$API/getUpdates?offset=${OFFSET}&timeout=5" | \
        NONCE="$NONCE" CHAT="$GATE_TG_CHAT_ID" python3 -c '
import json, os, sys
nonce, chat = os.environ["NONCE"], os.environ["CHAT"]
out, offset = "", None
try:
    for u in json.load(sys.stdin).get("result", []):
        offset = u.get("update_id", 0) + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != chat:
            continue          # pinned chat only - anyone else is ignored
        text = (m.get("text") or "").strip().lower()
        if text == f"approve {nonce}":
            out = "APPROVE"
        elif text == f"deny {nonce}":
            out = "DENY"
except Exception:
    pass
print(out + "|" + str(offset or 0))')
    ANSWER="${VERDICT%%|*}"
    NEWOFF="${VERDICT##*|}"
    [ -n "$NEWOFF" ] && [ "$NEWOFF" != "0" ] && OFFSET="$NEWOFF"
    if [ "$ANSWER" = "APPROVE" ]; then
        audit "GATE-APPROVED nonce=$NONCE argv=$ARGSUMMARY"
        exec bash "$@"
    fi
    [ "$ANSWER" = "DENY" ] && refuse "denied by the admin (nonce $NONCE)"
    sleep 2
done
refuse "no approval within ${TIMEOUT}s (nonce $NONCE) - failing closed"
