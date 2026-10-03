#!/bin/bash
# check-cert-expiry.sh - certificate / key expiry template. READ-ONLY.
# New in core. The cranston record's F5 class (expiring things nobody watches)
# produced a week of false alarms when a Tailscale node key lapsed and killed
# every fallback path. Anything with an expiry date deserves a countdown.
#
# Params:
#   CHECK_KEY    required
#   CERT_FILE    PEM file to inspect, or
#   CERT_HOST  + CERT_PORT (default 443) - live TLS endpoint
#   WARN_DAYS    default 21 - at/below emits CERT_EXPIRING (degraded)
#   CRIT_DAYS    default 7  - at/below emits CERT_EXPIRING as a full page
#
# Findings: CERT_EXPIRED, CERT_EXPIRING (degraded above CRIT_DAYS),
#           CERT_UNREADABLE

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key"
KEY="$CHECK_KEY"
WARN=$(param WARN_DAYS 21)
CRIT=$(param CRIT_DAYS 7)

if [ -n "${CERT_FILE:-}" ]; then
    enddate=$(openssl x509 -enddate -noout -in "$CERT_FILE" 2>/dev/null)
    WHAT="$CERT_FILE"
else
    require_param CERT_HOST "cert host (or set CERT_FILE)"
    PORT=$(param CERT_PORT 443)
    enddate=$(openssl s_client -connect "${CERT_HOST}:${PORT}" -servername "$CERT_HOST" \
                </dev/null 2>/dev/null | openssl x509 -enddate -noout 2>/dev/null)
    WHAT="${CERT_HOST}:${PORT}"
fi

if [ -z "$enddate" ]; then
    emit "$KEY" "CERT_UNREADABLE" "certificate" \
         "could not read a certificate from ${WHAT:-?} - endpoint down, no TLS, or unreadable file"
    exit 1
fi

# "notAfter=Oct  2 12:00:00 2027 GMT" -> days remaining. Parsed in python3,
# not GNU date -d, so Linux and macOS agree.
days_left=$(python3 - "$enddate" <<'PY'
import sys
from datetime import datetime, timezone
raw = sys.argv[1].split("=", 1)[-1].strip()
dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
print((dt - datetime.now(timezone.utc)).days)
PY
)

case "${days_left:-x}" in
    -*|0) emit "$KEY" "CERT_EXPIRED" "certificate" \
              "certificate for ${WHAT} is EXPIRED (${enddate#notAfter=}) - renew now"
          exit 1 ;;
    *[!0-9]*) emit "$KEY" "CERT_UNREADABLE" "certificate" \
                   "could not parse expiry '${enddate}' for ${WHAT}"
              exit 1 ;;
esac

if [ "$days_left" -le "$CRIT" ]; then
    emit "$KEY" "CERT_EXPIRING" "certificate" \
         "certificate for ${WHAT} expires in ${days_left} day(s) (${enddate#notAfter=}) - renew immediately"
    exit 1
fi

if [ "$days_left" -le "$WARN" ]; then
    emit "$KEY" "CERT_EXPIRING" "certificate" \
         "certificate for ${WHAT} expires in ${days_left} day(s) (${enddate#notAfter=}) - schedule the renewal" \
         "degraded"
    exit 1
fi

exit 0
