#!/bin/bash
# fake-remediation.sh - test fixture exercising the remediation library:
# refuse (64), cap (75), act + verify paths. Touches TARGET_FILE as its "fix".
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

TARGET="${1:-}"

rem_begin "fixture-svc" "fake-fix"
[ -n "$TARGET" ] || rem_refuse "usage: fake-remediation.sh <target-file>"
rem_cap "${CAP_MAX:-2}" "${CAP_WINDOW_H:-6}"

touch "$TARGET"

if [ "${FORCE_VERIFY_FAIL:-0}" = "1" ]; then
    rem_verify 2 0 false || { rem_result fail "verify budget exhausted"; exit 1; }
fi

rem_verify 3 0 test -f "$TARGET" || { rem_result fail "target never appeared"; exit 1; }
rem_result ok "target=$TARGET"
exit 0
