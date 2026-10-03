#!/bin/bash
# check-http.sh - HTTP service probe template. READ-ONLY.
# Archetype: authenticated or plain HTTP endpoint, optional second transport,
# ping split, and "our view is broken" degraded findings. Generalizes
# cranston's check-ha.sh + check-navidrome.sh (the 59-false-alarms lesson:
# never blame a host you can only see over one blocked path).
#
# Params (via the service's params block):
#   CHECK_KEY        required - the finding key / service name
#   HTTP_URL         required - primary (LAN) URL; prefer IP literals so the
#                    check keeps working during a DNS outage
#   HTTP_EXPECT      expected status code (default 200)
#   HTTP_AUTH_HEADER optional - e.g. "Authorization: Bearer ..." or set
#   HTTP_TOKEN_FILE  optional - file sourced first (exports the header's token;
#                    pair with HTTP_AUTH_HEADER referencing $..._TOKEN yourself)
#   HOST_IP          optional - enables the ping split (service vs host down)
#   HTTP_URL_ALT     optional - second transport (e.g. tailnet IP URL)
#   ALT_GUARD_CMD    optional - command run before trusting the alt transport;
#                    non-zero exit means the alt path measures OUR outage, not
#                    the service's (e.g. "tailscale-running.sh"). No guard =
#                    alt is always trusted.
#
# Findings:
#   AUTH_FAILED                 auth header set, got 401/403
#   API_ERROR                   unexpected status code
#   SERVICE_DOWN                host reachable (ping or alt view), service dead
#   HOST_DOWN                   both transports dead and no ping
#   LAN_UNREACHABLE  (degraded) alt transport healthy, primary dead - the
#                               service is fine, the primary path is not
#   TRANSPORT_BLIND  (degraded) primary dead, alt unusable, no ping - cannot
#                               tell host-down from a blocked path

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key"
require_param HTTP_URL "primary URL"
KEY="$CHECK_KEY"
EXPECT=$(param HTTP_EXPECT 200)
AUTH="${HTTP_AUTH_HEADER:-}"
[ -n "${HTTP_TOKEN_FILE:-}" ] && [ -f "$HTTP_TOKEN_FILE" ] && . "$HTTP_TOKEN_FILE"
AUTH="${HTTP_AUTH_HEADER:-$AUTH}"

code=$(http_code "$HTTP_URL" "$AUTH")

if [ "$code" = "$EXPECT" ]; then
    exit 0
fi

if [ -n "$AUTH" ] && { [ "$code" = "401" ] || [ "$code" = "403" ]; }; then
    emit "$KEY" "AUTH_FAILED" "credentials" \
         "HTTP ${code} from ${HTTP_URL} - token likely expired or revoked; refresh the credential"
    exit 1
fi

if [ "$code" != "000" ] && [ -n "$code" ]; then
    emit "$KEY" "API_ERROR" "service" \
         "unexpected HTTP ${code} from ${HTTP_URL} (expected ${EXPECT})"
    exit 1
fi

# ---- primary transport dead (no connection) ---------------------------------

alt_usable=0
if [ -n "${HTTP_URL_ALT:-}" ]; then
    if [ -z "${ALT_GUARD_CMD:-}" ] || $ALT_GUARD_CMD >/dev/null 2>&1; then
        alt_usable=1
    fi
fi

if [ "$alt_usable" = 1 ]; then
    alt_code=$(http_code "$HTTP_URL_ALT" "$AUTH")
    if [ "$alt_code" = "$EXPECT" ]; then
        emit "$KEY" "LAN_UNREACHABLE" "primary-path" \
             "service is HEALTHY via ${HTTP_URL_ALT} but unreachable at ${HTTP_URL} (HTTP ${code:-none}) - the service is fine; the primary path is broken (routing, isolation, or addressing)" \
             "degraded"
        exit 1
    fi
    # both transports genuinely probed and dead - the ping split says something
    if [ -n "${HOST_IP:-}" ] && host_pings "$HOST_IP"; then
        emit "$KEY" "SERVICE_DOWN" "service" \
             "host ${HOST_IP} pings but the service answered HTTP ${code:-none} at ${HTTP_URL} and HTTP ${alt_code:-none} at ${HTTP_URL_ALT} - service down or wedged"
    else
        emit "$KEY" "HOST_DOWN" "host" \
             "no response at ${HTTP_URL}, at ${HTTP_URL_ALT} (HTTP ${alt_code:-none})${HOST_IP:+, and ${HOST_IP} does not ping} - host likely down or unplugged"
    fi
    exit 1
fi

# No usable alt transport. Primary HTTP + ping still carry real signal:
if [ -n "${HOST_IP:-}" ] && host_pings "$HOST_IP"; then
    emit "$KEY" "SERVICE_DOWN" "service" \
         "host ${HOST_IP} pings but the service answered HTTP ${code:-none} at ${HTTP_URL}${HTTP_URL_ALT:+ (alt transport unverifiable)} - service down or wedged"
    exit 1
fi

if [ -n "${HTTP_URL_ALT:-}" ]; then
    # An alt transport is configured but unusable, and we have no ping
    # evidence: this is what a HEALTHY service can look like from a broken
    # vantage point. Blame the blindness, not the host.
    emit "$KEY" "TRANSPORT_BLIND" "own-transport" \
         "cannot verify the service: primary ${HTTP_URL} dead and the alternate transport is unusable (guard '${ALT_GUARD_CMD:-}' failed)${HOST_IP:+; ${HOST_IP} does not ping} - fix is likely on THIS box (overlay down, key expired), not the service" \
         "degraded"
    exit 1
fi

emit "$KEY" "HOST_DOWN" "host" \
     "no response at ${HTTP_URL}${HOST_IP:+ and ${HOST_IP} does not ping} - host likely down"
exit 1
