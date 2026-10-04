#!/bin/bash
# test_templates.sh - offline smoke tests for the check templates and the
# remediation library. No network beyond 127.0.0.1; runs on macOS and Linux.
# (check-dns needs a live resolver, so it gets bash -n plus the 127.0.0.1
# upstream case for the UPSTREAM_DOWN path; check-lan-inventory wants Linux
# `ip` live but runs here against stub `ip`/`ping` binaries.)

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
# PING_SPLIT=0: no ping ever runs, so the HOST_DOWN detail must not claim one did
out=$(CHECK_KEY=t TCP_HOST=127.0.0.1 TCP_PORT="$DEAD" PING_SPLIT=0 bash "$CK/check-tcp-port.sh" 2>&1); rc=$?
if [ "$rc" = 1 ] && printf '%s' "$out" | grep -q '"status": "HOST_DOWN"' \
        && ! printf '%s' "$out" | grep -q "not answering ping"; then
    ok "PING_SPLIT=0 -> HOST_DOWN without the ping claim"
else
    bad "PING_SPLIT=0 -> HOST_DOWN without the ping claim (rc=$rc out=$out)"
fi

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

echo "== ssh-forced-command remediation (stub ssh) =="
# The stub records its argv and then "restarts the service": it opens the
# loopback port the template verifies against (VERIFY_TCP). Nothing real is
# contacted. STUB_SSH_RC=255 makes it fail like a refused connection.
SSHSTUB="$T/sshstub"; mkdir -p "$SSHSTUB"
cat > "$SSHSTUB/ssh" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "${STUB_SSH_LOG:?}"
[ "${STUB_SSH_RC:-0}" = 0 ] || exit "$STUB_SSH_RC"
if [ -n "${STUB_LISTEN_PORT:-}" ]; then
    python3 -c '
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(5); s.settimeout(20)
try:
    c, _ = s.accept(); c.close()
except socket.timeout:
    pass
' "$STUB_LISTEN_PORT" >/dev/null 2>&1 </dev/null &
fi
exit 0
EOF
chmod +x "$SSHSTUB/ssh"
SSHSH="$SRCROOT/remediations/templates/ssh-forced-command.sh"
KEY="$T/id_test"; printf 'not a real key\n' > "$KEY"; chmod 600 "$KEY"
LPORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
ARGV="$T/ssh-argv.log"
SSHENV=(PATH="$SSHSTUB:$PATH" STUB_SSH_LOG="$ARGV" STUB_LISTEN_PORT="$LPORT" REMEDIATION_KEY=rk CAP_MAX=99
        SSH_TARGET=pi@host.example SSH_KEY="$KEY" VERIFY_TRIES=8 VERIFY_SLEEP=1)
assert "forced command runs, VERIFY_TCP comes up -> exit 0" 0 - -- \
    "${SSHENV[@]}" VERIFY_TCP="127.0.0.1:$LPORT" bash "$SSHSH" media-7
WANT=$(printf '%s\n' -i "$KEY" -p 22 -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 pi@host.example media-7)
[ "$(cat "$ARGV")" = "$WANT" ] && ok "ssh argv: -i key -p 22 BatchMode StrictHostKeyChecking ConnectTimeout target arg, in that order" \
    || bad "ssh argv order (got: $(tr '\n' ' ' < "$ARGV"))"
assert "VERIFY_URL against the loopback http server -> exit 0" 0 - -- \
    "${SSHENV[@]}" STUB_LISTEN_PORT= VERIFY_URL="http://127.0.0.1:$PORT/" SSH_PORT=2222 SSH_CONNECT_TIMEOUT=3 \
    SSH_KNOWN_HOSTS="$T/known" bash "$SSHSH"
WANT=$(printf '%s\n' -i "$KEY" -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=3 -o "UserKnownHostsFile=$T/known" pi@host.example)
[ "$(cat "$ARGV")" = "$WANT" ] && ok "no argument -> the target is the last word; port/timeout/known_hosts params land in argv" \
    || bad "ssh argv without arg (got: $(tr '\n' ' ' < "$ARGV"))"
