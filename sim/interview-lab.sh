#!/bin/bash
# interview-lab.sh [--no-soak] [--run DIR] - the interview against a populated
# home it has never seen.
#
# Builds the casa family home (sim/casa/up-casa.sh: Pi-hole, TrueNAS SMB +
# pool + cert, Plex on two transports, this box's disk), then throws the casa
# services.json away: the deployment starts with NO services. Every device
# enters the config through onboard/interview.py alone - `add`, then one
# question at a time, answered in Dana's words (sim/cloud/persona-admin.md) -
# and `fill --apply` is the only writer. Then it shows how that came out:
#
#   1. the full Q&A transcript                 $ART/interview-transcript.md
#   2. the interview-built services.json       $ART/interview-built.services.json
#      diffed field by field against the hand-written sim/casa/overlay config
#   3. sim/cloud/interview-check.py            Dana's stances, graded
#   4. the doctrine draft                      $ART/doctrine-draft.md
#   5. the 90-day soak + gates G1-G10          run with --services, i.e. the
#      real engine and delivery chain driven by the interview-built config
#
# Nothing here touches the repository; everything lives under sim/.run/.
# Runs must not overlap with sim/soak.py (fixed loopback ports, one hosts dir).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RUN="$HERE/.run/lab"
ART="$HERE/.run/lab-artifacts"     # the soak step rebuilds $RUN; what the lab shows lives here
SOAK=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-soak) SOAK=0 ;;
        --run) RUN="$2"; shift ;;
        *) echo "usage: interview-lab.sh [--no-soak] [--run DIR]" >&2; exit 64 ;;
    esac
    shift
done
pass=0; fail=0
ok() { if eval "$2"; then echo "  PASS $1"; pass=$((pass+1)); else echo "  FAIL $1"; fail=$((fail+1)); fi; }
scene() { echo; echo "### $*"; }

scene "0. the casa home, with no services configured"
rm -rf "$ART"; mkdir -p "$ART"
bash "$HERE/casa/up-casa.sh" --run "$RUN" >/dev/null || { echo "up-casa failed"; exit 1; }
# shellcheck disable=SC1091
. "$RUN/env.sh"
CFG="$DEPLOY/services.json"
HANDWRITTEN="$ART/handwritten.services.json"
cp "$CFG" "$HANDWRITTEN"                      # the rendered casa config, for the diff
python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["services"] = []                            # paths + defaults stay: install.sh seeds those
json.dump(d, open(p, "w"), indent=2)
PY
rm -f "$DEPLOY/interview.json" "$DEPLOY"/services.draft.json "$DEPLOY"/services.json.backup-*
casa-ctl ls | sed 's/^/    /'
ok "services.json has no services" '[ "$(python3 -c "import json;print(len(json.load(open(\"$CFG\"))[\"services\"]))")" = 0 ]'

I() { python3 "$DEPLOY/onboard/interview.py" "$@"; }
TRANSCRIPT="$ART/interview-transcript.md"
: > "$TRANSCRIPT"
echo "# Interview transcript — the casa home, added device by device" >> "$TRANSCRIPT"
echo >> "$TRANSCRIPT"
CTL="$RUN/bin/casa-ctl"

