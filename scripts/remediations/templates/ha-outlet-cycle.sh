#!/bin/bash
# ha-outlet-cycle.sh - FIXED-CONTENT remediation: power-cycle ONE Home
# Assistant switch (OFF, wait, ON) to bring a dark host back. ASK-FIRST ONLY,
# by nature: cutting power is never the fail-safe direction, so this template
# refuses to run on the engine's automatic path (SELFHEAL_AUTOMATION=true)
# even when a services.json mistakenly wires it as an auto-class string. The
# human approval path (approve-heal.py) never carries that marker, so an
# approved 'heal' runs it; nothing unattended ever does.
#
# Safety order (TEMPLATE.sh's rules, each paid for by an incident):
#   audit -> cap -> pre-flight: the API answers, the entity exists and reads
#   ON right now (cycling an outlet that is already off, or one we cannot
#   read, is not a restore path we have proven) -> arm the restore trap
#   (turn_on) BEFORE cutting -> turn_off -> wait -> turn_on -> verify ON.
#   The trap re-asserts ON however the script ends short of a verified
#   success: an unverified cycle, a refused turn_on, INT/TERM mid-cycle.
#
# Uses the same HA endpoints as ha-service-call.sh: GET /api/states/<entity>,
# POST /api/services/switch/turn_off and /turn_on.
#
# Params:
#   REMEDIATION_KEY     required
#   HA_URL              required
#   HA_TOKEN            or HA_TOKEN_FILE (sourced; must export HA_TOKEN)
#   OUTLET_ENTITY       required - the outlet; must be a switch.* entity
#   CYCLE_OFF_SECONDS   default 10, max 60 - how long the outlet stays off
#   VERIFY_TRIES        default 6, VERIFY_SLEEP default 5
#   CAP_MAX             default 2 per CAP_WINDOW_H (default 6)
#
# Argument: none needed; a single engine-passed subkey is accepted and ignored.
#
# Exit codes: 0 cycled+verified ON | 1 failed/unverified (ON re-asserted by
# the trap) | 64 refused | 75 cap.
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

: "${REMEDIATION_KEY:?set REMEDIATION_KEY in params}"
ENTITY="${OUTLET_ENTITY:-}"

rem_begin "$REMEDIATION_KEY" "ha-outlet-cycle-${ENTITY:-?}"

# Structural: never on the auto path, whatever the config says.
[ "${SELFHEAL_AUTOMATION:-}" != "true" ] \
    || rem_refuse "power-cycling an outlet is never automatic - this template is ask-first only; wire it as {\"ask\": ...}, not as an auto-class string"

rem_cap "${CAP_MAX:-2}" "${CAP_WINDOW_H:-6}"

# ---- pre-flight ----------------------------------------------------------------
[ $# -le 1 ] || rem_refuse "takes at most one argument, got $#"
[ -n "${HA_URL:-}" ] || rem_refuse "HA_URL not set in params"
[ -n "${HA_TOKEN_FILE:-}" ] && [ -f "$HA_TOKEN_FILE" ] && . "$HA_TOKEN_FILE"
[ -n "${HA_TOKEN:-}" ] || rem_refuse "HA_TOKEN or HA_TOKEN_FILE not set in params"
case "$ENTITY" in
    switch.?*) : ;;
    *) rem_refuse "OUTLET_ENTITY '${ENTITY:-unset}' is not a switch.* entity - this template cycles outlets only" ;;
esac
OFF_S="${CYCLE_OFF_SECONDS:-10}"
case "$OFF_S" in ''|*[!0-9]*) rem_refuse "CYCLE_OFF_SECONDS must be a whole number of seconds, 0-60 (is '$OFF_S')" ;; esac
[ "$OFF_S" -le 60 ] || rem_refuse "CYCLE_OFF_SECONDS $OFF_S exceeds the 60s maximum"

# state_of -> prints the entity's state; empty when the API did not answer 200
state_of() {
    local raw code body
    raw=$(curl -s --max-time 8 -w '\n%{http_code}' -H "Authorization: Bearer $HA_TOKEN" \
          "$HA_URL/api/states/$ENTITY" 2>/dev/null) || return 1
    code=$(printf '%s\n' "$raw" | tail -n 1)
    body=$(printf '%s\n' "$raw" | sed '$d')
    [ "$code" = "200" ] || return 1
    printf '%s' "$body" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("state", ""))' 2>/dev/null
}

pre=$(state_of) || rem_refuse "cannot read $ENTITY from $HA_URL - restore path unproven, not cutting power"
[ -n "$pre" ] || rem_refuse "$ENTITY answered without a state - restore path unproven, not cutting power"
[ "$pre" = "on" ] || rem_refuse "$ENTITY reads '$pre', not 'on' - cycling an outlet that is not on is not a restore path we have proven"

# ha_call <turn_off|turn_on> -> 0 when HA accepted the call
ha_call() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        -X POST "$HA_URL/api/services/switch/$1" \
        -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
        -d "{\"entity_id\": \"$ENTITY\"}" 2>/dev/null)
    [ "$code" = "200" ] || [ "$code" = "201" ]
}
restore_on() {
    rem_audit "RESTORE service=$REM_SERVICE action=turn_on entity=$ENTITY reason=trap"
    ha_call turn_on
}

# ---- act: arm the restore FIRST, then cut --------------------------------------
rem_trap_restore restore_on
# INT/TERM end the wait early; the EXIT half of the trap then re-asserts ON once.
trap 'rem_result fail "interrupted mid-cycle - the exit trap re-asserts ON"; exit 143' INT TERM

if ! ha_call turn_off; then
    rem_result fail "turn_off not accepted by HA (the exit trap re-asserts ON)"
    echo "turn_off was not accepted - outlet left as it was"
    exit 1
fi
i=0
while [ "$i" -lt "$OFF_S" ]; do sleep 1; i=$((i + 1)); done
if ! ha_call turn_on; then
    rem_result fail "turn_on not accepted by HA after the off period - the exit trap retries it"
    echo "turn_on was not accepted - the restore trap retries once on exit; CHECK THE OUTLET"
    exit 1
fi

# ---- verify ----------------------------------------------------------------------
verify_on() { [ "$(state_of)" = "on" ]; }

if rem_verify "${VERIFY_TRIES:-6}" "${VERIFY_SLEEP:-5}" verify_on; then
    rem_trap_clear
    rem_result ok "entity=$ENTITY cycled (off ${OFF_S}s) and reads on"
    exit 0
fi
rem_result fail "turn_on accepted but $ENTITY did not read on within the poll window (the exit trap re-asserts ON)"
echo "cycle unverified - $ENTITY did not read on in time; ON re-asserted on exit, the next monitor cycle is the authority"
exit 1
