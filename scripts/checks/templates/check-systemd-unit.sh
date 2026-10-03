#!/bin/bash
# check-systemd-unit.sh - systemd unit probe template. READ-ONLY. Linux-only.
# Generalizes cranston's check-gateway.sh, including the wedge lesson: a
# process can log its healthy traffic to a FILE sink while the journal gets
# only console errors - journal silence alone is NORMAL (that assumption
# caused two needless restarts). Wedged = file log stale AND journal silent.
#
# Params:
#   CHECK_KEY       required
#   UNIT            required - unit name
#   UNIT_SCOPE      user|system (default user)
#   PORT            optional - local TCP port that must answer
#   LOG_GLOB        optional - file-sink glob; newest file BY MTIME is used
#                   (a process keeps its boot-day file open past midnight -
#                   never select by date)
#   WEDGE_MINUTES   default 75 - both sinks quiet this long = wedged
#
# Findings: UNIT_INACTIVE, PORT_DEAD, WEDGED

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key"
require_param UNIT "systemd unit name"
KEY="$CHECK_KEY"
SCOPE=$(param UNIT_SCOPE user)
WEDGE=$(param WEDGE_MINUTES 75)

SCTL=(systemctl)
JCTL=(journalctl)
if [ "$SCOPE" = "user" ]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    SCTL=(systemctl --user)
    JCTL=(journalctl --user)
fi

active=$("${SCTL[@]}" is-active "$UNIT" 2>/dev/null)
if [ "$active" != "active" ]; then
    emit "$KEY" "UNIT_INACTIVE" "systemd-unit" "unit ${UNIT} state=${active:-unknown}"
    exit 1
fi

if [ -n "${PORT:-}" ]; then
    curl -s -o /dev/null --max-time 5 "http://127.0.0.1:${PORT}/" 2>/dev/null
    rc=$?
    if [ $rc -eq 7 ] || [ $rc -eq 28 ]; then
        emit "$KEY" "PORT_DEAD" "port" \
             "unit ${UNIT} active but port ${PORT} not answering (curl exit ${rc})"
        exit 1
    fi
fi

# Wedge heuristic only when a file sink is declared
[ -n "${LOG_GLOB:-}" ] || exit 0

newest_log=$(ls -t $LOG_GLOB 2>/dev/null | head -1)
if [ -n "$newest_log" ] && [ -n "$(find "$newest_log" -mmin "-${WEDGE}" 2>/dev/null)" ]; then
    exit 0
fi

if "${JCTL[@]}" -u "$UNIT" --since "-${WEDGE} min" -q -n 1 --no-pager 2>/dev/null | grep -q .; then
    exit 0
fi

emit "$KEY" "WEDGED" "internal" \
     "unit ${UNIT} active${PORT:+, port ${PORT} alive,} but no log activity in the file sink (${LOG_GLOB}) or the journal for ${WEDGE}+ min - likely wedged (e.g. a stale long-poll socket)"
exit 1