# Dana's answers (sim/cloud/persona-admin.md), keyed device:kind. Lab plumbing
# she could not know (the casa control script, the soak's rate cap) rides in
# the extras answer - exactly where a real admin puts remediation params.
dana() {
    case "$1:$2" in
        pihole-dns:setup)    echo "RESOLVER_IP=192.168.1.2 — the Pi in the hall" ;;
        pihole-dns:extras)   echo "TEST_DOMAIN=example.com CASACTL=$CTL CAP_MAX=99" ;;
        pihole-dns:class)    echo "fix — it breaks quietly and nobody notices DNS until it's dead" ;;
        pihole-dns:how)      echo "script:remediations/casa-restart.sh" ;;
        pihole-dns:drill)    echo "freely" ;;
        pihole-dns:nag)      echo "digest — if the internet is out that's not the Pi's fault; tell me in the catch-up" ;;
        truenas-smb:setup)   echo "TCP_HOST=127.0.0.1 TCP_PORT=18446" ;;
        truenas-smb:extras)  echo "CASACTL=$CTL CAP_MAX=99" ;;
        truenas-smb:class)   echo "ask — NEVER act on the NAS without asking me. This affects the whole household." ;;
        truenas-smb:how)     echo "script:remediations/casa-restart.sh truenas-smb" ;;
        truenas-smb:host)    echo "none — if the whole box is dark just tell me" ;;
        truenas-smb:consent) echo "household — everyone's photos live on it, warn the family when it runs" ;;
        truenas-smb:announce) echo "Heads-up from Cranston: power-cycling the NAS — the photos app will blip for a minute." ;;
        truenas-smb:drill)   echo "ok — only with my explicit OK, each time" ;;
        plex:setup)          echo "HTTP_URL=http://127.0.0.1:18324/identity" ;;
        plex:extras)         echo "HTTP_URL_ALT=http://127.0.0.1:18325/identity ALT_GUARD_CMD=true HOST_IP=192.168.1.4 CASACTL=$CTL CAP_MAX=99" ;;
        plex:class)          echo "fix — restart it freely" ;;
        plex:how)            echo "script:remediations/casa-restart.sh" ;;
        plex:host)           echo "none" ;;
        plex:drill)          echo "freely" ;;
        plex:nag)            echo "daily — if it keeps breaking, nag me daily, not hourly" ;;
        truenas-disk:setup)  echo "MOUNT_PATH=$RUN/vol/tank" ;;
        truenas-disk:extras) echo "MIN_FREE_PCT=15" ;;
        truenas-disk:local)  echo "no — that's the NAS's pool" ;;
        truenas-disk:nag)    echo "digest — just tell me in the daily digest" ;;
        truenas-cert:setup)  echo "CERT_FILE=$RUN/certs/truenas.pem" ;;
        truenas-cert:extras) echo "WARN_DAYS=21 CRIT_DAYS=7" ;;
        truenas-cert:nag)    echo "daily" ;;
        box-disk:setup)      echo "MOUNT_PATH=$RUN/vol/root" ;;
        box-disk:extras)     echo "MIN_FREE_PCT=10" ;;
        box-disk:local)      echo "yes — that's this box" ;;
        box-disk:nag)        echo "digest" ;;
        *) return 1 ;;
    esac
}

