#!/bin/bash
# restart-docker-container.sh - FIXED-CONTENT remediation: restart one LOCAL
# docker container, taken from the single allowed argument and validated
# against an allowlist param. Auto-class safe.
#
# Params:
#   REMEDIATION_KEY     required - service key prefix for audit/cap lines
#   ALLOWED_CONTAINERS  required - space-separated allowlist; anything else
#                       is refused (the engine derives the arg from the
#                       finding subkey, but never trust the caller)
#   CAP_MAX             default 3 per CAP_WINDOW_H (default 6)
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

: "${REMEDIATION_KEY:?set REMEDIATION_KEY in params}"
: "${ALLOWED_CONTAINERS:?set ALLOWED_CONTAINERS in params}"
CONTAINER="${1:-}"

[ -n "$CONTAINER" ] || { rem_begin "$REMEDIATION_KEY" "restart-container"; rem_refuse "usage: $(basename "$0") <container>"; }

rem_begin "$REMEDIATION_KEY/$CONTAINER" "restart-container"

ok=0
for c in $ALLOWED_CONTAINERS; do
    [ "$c" = "$CONTAINER" ] && ok=1
done
[ "$ok" = 1 ] || rem_refuse "container '$CONTAINER' not in allowlist ($ALLOWED_CONTAINERS)"

rem_cap "${CAP_MAX:-3}" "${CAP_WINDOW_H:-6}"

docker restart -t 20 "$CONTAINER" >/dev/null
rc=$?

if [ $rc -eq 0 ]; then
    verify() { docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; }
    if rem_verify 6 5 verify; then
        rem_result ok "container=$CONTAINER"
        exit 0
    fi
    rem_result fail "restart returned 0 but container not running"
    exit 1
fi
rem_result fail "rc=$rc container=$CONTAINER"
exit $rc
