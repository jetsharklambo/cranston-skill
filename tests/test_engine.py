#!/usr/bin/env python3
"""Stub-harness suite for the core v2 engine.

Copies engine/selfheal.py into an isolated temp install root (BASE is derived
from __file__, so the copy gets its own state/digest), monkeypatches the
clock, checks and remediations, and asserts on routed messages and state.

Groups 1-10 port the proven v1 consolidation suite (digest routing, recovery
hold-off / flap collapse, ask-first realert renewal, cap gating, legacy-state
compat). Groups 11+ cover what v2 added: params in the child env, relative
path resolution, pluggable alert_sink / approval_gate argv edges, blind-root
suppression, config-driven TTL, consent carriage. Groups 22-26 cover the
hardening: approve-heal strips the automation marker and GATE_* from its
inherited env, reserved params keys, finding-key validation, remediation
argument sanitization, and first_failed_at in state.

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

print("== 22. approve-heal strips SELFHEAL_AUTOMATION and GATE_* from the inherited env ==")
# Every gate template passes an allowlisted script through without a human
# when SELFHEAL_AUTOMATION=true, so an inherited marker (or one planted via
# params) skipped the second factor; an inherited GATE_* could repoint a
# gate's verifier. The gate below has the template's auto-pass branch on
# purpose: if the marker leaked, the no-code run WOULD succeed.
ah = Path(tempfile.mkdtemp(prefix="cranston-ah-test-"))
for d in ("engine", "state", "remediations"):
    (ah / d).mkdir()
shutil.copy(REPO / "authoring" / "scripts" / "engine" / "approve-heal.py",
            ah / "engine" / "approve-heal.py")
gate_out = ah / "gate-env.txt"
gate = ah / "my-gate.sh"
gate.write_text("#!/bin/bash\n"
                f"echo \"auto=${{SELFHEAL_AUTOMATION:-UNSET}} totp=${{GATE_TOTP_URL:-UNSET}} "
                f"code=${{GATE_CODE:-UNSET}} caller=$SELFHEAL_CALLER\" > {gate_out}\n"
                "if [ \"${SELFHEAL_AUTOMATION:-}\" = \"true\" ]; then exec bash \"$@\"; fi\n"
                "if [ \"${GATE_CODE:-}\" = \"654321\" ]; then exec bash \"$@\"; fi\n"
                "echo \"gate: bad code\"; exit 65\n")
fix = ah / "remediations" / "fix.sh"
fix.write_text("#!/bin/bash\nexit 0\n")
chk_ok = ah / "chk-ok.sh"
chk_ok.write_text("#!/bin/bash\nexit 0\n")
cfg = {"version": 2, "paths": {"approval_gate": str(gate)},
       "defaults": {"verify_delay_seconds": 0, "check_timeout_seconds": 5},
       "services": [{"name": "svcX", "check": str(chk_ok), "params": {"P": "1"}, "remediations": {}}]}
(ah / "services.json").write_text(json.dumps(cfg))
future = "2099-01-01T00:00:00Z"
def reset_pending(arg=None):
    (ah / "state" / "pending-approvals.json").write_text(json.dumps(
        {"svcX": {"status_code": "C", "script": str(fix), "arg": arg, "expires": future}}))
def pending_kept():
    return "svcX" in json.loads((ah / "state" / "pending-approvals.json").read_text())
def approved_execs():
    p = ah / "state" / "audit.log"
    return p.read_text().count("APPROVED-EXEC") if p.exists() else 0
evil_env = dict(os.environ, SELFHEAL_CONFIG=str(ah / "services.json"),
                SELFHEAL_AUTOMATION="true", GATE_TOTP_URL="http://evil")
def approve(*argv):
    return subprocess.run([sys.executable, str(ah / "engine" / "approve-heal.py"), *argv],
                          capture_output=True, text=True, env=evil_env)
reset_pending()
r = approve("svcX")                        # no code: only a leaked marker could pass
seen = gate_out.read_text() if gate_out.exists() else ""
ok(r.returncode == 1 and "gate refused" in r.stdout and "auto=UNSET" in seen,
   "inherited SELFHEAL_AUTOMATION=true never reaches the gate: no code, no pass")
ok("totp=UNSET" in seen and pending_kept(), "inherited GATE_TOTP_URL stripped; pending kept")
reset_pending()
r = approve("svcX", "654321")
seen = gate_out.read_text() if gate_out.exists() else ""
ok(r.returncode == 0 and "completed" in r.stdout, "with a code the gate approves and the heal completes")
ok("code=654321" in seen and "caller=approve-heal" in seen and "auto=UNSET" in seen
   and "totp=UNSET" in seen, "gate saw GATE_CODE and the caller, nothing inherited")
n_exec = approved_execs()                  # baseline: refusals below must not add to it
# a params block planting the marker: refused before anything runs
cfg["services"][0]["params"] = {"P": "1", "SELFHEAL_AUTOMATION": "true"}
(ah / "services.json").write_text(json.dumps(cfg))
reset_pending()
gate_out.unlink()
r = approve("svcX", "654321")
ok(r.returncode == 1 and "reserved key" in r.stdout and "SELFHEAL_AUTOMATION" in r.stdout,
   "params setting SELFHEAL_AUTOMATION: approve-heal refuses and names the key")
ok(pending_kept() and not gate_out.exists() and approved_execs() == n_exec,
   "pending kept, gate never ran, nothing audited as executed")
# a pending entry with a malformed argument: refused the same way
cfg["services"][0]["params"] = {"P": "1"}
(ah / "services.json").write_text(json.dumps(cfg))
reset_pending(arg="-rf")
r = approve("svcX", "654321")
ok(r.returncode == 1 and "invalid argument" in r.stdout and pending_kept()
   and not gate_out.exists() and approved_execs() == n_exec,
   "pending entry with arg '-rf': refused, pending kept, nothing ran")
shutil.rmtree(ah)

print("== 23. engine: a reserved params key yields CHECK_ERROR and nothing of the service runs ==")
marker = tmp / "reserved-ran.txt"
check_marker = write_exec(tmp / "checks" / "marker.sh", f'#!/bin/bash\ntouch "{marker}"\nexit 0\n')
svc_r = {"name": "svcR", "enabled": True, "local": True, "check": check_marker,
         "on_fail_forensics": check_marker, "verify_delay_seconds": 0,
         "params": {"CHECK_KEY": "svcR", "SELFHEAL_AUTOMATION": "true"},
         "remediations": {"CHECK_ERROR": "/x/auto.sh"}}
cfgr = json.loads(json.dumps(CONFIG)); cfgr["services"] = [svc_r]
cfg_path.write_text(json.dumps(cfgr))
hs = sh.SelfHeal(str(cfg_path))            # real run_hook kept on purpose
hs.flush_alerts = lambda: None
f = hs.run_check(svc_r)
ok(f["svcR"]["status"] == "CHECK_ERROR" and "reserved key SELFHEAL_AUTOMATION" in f["svcR"]["detail"],
   "reserved params key -> synthesized CHECK_ERROR naming the key")
ok(not marker.exists(), "the check was never executed")
ok(hs.run_hook(svc_r, "on_fail_forensics") == "" and not marker.exists(), "hooks do not run either")
rc, out = hs.run_remediation(svc_r, "/x/auto.sh", None)
ok(rc == 64 and "reserved key" in out, "remediations are refused for the same reason")
hs.run(); hs.run()                          # two full cycles: confirm + attempted remediation
ok(not marker.exists() and hs.state["keys"]["svcR"]["last_status_code"] == "CHECK_ERROR",
   "full cycles complete with nothing of the service executed")
ok(any("reserved key" in a for a in hs.alerts), "the page names the reserved key")

print("== 24. finding validation: foreign keys, traversal subkeys and status-less lines ==")
check_bad = write_exec(tmp / "checks" / "bad-findings.sh", "#!/bin/bash\n"
    'echo \'{"key": "other/x", "status": "X_DOWN", "layer": "l", "detail": "not mine"}\'\n'
    'echo \'{"key": "svcA/../../etc", "status": "X_DOWN", "layer": "l", "detail": "traversal"}\'\n'
    'echo \'{"key": "svcA", "layer": "l", "detail": "no status at all"}\'\n'
    'echo \'{"key": "svcA/db-1", "status": "DB_DOWN", "layer": "db", "detail": "replica down"}\'\n'
    "exit 1\n")
svc_v = {"name": "svcA", "enabled": True, "local": True, "check": check_bad,
         "verify_delay_seconds": 0, "remediations": {"DB_DOWN": "/x/auto.sh"}}
cfgv = json.loads(json.dumps(CONFIG)); cfgv["services"] = [svc_v]
hs = fresh(cfgv)
hs.state = {"keys": {}}
captured = []
hs.run_remediation = lambda svc, script, arg: (captured.append(arg), (0, "ok"))[1]
f = hs.run_check(svc_v)
ok(set(f) == {"svcA", "svcA/db-1"}, "only the root CHECK_ERROR and the valid subkey finding survive")
ok(f["svcA"]["status"] == "CHECK_ERROR" and "3 finding(s) rejected" in f["svcA"]["detail"]
   and "other/x" in f["svcA"]["detail"], "synthesized CHECK_ERROR counts the rejects and names the first")
hs.run(); hs.run()                          # threshold 2: the second cycle remediates svcA/db-1
ok(captured == ["db-1"], "the valid subkey's arg reached run_remediation; no crash on the status-less line")
ok(not any("/../" in k or k.startswith("other") for k in hs.state["keys"]),
   "no foreign or traversal key entered state")

print("== 25. invalid remediation argument refused; malformed pinned ask arg ignored with a WARN ==")
hs = fresh()
ok(hs.run_remediation(SVC_AUTO, "/x/auto.sh", "-rf") == (64, "refused: invalid remediation argument"),
   "run_remediation refuses arg '-rf' without running")
ok(hs.run_remediation(SVC_AUTO, "/x/auto.sh", "a/b")[0] == 64, "a slash in the arg is refused too")
svc_pin = {"name": "svcG", "enabled": True, "local": True, "check": "unused",
           "remediations": {"CODE_PIN": {"ask": "/x/fix-it.sh", "arg": "bad arg"}}}
fg = {"status": "CODE_PIN", "layer": "l", "detail": "needs a pin"}
logged = []
real_log = sh.log
sh.log = lambda m: logged.append(m)
hs.step(svc_pin, "svcG", fg)
hs.step(svc_pin, "svcG", fg)
sh.log = real_log
ok(any("WARN" in m and "malformed" in m and "bad arg" in m for m in logged), "malformed pinned arg logged as WARN")
ok(hs.pending["svcG"]["arg"] is None, "the pending entry carries no argument")
ok(len(hs.alerts) == 1 and "pinned arg ignored" in hs.alerts[0], "the proposal says the pin was ignored")
svc_pin["remediations"]["CODE_PIN"]["arg"] = "outlet-7"
hs = fresh()
hs.step(svc_pin, "svcG", fg)
hs.step(svc_pin, "svcG", fg)
ok(hs.pending["svcG"]["arg"] == "outlet-7" and "ignored" not in hs.alerts[0], "a well-formed pinned arg is kept")

print("== 26. first_failed_at marks the incident start and survives re-pages ==")
hs = fresh()
hs.state["keys"].pop("svcA", None)          # earlier groups left svcA on disk
t0 = sh.iso(clock.t)
hs.step(SVC_IMMEDIATE, "svcA", fp)          # 1/2, absorbing
rec = hs.state["keys"]["svcA"]
ok(rec.get("first_failed_at") == t0, "set on the first failing cycle, before the threshold")
clock.advance(61)
hs.step(SVC_IMMEDIATE, "svcA", fp)          # confirmed -> page
t_page1 = rec["last_transition"]
clock.advance(61)
hs.step(SVC_IMMEDIATE, "svcA", fp)          # realert due -> re-page
ok(len(hs.alerts) == 2 and t_page1 != rec["last_transition"] == sh.iso(clock.t),
   "re-page moved last_transition")
ok(rec["first_failed_at"] == t0, "first_failed_at did not move")
hs.step(SVC_IMMEDIATE, "svcA", None)
ok("first_failed_at" not in rec, "cleared when the key steps healthy")
ok("first_failed_at" not in sh.default_record(), "not in default_record: old state files load unchanged")

print(f"\nALL {PASS} ASSERTIONS PASSED")
shutil.rmtree(tmp)