assert "no VERIFY_* at all -> acts but exits 1 unverified" 1 "unverified by script" -- \
    "${SSHENV[@]}" STUB_LISTEN_PORT= bash "$SSHSH"
assert "ssh itself fails (255) -> exit 1" 1 "exited 255" -- \
    "${SSHENV[@]}" STUB_SSH_RC=255 VERIFY_TCP="127.0.0.1:$LPORT" bash "$SSHSH"
assert "missing key file -> refuse 64" 64 "no such file" -- \
    "${SSHENV[@]}" SSH_KEY="$T/no-such-key" VERIFY_TCP="127.0.0.1:$LPORT" bash "$SSHSH"
chmod 644 "$KEY"
assert "world-readable key (644) -> refuse 64" 64 "mode 600" -- \
    "${SSHENV[@]}" VERIFY_TCP="127.0.0.1:$LPORT" bash "$SSHSH"
chmod 600 "$KEY"
assert "SSH_TARGET without user@ -> refuse 64" 64 "user@host" -- \
    "${SSHENV[@]}" SSH_TARGET=host.example VERIFY_TCP="127.0.0.1:$LPORT" bash "$SSHSH"
assert "two arguments -> refuse 64" 64 "at most one argument" -- \
    "${SSHENV[@]}" VERIFY_TCP="127.0.0.1:$LPORT" bash "$SSHSH" a b
assert "VERIFY_TCP without a port -> refuse 64 before acting" 64 "host:port" -- \
    "${SSHENV[@]}" VERIFY_TCP="127.0.0.1" bash "$SSHSH"
: > "$ARGV"
env "${SSHENV[@]}" SSH_KEY="$T/no-such-key" VERIFY_TCP="127.0.0.1:$LPORT" bash "$SSHSH" >/dev/null 2>&1
[ ! -s "$ARGV" ] && ok "a refused run never reaches ssh" || bad "a refused run never reaches ssh"
# default internal cap: 3 per 6h for this key, the 4th run is refused with 75
for i in 1 2 3; do env "${SSHENV[@]}" CAP_MAX= REMEDIATION_KEY=rkcap STUB_LISTEN_PORT= bash "$SSHSH" >/dev/null 2>&1; done
assert "default cap 3/6h -> 4th run refused 75" 75 "internal rate cap" -- \
    "${SSHENV[@]}" CAP_MAX= REMEDIATION_KEY=rkcap STUB_LISTEN_PORT= bash "$SSHSH"

echo "== ha-outlet-cycle remediation (fake HA with turn_off/turn_on, call log) =="
# tests/fixtures/stub-ha.py only knows turn_on; the cycle needs turn_off and a
# record of the call order, so this fake lives here.
cat > "$T/stub-ha-cycle.py" <<'EOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, entity, log = int(sys.argv[1]), sys.argv[2], sys.argv[4]
state = {"value": sys.argv[3]}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == f"/api/states/{entity}":
            self._send(200, {"entity_id": entity, "state": state["value"], "attributes": {"state": "decoy"}})
        else:
            self._send(404, {"message": "not found"})
    def do_POST(self):
        svc = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/api/services/switch/") and svc in ("turn_on", "turn_off"):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            with open(log, "a") as f:
                f.write(f"{svc} {body.get('entity_id')}\n")
            state["value"] = "on" if svc == "turn_on" else "off"
            self._send(200, [])
        else:
            self._send(404, {"message": "not found"})
