#!/usr/bin/env python3
"""Stub-harness suite for the core v2 engine.

Copies engine/selfheal.py into an isolated temp install root (BASE is derived
from __file__, so the copy gets its own state/digest), monkeypatches the
clock, checks and remediations, and asserts on routed messages and state.

Groups 1-10 port the proven v1 consolidation suite (digest routing, recovery
hold-off / flap collapse, ask-first realert renewal, cap gating, legacy-state
compat). Groups 11+ cover what v2 added: params in the child env, relative
path resolution, pluggable alert_sink / approval_gate argv edges, blind-root
suppression, config-driven TTL, consent carriage.

Run anywhere: python3 tests/test_engine.py
"""

import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

tmp = Path(tempfile.mkdtemp(prefix="cranston-core-test-"))
(tmp / "engine").mkdir()
shutil.copy(REPO / "authoring" / "scripts" / "engine" / "selfheal.py",
            tmp / "engine" / "selfheal.py")

spec = importlib.util.spec_from_file_location("sh", tmp / "engine" / "selfheal.py")
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)

# ---- controllable clock -----------------------------------------------------
class Clock:
    def __init__(self):
        self.t = sh.datetime(2026, 10, 2, 12, 0, 0, tzinfo=sh.timezone.utc)
    def advance(self, minutes):
        self.t += timedelta(minutes=minutes)

clock = Clock()
sh.now = lambda: clock.t

def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)

SVC_IMMEDIATE = {"name": "svcA", "enabled": True, "local": True, "check": "unused",
                 "remediations": {"CODE_PLAIN": None}}
SVC_DEGRADED = {"name": "svcB", "enabled": True, "local": True, "check": "unused",
                "remediations": {"CODE_DEG": None},
                "realert_minutes_by_code": {"CODE_DEG": 1440}}
SVC_ASK = {"name": "svcC", "enabled": True, "local": True, "check": "unused",
           "remediations": {"CODE_ASK": {"ask": "/x/fix-it.sh"}},
           "realert_minutes_by_code": {"CODE_ASK": 300}}
SVC_AUTO = {"name": "svcD", "enabled": True, "local": True, "check": "unused",
            "verify_delay_seconds": 0,
            "remediations": {"CODE_AUTO": "/x/auto.sh"},
            "notify_by_code": {"CODE_AUTO": "digest"}}
SVC_AUTO_PAGE = {"name": "svcE", "enabled": True, "local": True, "check": "unused",
                 "verify_delay_seconds": 0,
                 "remediations": {"CODE_AUTO2": "/x/auto.sh"}}
SVC_CONSENT = {"name": "svcF", "enabled": True, "local": True, "check": "unused",
               "remediations": {"CODE_ASK2": {"ask": "/x/fix-it.sh",
                                              "consent": "household"}}}

CONFIG = {"version": 2,
          "paths": {},
          "defaults": {"fail_threshold": 2, "check_timeout_seconds": 5,
                       "cooldown_minutes": 30, "max_attempts": 2,
                       "attempt_window_hours": 6, "verify_delay_seconds": 0,
                       "realert_minutes": 60, "recovery_hold_minutes": 10,
                       "pending_ttl_hours": 6, "post_outage_grace_minutes": 6},
          "services": [SVC_IMMEDIATE, SVC_DEGRADED, SVC_ASK, SVC_AUTO,
                       SVC_AUTO_PAGE, SVC_CONSENT]}

cfg_path = tmp / "services.json"

def fresh(config=None):
    cfg_path.write_text(json.dumps(config or CONFIG))
    hs = sh.SelfHeal(str(cfg_path))
    hs.flush_alerts = lambda: None
    hs.run_hook = lambda svc, k: ""
    return hs

def read_digest():
    try:
        return [json.loads(l) for l in (tmp / "state" / "digest.jsonl").read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []

def clear_digest():
    p = tmp / "state" / "digest.jsonl"
    if p.exists():
        p.write_text("")

PASS = 0
def ok(cond, name):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}")
        sys.exit(1)

