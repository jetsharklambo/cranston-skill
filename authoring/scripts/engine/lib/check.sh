# check.sh - shared helpers for Cranston check templates. SOURCE this file.
#
# Check contract (what the engine expects):
#   exit 0              -> healthy, print nothing
#   exit 1 + one JSON object per stdout line -> findings:
#     {"key": K, "status": CODE, "layer": L, "detail": D[, "severity":"degraded"]}
#   any other exit, or unparseable output -> the engine synthesizes CHECK_ERROR
#
# Conventions:
#   - All probe targets come from the environment (the engine exports the
#     service's "params" block). Never hardcode an address in a template.
#   - severity "degraded" means: the service is provably fine by another
#     route, only our view of it is broken. Degraded findings default to the
#     daily digest instead of paging.
#   - Layer isolation: exit 0 SILENTLY when the root cause belongs to another
#     service (one root cause, one alert).
#
# Portability: bash 3.2+ (macOS) and POSIX-ish Linux. JSON escaping is done by
# python3, which both platforms ship.

# emit <key> <status> <layer> <detail> [severity]
emit() {
    python3 - "$@" <<'PY'
import json, sys
k, s, l, d = sys.argv[1:5]
f = {"key": k, "status": s, "layer": l, "detail": d}
if len(sys.argv) > 5 and sys.argv[5]:
    f["severity"] = sys.argv[5]
print(json.dumps(f))
PY
}

# param <VAR> <default> - echo $VAR if set and non-empty, else the default
param() {
    # bash 3.2-safe indirect expansion
    local v
    eval "v=\${$1-}"
    if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi
}

# require_param <VAR> <what it names> - exit 64 (usage) when unset
require_param() {
    local v
    eval "v=\${$1-}"
    if [ -z "$v" ]; then
        echo "check misconfigured: required param $1 not set ($2)" >&2
        exit 64
    fi
}

# host_pings <ip> - 0 when the host answers one ping
host_pings() {
    ping -c 1 -W 2 "$1" >/dev/null 2>&1
}

# http_code <url> [auth_header] - echo the HTTP status code, 000 on no connect
http_code() {
    if [ -n "${2:-}" ]; then
        curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "$2" "$1" 2>/dev/null
    else
        curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null
    fi
}
