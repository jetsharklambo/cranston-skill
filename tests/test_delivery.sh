#!/bin/bash
# test_delivery.sh - offline tests for the alert DELIVERY half and the adapter
# shims: bin/send-telegram.sh (against a stub Bot API on 127.0.0.1),
# bin/notify-alerts.sh (recording stub sender + the real send-alert.sh queue),
# bin/tailscale-running.sh (stub tailscale on PATH) and
# gates/secure-bash-argv.sh (stub 2FA wrapper). No network beyond 127.0.0.1,
# python3 stdlib only; runs on macOS and Linux. Every token/chat id is fake.

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRCROOT="$REPO/authoring/scripts"
BIN="$SRCROOT/bin"
GT="$SRCROOT/gates"
T="$(mktemp -d)"
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
# first line of a file equals the expected string
first_line_is() {  # <name> <file> <expected>
    local got; got=$(head -n 1 "$2" 2>/dev/null)
    [ "$got" = "$3" ] && ok "$1" || bad "$1 (got: '$got' want: '$3')"
}
freeport() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }

echo "== syntax: the delivery scripts and the gate parse =="
for f in "$BIN/send-alert.sh" "$BIN/notify-alerts.sh" "$BIN/send-telegram.sh" \
         "$BIN/flush-digest.sh" "$BIN/tailscale-running.sh" "$GT/secure-bash-argv.sh"; do
    bash -n "$f" && ok "bash -n $(basename "$f")" || bad "bash -n $f"
    [ -x "$f" ] && ok "$(basename "$f") is executable" || bad "$f is not executable"
done

# --- stub Bot API: records every POST (path, chat_id, text) as one JSON line;
# --- the mode file switches it between 200 and 500.
REQLOG="$T/tg-requests.jsonl"; TGMODE="$T/tg-mode"; echo ok > "$TGMODE"
cat > "$T/stub-botapi.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

port, logp, modep = int(sys.argv[1]), sys.argv[2], sys.argv[3]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"ok": True, "result": "stub"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        q = parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)
        rec = {"path": self.path, "chat_id": (q.get("chat_id") or [""])[0],
               "text": (q.get("text") or [""])[0]}
        with open(logp, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            mode = open(modep).read().strip()
        except Exception:
            mode = "ok"
        if mode == "500":
            self._reply(500, {"ok": False, "error_code": 500, "description": "stub: Internal Server Error"})
        else:
            self._reply(200, {"ok": True, "result": {"message_id": 1}})


HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
TGPORT=$(freeport)
python3 "$T/stub-botapi.py" "$TGPORT" "$REQLOG" "$TGMODE" & TG_PID=$!
for _ in $(seq 30); do curl -s -m 1 "http://127.0.0.1:$TGPORT/" >/dev/null 2>&1 && break; sleep 0.2; done
TGENV=(TG_API="http://127.0.0.1:$TGPORT" TG_BOT_TOKEN="000000:FAKE-TOKEN" TG_CHAT_ID="123456789")
req_count() { [ -f "$REQLOG" ] && wc -l < "$REQLOG" | tr -d ' ' || echo 0; }
req_field() {  # <n (1-based)> <field>
    python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read().splitlines()[int(sys.argv[2])-1])[sys.argv[3]])' "$REQLOG" "$1" "$2"
}

echo "== send-telegram.sh (stub Bot API) =="
assert "missing TG_* -> 64 with the names on stdout" 64 "TG_BOT_TOKEN" -- \
    -u TG_BOT_TOKEN -u TG_CHAT_ID TG_API="http://127.0.0.1:$TGPORT" bash "$BIN/send-telegram.sh" "hello"
assert "only the chat id missing -> 64" 64 "TG_CHAT_ID" -- \
    -u TG_CHAT_ID TG_BOT_TOKEN="000000:FAKE-TOKEN" TG_API="http://127.0.0.1:$TGPORT" bash "$BIN/send-telegram.sh" "hello"
assert "no text -> 64" 64 "usage" -- "${TGENV[@]}" bash "$BIN/send-telegram.sh"
assert "whitespace-only text -> 64" 64 "empty" -- "${TGENV[@]}" bash "$BIN/send-telegram.sh" "   "
[ "$(req_count)" = 0 ] && ok "refusals never reach the API" || bad "refusals never reach the API ($(req_count) requests)"

: > "$REQLOG"; echo ok > "$TGMODE"
assert "happy path -> 0" 0 - -- "${TGENV[@]}" bash "$BIN/send-telegram.sh" "hello cranston"
[ "$(req_count)" = 1 ] && ok "one message, one request" || bad "one request (got $(req_count))"
[ "$(req_field 1 chat_id)" = "123456789" ] && ok "body carries chat_id" || bad "body carries chat_id ($(req_field 1 chat_id))"
[ "$(req_field 1 text)" = "hello cranston" ] && ok "body carries the text" || bad "body carries the text ($(req_field 1 text))"
[ "$(req_field 1 path)" = "/bot000000:FAKE-TOKEN/sendMessage" ] && ok "posts to /bot<token>/sendMessage" || bad "sendMessage path ($(req_field 1 path))"