print("== 1. degraded finding -> digest, throttled ==")
hs = fresh()
f = {"status": "CODE_DEG", "layer": "l", "detail": "chronic thing", "severity": "degraded"}
hs.step(SVC_DEGRADED, "svcB", f)
hs.step(SVC_DEGRADED, "svcB", f)
ok(hs.alerts == [], "no immediate page")
d = read_digest()
ok(len(d) == 1 and "DEGRADED" in d[0]["message"] and d[0]["system"] == "svcB", "one digest line")
clock.advance(2)
hs.step(SVC_DEGRADED, "svcB", f)
ok(len(read_digest()) == 1, "second cycle throttled by realert (1440)")
clock.advance(1441)
hs.step(SVC_DEGRADED, "svcB", f)
ok(len(read_digest()) == 2, "re-digests after 1440 min")

print("== 2. digest-class recovery -> digest, no hold-off ==")
hs.step(SVC_DEGRADED, "svcB", None)
d = read_digest()
ok(len(d) == 3 and "recovered" in d[-1]["message"], "recovery went to digest immediately")
ok(hs.alerts == [], "still no page")
ok(hs.state["keys"]["svcB"].get("flap") is None, "no flap record for digest class")

print("== 3. ask-first -> immediate + approval renewal at 300min ==")
clear_digest()
hs = fresh()
fa = {"status": "CODE_ASK", "layer": "l", "detail": "needs human"}
hs.step(SVC_ASK, "svcC", fa)
hs.step(SVC_ASK, "svcC", fa)
ok(len(hs.alerts) == 1 and "heal svcC" in hs.alerts[0], "paged with heal prompt")
ok("svcC" in hs.pending, "pending approval created")
exp1 = hs.pending["svcC"]["expires"]
clock.advance(299)
hs.step(SVC_ASK, "svcC", fa)
ok(len(hs.alerts) == 1, "no re-page at 299 min")
clock.advance(2)
hs.step(SVC_ASK, "svcC", fa)
ok(len(hs.alerts) == 2, "re-paged at 301 min")
ok(hs.pending["svcC"]["expires"] > exp1, "approval renewed (expiry extended)")
ok(read_digest() == [], "nothing leaked to digest")

print("== 4. clean recovery -> single held ✅ after 10 min ==")
hs = fresh()
fp = {"status": "CODE_PLAIN", "layer": "l", "detail": "down"}
hs.step(SVC_IMMEDIATE, "svcA", fp)
hs.step(SVC_IMMEDIATE, "svcA", fp)
ok(len(hs.alerts) == 1 and "DOWN" in hs.alerts[0], "paged DOWN")
hs.step(SVC_IMMEDIATE, "svcA", None)
ok(len(hs.alerts) == 1, "✅ held back at recovery")
clock.advance(5)
hs.step(SVC_IMMEDIATE, "svcA", None)
ok(len(hs.alerts) == 1, "still held at 5 min")
clock.advance(6)
hs.step(SVC_IMMEDIATE, "svcA", None)
ok(len(hs.alerts) == 2 and hs.alerts[1].startswith("✅") and "flapped" not in hs.alerts[1],
   "✅ released after hold, no flap suffix")
clock.advance(5)
hs.step(SVC_IMMEDIATE, "svcA", None)
ok(len(hs.alerts) == 2, "✅ sent exactly once")

print("== 5. flap: down-up-down suppresses the pair, suffix on next alerts ==")
hs = fresh()
hs.step(SVC_IMMEDIATE, "svcA", fp)
hs.step(SVC_IMMEDIATE, "svcA", fp)
hs.step(SVC_IMMEDIATE, "svcA", None)
clock.advance(4)
hs.step(SVC_IMMEDIATE, "svcA", fp)
ok(len(hs.alerts) == 1, "pair suppressed (no ✅, re-down absorbing)")
hs.step(SVC_IMMEDIATE, "svcA", fp)
ok(len(hs.alerts) == 1, "confirmed re-down silent inside realert window")
clock.advance(61)
hs.step(SVC_IMMEDIATE, "svcA", fp)
ok(len(hs.alerts) == 2 and "flapped 1x" in hs.alerts[1], "re-page carries flap suffix")
hs.step(SVC_IMMEDIATE, "svcA", None)
clock.advance(11)
hs.step(SVC_IMMEDIATE, "svcA", None)
ok(len(hs.alerts) == 3 and "flapped 1x before settling" in hs.alerts[2],
   "final ✅ carries flap count")
