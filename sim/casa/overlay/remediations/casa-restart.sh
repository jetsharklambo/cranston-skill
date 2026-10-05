#!/bin/bash
# casa-restart.sh [service] - restart a simulated casa service (fail-safe
# direction: start only). Pattern copy of sandbox/overlay/remediations/
# sandbox-restart.sh, driving casa-ctl instead of svcctl.
#
# CAP_MAX defaults to 99 here (vs the sandbox's 3): the remediation library
# counts its internal rate cap in REAL epoch seconds, and a 90-day soak runs
# in ~a minute of wall time, so every execution of the whole soak lands
# inside one real 6-hour window - the production cap of 3/6h would trip on
# sim compression, not on real misbehavior. The ENGINE's own attempt caps
# (max_attempts per attempt_window_hours) run on the simulated clock and
# stay fully exercised.
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

CTL="${CASACTL:-casa-ctl}"
# the one argument, else the CASA_SVC param, else the service key itself: a
# config the interview wrote carries REMEDIATION_KEY, not CASA_SVC, and in
# casa the service key IS the fake service's name
TARGET="${1:-${CASA_SVC:-${REMEDIATION_KEY:-}}}"
rem_begin "${TARGET:-casa}" "casa-restart"
rem_cap "${CAP_MAX:-99}" "${CAP_WINDOW_H:-6}"
[ -n "$TARGET" ] || rem_refuse "no target service (argument, CASA_SVC or REMEDIATION_KEY param)"
case "$TARGET" in pihole-dns|truenas-smb|plex|plex-primary|plex-alt) ;;
    *) rem_refuse "unknown casa service '$TARGET'" ;; esac

"$CTL" start "$TARGET" || { rem_result fail "start rc=$?"; exit 1; }
rem_verify 3 1 "$CTL" status "$TARGET" >/dev/null \
    || { rem_result fail "not running after start"; exit 1; }
rem_result ok ""
exit 0