HTTPServer(("127.0.0.1", port), H).serve_forever()
EOF
CYCLE="$SRCROOT/remediations/templates/ha-outlet-cycle.sh"
CPORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
CALLS="$T/ha-calls.log"
ha_cycle_stub() {  # <initial state> - (re)start the fake HA serving switch.tv in that state, empty call log
    [ -n "${CYC_PID:-}" ] && { kill "$CYC_PID" 2>/dev/null; wait "$CYC_PID" 2>/dev/null; }
    : > "$CALLS"
    python3 "$T/stub-ha-cycle.py" "$CPORT" switch.tv "$1" "$CALLS" &
    CYC_PID=$!
    for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -o /dev/null "http://127.0.0.1:$CPORT/api/states/switch.tv" && break; sleep 0.3; done
}
ha_state() { curl -s "http://127.0.0.1:$CPORT/api/states/switch.tv" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])'; }
CYCENV=(REMEDIATION_KEY=tv HA_URL="http://127.0.0.1:$CPORT" HA_TOKEN=x OUTLET_ENTITY=switch.tv CAP_MAX=99
        CYCLE_OFF_SECONDS=1 VERIFY_TRIES=3 VERIFY_SLEEP=0)
ha_cycle_stub on
assert "outlet on: off, wait, on, verified -> exit 0" 0 - -- "${CYCENV[@]}" bash "$CYCLE"
[ "$(cat "$CALLS")" = "$(printf 'turn_off switch.tv\nturn_on switch.tv')" ] && ok "call order is exactly turn_off then turn_on" \
    || bad "call order (got: $(tr '\n' ' ' < "$CALLS"))"
[ "$(ha_state)" = on ] && ok "the outlet ends ON" || bad "the outlet ends ON (is $(ha_state))"
grep -q "RESULT service=tv status=ok" "$SELFHEAL_AUDIT_LOG" && ok "success audited" || bad "success audited"
: > "$CALLS"
assert "SELFHEAL_AUTOMATION=true (the engine's auto path) -> refuse 64" 64 "ask-first only" -- \
    "${CYCENV[@]}" SELFHEAL_AUTOMATION=true bash "$CYCLE"
[ ! -s "$CALLS" ] && ok "...and HA was not called at all" || bad "auto path made HA calls: $(cat "$CALLS")"
assert "a non-switch entity -> refuse 64" 64 "not a switch" -- "${CYCENV[@]}" OUTLET_ENTITY=light.tv bash "$CYCLE"
assert "an entity HA does not know -> refuse 64 (cannot read)" 64 "cannot read" -- "${CYCENV[@]}" OUTLET_ENTITY=switch.nope bash "$CYCLE"
assert "CYCLE_OFF_SECONDS over 60 -> refuse 64" 64 "60s maximum" -- "${CYCENV[@]}" CYCLE_OFF_SECONDS=90 bash "$CYCLE"
assert "HA unreachable -> refuse 64, no power cut" 64 "cannot read" -- "${CYCENV[@]}" HA_URL="http://127.0.0.1:$DEAD" bash "$CYCLE"
[ ! -s "$CALLS" ] && ok "no refusal made an HA call" || bad "a refusal made HA calls: $(cat "$CALLS")"
ha_cycle_stub off
assert "outlet already off -> refuse 64 (restore path unproven)" 64 "not 'on'" -- "${CYCENV[@]}" bash "$CYCLE"
[ ! -s "$CALLS" ] && ok "...and nothing was switched" || bad "an off outlet was switched: $(cat "$CALLS")"
# killed mid-cycle: the restore trap must put the outlet back ON (TERM after
# ~2s of a 30s off period; a plain kill, no `timeout`, so it runs on macOS too)
ha_cycle_stub on
env "${CYCENV[@]}" CYCLE_OFF_SECONDS=30 bash "$CYCLE" >/dev/null 2>&1 &
CYCRUN=$!
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do grep -q turn_off "$CALLS" 2>/dev/null && break; sleep 0.2; done
sleep 1
kill -TERM "$CYCRUN" 2>/dev/null
wait "$CYCRUN" 2>/dev/null; CYCRC=$?
[ "$CYCRC" = 143 ] && ok "killed mid-cycle: exits 143 promptly (1s wait slices)" || bad "killed mid-cycle rc=$CYCRC"
[ "$(cat "$CALLS")" = "$(printf 'turn_off switch.tv\nturn_on switch.tv')" ] && ok "killed mid-cycle: the trap re-asserted turn_on" \
    || bad "killed mid-cycle: calls were $(tr '\n' ' ' < "$CALLS")"
