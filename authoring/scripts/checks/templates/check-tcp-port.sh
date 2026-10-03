#!/bin/bash
# check-tcp-port.sh - plain TCP reachability template. READ-ONLY.
# New in core (no cranston precedent): a service that speaks neither HTTP nor
# DNS still deserves a ping-split probe. Uses python3's socket module, not
# nc, so it behaves identically on Linux and macOS.
#
# Params:
#   CHECK_KEY     required
#   TCP_HOST      required
#   TCP_PORT      required
#   TCP_TIMEOUT   seconds, default 3
#   PING_SPLIT    "1" (default) to distinguish PORT_CLOSED from HOST_DOWN
#
# Findings: PORT_CLOSED, HOST_DOWN

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key"
require_param TCP_HOST "target host"
require_param TCP_PORT "target port"
KEY="$CHECK_KEY"
TIMEOUT=$(param TCP_TIMEOUT 3)
SPLIT=$(param PING_SPLIT 1)

if python3 - "$TCP_HOST" "$TCP_PORT" "$TIMEOUT" <<'PY'
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    with socket.create_connection((host, port), timeout=timeout):
        pass
except OSError:
    raise SystemExit(1)
PY
then
    exit 0
fi

if [ "$SPLIT" = "1" ] && host_pings "$TCP_HOST"; then
    emit "$KEY" "PORT_CLOSED" "service" \
         "host ${TCP_HOST} pings but nothing is listening on tcp/${TCP_PORT} - service down or firewalled"
else
    emit "$KEY" "HOST_DOWN" "host" \
         "${TCP_HOST} unreachable on tcp/${TCP_PORT}${SPLIT:+ and not answering ping} - host likely down"
fi
exit 1
