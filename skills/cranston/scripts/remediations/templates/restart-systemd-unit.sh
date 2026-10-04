#!/bin/bash
# restart-systemd-unit.sh - FIXED-CONTENT remediation: restart one systemd
# unit. Linux-only. Auto-class safe: a restart moves toward the running state.
#
# Params:
#   REMEDIATION_KEY  required - service key for audit/cap lines
#   UNIT             required - the unit to restart
#   UNIT_SCOPE       user|system (default user)
#   VERIFY_TRIES     default 6, VERIFY_SLEEP default 5 - `systemctl is-active`
#                    poll after a successful restart (same scope as the restart)
#   CAP_MAX          default 3 per CAP_WINDOW_H (default 6) - backstop above
#                    the engine's own cap
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

: "${REMEDIATION_KEY:?set REMEDIATION_KEY in params}"
: "${UNIT:?set UNIT in params}"
SCOPE="${UNIT_SCOPE:-user}"

rem_begin "$REMEDIATION_KEY" "restart-unit-$UNIT"
rem_cap "${CAP_MAX:-3}" "${CAP_WINDOW_H:-6}"

if [ "$SCOPE" = "user" ]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    systemctl --user restart "$UNIT"
else
    systemctl restart "$UNIT"
fi
rc=$?

if [ $rc -eq 0 ]; then
    verify() {
        if [ "$SCOPE" = "user" ]; then
            systemctl --user is-active --quiet "$UNIT"
        else
            systemctl is-active --quiet "$UNIT"
        fi
    }
    if rem_verify "${VERIFY_TRIES:-6}" "${VERIFY_SLEEP:-5}" verify; then
        rem_result ok "unit=$UNIT active"
        exit 0
    fi
    rem_result fail "restart returned 0 but unit not active"
    exit 1
fi
rem_result fail "rc=$rc unit=$UNIT"
exit $rc