[ "$(ha_state)" = on ] && ok "killed mid-cycle: the outlet is back ON" || bad "killed mid-cycle: outlet is $(ha_state)"
grep -q "RESTORE service=tv action=turn_on" "$SELFHEAL_AUDIT_LOG" && ok "the trap's restore is audited" || bad "the trap's restore is audited"
# default internal cap: 2 per 6h for this key, the 3rd cycle is refused with 75
for i in 1 2; do env "${CYCENV[@]}" CAP_MAX= REMEDIATION_KEY=tvcap CYCLE_OFF_SECONDS=0 bash "$CYCLE" >/dev/null 2>&1; done
assert "default cap 2/6h -> 3rd cycle refused 75" 75 "internal rate cap" -- \
    "${CYCENV[@]}" CAP_MAX= REMEDIATION_KEY=tvcap CYCLE_OFF_SECONDS=0 bash "$CYCLE"
[ "$(ha_state)" = on ] && ok "the outlet is ON after the capped runs" || bad "outlet after capped runs is $(ha_state)"
kill "$CYC_PID" 2>/dev/null; wait "$CYC_PID" 2>/dev/null

echo "== restart-systemd-unit remediation (stub systemctl) =="
# The stub records each invocation's argv as one line, exits
# SYSCTL_RESTART_RC for `restart` and per SYSCTL_ACTIVE for `is-active`.
RSU="$SRCROOT/remediations/templates/restart-systemd-unit.sh"
SCSTUB="$T/scstub"; mkdir -p "$SCSTUB"
cat > "$SCSTUB/systemctl" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "${STUB_SYSCTL_LOG:?}"
for a in "$@"; do case "$a" in
    restart)   exit "${SYSCTL_RESTART_RC:-0}" ;;
    is-active) [ "${SYSCTL_ACTIVE:-1}" = 1 ] && exit 0; exit 3 ;;
esac; done
exit 0
EOF
chmod +x "$SCSTUB/systemctl"
SCLOG="$T/systemctl-argv.log"
RSUENV=(PATH="$SCSTUB:$PATH" STUB_SYSCTL_LOG="$SCLOG" REMEDIATION_KEY=rsu CAP_MAX=99 UNIT=fake.service)
: > "$SCLOG"
assert "restart ok + unit active -> exit 0" 0 - -- "${RSUENV[@]}" bash "$RSU"
if head -1 "$SCLOG" | grep -q "restart fake.service" && grep -q "is-active" "$SCLOG"; then
    ok "argv log: the restart, then is-active"
else
    bad "argv log: the restart, then is-active (got: $(tr '\n' ';' < "$SCLOG"))"
fi
grep -q "RESULT service=rsu status=ok" "$SELFHEAL_AUDIT_LOG" && ok "verified success audited" || bad "verified success audited"
: > "$SCLOG"
assert "system scope -> exit 0" 0 - -- "${RSUENV[@]}" UNIT_SCOPE=system bash "$RSU"
grep -q -- "--user" "$SCLOG" && bad "system scope passed --user (got: $(tr '\n' ';' < "$SCLOG"))" \
    || ok "system scope never passes --user"
: > "$SCLOG"
assert "restart ok but unit never active -> exit 1" 1 - -- \
    "${RSUENV[@]}" SYSCTL_ACTIVE=0 VERIFY_TRIES=2 VERIFY_SLEEP=0 bash "$RSU"
grep -q "status=fail restart returned 0 but unit not active" "$SELFHEAL_AUDIT_LOG" \
    && ok "'not active' failure audited" || bad "'not active' failure audited"
