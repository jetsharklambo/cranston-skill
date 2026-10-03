#!/bin/bash
# test_templates.sh - offline smoke tests for the check templates and the
# remediation library. No network beyond 127.0.0.1; runs on macOS and Linux.
# (check-dns needs a live resolver and check-lan-inventory needs Linux `ip`;
# both get bash -n only, plus dns against 127.0.0.1 upstreams for the
# UPSTREAM_DOWN path.)

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRCROOT="$REPO/authoring/scripts"
export SELFHEAL_ROOT="$SRCROOT"
T="$(mktemp -d)"
export SELFHEAL_AUDIT_LOG="$T/audit.log"
CK="$SRCROOT/checks/templates"
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  PASS $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL $1"; }
# assert <name> <want_rc> <want_substr|-> -- cmd...
assert() {
    local name="$1" want_rc="$2" want="$3"; shift 3; shift  # drop --
    local out rc
    out=$(env "$@" 2>&1); rc=$?
    if [ "$rc" != "$want_rc" ]; then bad "$name (rc=$rc want=$want_rc; out=$out)"; return; fi
    if [ "$want" != "-" ] && ! printf '%s' "$out" | grep -q "$want"; then
        bad "$name (missing '$want' in: $out)"; return
    fi
    ok "$name"
}

echo "== syntax: every shell file parses =="
synfail=0
while IFS= read -r f; do
    bash -n "$f" || { bad "bash -n $f"; synfail=1; }
done < <(find "$REPO" -name "*.sh" -not -path "*/.git/*")
[ "$synfail" = 0 ] && ok "all shell files parse"