ok(hs.state["keys"]["svcA"].get("flap") is None, "flap record cleared after settle")

print("== 6. auto-remediation success routed by notify_by_code ==")
clear_digest()
hs = fresh()
hs.run_remediation = lambda svc, script, arg: (0, "ok")
hs.run_check = lambda svc: {}
fd = {"status": "CODE_AUTO", "layer": "l", "detail": "low"}
hs.step(SVC_AUTO, "svcD", fd)
hs.step(SVC_AUTO, "svcD", fd)
ok(hs.alerts == [], "routine 🔧 did not page")
d = read_digest()
ok(len(d) == 1 and "auto-remediated" in d[0]["message"], "routine 🔧 went to digest")

print("== 7. auto success without notify_by_code still pages ==")
hs = fresh()
hs.run_remediation = lambda svc, script, arg: (0, "ok")
hs.run_check = lambda svc: {}
fe = {"status": "CODE_AUTO2", "layer": "l", "detail": "down"}
hs.step(SVC_AUTO_PAGE, "svcE", fe)
hs.step(SVC_AUTO_PAGE, "svcE", fe)
ok(len(hs.alerts) == 1 and "auto-remediated" in hs.alerts[0], "non-routine 🔧 pages")

print("== 8. remediation failure always pages ==")
hs = fresh()
hs.run_remediation = lambda svc, script, arg: (1, "boom")
hs.run_check = lambda svc: {"svcE": fe}
hs.step(SVC_AUTO_PAGE, "svcE", fe)
hs.step(SVC_AUTO_PAGE, "svcE", fe)
ok(len(hs.alerts) == 1 and "Remediation" in hs.alerts[0], "failed remediation pages")

print("== 9. cap alert is realert-gated ==")
hs = fresh()
hs.run_remediation = lambda svc, script, arg: (1, "boom")
hs.run_check = lambda svc: {"svcE": fe}
rec = hs.state["keys"].setdefault("svcE", sh.default_record())
rec["attempts"] = [sh.iso(clock.t - timedelta(minutes=5)), sh.iso(clock.t - timedelta(minutes=3))]
rec["last_alert"] = sh.iso(clock.t - timedelta(minutes=5))
rec["consecutive_failures"] = 5
rec["status"] = "failing"
hs.step(SVC_AUTO_PAGE, "svcE", fe)
ok(hs.alerts == [] and rec["status"] == "escalated", "cap reached silently inside realert window")
clock.advance(61)
hs.step(SVC_AUTO_PAGE, "svcE", fe)
ok(len(hs.alerts) == 1 and "STILL DOWN" in hs.alerts[0], "escalated re-nag after window")

print("== 10. backward compat: legacy record without new fields recovers ==")
hs = fresh()
hs.state["keys"]["svcA"] = {"status": "awaiting_approval", "consecutive_failures": 4,
                            "last_status_code": "CODE_PLAIN", "attempts": [],
                            "last_alert": sh.iso(clock.t - timedelta(minutes=120)),
                            "last_transition": sh.iso(clock.t - timedelta(minutes=120))}
hs.step(SVC_IMMEDIATE, "svcA", None)
clock.advance(11)
hs.step(SVC_IMMEDIATE, "svcA", None)
ok(len(hs.alerts) == 1 and hs.alerts[0].startswith("✅"), "legacy record: held ✅ delivered")

print("== 11. params reach the check environment ==")
out_file = tmp / "param-out.txt"
check = write_exec(tmp / "checks" / "fake-ok.sh",
                   f'#!/bin/bash\nprintf "%s|%s|%s" "$MY_PARAM" "$SELFHEAL_ROOT" "$SELFHEAL_AUDIT_LOG" > "{out_file}"\nexit 0\n')
svc_p = {"name": "svcP", "enabled": True, "local": True,
         "check": "checks/fake-ok.sh",   # relative: also proves resolution
         "params": {"MY_PARAM": "hello-params"}, "remediations": {}}
cfgp = dict(CONFIG); cfgp = json.loads(json.dumps(CONFIG)); cfgp["services"] = [svc_p]
hs = fresh(cfgp)
findings = hs.run_check(svc_p)
got = out_file.read_text().split("|")
ok(findings == {}, "relative check path resolved and ran healthy")
ok(got[0] == "hello-params", "service params exported to the check env")
ok(Path(got[1]).resolve() == tmp.resolve(), "SELFHEAL_ROOT exported")
ok(got[2].endswith("state/audit.log"), "SELFHEAL_AUDIT_LOG exported")