: > "$SCLOG"
assert "restart itself fails (5) -> exit 5" 5 - -- "${RSUENV[@]}" SYSCTL_RESTART_RC=5 bash "$RSU"
grep -q "is-active" "$SCLOG" && bad "failed restart still ran is-active (got: $(tr '\n' ';' < "$SCLOG"))" \
    || ok "failed restart never reaches is-active"

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

echo "== check-lan-inventory (stub ip/ping) =="
# Stub `ip` answers `-4 addr show` with one fixed inet line (STUB_SELF_INET)
# and `neigh show <ip>` with a configurable lladdr (STUB_NEIGH_MAC); stub
# `ping` exits STUB_PING_RC. The inventory path carries a single quote to
# prove both python readers take it via argv, not source interpolation.
LANSTUB="$T/lanstub"; mkdir -p "$LANSTUB"
cat > "$LANSTUB/ip" <<'EOF'
#!/bin/bash
case "$*" in
    "-4 addr show"*)
        echo "    inet ${STUB_SELF_INET:-192.168.9.47}/24 brd 192.168.9.255 scope global wlan0" ;;
    "neigh show "*)
        [ -n "${STUB_NEIGH_MAC:-}" ] && echo "$3 dev wlan0 lladdr ${STUB_NEIGH_MAC} REACHABLE" ;;
esac
exit 0
EOF
chmod +x "$LANSTUB/ip"
cat > "$LANSTUB/ping" <<'EOF'
#!/bin/bash
exit "${STUB_PING_RC:-0}"
EOF
chmod +x "$LANSTUB/ping"
INVQ="$T/inv'q.json"
cat > "$INVQ" <<'EOF'
{"self_ip": "192.168.9.47",
 "hosts": [{"name": "nas", "ip": "192.168.9.58", "mac": "AA:BB:CC:DD:EE:FF",
            "covered_by_service": false}]}
EOF
LANENV=(PATH="$LANSTUB:$PATH" CHECK_KEY=lan INVENTORY_FILE="$INVQ")
assert "everything matches -> exit 0, no findings" 0 - -- \
    "${LANENV[@]}" STUB_NEIGH_MAC=aa:bb:cc:dd:ee:ff bash "$CK/check-lan-inventory.sh"
assert "different MAC answers (quoted path parsed via argv) -> DEVICE_CHANGED" 1 '"status": "DEVICE_CHANGED"' -- \
    "${LANENV[@]}" STUB_NEIGH_MAC=11:22:33:44:55:66 bash "$CK/check-lan-inventory.sh"
assert "self_ip absent from addr output -> SELF_IP_CHANGED" 1 '"status": "SELF_IP_CHANGED"' -- \
    "${LANENV[@]}" STUB_NEIGH_MAC=aa:bb:cc:dd:ee:ff STUB_SELF_INET=10.0.0.5 bash "$CK/check-lan-inventory.sh"
assert "uncovered host not pinging -> HOST_UNREACHABLE" 1 '"status": "HOST_UNREACHABLE"' -- \
    "${LANENV[@]}" STUB_NEIGH_MAC=aa:bb:cc:dd:ee:ff STUB_PING_RC=1 bash "$CK/check-lan-inventory.sh"
assert "inventory file missing -> CHECK_ERROR" 1 '"status": "CHECK_ERROR"' -- \
    "${LANENV[@]}" INVENTORY_FILE="$T/absent.json" bash "$CK/check-lan-inventory.sh"

echo "== approval gates: TEMPLATE + auto-pass contract =="
GT="$SRCROOT/gates"
# a fake "remediations dir" layout so the auto-pass path check works
FAKEROOT="$T/fakeroot"; mkdir -p "$FAKEROOT/remediations"
MARK="$T/gate-exec-marker"
FIXSH="$FAKEROOT/remediations/fix.sh"
printf '#!/bin/bash\necho "ran $*" > "%s"\n' "$MARK" > "$FIXSH"
assert "template refuses with no factor (65)" 65 "no factor configured" -- \
    SELFHEAL_ROOT="$FAKEROOT" bash "$GT/TEMPLATE.sh" "$FAKEROOT/remediations/fix.sh"
