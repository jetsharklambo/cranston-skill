#!/bin/bash
# sandbox-restart.sh [service] - restart a fake sandbox service (fail-safe
# direction: start only). Built from remediations/templates/TEMPLATE.sh.
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

# the one argument, else the FAKE_SVC param, else the service key itself (a
# config the interview wrote carries REMEDIATION_KEY, and in the sandbox the
# service key IS the fake service's name)
TARGET="${1:-${FAKE_SVC:-${REMEDIATION_KEY:-}}}"
rem_begin "${REMEDIATION_KEY:-$TARGET}" "restart-fake-service"
rem_cap "${CAP_MAX:-3}" "${CAP_WINDOW_H:-6}"
case "$TARGET" in media-server|nas) ;; *) rem_refuse "unknown sandbox service '$TARGET'" ;; esac

"$SVCCTL" start "$TARGET"
rc=$?
rem_verify 5 1 "$SVCCTL" status "$TARGET" >/dev/null || { rem_result fail "not running after start"; exit 1; }
rem_result ok "rc=$rc"
exit $rc
