#!/bin/bash
# cloud-drill.sh <phase> - the real-time end-to-end drill for a throwaway
# cloud box (runbook: sim/CLOUD-TEST.md). Installs the skill with its own
# installer, points delivery at sim/phone/stub_botapi.py, wires the casa fake
# home into the deploy, drives sim/scenarios/cloud-75min.jsonl with real
# sleeps under real (or emulated) cron, scaffolds the Dana interview, and
# wraps up with a report skeleton.
#
# Phases (run in order; each prints a PHASE banner and PASS/FAIL lines):
#   install            installer + stub bot API + env file + cron (or emulator plan)
#   casa               up-casa against the same deploy; compressed timings; */15 digest
#   traffic            ~75 min scenario drive, then sim/report.py --mode drill
#   interview          write the GAPPY config and print the Dana instructions
#   interview-verify   interview-check.py + one forced failure -> ask page
#   wrap               kill background pids, print the report skeleton
#
# Everything lands under $DRILL_HOME (default ~/cranston-drill): the deploy,
# the casa run dir, the phone log, the per-phase assertion ledger. Re-running
# a phase is safe where it can be: install re-runs the (idempotent) installer
# after killing the old stub; casa re-copies and re-edits; traffic and
# interview-verify append to the same phone log with a recorded offset.
#
# Honest-failure notes the report must carry:
#   - no usable cron daemon -> cron-emulator.sh runs the marker block; SAY SO
#   - no sudo -> the cron lines' log path is rewritten from /var/log/cranston.log
#     to $DRILL_HOME/cranston.log; SAY SO
#
# bash 3.2 compatible, set -u, stdlib python only. Exit: 0 all phase
# assertions passed; 1 any failed; 64 usage; 65 contract file missing.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"            # sim/cloud
SIM="$(cd "$HERE/.." && pwd)"                    # sim/
REPO="$(cd "$SIM/.." && pwd)"                    # repo root
DRILL_HOME="${DRILL_HOME:-$HOME/cranston-drill}"
VARS="$DRILL_HOME/drill-vars.sh"
LEDGER="$DRILL_HOME/assertions.log"
PHASE="${1:-}"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS $1"; echo "PASS [$PHASE] $1" >> "$LEDGER"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL $1"; echo "FAIL [$PHASE] $1" >> "$LEDGER"; }
chk() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
note(){ echo "note: $*"; echo "NOTE [$PHASE] $*" >> "$LEDGER"; }
banner() { echo; echo "=== PHASE $PHASE: $* ==="; }
need() { [ -e "$1" ] || { echo "cloud-drill: missing contract file $1 - $2" >&2; exit 65; }; }
finish() {
    echo
    echo "phase '$PHASE' result: PASS=$PASS FAIL=$FAIL   (ledger: $LEDGER)"
    [ "$FAIL" = 0 ] || exit 1
    exit 0
}
fmode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }

usage() {
    echo "usage: cloud-drill.sh install|casa|traffic|interview|interview-verify|wrap"
    echo "state + artifacts: \$DRILL_HOME (currently $DRILL_HOME)"
    exit 64
}

mkdir -p "$DRILL_HOME"
[ -f "$VARS" ] && . "$VARS"

# Rewrite /var/log/cranston.log -> $CRON_LOG in whatever plan the box uses.
rewrite_log_path() {
    [ "$CRON_LOG" = "/var/log/cranston.log" ] && return 0
    if [ "$CRON_MODE" = "crontab" ]; then
        crontab -l 2>/dev/null | sed "s#/var/log/cranston.log#$CRON_LOG#g" | crontab -
    else
        sed "s#/var/log/cranston.log#$CRON_LOG#g" "$PLAN_FILE" > "$PLAN_FILE.tmp" \
            && mv "$PLAN_FILE.tmp" "$PLAN_FILE"
    fi
}

# The current plan text, wherever it lives.
plan_text() {
    if [ "$CRON_MODE" = "crontab" ]; then crontab -l 2>/dev/null; else cat "$PLAN_FILE" 2>/dev/null; fi
}

case "$PHASE" in

