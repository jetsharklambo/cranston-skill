#!/bin/bash
# check-lan-inventory.sh - LAN IP/MAC fingerprint watch template. READ-ONLY.
# Linux-only (`ip` tool). Port of cranston's check with the inventory path
# and self key lifted into params; the inventory file was already data-driven.
#
# Verifies each important host answers at its expected IP AND is the same
# physical device (MAC via the kernel neighbor table), so "DHCP moved the IP"
# (DEVICE_CHANGED) alerts distinctly from "host down". Hosts whose
# availability is owned by another service (covered_by_service:true) only
# alert here on fingerprint mismatch. All findings alert-only: an IP change
# needs a human (set the DHCP reservation you keep postponing).
#
# Params:
#   CHECK_KEY        required - key prefix
#   INVENTORY_FILE   required - JSON: {"self_ip": "...", "hosts":
#                    [{"name","ip","mac","covered_by_service"?}, ...]}
#
# Findings: KEY/self SELF_IP_CHANGED, KEY/<name> DEVICE_CHANGED |
#           HOST_UNREACHABLE, KEY/self CHECK_ERROR (inventory missing)

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key prefix"
require_param INVENTORY_FILE "inventory JSON path"
KEY="$CHECK_KEY"

if [ ! -f "$INVENTORY_FILE" ]; then
    emit "$KEY/self" "CHECK_ERROR" "inventory" "inventory file missing: ${INVENTORY_FILE}"
    exit 1
fi

fail=0

# Own address first: everything else assumes this box sits where configs say
# The inventory path travels as sys.argv, never interpolated into Python
# source - a quote in the path must not become code.
self_ip=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("self_ip",""))' "$INVENTORY_FILE" 2>/dev/null)
if [ -n "$self_ip" ] && ! ip -4 addr show 2>/dev/null | grep -qF "inet ${self_ip}/"; then
    have=$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{print $2}' | paste -sd, -)
    emit "$KEY/self" "SELF_IP_CHANGED" "self" \
         "this host no longer holds ${self_ip} (has: ${have:-none}) - DHCP reassigned it; everything that targets the old address will break"
    fail=1
fi

while IFS='|' read -r name ipa mac covered; do
    [ -n "$name" ] || continue
    if ! ping -c 2 -W 2 "$ipa" >/dev/null 2>&1; then
        if [ "$covered" = "false" ]; then
            emit "$KEY/$name" "HOST_UNREACHABLE" "reachability" \
                 "${name} (${ipa}) not responding to ping and no other service watches it"
            fail=1
        fi
        continue
    fi
    seen=$(ip neigh show "$ipa" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="lladdr"){print $(i+1); exit}}' | tr 'A-F' 'a-f')
    [ -n "$seen" ] || continue   # reachable but no neighbor entry - cannot judge
    want=$(printf '%s' "$mac" | tr 'A-F' 'a-f')
    if [ "$seen" != "$want" ]; then
        emit "$KEY/$name" "DEVICE_CHANGED" "fingerprint" \
             "a DIFFERENT device answers at ${ipa}: MAC ${seen}, expected ${want} (${name}) - DHCP likely reassigned the IP; find ${name}'s new address and fix the reservation"
        fail=1
    fi
done < <(python3 -c '
import json, sys
for h in json.load(open(sys.argv[1]))["hosts"]:
    print("|".join([h["name"], h["ip"], h["mac"], "true" if h.get("covered_by_service") else "false"]))
' "$INVENTORY_FILE" 2>/dev/null)

exit $fail
