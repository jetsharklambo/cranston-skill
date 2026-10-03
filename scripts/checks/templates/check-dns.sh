#!/bin/bash
# check-dns.sh - DNS resolver probe template with upstream isolation. READ-ONLY.
# Generalizes cranston's check-adguard.sh: a WAN/ISP outage must never be
# blamed on the resolver - DNS_DOWN is emitted only when an upstream still
# answers. If the resolver's host belongs to another service, exit 0 quietly
# when it doesn't ping (that service owns host-down).
#
# Params:
#   CHECK_KEY       required
#   RESOLVER_IP     required
#   TEST_DOMAIN     default example.com
#   UPSTREAM1       default 1.1.1.1
#   UPSTREAM2       default 8.8.8.8
#   OWNER_HOST_IP   optional - when set and not pinging, exit 0 silently
#
# Findings: DNS_DOWN, UPSTREAM_DOWN
#
# Note: dig prints ";; communications error" to STDOUT, so only lines that
# look like a real A record count as an answer.

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key"
require_param RESOLVER_IP "resolver address"
KEY="$CHECK_KEY"
TESTDOM=$(param TEST_DOMAIN example.com)
UP1=$(param UPSTREAM1 1.1.1.1)
UP2=$(param UPSTREAM2 8.8.8.8)

answers() { # @server
    dig +time=2 +tries=2 @"$1" "$TESTDOM" A +short 2>/dev/null \
        | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'
}

# Host down is the owner service's finding, not ours
if [ -n "${OWNER_HOST_IP:-}" ] && ! host_pings "$OWNER_HOST_IP"; then
    exit 0
fi

answers "$RESOLVER_IP" && exit 0

upstream_ok=false
answers "$UP1" && upstream_ok=true
$upstream_ok || { answers "$UP2" && upstream_ok=true; }

if ! $upstream_ok; then
    emit "$KEY" "UPSTREAM_DOWN" "upstream-wan" \
         "resolver ${RESOLVER_IP} AND upstreams (${UP1}/${UP2}) all failed to resolve ${TESTDOM} - likely WAN/ISP outage, the resolver is not at fault"
    exit 1
fi

emit "$KEY" "DNS_DOWN" "resolver" \
     "resolver ${RESOLVER_IP}:53 not resolving ${TESTDOM} while upstream DNS is healthy - resolver down or wedged"
exit 1