# ---------------------------------------------------------------------------
install)
    banner "install: stub bot API, env file, installer, cron"
    need "$REPO/scripts/install.sh" "run from a checkout of the skill repo"
    need "$SIM/phone/stub_botapi.py" "the casa agent's phone stub (sim/phone/) is not built yet"

    DEPLOY="$DRILL_HOME/deploy"
    CASA_RUN="$DRILL_HOME/casa-run"
    ENV_FILE="${DRILL_ENV_FILE:-$DRILL_HOME/cranston.env}"
    PHONE_LOG="$DRILL_HOME/phone.jsonl"
    TG_PORT="${DRILL_TG_PORT:-18081}"
    MODE_FILE="$CASA_RUN/tg-mode"
    PLAN_FILE="$DRILL_HOME/cron-plan.txt"
    mkdir -p "$CASA_RUN" "$DEPLOY"

    # -- 1. the phone: stub Bot API, backgrounded, pid recorded ---------------
    if [ -f "$DRILL_HOME/stub.pid" ] && kill -0 "$(cat "$DRILL_HOME/stub.pid")" 2>/dev/null; then
        kill "$(cat "$DRILL_HOME/stub.pid")" 2>/dev/null; sleep 1
    fi
    python3 "$SIM/phone/stub_botapi.py" --port "$TG_PORT" --log "$PHONE_LOG" \
        --mode-file "$MODE_FILE" >> "$DRILL_HOME/stub.out" 2>&1 &
    echo $! > "$DRILL_HOME/stub.pid"
    up=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if python3 -c "import socket; socket.create_connection(('127.0.0.1', $TG_PORT), 1).close()" 2>/dev/null; then
            up=1; break
        fi
        sleep 1
    done
    chk "stub bot API answering on 127.0.0.1:$TG_PORT (pid $(cat "$DRILL_HOME/stub.pid"))" "[ $up = 1 ]"

    # -- 2. the cron log target: /var/log with sudo, else a local fallback -----
    CRON_LOG="/var/log/cranston.log"
    if sudo -n touch /var/log/cranston.log 2>/dev/null \
            && sudo -n chmod 666 /var/log/cranston.log 2>/dev/null; then
        note "cron log: /var/log/cranston.log (sudo available)"
    else
        CRON_LOG="$DRILL_HOME/cranston.log"; : >> "$CRON_LOG"
        note "NO sudo - cron log redirected to $CRON_LOG (report this)"
    fi

    # -- 3. the env file, BEFORE the installer (which then keeps it). Cron
    # starts from an empty environment and this file is the only carrier, so
    # everything children need lives here: the casa exports (sourced from
    # env.sh once phase 'casa' writes it - PATH stub prepends, CASA_RUN, the
    # dig/df/ping shims), then our pins, which win by coming second.
    cat > "$ENV_FILE" <<EOF
# written by sim/cloud/cloud-drill.sh - the drill's single env carrier.
# casa's env.sh first (PATH shims, CASA_RUN, SELFHEAL_GW_OVERRIDE, TG_*);
# the drill's pins after it win on any overlap.
if [ -f "$CASA_RUN/env.sh" ]; then . "$CASA_RUN/env.sh"; fi
export TG_BOT_TOKEN=000000:DRILL-FAKE-TOKEN
export TG_CHAT_ID=1001
export TG_ANNOUNCE_CHAT_ID=2001
export TG_API=http://127.0.0.1:$TG_PORT
export SELFHEAL_SENT_WINDOW_MINUTES=4
export SELFHEAL_ALERT_FILE=$DEPLOY/state/alert-pending.json
export SELFHEAL_GW_OVERRIDE=192.168.1.1
export SELFHEAL_LOG_FILE=$CRON_LOG
export CASA_RUN=$CASA_RUN
EOF
    chmod 600 "$ENV_FILE"

    # -- 4. cron availability probe ---------------------------------------------
    CRON_MODE=emulator
    if command -v crontab >/dev/null 2>&1 \
            && { service cron status >/dev/null 2>&1 || pgrep -x cron >/dev/null 2>&1 \
                 || pgrep -x crond >/dev/null 2>&1; }; then
        CRON_MODE=crontab
    fi
    note "cron path: $CRON_MODE$([ "$CRON_MODE" = emulator ] && echo ' (no usable cron daemon - cron-emulator.sh will run the plan; report this)')"

    # -- 5. the installer. SELFHEAL_GW_OVERRIDE=127.0.0.1 only for its dry run
    # (the casa ping shims don't exist yet; the env FILE keeps 192.168.1.1 for
    # the cron children, who will have the shims).
    APPLY=""
    [ "$CRON_MODE" = "crontab" ] && APPLY="--apply-cron"
    env SELFHEAL_GW_OVERRIDE=127.0.0.1 bash "$REPO/scripts/install.sh" \
        --deploy "$DEPLOY" --env-file "$ENV_FILE" --test-page $APPLY \
        2>&1 | tee "$DRILL_HOME/install.out"
    INSTALL_RC=${PIPESTATUS[0]}
    chk "installer exited 0 (rc=$INSTALL_RC)" "[ '$INSTALL_RC' = 0 ]"

    if [ "$CRON_MODE" = "emulator" ]; then
        {
            echo "# cranston-skill begin"
            grep -E "selfheal\.py|notify-alerts\.sh|flush-digest\.sh" "$DRILL_HOME/install.out" \
                | grep -E "^(\*|[0-9])" || true
            echo "# cranston-skill end"
        } > "$PLAN_FILE"
    fi
    rewrite_log_path

    # -- 6. assertions ----------------------------------------------------------
    alldirs=1
    for d in engine bin checks gates remediations onboard; do
        [ -d "$DEPLOY/$d" ] || { alldirs=0; bad "deploy code dir $d exists"; }
    done
    [ "$alldirs" = 1 ] && ok "all six code dirs deployed (engine bin checks gates remediations onboard)"
    chk "services.json seeded" "[ -f '$DEPLOY/services.json' ]"
    chk "env file kept by the installer (drill pins intact)" "grep -qF 'TG_API=http://127.0.0.1:$TG_PORT' '$ENV_FILE'"
    chk "env file mode 600" "[ \"\$(fmode '$ENV_FILE')\" = 600 ]"
    chk "stub received the test page" "grep -q 'cranston test page' '$PHONE_LOG'"

    # idempotence: an edited services.json survives a re-run
    python3 - "$DEPLOY/services.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["_drill_marker"] = "edited-before-rerun"
