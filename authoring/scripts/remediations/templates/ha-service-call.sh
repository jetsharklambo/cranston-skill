#!/bin/bash
# ha-service-call.sh - FIXED-CONTENT remediation: one Home Assistant service
# call, restricted to the FAIL-SAFE DIRECTION. Only turn_on-family services
# are permitted - this template structurally cannot switch anything off.
# (Cranston's charging doctrine: when in doubt, the automatic action may only
# move toward the safe state; the OFF side belongs to humans and the
# platform's own automations.)
#
# Params:
#   REMEDIATION_KEY  required
#   HA_URL           required
#   HA_TOKEN         or HA_TOKEN_FILE (sourced; must export HA_TOKEN)
#   HA_DOMAIN        default switch
#   HA_SERVICE       default turn_on  (allowlist: turn_on only)
#   HA_ENTITY        required - the entity to act on
#   VERIFY_STATE     default on - expected state after the call
#   VERIFY_TRIES     default 6, VERIFY_SLEEP default 5 (respect the data
#                    source's refresh cadence - a cloud entity may need a
#                    long verify_delay at the ENGINE level instead)
#   CAP_MAX          default 4 per CAP_WINDOW_H (default 6)
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

: "${REMEDIATION_KEY:?set REMEDIATION_KEY in params}"
: "${HA_URL:?set HA_URL in params}"
[ -n "${HA_TOKEN_FILE:-}" ] && [ -f "$HA_TOKEN_FILE" ] && . "$HA_TOKEN_FILE"
: "${HA_TOKEN:?set HA_TOKEN or HA_TOKEN_FILE in params}"
: "${HA_ENTITY:?set HA_ENTITY in params}"
DOMAIN="${HA_DOMAIN:-switch}"
SVC="${HA_SERVICE:-turn_on}"

rem_begin "$REMEDIATION_KEY" "ha-${DOMAIN}.${SVC}-${HA_ENTITY}"

case "$SVC" in
    turn_on) : ;;
    *) rem_refuse "service '$SVC' is not fail-safe-direction; this template only turns things ON" ;;
esac

rem_cap "${CAP_MAX:-4}" "${CAP_WINDOW_H:-6}"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
    -X POST "$HA_URL/api/services/$DOMAIN/$SVC" \
    -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
    -d "{\"entity_id\": \"$HA_ENTITY\"}" 2>/dev/null)

if [ "$code" != "200" ] && [ "$code" != "201" ]; then
    rem_result fail "service call HTTP ${code:-none}"
    echo "service call failed: HTTP ${code:-none}"
    exit 1
fi

verify() {
    curl -s --max-time 8 -H "Authorization: Bearer $HA_TOKEN" \
         "$HA_URL/api/states/$HA_ENTITY" 2>/dev/null \
        | grep -q "\"state\": *\"${VERIFY_STATE:-on}\""
}

if rem_verify "${VERIFY_TRIES:-6}" "${VERIFY_SLEEP:-5}" verify; then
    rem_result ok "entity=$HA_ENTITY now ${VERIFY_STATE:-on}"
    exit 0
fi

# The call was accepted but the state did not confirm. With a cloud-fed
# entity this can be staleness, not failure - the engine's verify re-check
# (after verify_delay_seconds) is the authority. Report honestly.
rem_result fail "call accepted (HTTP $code) but state did not read ${VERIFY_STATE:-on} within the poll window"
echo "call accepted but unverified - cloud state may be stale; the next monitor cycle is the authority"
exit 1