echo "== check-http =="
SRV="$T/www"; mkdir -p "$SRV"; echo hi > "$SRV/index.html"
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
( cd "$SRV" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
HTTP_PID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.3; done
DEAD=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')

assert "healthy endpoint -> exit 0" 0 - -- \
    CHECK_KEY=t HTTP_URL="http://127.0.0.1:$PORT/" bash "$CK/check-http.sh"
assert "404 -> API_ERROR" 1 '"status": "API_ERROR"' -- \
    CHECK_KEY=t HTTP_URL="http://127.0.0.1:$PORT/missing" bash "$CK/check-http.sh"
assert "dead port + pinging host -> SERVICE_DOWN" 1 '"status": "SERVICE_DOWN"' -- \
    CHECK_KEY=t HTTP_URL="http://127.0.0.1:$DEAD/" HOST_IP=127.0.0.1 bash "$CK/check-http.sh"
assert "primary dead, alt healthy -> LAN_UNREACHABLE degraded" 1 '"severity": "degraded"' -- \
    CHECK_KEY=t HTTP_URL="http://127.0.0.1:$DEAD/" HTTP_URL_ALT="http://127.0.0.1:$PORT/" bash "$CK/check-http.sh"
assert "primary dead, alt guard fails, no ping evidence -> TRANSPORT_BLIND" 1 '"status": "TRANSPORT_BLIND"' -- \
    CHECK_KEY=t HTTP_URL="http://127.0.0.1:$DEAD/" HTTP_URL_ALT="http://127.0.0.1:$PORT/" ALT_GUARD_CMD=false bash "$CK/check-http.sh"
assert "both transports dead, host pings -> SERVICE_DOWN" 1 '"status": "SERVICE_DOWN"' -- \
    CHECK_KEY=t HTTP_URL="http://127.0.0.1:$DEAD/" HTTP_URL_ALT="http://127.0.0.1:$DEAD/" HOST_IP=127.0.0.1 bash "$CK/check-http.sh"
assert "missing required param -> exit 64" 64 - -- \
    CHECK_KEY=t bash "$CK/check-http.sh"

echo "== check-tcp-port =="
assert "open port -> exit 0" 0 - -- \
    CHECK_KEY=t TCP_HOST=127.0.0.1 TCP_PORT="$PORT" bash "$CK/check-tcp-port.sh"
assert "closed port, host pings -> PORT_CLOSED" 1 '"status": "PORT_CLOSED"' -- \
    CHECK_KEY=t TCP_HOST=127.0.0.1 TCP_PORT="$DEAD" bash "$CK/check-tcp-port.sh"

echo "== check-disk-space =="
MNT="$T/vol"; mkdir -p "$MNT/BAK"
assert "present volume -> exit 0" 0 - -- \
    CHECK_KEY=t MOUNT_PATH="$MNT" MIN_FREE_PCT=0 MARKER_DIRS=BAK bash "$CK/check-disk-space.sh"
assert "absent volume -> VOLUME_ABSENT" 1 '"status": "VOLUME_ABSENT"' -- \
    CHECK_KEY=t MOUNT_PATH="$T/nope" bash "$CK/check-disk-space.sh"
assert "missing marker -> MARKER_MISSING" 1 '"status": "MARKER_MISSING"' -- \
    CHECK_KEY=t MOUNT_PATH="$MNT" MIN_FREE_PCT=0 MARKER_DIRS="BAK models" bash "$CK/check-disk-space.sh"
assert "low space -> LOW_SPACE degraded" 1 '"severity": "degraded"' -- \
    CHECK_KEY=t MOUNT_PATH="$MNT" MIN_FREE_PCT=100 bash "$CK/check-disk-space.sh"

echo "== check-cert-expiry =="
openssl req -x509 -newkey rsa:2048 -keyout "$T/k.pem" -out "$T/c.pem" \
    -days 365 -nodes -subj "/CN=test" >/dev/null 2>&1
assert "1y cert, default thresholds -> exit 0" 0 - -- \
    CHECK_KEY=t CERT_FILE="$T/c.pem" bash "$CK/check-cert-expiry.sh"
assert "1y cert, WARN_DAYS=400 -> degraded CERT_EXPIRING" 1 '"severity": "degraded"' -- \
    CHECK_KEY=t CERT_FILE="$T/c.pem" WARN_DAYS=400 bash "$CK/check-cert-expiry.sh"
assert "1y cert, CRIT_DAYS=400 -> full-page CERT_EXPIRING" 1 '"status": "CERT_EXPIRING"' -- \
    CHECK_KEY=t CERT_FILE="$T/c.pem" WARN_DAYS=500 CRIT_DAYS=400 bash "$CK/check-cert-expiry.sh"
assert "missing file -> CERT_UNREADABLE" 1 '"status": "CERT_UNREADABLE"' -- \
    CHECK_KEY=t CERT_FILE="$T/absent.pem" bash "$CK/check-cert-expiry.sh"

echo "== check-host-power (fake sysfs) =="
AC="$T/sys/AC"; BAT="$T/sys/BAT0"; mkdir -p "$AC" "$BAT"
echo 1 > "$AC/online"; echo 1 > "$BAT/present"; echo 80 > "$BAT/capacity"; echo Discharging > "$BAT/status"
assert "on mains -> exit 0" 0 - -- \
    CHECK_KEY=hp AC_SYSFS="$AC" BAT_SYSFS="$BAT" bash "$CK/check-host-power.sh"
echo 0 > "$AC/online"
assert "on battery (80%) -> power-lost finding only" 1 '"status": "ON_EXTERNAL_POWER_LOST"' -- \
    CHECK_KEY=hp AC_SYSFS="$AC" BAT_SYSFS="$BAT" BAT_LOW_PCT=50 bash "$CK/check-host-power.sh"
echo 30 > "$BAT/capacity"
out=$(CHECK_KEY=hp AC_SYSFS="$AC" BAT_SYSFS="$BAT" BAT_LOW_PCT=50 bash "$CK/check-host-power.sh" 2>&1)
if printf '%s' "$out" | grep -q INTERNAL_BATTERY_LOW && printf '%s' "$out" | grep -q ON_EXTERNAL_POWER_LOST; then
    ok "on battery (30%) -> both findings"
else
    bad "on battery (30%) -> both findings (got: $out)"
fi
rm -f "$AC/online"
assert "unreadable sysfs -> HOST_POWER_UNREADABLE" 1 '"status": "HOST_POWER_UNREADABLE"' -- \
    CHECK_KEY=hp AC_SYSFS="$AC" BAT_SYSFS="$BAT" bash "$CK/check-host-power.sh"

echo "== check-ha-entity (stub HA) =="
HAPORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
python3 "$REPO/tests/fixtures/stub-ha.py" "$HAPORT" sensor.batt 55 0 &
HA_PID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null "http://127.0.0.1:$HAPORT/api/states/sensor.batt" && break; sleep 0.3; done
BASEENV=(CHECK_KEY=he HA_URL="http://127.0.0.1:$HAPORT" HA_TOKEN=x ENTITY_ID=sensor.batt)
assert "numeric 55, low=50 -> exit 0" 0 - -- "${BASEENV[@]}" VALUE_LOW=50 bash "$CK/check-ha-entity.sh"
assert "numeric 55, low=60 -> VALUE_LOW" 1 '"status": "VALUE_LOW"' -- "${BASEENV[@]}" VALUE_LOW=60 bash "$CK/check-ha-entity.sh"
assert "numeric 55, crit=60 -> VALUE_CRITICAL" 1 '"status": "VALUE_CRITICAL"' -- "${BASEENV[@]}" VALUE_CRITICAL=60 bash "$CK/check-ha-entity.sh"
assert "HA itself dead -> silent exit 0 (layer isolation)" 0 - -- \
    CHECK_KEY=he HA_URL="http://127.0.0.1:$DEAD" HA_TOKEN=x ENTITY_ID=sensor.batt bash "$CK/check-ha-entity.sh"
kill $HA_PID 2>/dev/null; wait $HA_PID 2>/dev/null
python3 "$REPO/tests/fixtures/stub-ha.py" "$HAPORT" sensor.batt unavailable 5 &
HA_PID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null "http://127.0.0.1:$HAPORT/api/states/sensor.batt" && break; sleep 0.3; done
assert "blind 5 min < 15 -> silent warm-up" 0 - -- "${BASEENV[@]}" bash "$CK/check-ha-entity.sh"
kill $HA_PID 2>/dev/null; wait $HA_PID 2>/dev/null
python3 "$REPO/tests/fixtures/stub-ha.py" "$HAPORT" sensor.batt unavailable 30 &
HA_PID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null "http://127.0.0.1:$HAPORT/api/states/sensor.batt" && break; sleep 0.3; done
assert "blind 30 min >= 15 -> ENTITY_BLIND" 1 '"status": "ENTITY_BLIND"' -- "${BASEENV[@]}" bash "$CK/check-ha-entity.sh"

echo "== ha-service-call remediation (stub HA) =="
kill $HA_PID 2>/dev/null; wait $HA_PID 2>/dev/null
python3 "$REPO/tests/fixtures/stub-ha.py" "$HAPORT" switch.plug off 0 &
HA_PID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null "http://127.0.0.1:$HAPORT/api/states/switch.plug" && break; sleep 0.3; done
assert "turn_on + verify -> exit 0" 0 - -- \
    REMEDIATION_KEY=rk HA_URL="http://127.0.0.1:$HAPORT" HA_TOKEN=x HA_ENTITY=switch.plug \
    VERIFY_TRIES=3 VERIFY_SLEEP=0 bash "$SRCROOT/remediations/templates/ha-service-call.sh"
assert "turn_off is structurally refused (64)" 64 "not fail-safe-direction" -- \
    REMEDIATION_KEY=rk HA_URL="http://127.0.0.1:$HAPORT" HA_TOKEN=x HA_ENTITY=switch.plug \
    HA_SERVICE=turn_off bash "$SRCROOT/remediations/templates/ha-service-call.sh"
kill $HA_PID 2>/dev/null; wait $HA_PID 2>/dev/null

echo "== remediation library (cap / refuse / verify) =="
FIX="$REPO/tests/fixtures/fake-remediation.sh"
assert "act + verify -> exit 0" 0 - -- CAP_MAX=99 bash "$FIX" "$T/fixed1"
assert "missing arg -> refuse 64" 64 "REFUSED" -- CAP_MAX=99 bash "$FIX"
assert "forced verify failure -> exit 1" 1 - -- CAP_MAX=99 FORCE_VERIFY_FAIL=1 bash "$FIX" "$T/fixed2"
# cap: fresh audit log, budget 2 -> third execution refused with 75
export SELFHEAL_AUDIT_LOG="$T/audit-cap.log"
CAP_MAX=2 bash "$FIX" "$T/c1" >/dev/null
CAP_MAX=2 bash "$FIX" "$T/c2" >/dev/null
assert "third execution over cap -> 75" 75 "internal rate cap" -- CAP_MAX=2 bash "$FIX" "$T/c3"
grep -q "REFUSED .*internal-cap" "$SELFHEAL_AUDIT_LOG" && ok "cap refusal audited" || bad "cap refusal audited"

echo "== check-dns (upstream isolation only; no live resolver assumed) =="
assert "resolver+upstreams all dead -> UPSTREAM_DOWN" 1 '"status": "UPSTREAM_DOWN"' -- \
    CHECK_KEY=dns RESOLVER_IP=127.0.0.1 UPSTREAM1=127.0.0.1 UPSTREAM2=127.0.0.1 \
    TEST_DOMAIN=example.invalid bash "$CK/check-dns.sh"

kill $HTTP_PID 2>/dev/null; wait $HTTP_PID 2>/dev/null
rm -rf "$T"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] || exit 1