json.dump(d, open(p, "w"), indent=2)
PY
    cp "$DEPLOY/services.json" "$DRILL_HOME/services.edited"
    env SELFHEAL_GW_OVERRIDE=127.0.0.1 bash "$REPO/scripts/install.sh" \
        --deploy "$DEPLOY" --env-file "$ENV_FILE" > "$DRILL_HOME/install2.out" 2>&1
    chk "re-run keeps the edited services.json byte-identical" \
        "cmp -s '$DEPLOY/services.json' '$DRILL_HOME/services.edited'"

    BLOCK_LINES=$(plan_text | awk '/^# cranston-skill begin$/{b=1;next}/^# cranston-skill end$/{b=0}b' | grep -c . || true)
    chk "marker block present with 3 cron lines ($CRON_MODE)" "[ \"$BLOCK_LINES\" = 3 ]"

    cat > "$VARS" <<EOF
DEPLOY="$DEPLOY"
CASA_RUN="$CASA_RUN"
ENV_FILE="$ENV_FILE"
PHONE_LOG="$PHONE_LOG"
TG_PORT="$TG_PORT"
CRON_MODE="$CRON_MODE"
CRON_LOG="$CRON_LOG"
PLAN_FILE="$PLAN_FILE"
EOF
    note "vars saved to $VARS"
    finish
    ;;

# ---------------------------------------------------------------------------
casa)
    banner "casa config: fake home, compressed timings, */15 digest"
    [ -f "$VARS" ] || { echo "run 'cloud-drill.sh install' first"; exit 64; }
    need "$SIM/casa/up-casa.sh" "the casa agent's fake home (sim/casa/) is not built yet"

    bash "$SIM/casa/up-casa.sh" --deploy "$DEPLOY" --run "$CASA_RUN" \
        2>&1 | tee "$DRILL_HOME/up-casa.out"
    chk "up-casa wrote $CASA_RUN/env.sh" "[ -f '$CASA_RUN/env.sh' ]"

    # the deploy must run the CASA config. up-casa --deploy should have
    # installed it; if the deploy's config still lacks the casa services and a
    # casa copy exists, install it ourselves.
    if ! grep -q '"plex"' "$DEPLOY/services.json" 2>/dev/null; then
        if [ -f "$CASA_RUN/services.json" ]; then
            cp "$CASA_RUN/services.json" "$DEPLOY/services.json"
            note "copied casa services.json from $CASA_RUN into the deploy"
        fi
    fi
    chk "deploy services.json holds the casa services" \
        "grep -q '\"plex\"' '$DEPLOY/services.json' && grep -q '\"truenas-smb\"' '$DEPLOY/services.json'"

    # compress the engine defaults in place for a 75-minute wall clock
    python3 - "$DEPLOY/services.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.setdefault("defaults", {}).update({
    "realert_minutes": 5,
    "recovery_hold_minutes": 2,
    "post_outage_grace_minutes": 2,
    "pending_ttl_hours": 1,
    "verify_delay_seconds": 2,
    "ask_demote_after": 3,
})
json.dump(d, open(p, "w"), indent=2)
print("defaults compressed:", {k: d["defaults"][k] for k in
      ("realert_minutes", "recovery_hold_minutes", "post_outage_grace_minutes",
       "pending_ttl_hours", "verify_delay_seconds", "ask_demote_after")})