print("== 12. pluggable alert_sink receives lines as argv ==")
sink_out = tmp / "sink-out.txt"
sink = write_exec(tmp / "bin" / "capture-sink.sh",
                  f'#!/bin/bash\nprintf "%s\\n" "$@" > "{sink_out}"\n')
cfgs = json.loads(json.dumps(CONFIG))
cfgs["paths"] = {"alert_sink": ["bash", str(sink)]}
cfg_path.write_text(json.dumps(cfgs))
hs = sh.SelfHeal(str(cfg_path))
hs.alerts = ["🚨 one", "✅ two"]
hs.flush_alerts()
ok(sink_out.read_text().splitlines() == ["🚨 one", "✅ two"],
   "alert sink called with alert lines as argv")

print("== 13. approval_gate runs remediations as argv prefix ==")
gate_out = tmp / "gate-out.txt"
gate = write_exec(tmp / "bin" / "capture-gate.sh",
                  f'#!/bin/bash\nprintf "%s\\n" "$@" > "{gate_out}"\necho "auto=$SELFHEAL_AUTOMATION" >> "{gate_out}"\nexit 0\n')
fix = write_exec(tmp / "remediations" / "fake-fix.sh", "#!/bin/bash\nexit 0\n")
cfgg = json.loads(json.dumps(CONFIG))
cfgg["paths"] = {"approval_gate": ["bash", str(gate)]}
hs = fresh(cfgg)
rc, out = hs.run_remediation({"params": {}}, fix, "theArg")
lines = gate_out.read_text().splitlines()
ok(rc == 0 and lines[0] == fix and lines[1] == "theArg" and lines[2] == "auto=true",
   "gate received [script, arg] argv with SELFHEAL_AUTOMATION=true")

print("== 14. blind-root suppression ==")
hs = fresh()
root_f = {"key": "svcA", "status": "CODE_PLAIN", "layer": "l", "detail": "root unreachable"}
# preset a confirmed-failing subkey that would otherwise 'recover'
hs.state["keys"]["svcA/sub"] = {"status": "awaiting_approval", "consecutive_failures": 3,
                                "last_status_code": "SUB_DOWN", "attempts": [],
                                "last_alert": sh.iso(clock.t - timedelta(minutes=120)),
                                "last_transition": sh.iso(clock.t - timedelta(minutes=120))}
hs.run_check = lambda svc: {"svcA": dict(root_f)}
hs.config["services"] = [SVC_IMMEDIATE]
hs.run()
sub = hs.state["keys"]["svcA/sub"]
ok(sub["status"] == "awaiting_approval" and sub["consecutive_failures"] == 3,
   "subkey untouched while root has a finding")
ok(not any(a.startswith("✅ svcA/sub") for a in hs.alerts), "no phantom subkey recovery")
ok(hs.state["keys"]["svcA"]["consecutive_failures"] == 1, "root key itself stepped (absorbing)")
# root recovers -> subkey steps healthy again on the next cycle
hs.run_check = lambda svc: {}
hs.run()
ok(hs.state["keys"]["svcA/sub"]["status"] == "ok", "subkey steps again once root is clear")

print("== 15. pending_ttl_hours honored from config ==")
cfgt = json.loads(json.dumps(CONFIG))
cfgt["defaults"]["pending_ttl_hours"] = 1
hs = fresh(cfgt)
hs.add_pending("k", "C", "/x/s.sh", None)
delta = sh.parse_iso(hs.pending["k"]["expires"]) - clock.t
ok(delta == timedelta(hours=1), "TTL from config (1h)")

print("== 16. consent carried on ask-first proposals ==")
hs = fresh()
fc = {"status": "CODE_ASK2", "layer": "l", "detail": "tv thing"}
hs.step(SVC_CONSENT, "svcF", fc)
hs.step(SVC_CONSENT, "svcF", fc)
ok(len(hs.alerts) == 1 and "affects the household" in hs.alerts[0],
   "household consent surfaces in the page")