assert "template refuses a missing script (65)" 65 "no such remediation" -- \
    bash "$GT/TEMPLATE.sh" "$T/does-not-exist.sh"
rm -f "$MARK"
SELFHEAL_AUTOMATION=true SELFHEAL_ROOT="$FAKEROOT" bash "$GT/TEMPLATE.sh" "$FAKEROOT/remediations/fix.sh" >/dev/null 2>&1
[ -f "$MARK" ] && ok "auto path passes through and runs the remediation" || bad "auto path passes through"
rm -f "$MARK"
SELFHEAL_AUTOMATION=true SELFHEAL_ROOT="$FAKEROOT" bash "$GT/TEMPLATE.sh" "$T/does-not-exist.sh" >/dev/null 2>&1
[ ! -f "$MARK" ] && ok "auto path still refuses scripts outside remediations/" || bad "auto path path-anchored"

echo "== approval gates: argv contract (argc / shape / full path) =="
# Every gate enforces the engine's argument rule ITSELF, above the auto-pass
# branch: <script> plus at most one [A-Za-z0-9][A-Za-z0-9._@:-]{0,63}. Run with
# SELFHEAL_AUTOMATION=true so a bypass would show up as exit 0.
export SELFHEAL_AUDIT_LOG="$T/audit-gates.log"
AUTO=(SELFHEAL_AUTOMATION=true SELFHEAL_ROOT="$FAKEROOT")
A64=$(printf '%064d' 0); A65=$(printf '%065d' 0)
for g in TEMPLATE.sh gate-totp-remote.sh gate-telegram-confirm.sh; do
    assert "$g: 3 arguments -> 65" 65 "got 3 arguments" -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" db-1 extra
    assert "$g: 0 arguments -> 65" 65 "got 0 arguments" -- "${AUTO[@]}" bash "$GT/$g"
    assert "$g: arg '-rf' -> 65 malformed" 65 "malformed" -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" -rf
    assert "$g: arg 'a b' -> 65 malformed" 65 "malformed" -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" "a b"
    assert "$g: arg with a newline -> 65 malformed" 65 "malformed" -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" "db-1
GATE-APPROVED forged"
    assert "$g: arg '../x' -> 65 malformed" 65 "malformed" -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" ../x
    assert "$g: empty arg -> 65 malformed" 65 "malformed" -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" ""
    assert "$g: 65-char arg -> 65 malformed" 65 "malformed" -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" "$A65"
    assert "$g: arg '-rf' on the human path -> 65 malformed" 65 "malformed" -- SELFHEAL_ROOT="$FAKEROOT" bash "$GT/$g" "$FIXSH" -rf
    rm -f "$MARK"
    assert "$g: valid arg 'db-1' auto-passes (0)" 0 - -- "${AUTO[@]}" bash "$GT/$g" "$FIXSH" db-1
    grep -q "ran db-1" "$MARK" 2>/dev/null && ok "$g: remediation received the argument" || bad "$g: remediation received the argument"
    grep -q "GATE-AUTO-PASS $FIXSH db-1" "$SELFHEAL_AUDIT_LOG" && ok "$g: audit names the full script path" || bad "$g: audit names the full script path"
done
assert "64-char arg (boundary) auto-passes (0)" 0 - -- "${AUTO[@]}" bash "$GT/TEMPLATE.sh" "$FIXSH" "$A64"
( cd "$FAKEROOT" && env "${AUTO[@]}" bash "$GT/TEMPLATE.sh" remediations/fix.sh rel-1 >/dev/null 2>&1 )
grep -q "GATE-AUTO-PASS $FIXSH rel-1" "$SELFHEAL_AUDIT_LOG" && ok "relative script path is audited resolved" || bad "relative script path is audited resolved"
# the newline arg above was refused; its text must not have become a line of its own
grep -q "^GATE-APPROVED" "$SELFHEAL_AUDIT_LOG" && bad "refused newline arg cannot forge an audit line" || ok "refused newline arg cannot forge an audit line"