PY
    chk "compressed defaults written (realert 5, hold 2, grace 2, ttl 1h, verify 2s, demote 3)" \
        "python3 -c \"import json; d=json.load(open('$DEPLOY/services.json'))['defaults']; assert (d['realert_minutes'],d['recovery_hold_minutes'],d['post_outage_grace_minutes'],d['pending_ttl_hours'],d['verify_delay_seconds'],d['ask_demote_after'])==(5,2,2,1,2,3)\""
    cp "$DEPLOY/services.json" "$DRILL_HOME/services.casa.json"

    # the digest line goes to */15 (daily is useless in a 75-min drill);
    # the engine line stays */2. Edited INSIDE the marker block only.
    plan_text > "$DRILL_HOME/plan.before"
    python3 - "$DRILL_HOME/plan.before" > "$DRILL_HOME/plan.after" <<'PY'
import re, sys
inblk = False
for line in open(sys.argv[1]):
    line = line.rstrip("\n")
    if line == "# cranston-skill begin":
        inblk = True
    elif line == "# cranston-skill end":
        inblk = False
    elif inblk and "flush-digest.sh" in line:
        line = re.sub(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+", "*/15 * * * *", line, count=1)
    print(line)
PY
    if [ "$CRON_MODE" = "crontab" ]; then
        crontab - < "$DRILL_HOME/plan.after"
    else
        cp "$DRILL_HOME/plan.after" "$PLAN_FILE"
    fi
    chk "digest cron line now */15 (inside the marker block)" \
        "plan_text | grep flush-digest.sh | grep -q '^\*/15 '"
    chk "engine cron line still */2" \
        "plan_text | grep selfheal.py | grep -q '^\*/2 '"

    # clean slate so traffic starts from silence
    rm -f "$DEPLOY/state/state.json" "$DEPLOY/state/pending-approvals.json" \
          "$DEPLOY/state/digest.jsonl" "$DEPLOY/state/alert-pending.json" \
          "$DEPLOY/state/alert-pending.json.sent.json" "$DEPLOY/state/audit.log"
    chk "casa-ctl ls answers" "( . '$ENV_FILE'; '$SIM/casa/casa-ctl' ls )"
    finish
    ;;

# ---------------------------------------------------------------------------
traffic)
    banner "traffic: ~75 min real-time scenario (cloud-75min.jsonl)"
    [ -f "$VARS" ] || { echo "run install + casa first"; exit 64; }
    SCENARIO="$SIM/scenarios/cloud-75min.jsonl"
    need "$SCENARIO" "scenario file missing"
    need "$SIM/report.py" "the casa agent's gate checker (sim/report.py) is not built yet"

    if [ "$CRON_MODE" = "emulator" ]; then
        if [ -f "$DRILL_HOME/emulator.pid" ] && kill -0 "$(cat "$DRILL_HOME/emulator.pid")" 2>/dev/null; then
            note "cron-emulator already running (pid $(cat "$DRILL_HOME/emulator.pid"))"
        else
            CRON_EMULATOR_LOG="$DRILL_HOME/cron-emulator.log" \
                bash "$HERE/cron-emulator.sh" "$ENV_FILE" "$DEPLOY" "$PLAN_FILE" &
            echo $! > "$DRILL_HOME/emulator.pid"
            note "cron-emulator started (pid $(cat "$DRILL_HOME/emulator.pid"), log $DRILL_HOME/cron-emulator.log)"
        fi
    fi

    OFFSET_BYTES=$( [ -f "$PHONE_LOG" ] && wc -c < "$PHONE_LOG" | tr -d ' ' || echo 0 )
    note "phone log offset at drill start: $OFFSET_BYTES bytes"

    # the driver: real sleeps to each +MM:SS offset, actions via casa-ctl /
    # approve-heal, everything logged. Runs with the env file sourced so
    # casa-ctl sees CASA_RUN and the shims.
    ( . "$ENV_FILE"; python3 - "$SCENARIO" "$SIM/casa/casa-ctl" "$DEPLOY" "$DRILL_HOME/t0" <<'PY'
import json, subprocess, sys, time

scenario, casactl, deploy, t0file = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
start = time.time()
with open(t0file, "w") as f:   # the drill epoch collect-run.py maps offsets onto
    f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)))