ok(hs.pending["svcF"]["consent"] == "household", "consent stored on the pending entry")

print("== 17. network gate: failed route read fails OPEN, clean empty read is down ==")
class FakeResult:
    def __init__(self, rc, stdout=""):
        self.returncode = rc
        self.stdout = stdout
real_subprocess_run = sh.subprocess.run
sh.subprocess.run = lambda *a, **k: FakeResult(1, "")   # `ip` refused (Android netlink)
ok(sh.local_network_up() == (True, None), "ip exits non-zero -> fail open (network treated UP)")
sh.subprocess.run = lambda *a, **k: FakeResult(0, "")   # clean read, no route
ok(sh.local_network_up() == (False, None), "clean read with no default route -> down")
def raise_oserror(*a, **k):
    raise OSError("no such binary")
sh.subprocess.run = raise_oserror
ok(sh.local_network_up() == (True, None), "ip binary missing -> fail open")
sh.subprocess.run = real_subprocess_run

print("== 18. defaults.gateway_ip drives the gate instead of the route table ==")
calls = []
def fake_run(argv, **k):
    calls.append(argv[0])
    if argv[0] == "ip":
        raise AssertionError("route table consulted despite gateway_ip")
    return FakeResult(0, "")   # ping succeeds
sh.subprocess.run = fake_run
ok(sh.local_network_up("10.0.0.1") == (True, "10.0.0.1"),
   "config gateway pinged, route table never read")
ok(calls and all(c == "ping" for c in calls), "only ping was invoked")
sh.subprocess.run = real_subprocess_run
# and the engine passes defaults.gateway_ip through run()
cfgw = json.loads(json.dumps(CONFIG))
cfgw["defaults"]["gateway_ip"] = "10.9.9.9"
hs = fresh(cfgw)
hs.run_check = lambda svc: {}
seen_gw = []
real_lnu = sh.local_network_up
sh.local_network_up = lambda g=None: (seen_gw.append(g), (True, g))[1]
hs.run()
sh.local_network_up = real_lnu
ok(seen_gw == ["10.9.9.9"], "run() hands defaults.gateway_ip to the gate")

print("== 19. ask-first demotion after 3 unanswered pages ==")
clear_digest()
hs = fresh()
fa = {"status": "CODE_ASK", "layer": "l", "detail": "needs human"}
hs.step(SVC_ASK, "svcC", fa)
hs.step(SVC_ASK, "svcC", fa)                    # page 1 (immediate)
for _ in range(2):                              # pages 2 and 3 on the realert clock
    clock.advance(301)
    hs.step(SVC_ASK, "svcC", fa)
ok(len(hs.alerts) == 3, "three immediate pages allowed")
ok(read_digest() == [], "nothing in the digest during the immediate phase")
clock.advance(301)
hs.step(SVC_ASK, "svcC", fa)                    # 4th re-page -> demoted
ok(len(hs.alerts) == 3, "fourth re-page is NOT an immediate page")
d = read_digest()
ok(len(d) == 1 and "heal svcC" in d[0]["message"], "fourth re-page became a digest line")
ok("svcC" in hs.pending, "pending approval still alive after demotion")
hs.step(SVC_ASK, "svcC", None)                  # recovery resets the thread
rec = hs.state["keys"]["svcC"]
ok("ask_pages" not in rec and "ask_code" not in rec, "recovery clears the demotion counter")
hs.step(SVC_ASK, "svcC", fa)
hs.step(SVC_ASK, "svcC", fa)
ok(len(hs.alerts) == 4, "a fresh incident pages immediately again")

print("== 20. approve-heal runs the gate and passes GATE_CODE ==")
import subprocess
ah = Path(tempfile.mkdtemp(prefix="cranston-ah-test-"))
(ah / "engine").mkdir()
(ah / "state").mkdir()
(ah / "remediations").mkdir()
shutil.copy(REPO / "authoring" / "scripts" / "engine" / "approve-heal.py",
            ah / "engine" / "approve-heal.py")
gate_out = ah / "gate-env.txt"
gate = ah / "my-gate.sh"
gate.write_text("#!/bin/bash\n"
                f"echo \"code=$GATE_CODE caller=$SELFHEAL_CALLER auto=${{SELFHEAL_AUTOMATION:-}}\" > {gate_out}\n"
                "if [ \"$GATE_CODE\" = \"654321\" ]; then exec bash \"$@\"; fi\n"
                "echo \"gate: bad code\"; exit 65\n")