echo "== approval gates: totp server + client =="
SECF="$T/totp-secret"; printf 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ' > "$SECF"; chmod 600 "$SECF"
TPORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
GATE_TOTP_NOW=59 python3 "$GT/gate-totp-server.py" "$SECF" "$TPORT" >/dev/null 2>&1 & TOTP_PID=$!
for _ in $(seq 30); do curl -s -m 1 "http://127.0.0.1:$TPORT/health" >/dev/null 2>&1 && break; sleep 0.2; done
# RFC 6238 known answer at T=59 for this secret is 287082
assert "verifier accepts the RFC 6238 code" 0 '"ok": true' -- \
    curl -s -m 5 -X POST "http://127.0.0.1:$TPORT/verify" -H "Content-Type: application/json" -d '{"code": "287082"}'
R=$(curl -s -m 5 -X POST "http://127.0.0.1:$TPORT/verify" -H "Content-Type: application/json" -d '{"code": "000000"}')
echo "$R" | grep -q '"ok": false' && ok "verifier rejects a wrong code" || bad "verifier rejects a wrong code ($R)"
for i in 1 2 3 4 5; do curl -s -m 5 -X POST "http://127.0.0.1:$TPORT/verify" -d '{"code": "111111"}' >/dev/null; done
R=$(curl -s -m 5 -X POST "http://127.0.0.1:$TPORT/verify" -d '{"code": "287082"}')
echo "$R" | grep -q "locked out" && ok "lockout engages after repeated failures" || bad "lockout engages ($R)"
kill $TOTP_PID 2>/dev/null; wait $TOTP_PID 2>/dev/null
# client gate against a fresh (unlocked) server
GATE_TOTP_NOW=59 python3 "$GT/gate-totp-server.py" "$SECF" "$TPORT" >/dev/null 2>&1 & TOTP_PID=$!
for _ in $(seq 30); do curl -s -m 1 "http://127.0.0.1:$TPORT/health" >/dev/null 2>&1 && break; sleep 0.2; done
rm -f "$MARK"
GATE_CODE=287082 GATE_TOTP_URL="http://127.0.0.1:$TPORT" SELFHEAL_ROOT="$FAKEROOT" \
    bash "$GT/gate-totp-remote.sh" "$FAKEROOT/remediations/fix.sh" >/dev/null 2>&1
[ -f "$MARK" ] && ok "totp client gate execs on a valid code" || bad "totp client gate execs on valid code"
assert "totp client gate refuses a bad code (65)" 65 "failing closed" -- \
    env GATE_CODE=999999 GATE_TOTP_URL="http://127.0.0.1:$TPORT" SELFHEAL_ROOT="$FAKEROOT" \
    bash "$GT/gate-totp-remote.sh" "$FAKEROOT/remediations/fix.sh"
assert "totp client gate refuses with no code and no tty (65)" 65 "no code supplied" -- \
    env GATE_TOTP_URL="http://127.0.0.1:$TPORT" SELFHEAL_ROOT="$FAKEROOT" \
    bash "$GT/gate-totp-remote.sh" "$FAKEROOT/remediations/fix.sh" < /dev/null
# the verifier only knows 6-digit codes (gate-totp-server.py DIGITS=6): an
# 8-digit code must be refused CLIENT-side, before any network attempt - the
# URL here points at a dead port, so reaching curl would hang/fail differently
assert "totp client gate refuses an 8-digit code pre-network (65)" 65 "malformed code (expect 6 digits)" -- \
    env GATE_CODE=12345678 GATE_TOTP_URL="http://127.0.0.1:9" SELFHEAL_ROOT="$FAKEROOT" \
    bash "$GT/gate-totp-remote.sh" "$FAKEROOT/remediations/fix.sh"