def say(msg):
    print(f"[driver +{int(time.time()-start)//60:02d}:{int(time.time()-start)%60:02d}] {msg}", flush=True)


def run(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    say(f"$ {' '.join(argv)} -> rc={r.returncode}" + (f" | {out[:300]}" if out else ""))
    return r.returncode


events = [json.loads(l) for l in open(scenario) if l.strip()]
for e in events:
    m, s = e["t"].lstrip("+").split(":")
    due = int(m) * 60 + int(s)
    wait = start + due - time.time()
    if wait > 0:
        time.sleep(wait)
    say(f"{e['id']} ({e['ev']}): expect={e.get('expect','-')} "
        f"{e.get('note','')[:120]}")
    ev = e["ev"]
    if ev == "svc_down":
        run([casactl, "stop", e["svc"]])
    elif ev == "svc_up":
        run([casactl, "start", e["svc"]])
    elif ev == "gw_down":
        run([casactl, "unplug", e["ip"]])
    elif ev == "gw_up":
        run([casactl, "plug", e["ip"]])
    elif ev == "disk_fill":
        run([casactl, "disk-fill", e["vol"], str(e["pct"])])
    elif ev == "admin_heal":
        run(["python3", f"{deploy}/engine/approve-heal.py", e["key"]])
    elif ev in ("note", "end"):
        pass
    else:
        say(f"UNKNOWN event type {ev} - skipped")
say("scenario complete")
PY
    ) 2>&1 | tee "$DRILL_HOME/driver.out"

    # the */15 digest flush rides the wall clock; give it up to 16 more minutes
    DIGEST_SEEN=0
    for i in $(seq 1 32); do
        if tail -c "+$((OFFSET_BYTES + 1))" "$PHONE_LOG" 2>/dev/null | grep -q "Daily catch-up"; then
            DIGEST_SEEN=1; break
        fi
        sleep 30
    done
    chk "digest flush delivered ('Daily catch-up' header, within 16 min of scenario end)" "[ $DIGEST_SEEN = 1 ]"

    NEW="$DRILL_HOME/phone.traffic.jsonl"
    tail -c "+$((OFFSET_BYTES + 1))" "$PHONE_LOG" > "$NEW" 2>/dev/null || : > "$NEW"
    chk "auto-fix relief page (plex: 'is back - I ran ... re-checked')" \
        "grep -q 'plex is back' '$NEW' && grep -q 're-checked: healthy again' '$NEW'"
    chk "ask page proposes the fix (Reply 'heal truenas-smb' to approve.)" \
        "grep -qF \"Reply 'heal truenas-smb' to approve.\" '$NEW'"
    chk "consent scope line (This affects the household ...)" \
        "grep -qF 'This affects the household' '$NEW'"
    chk "announce reached chat 2001" \
        "python3 -c \"import json,sys; lines=[json.loads(l) for l in open('$NEW') if l.strip()]; sys.exit(0 if any(str(l.get('chat_id'))=='2001' for l in lines) else 1)\""
    chk "nothing to 2001 wears a page header" \
        "python3 -c \"import json,sys; lines=[json.loads(l) for l in open('$NEW') if l.strip()]; sys.exit(1 if any('Cranston:' in l.get('text','') for l in lines if str(l.get('chat_id'))=='2001') else 0)\""
    chk "recovery for truenas-smb surfaced (page or digest)" \
        "grep -q 'truenas-smb recovered' '$NEW' || grep -q 'truenas-smb' '$DEPLOY/state/digest.jsonl' 2>/dev/null || tail -c +$((OFFSET_BYTES+1)) '$PHONE_LOG' | grep -q 'truenas-smb recovered'"

    # bridge the real-time artifacts into a soak-shaped run dir so the drill
    # and the soak share one gate checker (G4 is soak-only; see collect-run.py)
    python3 "$HERE/collect-run.py" --stub-log "$PHONE_LOG" --scenario "$SCENARIO" \
        --start "$(cat "$DRILL_HOME/t0")" --out "$DRILL_HOME/out" \
        --deploy "$DEPLOY" --run-log "$CRON_LOG" --since-offset "$OFFSET_BYTES" \
        2>&1 | tee "$DRILL_HOME/collect.out"
    python3 "$SIM/report.py" --run "$DRILL_HOME/out" --mode drill --sent-window 4 \
        2>&1 | tee "$DRILL_HOME/report.out"
    REPORT_RC=${PIPESTATUS[0]}
    if [ "$REPORT_RC" = 0 ]; then
        ok "sim/report.py drill gates pass (rc=0)"
    else
        bad "sim/report.py drill gates BREACHED (rc=$REPORT_RC) - read $DRILL_HOME/report.out"
    fi
    python3 "$SIM/report.py" --run "$DRILL_HOME/out" --mode drill --sent-window 4 \
        --emit-judge-input "$DRILL_HOME/judge-input.jsonl" > /dev/null 2>&1 || true
    chk "judge input emitted ($DRILL_HOME/judge-input.jsonl)" "[ -s '$DRILL_HOME/judge-input.jsonl' ]"
    finish
    ;;