: > "$REQLOG"
assert "two arguments arrive as two lines" 0 - -- "${TGENV[@]}" bash "$BIN/send-telegram.sh" "line one" "line two"
[ "$(req_field 1 text)" = "line one
line two" ] && ok "arguments joined with newlines" || bad "arguments joined with newlines ($(req_field 1 text))"

: > "$REQLOG"; echo 500 > "$TGMODE"
assert "HTTP 500 -> exit 1 (curl -f, not -s)" 1 - -- "${TGENV[@]}" bash "$BIN/send-telegram.sh" "page"
[ "$(req_count)" = 1 ] && ok "no retry without TG_API_IP" || bad "no retry without TG_API_IP ($(req_count) requests)"
: > "$REQLOG"
assert "HTTP 500 with TG_API_IP -> still exit 1" 1 - -- "${TGENV[@]}" TG_API_IP=127.0.0.1 bash "$BIN/send-telegram.sh" "page"
[ "$(req_count)" = 2 ] && ok "TG_API_IP retries exactly once" || bad "TG_API_IP retries exactly once ($(req_count) requests)"

: > "$REQLOG"; echo ok > "$TGMODE"
BIG=$(python3 -c 'print("\n".join("L%04d " % i + "x" * 23 for i in range(300)))')   # 300 lines x 29 chars = 8999 chars
assert "9000-char text -> 0" 0 - -- "${TGENV[@]}" bash "$BIN/send-telegram.sh" "$BIG"
R=$(BIG="$BIG" python3 - "$REQLOG" <<'PY'
import json, os, sys
texts = [json.loads(l)["text"] for l in open(sys.argv[1]).read().splitlines()]
if len(texts) != 3:
    print(f"want 3 messages, got {len(texts)}"); sys.exit()
if any(len(t.encode("utf-16-le")) // 2 > 4096 for t in texts):
    print("a chunk exceeds 4096 units: " + str([len(t) for t in texts])); sys.exit()
if "\n".join(texts) != os.environ["BIG"]:
    print("chunks do not rejoin to the original at line boundaries"); sys.exit()
print("OK")
PY
)
[ "$R" = OK ] && ok "arrives as 3 messages split at line boundaries" || bad "chunking ($R)"

: > "$REQLOG"
ONELINE=$(python3 -c 'print("y" * 5000)')
assert "a single 5000-char line -> 0" 0 - -- "${TGENV[@]}" bash "$BIN/send-telegram.sh" "$ONELINE"
R=$(python3 -c 'import json,sys; t=[json.loads(l)["text"] for l in open(sys.argv[1]).read().splitlines()]; print("OK" if len(t)==2 and "".join(t)=="y"*5000 and max(map(len,t))<=4096 else t and [len(x) for x in t])' "$REQLOG")
[ "$R" = OK ] && ok "an over-long line is hard-cut, nothing lost" || bad "hard cut ($R)"

echo "== notify-alerts.sh (recording stub sender, real send-alert.sh queue) =="
AF="$T/pending.json"
SENTLOG="$T/stub-sent.txt"
NLOG="$T/notify.log"
STUB="$T/stub-sender.sh"
cat > "$STUB" <<'EOF'
#!/bin/bash
# stub SELFHEAL_NOTIFY_CMD: record the message (last in STUB_LOG, every send
# appended to .all, the per-send TG_CHAT_ID to .chat); optionally append to
# the queue while "sending" (the engine paging mid-delivery) or fail.
printf '%s\n' "$1" > "${STUB_LOG:?}"
printf '%s\n' "$1" >> "${STUB_LOG}.all"
printf '%s\n' "${TG_CHAT_ID:-}" >> "${STUB_LOG}.chat"
echo x >> "${STUB_LOG}.count"
[ -n "${STUB_APPEND:-}" ] && bash "${SENDALERT:?}" "$STUB_APPEND"
[ -f "${STUB_FAIL:-/nonexistent}" ] && exit 1
exit 0
EOF
chmod +x "$STUB"
NA=(SELFHEAL_ALERT_FILE="$AF" SELFHEAL_NOTIFY_CMD="$STUB" STUB_LOG="$SENTLOG" SENDALERT="$BIN/send-alert.sh" SELFHEAL_LOG_FILE="$NLOG")
enqueue() { SELFHEAL_ALERT_FILE="$AF" bash "$BIN/send-alert.sh" "$@"; }
alerts_json() { python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get("alerts",[]), ensure_ascii=False))' "$AF" 2>/dev/null || echo "<absent>"; }
sends() { [ -f "$SENTLOG.count" ] && wc -l < "$SENTLOG.count" | tr -d ' ' || echo 0; }
reset_na() { rm -f "$AF" "$AF.sent.json" "$AF.lock" "$AF.tmp" "$SENTLOG" "$SENTLOG.count" "$SENTLOG.all" "$SENTLOG.chat" "$NLOG"; }
WARN=$(python3 -c 'print("⚠️", end="")')   # ⚠️ with the variation selector, as the engine emits it

reset_na
assert "nothing pending -> 0, silent" 0 - -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
[ "$(sends)" = 0 ] && ok "nothing pending -> no send" || bad "nothing pending -> no send"

reset_na
enqueue "✅ nas recovered (5m)." "$WARN navidrome DEGRADED (LAN_UNREACHABLE)" "🚨 home-assistant DOWN (SERVICE_DOWN)"
assert "3 mixed lines -> 0" 0 - -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
first_line_is "header is 🚨 when a 🚨 line is present" "$SENTLOG" "🚨 Cranston: 3 alerts"
[ "$(sed -n '2,4p' "$SENTLOG")" = "✅ nas recovered (5m).
$WARN navidrome DEGRADED (LAN_UNREACHABLE)
🚨 home-assistant DOWN (SERVICE_DOWN)" ] && ok "all lines follow the header in queue order" || bad "lines after header: $(cat "$SENTLOG")"
[ ! -f "$AF" ] && ok "fully delivered queue is deleted" || bad "fully delivered queue is deleted ($(alerts_json))"
grep -q "home-assistant DOWN" "$AF.sent.json" && ok "delivered text recorded in .sent.json" || bad "delivered text recorded in .sent.json"
grep -q "notify-alerts: sent via .*Cranston: 3 alerts" "$NLOG" && ok "log line per real action" || bad "log line per real action ($(cat "$NLOG" 2>/dev/null))"

reset_na; enqueue "✅ nas recovered (5m)."
assert "only ✅ -> 0" 0 - -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
first_line_is "header is ✅ when only a ✅ line is present" "$SENTLOG" "✅ Cranston: 1 alert"
reset_na; enqueue "✅ nas recovered (5m)." "$WARN navidrome DEGRADED (LAN_UNREACHABLE)"
env "${NA[@]}" bash "$BIN/notify-alerts.sh" >/dev/null 2>&1
first_line_is "header is ⚠️ for degraded + recovered" "$SENTLOG" "$WARN Cranston: 2 alerts"
reset_na; enqueue "🔧 Approved heal for nas: restart.sh completed, verified" "✅ nas recovered (5m)."
env "${NA[@]}" bash "$BIN/notify-alerts.sh" >/dev/null 2>&1
first_line_is "header is 🔧 for fix + recovered" "$SENTLOG" "🔧 Cranston: 2 alerts"
reset_na; enqueue "plain text line without an icon"
env "${NA[@]}" bash "$BIN/notify-alerts.sh" >/dev/null 2>&1
first_line_is "no icon -> plain header" "$SENTLOG" "Cranston: 1 alert"

reset_na; enqueue "🚨 a DOWN (first)"
assert "line appended mid-send -> 0" 0 - -- "${NA[@]}" STUB_APPEND="🚨 b DOWN (appended mid-send)" bash "$BIN/notify-alerts.sh"
[ "$(alerts_json)" = '["🚨 b DOWN (appended mid-send)"]' ] && ok "delivered line removed, mid-send line survives" || bad "mid-send survival (queue: $(alerts_json))"
grep -q "a DOWN (first)" "$AF.sent.json" && ! grep -q "appended mid-send" "$AF.sent.json" \
    && ok "only the delivered line is in the sent window" || bad "sent window after race: $(cat "$AF.sent.json")"
assert "next minute delivers the survivor" 0 - -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
first_line_is "survivor delivered with its own header" "$SENTLOG" "🚨 Cranston: 1 alert"
[ ! -f "$AF" ] && ok "queue empty after the second run" || bad "queue empty after the second run"

reset_na; enqueue "🚨 x DOWN (keep me)" "$WARN y DEGRADED (keep me too)"; cp "$AF" "$AF.before"; touch "$T/fail"
assert "failing sender -> exit 1" 1 - -- "${NA[@]}" STUB_FAIL="$T/fail" bash "$BIN/notify-alerts.sh"
cmp -s "$AF" "$AF.before" && ok "failed delivery leaves the queue byte-identical" || bad "queue changed on failure ($(alerts_json))"
grep -q "keep me" "$AF.sent.json" 2>/dev/null && bad "nothing enters the sent window on failure" || ok "nothing enters the sent window on failure"
grep -q "send FAILED rc=1" "$NLOG" && ok "failure is logged" || bad "failure is logged ($(cat "$NLOG" 2>/dev/null))"
rm -f "$T/fail" "$AF.before"

reset_na; enqueue "🚨 svc DOWN (X)"
env "${NA[@]}" bash "$BIN/notify-alerts.sh" >/dev/null 2>&1
enqueue "🚨 svc DOWN (X)"
assert "same text within 30 min -> 0" 0 - -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
[ "$(sends)" = 1 ] && ok "same text within the window is not re-sent" || bad "within-window repeat was sent ($(sends) sends)"
[ ! -f "$AF" ] && ok "within-window repeat is dropped from the queue" || bad "repeat left in queue ($(alerts_json))"
grep -q "dropped 1 within-window repeat" "$NLOG" && ok "drop is logged" || bad "drop is logged ($(cat "$NLOG"))"
python3 - "$AF.sent.json" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
p = sys.argv[1]
old = (datetime.now(timezone.utc) - timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
d = json.load(open(p))
json.dump({k: old for k in d}, open(p, "w"))
PY
enqueue "🚨 svc DOWN (X)"
assert "same text after the window -> 0" 0 - -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
[ "$(sends)" = 2 ] && ok "same text after the window is re-sent" || bad "after-window repeat not sent ($(sends) sends)"
grep -q "svc DOWN" "$AF.sent.json" && ok "stale window entry pruned and re-stamped" || bad "window re-stamped"
enqueue "🚨 svc DOWN (X)"
assert "window 0 -> 0" 0 - -- "${NA[@]}" SELFHEAL_SENT_WINDOW_MINUTES=0 bash "$BIN/notify-alerts.sh"
[ "$(sends)" = 3 ] && ok "SELFHEAL_SENT_WINDOW_MINUTES=0 disables the window" || bad "window 0 ($(sends) sends)"

reset_na; echo '{not json' > "$AF"
assert "corrupt queue -> exit 1, nothing sent" 1 "not valid JSON" -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
[ -f "$AF" ] && [ "$(sends)" = 0 ] && ok "corrupt queue is left in place" || bad "corrupt queue handling"

reset_na; : > "$REQLOG"; echo ok > "$TGMODE"; enqueue "🚨 default sender test"
assert "default sender (send-telegram.sh next to it) -> 0" 0 - -- \
    -u SELFHEAL_NOTIFY_CMD SELFHEAL_ALERT_FILE="$AF" "${TGENV[@]}" bash "$BIN/notify-alerts.sh"
[ "$(req_field 1 text)" = "🚨 Cranston: 1 alert
🚨 default sender test" ] && ok "default path posts header + lines to the Bot API" || bad "default path text: $(req_field 1 text)"
[ ! -f "$AF" ] && ok "default path deletes the delivered queue" || bad "default path deletes the delivered queue"
reset_na; : > "$REQLOG"; echo 500 > "$TGMODE"; enqueue "🚨 default sender, API down"
assert "default sender against HTTP 500 -> exit 1" 1 - -- \
    -u SELFHEAL_NOTIFY_CMD SELFHEAL_ALERT_FILE="$AF" "${TGENV[@]}" bash "$BIN/notify-alerts.sh"
[ "$(alerts_json)" = '["🚨 default sender, API down"]' ] && ok "queue kept when the Bot API fails" || bad "queue after API 500: $(alerts_json)"
echo ok > "$TGMODE"

echo "== announce routing (channel queue + drainer) =="
ann_enqueue() { SELFHEAL_ALERT_FILE="$AF" SELFHEAL_ALERT_CHANNEL=announce bash "$BIN/send-alert.sh" "$@"; }
chats() { tr '\n' ' ' < "$SENTLOG.chat" 2>/dev/null | sed 's/ $//'; }
ANN=("${NA[@]}" TG_CHAT_ID=111111 TG_ANNOUNCE_CHAT_ID=987654)

# (a) the queue entry format, and queue-side dedupe on the object form
reset_na
ann_enqueue "music will stop while the speaker reboots"
[ "$(alerts_json)" = '[{"text": "music will stop while the speaker reboots", "channel": "announce"}]' ] \
    && ok "announce line queues as the {text, channel} object" || bad "announce queue form: $(alerts_json)"
ann_enqueue "music will stop while the speaker reboots"
[ "$(alerts_json)" = '[{"text": "music will stop while the speaker reboots", "channel": "announce"}]' ] \
    && ok "identical announce line does not duplicate" || bad "announce dedupe: $(alerts_json)"

# (b) mixed queue + announce chat set -> TWO sends on two chat ids
reset_na
enqueue "🚨 nas DOWN (SERVICE_DOWN)"
ann_enqueue "music will stop while the speaker reboots"
assert "mixed queue, announce chat set -> 0" 0 - -- "${ANN[@]}" bash "$BIN/notify-alerts.sh"
[ "$(sends)" = 2 ] && ok "two sends: page then announce" || bad "two sends (got $(sends))"
[ "$(chats)" = "111111 987654" ] && ok "page to the admin chat, announce to the announce chat" || bad "chat routing: '$(chats)'"
grep -q "🚨 Cranston: 1 alert" "$SENTLOG.all" && grep -q "nas DOWN" "$SENTLOG.all" \
    && ok "page message keeps its alert-count header" || bad "page message: $(cat "$SENTLOG.all")"
first_line_is "announce message is the bare text" "$SENTLOG" "music will stop while the speaker reboots"
grep -q "Cranston:" "$SENTLOG" && bad "announce message has no alert header" || ok "announce message has no alert header"
[ ! -f "$AF" ] && ok "mixed queue fully delivered -> deleted" || bad "mixed queue after drain: $(alerts_json)"
grep -q "sent announce via" "$NLOG" && ok "announce send is logged" || bad "announce send is logged ($(cat "$NLOG" 2>/dev/null))"

# (c) same mixed queue, announce chat UNSET -> ONE folded send, nothing dropped
reset_na
enqueue "🚨 nas DOWN (SERVICE_DOWN)"
ann_enqueue "music will stop while the speaker reboots"
assert "mixed queue, announce chat unset -> 0" 0 - -- "${NA[@]}" bash "$BIN/notify-alerts.sh"
[ "$(sends)" = 1 ] && ok "one folded send without the announce chat" || bad "folded sends (got $(sends))"
first_line_is "folded header counts both" "$SENTLOG" "🚨 Cranston: 2 alerts"
grep -q "music will stop" "$SENTLOG" && ok "announce text folded into the page, not dropped" || bad "folded body: $(cat "$SENTLOG")"
[ ! -f "$AF" ] && ok "folded delivery removes the announce object too" || bad "queue after folded drain: $(alerts_json)"

# (d) legacy hand-written queue drains exactly as before
reset_na
echo '{"alerts":["old line"]}' > "$AF"
assert "legacy plain-string queue -> 0" 0 - -- "${ANN[@]}" bash "$BIN/notify-alerts.sh"
first_line_is "legacy line keeps the plain header" "$SENTLOG" "Cranston: 1 alert"
[ "$(sends)" = 1 ] && [ ! -f "$AF" ] && ok "legacy queue drains and deletes" || bad "legacy queue drain ($(sends) sends, queue $(alerts_json))"

# (e) end to end: the REAL send-telegram.sh against the stub Bot API
reset_na; : > "$REQLOG"; echo ok > "$TGMODE"
enqueue "🚨 nas DOWN (SERVICE_DOWN)"
ann_enqueue "music will stop while the speaker reboots"
assert "end-to-end via send-telegram.sh -> 0" 0 - -- \
    -u SELFHEAL_NOTIFY_CMD SELFHEAL_ALERT_FILE="$AF" "${TGENV[@]}" TG_ANNOUNCE_CHAT_ID=987654 bash "$BIN/notify-alerts.sh"
[ "$(req_count)" = 2 ] && ok "two Bot API requests" || bad "Bot API requests (got $(req_count))"
[ "$(req_field 1 chat_id)" = "123456789" ] && ok "page request carries the admin chat id" || bad "page chat_id: $(req_field 1 chat_id)"
[ "$(req_field 2 chat_id)" = "987654" ] && ok "announce request carries the announce chat id" || bad "announce chat_id: $(req_field 2 chat_id)"
[ "$(req_field 2 text)" = "music will stop while the speaker reboots" ] && ok "announce request is the bare text" || bad "announce text: $(req_field 2 text)"

# (f) the announce send FAILS after a delivered page: sibling stub that fails
# from the Nth call on, so call 1 (page) succeeds and call 2 (announce) fails
STUB2="$T/stub-sender-fail-from.sh"
cat > "$STUB2" <<'EOF'
#!/bin/bash
# like the main stub, but FAILS from call number STUB_FAIL_FROM (1-based) on
printf '%s\n' "$1" > "${STUB_LOG:?}"
printf '%s\n' "$1" >> "${STUB_LOG}.all"
printf '%s\n' "${TG_CHAT_ID:-}" >> "${STUB_LOG}.chat"
echo x >> "${STUB_LOG}.count"
n=$(wc -l < "${STUB_LOG}.count" | tr -d ' ')
[ "$n" -ge "${STUB_FAIL_FROM:-9999}" ] && exit 1
exit 0
EOF
chmod +x "$STUB2"
reset_na
enqueue "🚨 nas DOWN (SERVICE_DOWN)"
ann_enqueue "music will stop while the speaker reboots"
assert "announce send fails -> exit 1" 1 - -- "${ANN[@]}" SELFHEAL_NOTIFY_CMD="$STUB2" STUB_FAIL_FROM=2 bash "$BIN/notify-alerts.sh"
[ "$(alerts_json)" = '[{"text": "music will stop while the speaker reboots", "channel": "announce"}]' ] \
    && ok "delivered page removed, failed announce entry kept" || bad "queue after announce failure: $(alerts_json)"
grep -q "nas DOWN" "$AF.sent.json" && ! grep -q "music will stop" "$AF.sent.json" \
    && ok "only the delivered page text enters the sent window" || bad "sent window after announce failure: $(cat "$AF.sent.json")"
grep -q "announce send FAILED rc=1" "$NLOG" && ok "announce failure is logged" || bad "announce failure log: $(cat "$NLOG" 2>/dev/null)"
assert "next minute retries the announce alone -> 0" 0 - -- "${ANN[@]}" bash "$BIN/notify-alerts.sh"
first_line_is "retried announce is the bare text" "$SENTLOG" "music will stop while the speaker reboots"
[ "$(tail -n 1 "$SENTLOG.chat")" = "987654" ] && ok "retry goes to the announce chat" || bad "retry chat: $(tail -n 1 "$SENTLOG.chat")"
[ ! -f "$AF" ] && ok "queue empty after the announce retry" || bad "queue after announce retry: $(alerts_json)"

# (g) the sent window covers announce texts too
reset_na
ann_enqueue "🎵 music pausing for an update"
env "${ANN[@]}" bash "$BIN/notify-alerts.sh" >/dev/null 2>&1
ann_enqueue "🎵 music pausing for an update"
assert "announce repeat within the window -> 0" 0 - -- "${ANN[@]}" bash "$BIN/notify-alerts.sh"
[ "$(sends)" = 1 ] && ok "announce text within the window is not re-sent" || bad "announce window repeat ($(sends) sends)"
[ ! -f "$AF" ] && ok "within-window announce repeat dropped from the queue" || bad "announce repeat left: $(alerts_json)"
grep -q "dropped 1 within-window repeat" "$NLOG" && ok "announce drop is logged" || bad "announce drop log ($(cat "$NLOG"))"

echo "== flush-digest.sh (recording stub sender) =="
DIG="$T/digest.jsonl"
FDLOG="$T/flush.log"
# SENDALERT stand-in for the digest: STUB_APPEND must land a JSONL line in the
# DIGEST file (the engine appending mid-send), not in the alert queue
DIGAPPEND="$T/digest-append.sh"
cat > "$DIGAPPEND" <<'EOF'
#!/bin/bash
printf '{"system": "race", "message": "%s", "timestamp": "2026-10-04T09:30:00Z"}\n' "$1" >> "${DIG_FILE:?}"
EOF
chmod +x "$DIGAPPEND"
FD=(SELFHEAL_DIGEST_FILE="$DIG" SELFHEAL_NOTIFY_CMD="$STUB" STUB_LOG="$SENTLOG" SENDALERT="$DIGAPPEND" DIG_FILE="$DIG" SELFHEAL_LOG_FILE="$FDLOG")
dig_entry() { printf '{"system": "%s", "message": "%s", "timestamp": "%s"}\n' "$1" "$2" "$3" >> "$DIG"; }
reset_fd() { rm -f "$DIG" "$DIG.lock" "$DIG.before" "$SENTLOG" "$SENTLOG.count" "$SENTLOG.all" "$SENTLOG.chat" "$FDLOG"; }

reset_fd
assert "missing digest -> 0, silent" 0 - -- "${FD[@]}" bash "$BIN/flush-digest.sh"
[ "$(sends)" = 0 ] && ok "missing digest -> no send" || bad "missing digest sent ($(sends))"
: > "$DIG"
assert "empty digest -> 0, silent" 0 - -- "${FD[@]}" bash "$BIN/flush-digest.sh"
[ "$(sends)" = 0 ] && ok "empty digest -> no send" || bad "empty digest sent ($(sends))"

reset_fd
dig_entry nas "disk 91% full" "2026-10-04T08:00:00Z"
dig_entry nas "disk 92% full" "2026-10-04T09:00:00Z"
dig_entry navidrome "LAN path flapping" "2026-10-04T08:30:00Z"
assert "3 entries / 2 systems -> 0" 0 - -- "${FD[@]}" bash "$BIN/flush-digest.sh"
[ "$(sends)" = 1 ] && ok "one send for the whole digest" || bad "digest sends (got $(sends))"
first_line_is "fallback header" "$SENTLOG" "⚠️ Daily issues digest (summary didn't run - fallback sender):"
grep -q "^nas:$" "$SENTLOG" && grep -q "^navidrome:$" "$SENTLOG" && ok "grouped by system" || bad "grouping: $(cat "$SENTLOG")"
grep -q "^  • 2026-10-04 08:00: disk 91% full$" "$SENTLOG" && ok "bullet carries timestamp + message" || bad "bullets: $(cat "$SENTLOG")"
[ ! -s "$DIG" ] && ok "sent digest is cleared" || bad "digest after send: $(cat "$DIG")"
grep -q "sent fallback digest" "$FDLOG" && ok "flush is logged" || bad "flush log: $(cat "$FDLOG" 2>/dev/null)"

reset_fd
dig_entry nas "disk 91% full" "2026-10-04T08:00:00Z"
assert "mid-send append -> 0" 0 - -- "${FD[@]}" STUB_APPEND="engine wrote me mid-send" bash "$BIN/flush-digest.sh"
grep -q "engine wrote me mid-send" "$DIG" && ok "mid-send entry survives in the file" || bad "mid-send entry lost: $(cat "$DIG")"
grep -q "disk 91% full" "$DIG" && bad "sent line removed despite the race" || ok "sent line removed despite the race"
assert "second run delivers the survivor" 0 - -- "${FD[@]}" bash "$BIN/flush-digest.sh"
grep -q "engine wrote me mid-send" "$SENTLOG" && ok "survivor delivered on the next run" || bad "survivor message: $(cat "$SENTLOG")"
[ ! -s "$DIG" ] && ok "digest empty after the second run" || bad "digest after second run: $(cat "$DIG")"

reset_fd
dig_entry nas "disk 91% full" "2026-10-04T08:00:00Z"
dig_entry navidrome "LAN path flapping" "2026-10-04T08:30:00Z"
cp "$DIG" "$DIG.before"; touch "$T/fail"
assert "failing sender -> exit 1, digest kept" 1 - -- "${FD[@]}" STUB_FAIL="$T/fail" bash "$BIN/flush-digest.sh"
cmp -s "$DIG" "$DIG.before" && ok "failed flush leaves the digest byte-identical" || bad "digest changed on failure"
grep -q "send FAILED" "$FDLOG" && ok "flush failure is logged" || bad "flush failure log: $(cat "$FDLOG" 2>/dev/null)"
rm -f "$T/fail" "$DIG.before"

reset_fd
dig_entry nas "disk 91% full" "2026-10-04T08:00:00Z"
echo 'this line is not json {' >> "$DIG"
dig_entry navidrome "LAN path flapping" "2026-10-04T08:30:00Z"
assert "unparseable line mixed in -> 0" 0 - -- "${FD[@]}" bash "$BIN/flush-digest.sh"
grep -q "not json" "$SENTLOG" && bad "message omits the unparseable line" || ok "message omits the unparseable line"
grep -q "disk 91% full" "$SENTLOG" && grep -q "LAN path flapping" "$SENTLOG" && ok "valid entries still delivered" || bad "valid entries: $(cat "$SENTLOG")"
grep -qF 'this line is not json {' "$DIG" && ok "unparseable line is kept in the file" || bad "unparseable line gone: $(cat "$DIG")"
grep -q "disk 91% full" "$DIG" && bad "delivered entries removed around the bad line" || ok "delivered entries removed around the bad line"

# pinned current behavior: clearing matches on message TEXT, so twins with
# different timestamps are BOTH cleared by the one send (the dedupe-by-message
# gotcha documented in flush-digest.sh)
reset_fd
dig_entry nas "disk 91% full" "2026-10-04T08:00:00Z"
dig_entry nas "disk 91% full" "2026-10-04T10:00:00Z"
assert "identical messages, two timestamps -> 0" 0 - -- "${FD[@]}" bash "$BIN/flush-digest.sh"
[ "$(sends)" = 1 ] && ok "one send covers both twins" || bad "twin sends (got $(sends))"
[ ! -s "$DIG" ] && ok "BOTH twins cleared by one send (dedupe-by-text, pinned)" || bad "twins left: $(cat "$DIG")"

echo "== tailscale-running.sh (stub tailscale on PATH) =="
TSBIN="$T/tsbin"; mkdir -p "$TSBIN"
cat > "$TSBIN/tailscale" <<'EOF'
#!/bin/bash
case "${TS_STATE:-Running}" in
    garbage) echo "not json at all" ;;
    error)   echo "failed to connect to local tailscaled" >&2; exit 1 ;;
    *)       printf '{"Version": "1.0.0", "BackendState": "%s", "Self": {"HostName": "stub"}}\n' "$TS_STATE" ;;
esac
EOF
chmod +x "$TSBIN/tailscale"
assert "BackendState Running -> 0" 0 - -- PATH="$TSBIN:$PATH" TS_STATE=Running bash "$BIN/tailscale-running.sh"
assert "BackendState Stopped -> 1" 1 - -- PATH="$TSBIN:$PATH" TS_STATE=Stopped bash "$BIN/tailscale-running.sh"
assert "BackendState NeedsLogin -> 1" 1 - -- PATH="$TSBIN:$PATH" TS_STATE=NeedsLogin bash "$BIN/tailscale-running.sh"
assert "daemon unreachable (stderr, exit 1) -> 1" 1 - -- PATH="$TSBIN:$PATH" TS_STATE=error bash "$BIN/tailscale-running.sh"
assert "non-JSON output -> 1" 1 - -- PATH="$TSBIN:$PATH" TS_STATE=garbage bash "$BIN/tailscale-running.sh"
NOPATH="$T/nopath"; mkdir -p "$NOPATH"   # python3 and bash only - no tailscale anywhere on PATH
printf '#!/bin/bash\nexec %s "$@"\n' "$(command -v python3)" > "$NOPATH/python3"
printf '#!/bin/bash\nexec %s "$@"\n' "$(command -v bash)" > "$NOPATH/bash"
chmod +x "$NOPATH/python3" "$NOPATH/bash"
assert "tailscale not installed -> 1" 1 - -- PATH="$NOPATH" bash "$BIN/tailscale-running.sh"

echo "== gates/secure-bash-argv.sh =="
GATE="$GT/secure-bash-argv.sh"
FAKEROOT="$T/fakeroot"; mkdir -p "$FAKEROOT/remediations"
MARK="$T/gate-exec-marker"
FIXSH="$FAKEROOT/remediations/fix.sh"
printf '#!/bin/bash\necho "ran $*" > "%s"\n' "$MARK" > "$FIXSH"
export SELFHEAL_AUDIT_LOG="$T/audit-gate.log"
SBLOG="$T/secure-bash.log"
SB="$T/stub-secure-bash"
cat > "$SB" <<'EOF'
#!/bin/bash
# the deployment's 2FA wrapper, stubbed: records argc, the ONE string, GATE_CODE
printf '%s\n' "$#" "${1:-}" "${GATE_CODE:-<none>}" > "${SB_LOG:?}"
EOF
chmod +x "$SB"
SBENV="$T/secure-bash.env"; printf 'SECURE_BASH="%s"\n' "$SB" > "$SBENV"; chmod 600 "$SBENV"
HUMAN=(SELFHEAL_ROOT="$FAKEROOT" GATE_SECURE_BASH_ENV="$SBENV" SB_LOG="$SBLOG")
AUTO=(SELFHEAL_AUTOMATION=true SELFHEAL_ROOT="$FAKEROOT" GATE_SECURE_BASH_ENV="$SBENV" SB_LOG="$SBLOG")

rm -f "$MARK" "$SBLOG"
assert "auto path, script under remediations/ -> 0" 0 - -- "${AUTO[@]}" bash "$GATE" "$FIXSH" db-1
grep -q "ran db-1" "$MARK" 2>/dev/null && ok "auto path execs the remediation with its argument" || bad "auto path exec"
[ ! -f "$SBLOG" ] && ok "auto path never consults the wrapper" || bad "auto path consulted the wrapper"
grep -q "GATE-AUTO-PASS $FIXSH db-1" "$SELFHEAL_AUDIT_LOG" && ok "auto pass audited with the full path" || bad "auto pass audit"

rm -f "$MARK" "$SBLOG"
assert "human path -> wrapper runs (0)" 0 - -- "${HUMAN[@]}" GATE_CODE=424242 bash "$GATE" "$FIXSH" db-1
[ "$(sed -n 1p "$SBLOG" 2>/dev/null)" = "1" ] && ok "wrapper received exactly ONE argument" || bad "wrapper argc: $(sed -n 1p "$SBLOG" 2>/dev/null)"
[ "$(sed -n 2p "$SBLOG" 2>/dev/null)" = "bash $FIXSH db-1" ] && ok "the string is exactly 'bash <fullpath> db-1'" || bad "wrapper string: '$(sed -n 2p "$SBLOG" 2>/dev/null)'"
[ "$(sed -n 3p "$SBLOG" 2>/dev/null)" = "424242" ] && ok "GATE_CODE passes through to the wrapper" || bad "GATE_CODE: $(sed -n 3p "$SBLOG" 2>/dev/null)"
[ ! -f "$MARK" ] && ok "human path does not exec the remediation itself" || bad "gate ran the remediation around the wrapper"
grep -q "GATE-FORWARDED $FIXSH db-1 via=$SB" "$SELFHEAL_AUDIT_LOG" && ok "forwarding audited with full path and wrapper" || bad "GATE-FORWARDED audit"
rm -f "$SBLOG"
assert "human path, no argument -> 0" 0 - -- "${HUMAN[@]}" bash "$GATE" "$FIXSH"
[ "$(sed -n 2p "$SBLOG" 2>/dev/null)" = "bash $FIXSH" ] && ok "no argument -> no trailing space in the string" || bad "no-arg string: '$(sed -n 2p "$SBLOG" 2>/dev/null)'"
[ "$(sed -n 3p "$SBLOG" 2>/dev/null)" = "<none>" ] && ok "no GATE_CODE -> none passed" || bad "GATE_CODE leaked: $(sed -n 3p "$SBLOG")"
rm -f "$SBLOG"
( cd "$FAKEROOT" && env "${HUMAN[@]}" bash "$GATE" remediations/fix.sh rel-1 >/dev/null 2>&1 )
[ "$(sed -n 2p "$SBLOG" 2>/dev/null)" = "bash $FIXSH rel-1" ] && ok "relative script path is forwarded resolved" || bad "relative path string: '$(sed -n 2p "$SBLOG" 2>/dev/null)'"

echo "-- refusals (all 65, nothing runs) --"
rm -f "$MARK" "$SBLOG"
assert "3 arguments -> 65" 65 "got 3 arguments" -- "${HUMAN[@]}" bash "$GATE" "$FIXSH" db-1 extra
assert "0 arguments -> 65" 65 "got 0 arguments" -- "${HUMAN[@]}" bash "$GATE"
assert "arg '-rf' -> 65 malformed" 65 "malformed" -- "${HUMAN[@]}" bash "$GATE" "$FIXSH" -rf
assert "arg 'a b' -> 65 malformed" 65 "malformed" -- "${HUMAN[@]}" bash "$GATE" "$FIXSH" "a b"
assert "arg 'x;id' -> 65 malformed" 65 "malformed" -- "${HUMAN[@]}" bash "$GATE" "$FIXSH" "x;id"
assert "arg with a newline -> 65 malformed" 65 "malformed" -- "${HUMAN[@]}" bash "$GATE" "$FIXSH" "db-1
GATE-FORWARDED forged"
assert "arg 'a b' on the auto path -> 65 malformed" 65 "malformed" -- "${AUTO[@]}" bash "$GATE" "$FIXSH" "a b"
mkdir -p "$T/elsewhere"; cp "$FIXSH" "$T/elsewhere/fix.sh"
assert "script outside remediations/ (human) -> 65" 65 "remediations/" -- "${HUMAN[@]}" bash "$GATE" "$T/elsewhere/fix.sh"
assert "script outside remediations/ (auto) -> 65, not forwarded" 65 "remediations/" -- "${AUTO[@]}" bash "$GATE" "$T/elsewhere/fix.sh"
assert "SELFHEAL_ROOT unset (human) -> 65" 65 "SELFHEAL_ROOT=unset" -- -u SELFHEAL_ROOT GATE_SECURE_BASH_ENV="$SBENV" SB_LOG="$SBLOG" bash "$GATE" "$FIXSH"
assert "missing script -> 65" 65 "no such remediation" -- "${HUMAN[@]}" bash "$GATE" "$FAKEROOT/remediations/absent.sh"
assert "missing env file -> 65" 65 "env file missing" -- SELFHEAL_ROOT="$FAKEROOT" GATE_SECURE_BASH_ENV="$T/absent.env" SB_LOG="$SBLOG" bash "$GATE" "$FIXSH"
chmod 644 "$SBENV"
assert "env file mode 644 -> 65" 65 "mode 600" -- "${HUMAN[@]}" bash "$GATE" "$FIXSH"
chmod 600 "$SBENV"
EMPTYENV="$T/empty.env"; : > "$EMPTYENV"; chmod 600 "$EMPTYENV"
assert "SECURE_BASH unset in the env file -> 65" 65 "SECURE_BASH not set" -- SELFHEAL_ROOT="$FAKEROOT" GATE_SECURE_BASH_ENV="$EMPTYENV" bash "$GATE" "$FIXSH"
NOEXEC="$T/noexec.env"; printf 'SECURE_BASH="%s"\n' "$T/not-executable" > "$NOEXEC"; chmod 600 "$NOEXEC"; : > "$T/not-executable"
assert "SECURE_BASH not executable -> 65" 65 "not an executable" -- SELFHEAL_ROOT="$FAKEROOT" GATE_SECURE_BASH_ENV="$NOEXEC" bash "$GATE" "$FIXSH"
mkdir -p "$FAKEROOT/remediations/sub dir"; cp "$FIXSH" "$FAKEROOT/remediations/sub dir/fix.sh"
assert "path with a space (human) -> 65, never composed" 65 "unsafe" -- "${HUMAN[@]}" bash "$GATE" "$FAKEROOT/remediations/sub dir/fix.sh"
rm -f "$MARK"
assert "path with a space (auto, argv) -> 0" 0 - -- "${AUTO[@]}" bash "$GATE" "$FAKEROOT/remediations/sub dir/fix.sh" sp-1
grep -q "ran sp-1" "$MARK" 2>/dev/null && ok "argv exec is unaffected by spaces in the path" || bad "auto path with spaced path"
[ ! -f "$SBLOG" ] && ok "no refusal reached the wrapper" || bad "a refusal reached the wrapper: $(cat "$SBLOG")"
grep -q "^GATE-FORWARDED forged" "$SELFHEAL_AUDIT_LOG" && bad "refused newline arg cannot forge an audit line" || ok "refused newline arg cannot forge an audit line"
grep -c "GATE-REFUSED" "$SELFHEAL_AUDIT_LOG" | grep -q "^1[0-9]$" && ok "every refusal is audited" || bad "refusal audit count: $(grep -c GATE-REFUSED "$SELFHEAL_AUDIT_LOG")"

kill $TG_PID 2>/dev/null; wait $TG_PID 2>/dev/null
rm -rf "$T"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] || exit 1