kill $TOTP_PID 2>/dev/null; wait $TOTP_PID 2>/dev/null

echo "== approval gates: telegram confirm (stub Bot API) =="
GPORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
TGENV="$T/tg.env"; printf 'GATE_TG_BOT_TOKEN="stub"\nGATE_TG_CHAT_ID="123456789"\n' > "$TGENV"; chmod 600 "$TGENV"
tg_case() {  # <mode> -> runs the gate, echoes rc; marker tells if exec happened
    local mode="$1"
    STUB_MODE="$mode" python3 "$REPO/tests/fixtures/stub-telegram.py" "$GPORT" & local spid=$!
    for _ in $(seq 30); do curl -s -m 1 "http://127.0.0.1:$GPORT/botstub/getUpdates" >/dev/null 2>&1 && break; sleep 0.2; done
    rm -f "$MARK"
    GATE_TELEGRAM_ENV="$TGENV" GATE_TG_API="http://127.0.0.1:$GPORT" GATE_TIMEOUT=6 \
        SELFHEAL_ROOT="$FAKEROOT" bash "$GT/gate-telegram-confirm.sh" "$FAKEROOT/remediations/fix.sh" >/dev/null 2>&1
    local rc=$?
    kill "$spid" 2>/dev/null; wait "$spid" 2>/dev/null
    echo "$rc"
}
RC=$(tg_case approve)
[ "$RC" = 0 ] && [ -f "$MARK" ] && ok "telegram gate execs on 'approve <nonce>'" || bad "telegram gate approve (rc=$RC)"
RC=$(tg_case deny)
[ "$RC" = 65 ] && [ ! -f "$MARK" ] && ok "telegram gate refuses on 'deny <nonce>'" || bad "telegram gate deny (rc=$RC)"
RC=$(tg_case wrong)
[ "$RC" = 65 ] && [ ! -f "$MARK" ] && ok "telegram gate times out on a wrong nonce" || bad "telegram gate wrong nonce (rc=$RC)"
RC=$(tg_case stranger)
[ "$RC" = 65 ] && [ ! -f "$MARK" ] && ok "telegram gate ignores the right text from the wrong chat" || bad "telegram gate stranger chat (rc=$RC)"
# The prompt must name the FULL script path (stub-telegram.py only parses the
# nonce), so a curl shim records the exact text the gate sends and answers like
# an idle Bot API; the gate then times out and refuses.
SHIM="$T/shim"; mkdir -p "$SHIM"
cat > "$SHIM/curl" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" >> "${CURL_LOG:?}"
echo '{"ok": true, "result": []}'
EOF
chmod +x "$SHIM/curl"
CURL_LOG="$T/curl.log" PATH="$SHIM:$PATH" GATE_TELEGRAM_ENV="$TGENV" GATE_TIMEOUT=1 SELFHEAL_ROOT="$FAKEROOT" \
    bash "$GT/gate-telegram-confirm.sh" "$FIXSH" db-1 >/dev/null 2>&1
grep -qxF "$FIXSH db-1" "$T/curl.log" && ok "telegram prompt names the full script path + arg" \
    || bad "telegram prompt names the full script path (sent: $(grep -A1 'text=' "$T/curl.log" 2>/dev/null | tr '\n' ' '))"
chmod 644 "$TGENV"
assert "telegram gate refuses a world-readable creds file (65)" 65 "mode 600" -- \
    env GATE_TELEGRAM_ENV="$TGENV" SELFHEAL_ROOT="$FAKEROOT" \
    bash "$GT/gate-telegram-confirm.sh" "$FAKEROOT/remediations/fix.sh"

kill $HTTP_PID 2>/dev/null; wait $HTTP_PID 2>/dev/null
rm -rf "$T"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] || exit 1