# ---------------------------------------------------------------------------
interview)
    banner "interview scaffold: gappy config for the Dana session"
    [ -f "$VARS" ] || { echo "run install + casa first"; exit 64; }
    cp "$DEPLOY/services.json" "$DRILL_HOME/services.pre-interview.json"

    # strip ONLY the open decisions persona-admin.md lists: truenas-smb's
    # consent + consent_notes, truenas-disk's nag fields, and all _interview
    # provenance. Hand-set auto strings and the announce text stay.
    python3 - "$DEPLOY/services.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for svc in d.get("services", []):
    svc.pop("_interview", None)
    name = svc.get("name")
    if name == "truenas-smb":
        svc.pop("consent_notes", None)
        for code, v in (svc.get("remediations") or {}).items():
            if isinstance(v, dict):
                v.pop("consent", None)
    if name == "truenas-disk":
        svc.pop("realert_minutes_by_code", None)
        svc.pop("notify_by_code", None)
json.dump(d, open(p, "w"), indent=2)
print("gappy config written")
PY
    rm -f "$DEPLOY/interview.json"
    chk "gappy config: truenas-smb ask entries carry no consent" \
        "! python3 -c \"import json; d=json.load(open('$DEPLOY/services.json')); s=[x for x in d['services'] if x['name']=='truenas-smb'][0]; assert any('consent' in v for v in s['remediations'].values() if isinstance(v,dict))\" 2>/dev/null"
    chk "gappy config: truenas-disk nag fields stripped" \
        "python3 -c \"import json; d=json.load(open('$DEPLOY/services.json')); s=[x for x in d['services'] if x['name']=='truenas-disk'][0]; assert 'realert_minutes_by_code' not in s and 'notify_by_code' not in s\""
    chk "interview plan sees open decisions" \
        "python3 '$DEPLOY/onboard/interview.py' --config '$DEPLOY/services.json' plan | grep -q 'open decisions'"

    cat <<EOF

--- hand-off to the Claude session ------------------------------------------
You are now Dana (sim/cloud/persona-admin.md). The config at
  $DEPLOY/services.json
has open decisions. Drive the interview YOURSELF - never edit the JSON:

  python3 $DEPLOY/onboard/interview.py --config $DEPLOY/services.json plan
  python3 $DEPLOY/onboard/interview.py --config $DEPLOY/services.json next
  python3 $DEPLOY/onboard/interview.py --config $DEPLOY/services.json answer <device> <kind> "<Dana's words>"
  ... loop next/answer until plan says nothing is open ...
  python3 $DEPLOY/onboard/interview.py --config $DEPLOY/services.json fill --apply
  python3 $DEPLOY/onboard/interview.py --config $DEPLOY/services.json doctrine --out $DRILL_HOME/doctrine-draft.md

Answer one device at a time, keyword first, in Dana's own words (her exact
stances and the photos/blip phrasing are in persona-admin.md). When done, run:

  bash $HERE/cloud-drill.sh interview-verify
------------------------------------------------------------------------------
EOF
    finish
    ;;