# add one device, then answer whatever the interview asks about it, in order
interview() {
    local dev="$1" kind="$2" n=0 q qkind ask a
    I add "$dev" "$kind" >/dev/null || { echo "add $dev $kind failed"; exit 1; }
    printf '## %s  (`add %s %s`)\n\n' "$dev" "$dev" "$kind" >> "$TRANSCRIPT"
    while :; do
        q=$(I next --device "$dev" --json 2>/dev/null)
        case "$q" in "{"*) ;; *) break ;; esac          # "nothing open" is plain text
        qkind=$(printf '%s' "$q" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kind"])')
        ask=$(printf '%s' "$q" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ask"])')
        a=$(dana "$dev" "$qkind") || { echo "no scripted answer for $dev:$qkind"; exit 1; }
        printf '**Q (%s)** %s\n\n**Dana:** %s\n\n' "$qkind" "$ask" "$a" >> "$TRANSCRIPT"
        I answer "$dev" "$qkind" "$a" >/dev/null || { echo "answer refused for $dev:$qkind: $a"; exit 1; }
        n=$((n+1))
    done
    echo "  $dev: $n questions"
    printf '_%s questions._\n\n' "$n" >> "$TRANSCRIPT"
}

scene "1. Dana adds the home, one device at a time"
interview pihole-dns dns
interview truenas-smb tcp
interview plex http
interview truenas-disk disk
interview truenas-cert cert
interview box-disk disk
I plan | tee "$ART/plan.out" | sed 's/^/    /'
ok "nothing left to ask" 'grep -q "nothing to ask" "$ART/plan.out"'
ok "still zero services in services.json (everything is staged)" '[ "$(python3 -c "import json;print(len(json.load(open(\"$CFG\"))[\"services\"]))")" = 0 ]'

scene "2. fill: draft, then apply"
I fill > "$ART/fill-draft.out"
ok "the draft leaves services.json untouched" '[ "$(python3 -c "import json;print(len(json.load(open(\"$CFG\"))[\"services\"]))")" = 0 ]'
I fill --apply | tee "$ART/fill-apply.out" | grep -E "applied|lint" | sed 's/^/    /'
ok "six services written"            '[ "$(python3 -c "import json;print(len(json.load(open(\"$CFG\"))[\"services\"]))")" = 6 ]'
ok "lint ok"                         'grep -q "lint: ok" "$ART/fill-apply.out"'
ok "nothing left staged"             '[ "$(python3 -c "import json;print(len(json.load(open(\"$DEPLOY/interview.json\")).get(\"added\",{})))")" = 0 ]'
cp "$CFG" "$ART/interview-built.services.json"
I doctrine --out "$ART/doctrine-draft.md" >/dev/null

scene "3. interview-built vs hand-written (sim/casa/overlay/services.casa.json)"
python3 - "$HANDWRITTEN" "$ART/interview-built.services.json" <<'PY' | tee "$ART/diff.out"
import json, sys
hand = {s["name"]: s for s in json.load(open(sys.argv[1]))["services"]}
mine = {s["name"]: s for s in json.load(open(sys.argv[2]))["services"]}
PLUMBING = {"CHECK_KEY", "REMEDIATION_KEY", "CASA_RUN", "CASA_SVC", "CASACTL", "CAP_MAX"}
same = diff = 0
def show(label, a, b):
    global same, diff
    if a == b:
        same += 1; print(f"    = {label}: {json.dumps(a, ensure_ascii=False)}")
    else:
        diff += 1; print(f"    ~ {label}: hand {json.dumps(a, ensure_ascii=False)}  |  interview {json.dumps(b, ensure_ascii=False)}")
for name in hand:
    print(f"  {name}" + ("" if name in mine else "   MISSING from the interview-built config"))
    if name not in mine: diff += 1; continue
    h, m = hand[name], mine[name]
    show("check", h["check"], m["check"])
    show("local", h.get("local", False), m.get("local", False))
    hp = {k: v for k, v in h.get("params", {}).items() if k not in PLUMBING}
    mp = {k: v for k, v in m.get("params", {}).items() if k not in PLUMBING}
    show("params", hp, mp)
    codes = sorted(set(h.get("remediations") or {}) | set(m.get("remediations") or {}))
    for c in codes:
        show(f"remediations.{c}", (h.get("remediations") or {}).get(c, "<absent>"), (m.get("remediations") or {}).get(c, "<absent>"))
    show("notify_by_code", h.get("notify_by_code", {}), m.get("notify_by_code", {}))
    show("realert_minutes_by_code", h.get("realert_minutes_by_code", {}), m.get("realert_minutes_by_code", {}))
extra = sorted(set(mine) - set(hand))
if extra: print(f"  interview-built has extra services: {extra}"); diff += len(extra)
print(f"  {same} fields identical, {diff} differ (plumbing params {sorted(PLUMBING)} ignored)")
PY

scene "4. Dana's stances, graded (sim/cloud/interview-check.py)"
python3 "$HERE/cloud/interview-check.py" "$ART/interview-built.services.json" | tee "$ART/interview-check.out" | sed 's/^/    /'
ok "interview-check passes" 'grep -q "all assertions pass" "$ART/interview-check.out"'

if [ "$SOAK" = 1 ]; then
    scene "5. 90 simulated days on the interview-built config (sim/soak.py --services)"
    OUT="$HERE/.run/lab-out"
    python3 "$HERE/soak.py" --scenario "$HERE/scenarios/family-home-90d.jsonl" \
        --casa-run "$RUN" --services "$ART/interview-built.services.json" --out "$OUT" 2>&1 | tail -1 | sed 's/^/    /'
    python3 "$HERE/report.py" --run "$OUT" --scenario "$HERE/scenarios/family-home-90d.jsonl" | tee "$ART/report.out" | sed 's/^/    /'
    ok "all gates pass on the interview-built config" 'grep -q "ALL GATES PASS" "$ART/report.out"'
else
    echo; echo "(soak skipped: --no-soak)"
fi

echo
echo "== $pass passed, $fail failed"
echo "   transcript: $TRANSCRIPT"
echo "   built config: $ART/interview-built.services.json   doctrine: $ART/doctrine-draft.md"
[ "$fail" = 0 ]
