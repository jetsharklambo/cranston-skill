#!/bin/bash
# interview-drill.sh [--keep] - the per-device interview against the sandbox.
#
# Starts from a HAND-WRITTEN config with gaps (the sandbox config minus every
# remediation decision), lets the interview rank the devices and ask, fills
# the config, then proves the filled config drives the engine the way the
# answers said - including a per-device re-interview and a hand edit the
# interview must not touch. Restores the full sandbox config at the end so
# drill.sh keeps working; --keep leaves the interview-filled config and
# interview.json in place for poking.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/.run/env.sh" 2>/dev/null || { echo "run sandbox/up.sh first"; exit 2; }
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
ST="$DEPLOY/state"; OUT="$ST/outbox.log"; CFG="$DEPLOY/services.json"
pass=0; fail=0

I()       { python3 "$DEPLOY/onboard/interview.py" "$@"; }
cycle()   { python3 "$DEPLOY/engine/selfheal.py" --once 2>&1 | sed 's/^/    engine| /'; }
ok()      { if eval "$2"; then echo "  PASS $1"; pass=$((pass+1)); else echo "  FAIL $1"; fail=$((fail+1)); fi; }
paged()   { grep -qF -- "$1" "$OUT" 2>/dev/null; }
scene()   { echo; echo "### $*"; }
# a clean slate also drops audit.log: each remediation counts its own EXEC
# lines there and refuses past 3 in 6h (exit 75) - real, and right, but a
# drill restarts the same two services far more often than a real home does
clean()   { rm -f "$ST/state.json" "$ST/pending-approvals.json" "$ST/digest.jsonl" "$ST/audit.log"; : > "$OUT"; }
field()   { python3 - "$CFG" "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); s = next(x for x in d["services"] if x["name"] == sys.argv[2])
v = s
for k in sys.argv[3].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print(json.dumps(v))
PY
}

cp "$CFG" "$DEPLOY/services.full.json"
restore() {
    if [ "$KEEP" = 1 ]; then
        echo "   --keep: interview-filled config and $DEPLOY/interview.json left in place (bash sandbox/up.sh resets)"
    else
        cp "$DEPLOY/services.full.json" "$CFG"; rm -f "$DEPLOY/interview.json"
    fi
    rm -f "$DEPLOY/services.full.json"
    # leave the sandbox as up.sh does: services running, hosts plugged, no
    # engine state (a held recovery or a realert window would mute drill.sh)
    svcctl start media-server; svcctl start nas
    svcctl plug 192.168.1.1; svcctl plug 192.168.1.58; svcctl plug 127.0.0.1
    clean
}
trap restore EXIT

scene "0. a hand-written config with gaps: no remediation decisions anywhere"
python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
for s in d["services"]:
    for k in ("remediations", "_interview", "consent_notes", "realert_minutes_by_code", "notify_by_code"):
        s.pop(k, None)
json.dump(d, open(p, "w"), indent=2)
PY
cp "$HERE/overlay/remediations/sandbox-restart.sh" "$DEPLOY/remediations/"
rm -f "$DEPLOY/interview.json" "$DEPLOY"/services.draft.json "$DEPLOY"/services.json.backup-*
svcctl start media-server; svcctl start nas; clean; cycle >/dev/null
ok "no decisions in the config"           '[ "$(field nas remediations)" = null ] && [ "$(field media-server remediations)" = null ]'

scene "1. plan: every device ranked, with reasons"
I plan | tee "$ST/plan.out"
ok "media-server and nas need a class decision" 'grep -q "no fix/ask/tell decision for API_ERROR, SERVICE_DOWN" "$ST/plan.out" && grep -q "no fix/ask/tell decision for PORT_CLOSED" "$ST/plan.out"'
ok "box-disk only gets the nag question"        'grep -q "box-disk .*open: nag$" "$ST/plan.out"'

scene "2. live trouble jumps the queue: nas goes down"
svcctl stop nas; cycle >/dev/null; cycle >/dev/null
I plan | tee "$ST/plan.out"
ok "nas ranks first"                             'sed -n 2p "$ST/plan.out" | grep -q " nas "'
ok "because it is failing now"                   'grep -q "failing now (PORT_CLOSED)" "$ST/plan.out"'
I next | tee "$ST/next.out"
ok "next question is nas:class, naming its port" 'grep -q "\[nas:class\]" "$ST/next.out" && grep -q "127.0.0.1:18445" "$ST/next.out"'