# ---------------------------------------------------------------------------
interview-verify)
    banner "interview verify: config assertions + one forced failure"
    [ -f "$VARS" ] || { echo "run the earlier phases first"; exit 64; }

    if python3 "$HERE/interview-check.py" "$DEPLOY/services.json"; then
        ok "interview-check.py: filled config matches the persona table"
    else
        bad "interview-check.py found FAILs (see lines above)"
    fi
    chk "doctrine draft written" "[ -s '$DRILL_HOME/doctrine-draft.md' ]"

    # forced failure: stop the NAS, wait 3 engine cycles (*/2 cron -> ~7 min
    # incl. the notify minute), expect the ask page with the approval prompt.
    OFFSET_BYTES=$( [ -f "$PHONE_LOG" ] && wc -c < "$PHONE_LOG" | tr -d ' ' || echo 0 )
    ( . "$ENV_FILE"; "$SIM/casa/casa-ctl" stop truenas-smb )
    note "truenas-smb stopped; waiting ~7.5 min (3 engine cycles + delivery)"
    sleep 450
    chk "forced failure paged: ask page with Reply 'heal truenas-smb' to approve." \
        "tail -c +$((OFFSET_BYTES + 1)) '$PHONE_LOG' | grep -qF \"Reply 'heal truenas-smb' to approve.\""
    chk "forced failure paged the household consent line" \
        "tail -c +$((OFFSET_BYTES + 1)) '$PHONE_LOG' | grep -qF 'This affects the household'"
    ( . "$ENV_FILE"; "$SIM/casa/casa-ctl" start truenas-smb )
    note "truenas-smb restarted (recovery clears the pending approval on its own)"
    finish
    ;;

# ---------------------------------------------------------------------------
wrap)
    banner "wrap: stop background pids, report skeleton"
    for pf in stub.pid emulator.pid; do
        if [ -f "$DRILL_HOME/$pf" ]; then
            pid=$(cat "$DRILL_HOME/$pf")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null
                ok "stopped $pf (pid $pid)"
            else
                note "$pf (pid $pid) already gone"
            fi
            rm -f "$DRILL_HOME/$pf"
        fi
    done
    if [ "${CRON_MODE:-}" = "crontab" ]; then
        note "crontab still carries the marker block - remove with: crontab -l | awk '/^# cranston-skill begin\$/{b=1;next}/^# cranston-skill end\$/{b=0;next}!b' | crontab -"
    fi

    echo
    echo "=== REPORT SKELETON (fill per sim/CLOUD-TEST.md §Report) ==="
    echo
    echo "## Install"
    grep -E "^(PASS|FAIL|NOTE) \[install\]" "$LEDGER" 2>/dev/null || echo "(install phase never ran)"
    echo
    echo "## Casa + traffic gates"
    grep -E "^(PASS|FAIL|NOTE) \[(casa|traffic)\]" "$LEDGER" 2>/dev/null || echo "(not run)"
    echo
    echo "## Interview"
    grep -E "^(PASS|FAIL|NOTE) \[interview" "$LEDGER" 2>/dev/null || echo "(not run)"
    echo
    echo "## Phone metrics"
    if [ -f "${PHONE_LOG:-}" ]; then
        python3 - "$PHONE_LOG" <<'PY'
import json, sys
from collections import Counter
lines = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
by_chat = Counter(str(l.get("chat_id")) for l in lines)
print(f"messages delivered: {len(lines)} total; per chat: {dict(by_chat)}")
if lines:
    print("per-hour rate and the /day extrapolation belong in the session report "
          "(wall time is the drill's ~75-120 min window).")
PY
    else
        echo "(no phone log)"
    fi
    echo
    echo "## Artifacts"
    for f in install.out up-casa.out driver.out report.out judge-input.jsonl \
             doctrine-draft.md phone.jsonl cranston.log cron-emulator.log assertions.log; do
        [ -e "$DRILL_HOME/$f" ] && echo "  $DRILL_HOME/$f"
    done
    echo "  ${DEPLOY:-<deploy>}/state/  (state.json, digest.jsonl, audit.log)"
    echo
    echo "## Cron path: ${CRON_MODE:-unknown}; cron log: ${CRON_LOG:-unknown}"
    echo
    echo "Judge phase: score every unique delivered message per sim/judge/judge.md"
    echo "against sim/judge/rubric.md, input $DRILL_HOME/judge-input.jsonl."
    finish
    ;;

*)
    usage
    ;;
esac