fix = ah / "remediations" / "fix.sh"
fix.write_text("#!/bin/bash\nexit 0\n")
cfg = {"version": 2,
       "paths": {"approval_gate": str(gate)},
       "defaults": {"verify_delay_seconds": 0, "check_timeout_seconds": 5},
       "services": []}
(ah / "services.json").write_text(json.dumps(cfg))
future = "2099-01-01T00:00:00Z"
(ah / "state" / "pending-approvals.json").write_text(json.dumps(
    {"svcX": {"status_code": "C", "script": str(fix), "arg": None, "expires": future}}))
env = dict(__import__("os").environ, SELFHEAL_CONFIG=str(ah / "services.json"))
r = subprocess.run([sys.executable, str(ah / "engine" / "approve-heal.py"), "svcX", "654321"],
                   capture_output=True, text=True, env=env)
ok(r.returncode == 0 and "completed" in r.stdout, "valid code: gate approved, heal completed")
ok(gate_out.exists() and "code=654321" in gate_out.read_text()
   and "caller=approve-heal" in gate_out.read_text(),
   "gate saw GATE_CODE and the approve-heal caller")
ok("auto=\n" in gate_out.read_text() or gate_out.read_text().rstrip().endswith("auto="),
   "SELFHEAL_AUTOMATION is NOT set on the human path")
(ah / "state" / "pending-approvals.json").write_text(json.dumps(
    {"svcX": {"status_code": "C", "script": str(fix), "arg": None, "expires": future}}))
r = subprocess.run([sys.executable, str(ah / "engine" / "approve-heal.py"), "svcX", "111111"],
                   capture_output=True, text=True, env=env)
pend = json.loads((ah / "state" / "pending-approvals.json").read_text())
ok(r.returncode == 1 and "gate refused" in r.stdout and "svcX" in pend,
   "bad code: gate refusal surfaces and the pending approval is KEPT")

print("== 21. approve-heal exports the deployment's audit log and state dir to the remediation ==")
# Without SELFHEAL_AUDIT_LOG the remediation library falls back to a /tmp
# file: the rate cap then counts every deployment on the box and the
# EXEC/RESULT lines never reach the deployment's audit trail.
env_out = ah / "fix-env.txt"
fix_env = ah / "remediations" / "fix-env.sh"
fix_env.write_text("#!/bin/bash\n"
                   f"printf '%s|%s|%s' \"$SELFHEAL_AUDIT_LOG\" \"$SELFHEAL_STATE_DIR\" \"$SELFHEAL_ROOT\" > {env_out}\n"
                   "exit 0\n")
(ah / "state" / "pending-approvals.json").write_text(json.dumps(
    {"svcX": {"status_code": "C", "script": str(fix_env), "arg": None, "expires": future}}))
# a configured service with a passing check, so the post-heal VERIFY path
# (which re-runs the check with the same env) is exercised too
chk_ok = ah / "chk-ok.sh"
chk_ok.write_text("#!/bin/bash\nexit 0\n")
cfg["services"] = [{"name": "svcX", "check": str(chk_ok), "params": {"P": "1"}, "remediations": {}}]
(ah / "services.json").write_text(json.dumps(cfg))
r = subprocess.run([sys.executable, str(ah / "engine" / "approve-heal.py"), "svcX", "654321"],
                   capture_output=True, text=True, env=env)
got = env_out.read_text().split("|") if env_out.exists() else ["", "", ""]
ok(r.returncode == 0 and "verified HEALTHY" in r.stdout,
   "heal completed and the verify check ran (no crash on the verify path)")
ok(Path(got[0]).resolve() == (ah / "state" / "audit.log").resolve(),
   "SELFHEAL_AUDIT_LOG is the deployment's audit log")
ok(Path(got[1]).resolve() == (ah / "state").resolve(), "SELFHEAL_STATE_DIR exported")
ok(Path(got[2]).resolve() == ah.resolve(), "SELFHEAL_ROOT exported")
shutil.rmtree(ah)

print(f"\nALL {PASS} ASSERTIONS PASSED")
shutil.rmtree(tmp)