scene "3. the admin answers, one device at a time, in their own words"
I answer nas class "ask — it's the family NAS, don't surprise anyone"
I answer nas how "script:remediations/sandbox-restart.sh"
I answer nas host "none"
I answer nas consent "household — everyone's photos live on it"
I answer nas drill "ok"
I answer media-server class "fix"
I answer media-server how "script:remediations/sandbox-restart.sh"
I answer media-server host "none"
I answer media-server drill "freely"
I answer media-server nag "daily"
I answer box-disk nag "digest"
I plan | tee "$ST/plan.out"
ok "nothing left to ask"                         'grep -q "nothing to ask" "$ST/plan.out"'

scene "4. fill: a draft first, services.json only with --apply"
cp "$CFG" "$ST/before.json"
I fill | tee "$ST/fill.out"
ok "services.json untouched by a plain fill"     'cmp -s "$CFG" "$ST/before.json"'
ok "draft written next to it"                    '[ -f "$DEPLOY/services.draft.json" ]'
I fill --apply | tail -2
ok "backup kept"                                 'ls "$DEPLOY"/services.json.backup-* >/dev/null 2>&1'
ok "nas PORT_CLOSED: ask-first, household consent" '[ "$(field nas remediations.PORT_CLOSED)" = "{\"ask\": \"remediations/sandbox-restart.sh\", \"consent\": \"household\"}" ]'
ok "media-server SERVICE_DOWN: auto"             '[ "$(field media-server remediations.SERVICE_DOWN)" = "\"remediations/sandbox-restart.sh\"" ]'
ok "HOST_DOWN tell-only on both"                 '[ "$(field nas remediations.HOST_DOWN)" = null ] && [ "$(field media-server remediations.HOST_DOWN)" = null ]'
ok "media-server chronic codes nag daily"        '[ "$(field media-server realert_minutes_by_code.LAN_UNREACHABLE)" = 1440 ]'
ok "box-disk LOW_SPACE routes to the digest"     '[ "$(field box-disk notify_by_code.LOW_SPACE)" = "\"digest\"" ]'
ok "consent_notes quotes the admin"              "[ \"\$(field nas consent_notes)\" = '\"everyone'\"'\"'s photos live on it\"' ]"

scene "5. the filled config drives the engine the way the answers said"
clean; svcctl start nas; svcctl start media-server; cycle >/dev/null
svcctl stop nas; svcctl stop media-server
cycle; cycle
ok "media-server (fix) was auto-remediated"      'paged "🔧 media-server is back" && svcctl status media-server >/dev/null'
ok "nas (ask) paged with the heads-up and waited" "paged \"Reply 'heal nas'\" && paged \"heads-up when it runs\" && ! svcctl status nas >/dev/null"
python3 "$DEPLOY/engine/approve-heal.py" nas | sed 's/^/    approve| /'
ok "approve-heal fixed nas"                      'svcctl status nas >/dev/null'

scene "6. re-interview ONE device: nas becomes tell-only"
I reask nas | tee "$ST/reask.out"
ok "nas's class question is open again"          'grep -q "open again: class" "$ST/reask.out"'
I plan | tee "$ST/plan.out"
ok "media-server is not re-asked"                '! grep -q "^ *[0-9]*\. media-server" "$ST/plan.out"'
I answer nas class "tell — I'll handle it myself"
I fill --apply | tee "$ST/fill.out"
ok "the earlier ask entry was replaced (~)"      'grep -q "~ remediations.PORT_CLOSED" "$ST/fill.out"'
ok "nas PORT_CLOSED is now null"                 '[ "$(field nas remediations.PORT_CLOSED)" = null ]'
clean; cycle >/dev/null; svcctl stop nas; cycle; cycle
ok "the engine now only tells"                   'paged "no fix I can safely run"'
svcctl start nas

scene "7. manual config wins: a hand edit survives --overwrite"
python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
s = next(x for x in d["services"] if x["name"] == "media-server")
s["realert_minutes_by_code"]["LAN_UNREACHABLE"] = 720      # the admin changed their mind by hand
json.dump(d, open(p, "w"), indent=2)
PY
I answer media-server nag "hourly"
I fill --device media-server --overwrite --apply | tee "$ST/fill.out"
ok "hand-set 720 kept, and said so"              '[ "$(field media-server realert_minutes_by_code.LAN_UNREACHABLE)" = 720 ] && grep -q "hand-set" "$ST/fill.out"'
ok "the interview's own fill revised to 60"      '[ "$(field media-server realert_minutes_by_code.TRANSPORT_BLIND)" = 60 ]'

scene "8. doctrine draft quotes the admin"
I doctrine --out "$ST/doctrine.draft.md" >/dev/null
ok "class table and verbatim notes"              'grep -q "| media-server | auto |" "$ST/doctrine.draft.md" && grep -q "handle it myself" "$ST/doctrine.draft.md"'

echo
echo "== $pass passed, $fail failed"
echo "   doctrine: $ST/doctrine.draft.md"
[ "$fail" = 0 ]
