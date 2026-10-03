#!/bin/bash
# check-disk-space.sh - local volume presence / writability / free-space
# template. READ-ONLY. New in core; the design comes from cranston's NIKE
# lessons: check FUNCTION, not liveness - a mount can be present but the
# WRONG volume (markers prove identity), present but remounted read-only
# (dirty ExFAT), or present with no room. Inode counts are deliberately never
# read: ExFAT reports garbage (1 used / 0 free / 100%).
#
# Params:
#   CHECK_KEY        required
#   MOUNT_PATH       required - the directory that must be a live mount/dir
#   MIN_FREE_PCT     default 10 - below this emits LOW_SPACE (degraded:
#                    nothing is broken yet)
#   MARKER_DIRS      optional space-separated subpaths that prove the right
#                    volume is mounted (e.g. "BAK models")
#   REQUIRE_WRITABLE "1" to emit VOLUME_READONLY when not writable (checked
#                    non-invasively with test -w; never writes a probe file,
#                    so a 2-minute poll can't keep a disk awake)
#
# Findings: VOLUME_ABSENT, MARKER_MISSING, VOLUME_READONLY, LOW_SPACE (degraded)

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key"
require_param MOUNT_PATH "volume path"
KEY="$CHECK_KEY"
MIN_FREE=$(param MIN_FREE_PCT 10)

if [ ! -d "$MOUNT_PATH" ]; then
    emit "$KEY" "VOLUME_ABSENT" "volume" \
         "${MOUNT_PATH} is not present - volume unplugged, unmounted, or hung"
    exit 1
fi

fail=0

for m in ${MARKER_DIRS:-}; do
    if [ ! -e "$MOUNT_PATH/$m" ]; then
        emit "$KEY" "MARKER_MISSING" "volume" \
             "${MOUNT_PATH} exists but marker '${m}' is missing - the WRONG volume may be mounted at this path, or the mount is stale"
        fail=1
    fi
done

if [ "$(param REQUIRE_WRITABLE 0)" = "1" ] && [ ! -w "$MOUNT_PATH" ]; then
    emit "$KEY" "VOLUME_READONLY" "volume" \
         "${MOUNT_PATH} is not writable - the OS likely remounted a dirty volume read-only; unmount, fsck, remount"
    fail=1
fi

# df -P is POSIX; column 5 is Capacity% on both Linux and macOS
used_pct=$(df -P "$MOUNT_PATH" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
case "${used_pct:-x}" in
    *[!0-9]*) : ;;  # unreadable - the absent/marker findings cover real trouble
    *)
        free_pct=$((100 - used_pct))
        if [ "$free_pct" -lt "$MIN_FREE" ]; then
            emit "$KEY" "LOW_SPACE" "capacity" \
                 "${MOUNT_PATH} has ${free_pct}% free (threshold ${MIN_FREE}%) - nothing is broken yet, but backups/writes will start failing" \
                 "degraded"
            fail=1
        fi ;;
esac

exit $fail
