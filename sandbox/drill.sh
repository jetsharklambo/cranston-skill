#!/bin/bash
# drill.sh [--full] - scripted forced-failure drills against the sandbox
# (SKILL.md "Verification"). Each scenario breaks the fake LAN, runs engine
# cycles, and asserts on what the admin would see (state/outbox.log), the
# state machine (state.json / pending-approvals.json) and the audit log.
# --full also waits out the 1-minute recovery hold to see the held ✅ release.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/.run/env.sh" 2>/dev/null || { echo "run sandbox/up.sh first"; exit 2; }
FULL=0; [ "${1:-}" = "--full" ] && FULL=1
ST="$DEPLOY/state"; OUT="$ST/outbox.log"
pass=0; fail=0

cycle()   { python3 "$DEPLOY/engine/selfheal.py" --once 2>&1 | sed 's/^/    engine| /'; }
status()  { python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['keys'].get(sys.argv[2],{}).get('status'))" "$ST/state.json" "$1"; }
pending() { python3 -c "import json,sys; print(sys.argv[2] in json.load(open(sys.argv[1])))" "$ST/pending-approvals.json" "$1"; }
fresh()   { : > "$OUT"; }
ok()      { if eval "$2"; then echo "  PASS $1"; pass=$((pass+1)); else echo "  FAIL $1"; fail=$((fail+1)); fi; }
paged()   { grep -qF -- "$1" "$OUT" 2>/dev/null; }
silent()  { [ ! -s "$OUT" ]; }
scene()   { echo; echo "### $*"; fresh; }

scene "1. all healthy"
# clean slate: the engine remembers held recoveries and realert windows (either
# would mute the pages the later scenes assert on), and each remediation counts
# its own EXEC lines in audit.log and refuses past 3 in 6h - right for a home,
# wrong for a drill that restarts the same service every minute
rm -f "$ST/state.json" "$ST/pending-approvals.json" "$ST/digest.jsonl" "$ST/audit.log"
svcctl start media-server; svcctl start nas
svcctl plug 192.168.1.1; svcctl plug 192.168.1.58; svcctl plug 127.0.0.1
fresh
cycle
ok "no pages"                          'silent'
ok "state.json written, no failing keys" '[ -f "$ST/state.json" ] && [ "$(status media-server)" = None ]'

scene "2. AUTO class: media-server process dies -> restarted unasked"
svcctl stop media-server
cycle
ok "first bad cycle is absorbed (anti-flap)" 'silent'
cycle
ok "🔧 auto-remediated page"           'paged "🔧 media-server: SERVICE_DOWN"'
ok "service is running again"          'svcctl status media-server >/dev/null'
ok "audit log has EXEC + RESULT ok"    'grep -q "EXEC service=media-server" "$ST/audit.log" && grep -q "RESULT service=media-server status=ok" "$ST/audit.log"'

scene "3. ASK-FIRST class: nas port closes -> page, wait for 'heal nas'"
svcctl stop nas
cycle; cycle
ok "🚨 page proposes the fix"           "paged \"Reply 'heal nas' to approve\""
ok "consent heads-up included"         'paged "give them a heads-up"'
ok "pending approval recorded"         '[ "$(pending nas)" = True ]'
cycle
ok "engine did NOT fix it on its own"  '! svcctl status nas >/dev/null'
echo "  -> admin replies 'heal nas'; agent runs approve-heal.py"
python3 "$DEPLOY/engine/approve-heal.py" nas | tee "$ST/approve.out" | sed 's/^/    approve| /'
ok "approve-heal reports verified fix" 'grep -q "Approved heal for nas" "$ST/approve.out"'
ok "nas running"                       'svcctl status nas >/dev/null'
ok "pending cleared"                   '[ "$(pending nas)" = False ]'

scene "4. NETWORK GATE: router unplugged -> remote services unknowable, not down"
svcctl unplug 192.168.1.1
svcctl stop media-server
before=$(python3 -c "import json; print(json.load(open('$ST/state.json'))['keys']['media-server']['consecutive_failures'])")
cycle | tee "$ST/net.out"; cycle; cycle
ok "logged LOCAL NETWORK DOWN"         'grep -q "LOCAL NETWORK DOWN" "$ST/net.out"'
ok "no pages during the outage"        'silent'
ok "no failure counted for media-server" '[ "$(python3 -c "import json; print(json.load(open(\"$ST/state.json\"))[\"keys\"][\"media-server\"][\"consecutive_failures\"])")" = "$before" ]'
svcctl plug 192.168.1.1; svcctl start media-server; cycle

scene "5. WATCH-ONLY: media-server's host unplugged -> page, no fix proposed"
svcctl unplug 192.168.1.58
svcctl stop media-server
cycle; cycle
ok "🚨 HOST_DOWN page"                  'paged "🚨 media-server DOWN — layer: host"'
ok "says no safe automatic fix"        'paged "No safe automatic fix known"'
ok "engine did not touch it"           '! svcctl status media-server >/dev/null'
fresh
svcctl plug 192.168.1.58; svcctl start media-server
cycle
ok "recovery ✅ is HELD (not paged yet)" 'silent'
if [ "$FULL" = 1 ]; then
    echo "  waiting 62s for the 1-minute recovery hold..."; sleep 62
    cycle
    ok "held ✅ released"               'paged "✅ media-server recovered (HOST_DOWN)"'
else
    echo "  (skip: run with --full to wait out the hold and see the ✅)"
fi

echo
echo "== $pass passed, $fail failed"
echo "   outbox: $OUT   audit: $ST/audit.log   state: $ST/state.json"
[ "$fail" = 0 ]
