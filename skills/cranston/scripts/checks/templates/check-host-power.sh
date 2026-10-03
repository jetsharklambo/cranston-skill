#!/bin/bash
# check-host-power.sh - OWN mains + internal battery, from local sysfs.
# READ-ONLY. Linux-only (sysfs). Port of cranston's check with the paths and
# threshold lifted into params.
#
# This answers a different question than any platform-side battery sensor:
# is THIS box still receiving power at all? AC going offline is the earliest
# local signal that the upstream supply died, and the one window where an
# alert can still get out before the dead-man's switch fires. Keep this
# service immediate-page class.
#
# Params:
#   CHECK_KEY     required - key prefix (findings: KEY, KEY/ac, KEY/soc)
#   AC_SYSFS      default /sys/class/power_supply/AC
#   BAT_SYSFS     default /sys/class/power_supply/BAT0
#   BAT_LOW_PCT   default 50
#
# Findings: KEY/ac ON_EXTERNAL_POWER_LOST, KEY/soc INTERNAL_BATTERY_LOW,
#           KEY HOST_POWER_UNREADABLE. All alert-only: software cannot fix a
#           dead outlet.
#
# Deliberately NOT a finding here: pack degradation. It is permanently true
# and slow-moving; realert would nag alongside urgent findings forever, and
# rate-limiting it in-script would starve fail_threshold's consecutive-
# failure semantics. Battery health belongs in the daily digest.
#
# Running battery-less on purpose is a supported state: no battery while on
# AC exits healthy.

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key prefix"
KEY="$CHECK_KEY"
AC_DIR=$(param AC_SYSFS /sys/class/power_supply/AC)
BAT_DIR=$(param BAT_SYSFS /sys/class/power_supply/BAT0)
LOW_PCT=$(param BAT_LOW_PCT 50)

rd() { [ -r "$1" ] && tr -d '\n\r' < "$1" 2>/dev/null; }

online=$(rd "$AC_DIR/online")

if [ -z "$online" ]; then
    emit "$KEY" "HOST_POWER_UNREADABLE" "sysfs" \
         "cannot read ${AC_DIR}/online - unable to tell whether this host is on mains or on battery"
    exit 1
fi

[ "$online" = "1" ] && exit 0

# ---- AC is offline: running on the internal cell ----------------------------
present=$(rd "$BAT_DIR/present")
capacity=$(rd "$BAT_DIR/capacity")
status=$(rd "$BAT_DIR/status")

if [ "$present" != "1" ]; then
    emit "$KEY" "HOST_POWER_UNREADABLE" "sysfs" \
         "AC offline but no battery present at ${BAT_DIR} - power state is inconsistent; investigate immediately"
    exit 1
fi

# Estimated runtime from the LEARNED full charge, not the design capacity -
# on an aged pack those differ severalfold.
runtime=""
charge_now=$(rd "$BAT_DIR/charge_now")
current_now=$(rd "$BAT_DIR/current_now")
case "${charge_now:-x}${current_now:-x}" in
    *[!0-9]*) ;;
    *) if [ "${current_now:-0}" -gt 0 ]; then
           mins=$(( charge_now * 60 / current_now ))
           runtime=", ~${mins} min remaining at the current draw"
       fi ;;
esac

numeric=1
case "${capacity:-}" in ''|*[!0-9]*) numeric=0 ;; esac

emit "$KEY/ac" "ON_EXTERNAL_POWER_LOST" "mains" \
     "external power OFFLINE - running on the internal battery at ${capacity:-unknown}% (${status:-unknown})${runtime}. The upstream supply has stopped delivering; check it now."

if [ "$numeric" -eq 1 ] && [ "$capacity" -le "$LOW_PCT" ]; then
    emit "$KEY/soc" "INTERNAL_BATTERY_LOW" "battery" \
         "internal battery at ${capacity}% (low <=${LOW_PCT}%) with no external power${runtime} - this host shuts down soon and the site goes dark"
fi

exit 1
