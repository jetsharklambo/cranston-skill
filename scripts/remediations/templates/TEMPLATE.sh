#!/bin/bash
# TEMPLATE.sh - annotated skeleton for a Cranston remediation.
#
# Rules every remediation lives by (each one paid for by an incident):
#   * FIXED CONTENT. The engine passes at most ONE argument (the subkey or a
#     pinned arg). Everything else comes from params in the environment. The
#     adapter's dangerous-command gate anchors its no-2FA bypass on exactly
#     this shape - a second argument must make the gate refuse.
#   * FAIL-SAFE DIRECTION ONLY for auto-class remediations: move the system
#     toward its safe state (turn ON, restart, remount). Anything that cuts
#     power/connectivity belongs in ask-first or on-demand class.
#   * NEVER TRUST THE CALLER: keep an internal rate cap even though the
#     engine has its own (rem_cap).
#   * VERIFY before claiming success; exit 1 when unverified.
#   * If you turn something OFF on the way to fixing it, arm rem_trap_restore
#     FIRST, and prove the restore path exists before cutting (pre-flight).
#
# Exit codes: 0 fixed+verified | 1 failed/unverified | 64 refused | 75 cap.

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

SERVICE_KEY="${REMEDIATION_KEY:?set REMEDIATION_KEY in params}"   # e.g. "my-service"
ARG="${1:-}"                                                      # optional subkey / pinned arg

# 1. audit first - the cap counts this line
rem_begin "$SERVICE_KEY" "describe-the-action"

# 2. internal cap backstop (engine cap is usually tighter)
rem_cap "${CAP_MAX:-3}" "${CAP_WINDOW_H:-6}"

# 3. pre-flight: refuse when acting would be unsafe or unverifiable
# [ -n "$ARG" ] || rem_refuse "usage: $(basename "$0") <target>"
# some_precondition || rem_refuse "restore path unproven - not acting"

# 4. act (argv only - no eval, no string-built commands)
# some_command --target "$ARG"
rc=$?

# 5. verify (poll - respect the data source's refresh granularity)
# rem_verify 6 5 test_command "$ARG" || { rem_result fail "unverified"; exit 1; }

rem_result ok "rc=$rc"
exit $rc
