#!/usr/bin/env python3
"""Offline tests for the per-device interview (scripts/onboard/interview.py).

Builds a throwaway install root from authoring/scripts, writes a hand-written
services.json with gaps, and drives the tool through plan / next / answer /
fill / reask / doctrine, asserting on the config it produces and on the rules
it must never break: manual config wins, nothing is written without --apply,
a hand-edited field is never overwritten, a re-interview is per device.
Also keeps onboard/catalog.json in step with the check templates' headers and
with references/interview.md. Stdlib only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "authoring" / "scripts"
CATALOG = json.loads((SCRIPTS / "onboard" / "catalog.json").read_text())
PASS = 0
FAILED = []


def ok(cond, name, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


# ---- catalog <-> templates <-> interview.md ----------------------------------------

print("== catalog agrees with the check templates' '# Findings:' headers ==")
# a finding code: UPPER_SNAKE, or one upper word of 6+ letters (WEDGED)
TOKEN = re.compile(r"\b(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{6,})\b")


def header_codes(path):
    lines = path.read_text().splitlines()
    block, grab = [], False
    for line in lines:
        if line.startswith("# Findings"):
            grab = True
            block.append(line.split(":", 1)[1])
            continue
        if grab:
            if not line.startswith("#") or line.strip() == "#":
                break
            block.append(line.lstrip("# "))
    codes = set()
    for line in block:
        if "NOT emitted" in line:
            continue
        line = re.sub(r"\([^)]*\)", "", line)   # "(degraded above CRIT_DAYS)" etc.
        codes |= set(TOKEN.findall(line))
    return codes


for rel, tpl in CATALOG["templates"].items():
    path = SCRIPTS / rel
    ok(path.exists(), f"{rel} exists")
    if path.exists():
        want, have = header_codes(path), set(tpl["codes"])
        ok(want == have, f"{Path(rel).name}: catalog codes == header codes",
           f"header-only {sorted(want - have)} catalog-only {sorted(have - want)}")
        ok(all(l in CATALOG["layers"] for l in tpl["codes"].values()),
           f"{Path(rel).name}: every code has a known layer")
shipped = {str(p.relative_to(SCRIPTS)) for p in (SCRIPTS / "checks" / "templates").glob("*.sh")}
ok(shipped == set(CATALOG["templates"]), "every shipped check template is in the catalog",
   f"missing {sorted(shipped - set(CATALOG['templates']))}")
for key, fix in CATALOG["fixes"].items():
    if key != "script":
        p = SCRIPTS / fix["script"]
        ok(p.exists() and os.access(p, os.X_OK), f"fix '{key}' points at a shipped, executable remediation")
    ok("fix" in fix["classes"] or "refuse" in fix, f"fix '{key}': an ask-first-only fix carries its refuse text")
    ok("option" in fix and set(fix.get("questions", ["how"])) <= {"how", "host"},
       f"fix '{key}': has an option text and names the questions that accept it")
ok(CATALOG["fixes"]["outlet"]["classes"] == ["ask"], "the outlet cycle is ask-first only in the catalog")

print("== catalog questions cite real interview.md / discovery.md questions ==")
interview_md = (REPO / "authoring" / "references" / "interview.md").read_text()
discovery_md = (REPO / "authoring" / "references" / "discovery.md").read_text()
for kind, q in CATALOG["questions"].items():
    for u in q["u"].split():
        src, where = (discovery_md, "discovery.md") if u.startswith("D") else (interview_md, "interview.md")
        ok(re.search(rf"^## {u}\.", src, re.M) is not None, f"{kind} cites {u} (exists in {where})")
ok("onboard/interview.py" in interview_md, "interview.md documents the tool")
ok(all(CATALOG["kinds"][k] in CATALOG["templates"] for k in CATALOG["kinds"]), "every add kind maps to a catalog template")
ok(all("setup" in t for t in CATALOG["templates"].values()), "every template has a setup spec")

# ---- a throwaway deployment ----------------------------------------------------------

tmp = Path(tempfile.mkdtemp(prefix="cranston-interview-"))
DEPLOY = tmp / "deploy"
shutil.copytree(SCRIPTS, DEPLOY)
TOOL = DEPLOY / "onboard" / "interview.py"
CFG = DEPLOY / "services.json"


def stub_script(rel):
    """A fixed-content script the admin 'wrote' (a script: answer must resolve to a file)."""
    p = DEPLOY / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/bash\n# test stub: a fixed-content remediation\nexit 0\n")
    p.chmod(0o755)


for rel in ("remediations/restart-media.sh", "remediations/nas-restart.sh", "bin/outside-restart.sh"):
    stub_script(rel)


def run(*args, expect=0):
    r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=str(tmp))
    if r.returncode != expect:
        print(f"    [{' '.join(args)}] exit {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def cfg():
    return json.loads(CFG.read_text())


def svc(name):
    return next(s for s in cfg()["services"] if s["name"] == name)


DEFAULTS = json.loads((REPO / "authoring" / "assets" / "services.example.json").read_text())["defaults"]


def write_cfg(services, extra=None):
    data = {"version": 2, "paths": {"state_dir": "state", "alert_sink": ["true"]},
            "defaults": dict(DEFAULTS, gateway_ip="127.0.0.1"), "services": services}
    data.update(extra or {})
    CFG.write_text(json.dumps(data, indent=2) + "\n")


HANDWRITTEN = [
    {"name": "media-server", "check": "checks/templates/check-http.sh",
     "params": {"CHECK_KEY": "media-server", "HTTP_URL": "http://192.168.1.58:4533/ping", "HOST_IP": "192.168.1.58"}},
    {"name": "nas", "check": "checks/templates/check-tcp-port.sh",
     "params": {"CHECK_KEY": "nas", "TCP_HOST": "192.168.1.60", "TCP_PORT": "445"}},
    {"name": "gateway", "check": "checks/templates/check-systemd-unit.sh",
     "params": {"CHECK_KEY": "gateway", "UNIT": "openclaw-gateway"}},
    {"name": "box-disk", "local": True, "check": "checks/templates/check-disk-space.sh",
     "params": {"CHECK_KEY": "box-disk", "MOUNT_PATH": "/"}},
    {"name": "charger", "check": "checks/templates/check-ha-entity.sh",
     "params": {"CHECK_KEY": "charger", "HA_URL": "http://192.168.1.35:8123", "HA_TOKEN_FILE": "/etc/ha.env",
                "ENTITY_ID": "sensor.battery_soc", "VALUE_LOW": "30"}},
    {"name": "old-dns", "enabled": False, "check": "checks/templates/check-dns.sh",
     "params": {"CHECK_KEY": "old-dns", "RESOLVER_IP": "192.168.1.2"}},
    {"name": "wifi", "check": "checks/check-wifi.sh", "params": {},
     "remediations": {"WIFI_WEDGED": {"ask": "remediations/wifi-reset.sh"}}},
    {"name": "decided", "check": "checks/templates/check-dns.sh",
     "params": {"CHECK_KEY": "decided", "RESOLVER_IP": "192.168.1.35"},
     "remediations": {"DNS_DOWN": None, "UPSTREAM_DOWN": None},
     "realert_minutes_by_code": {"UPSTREAM_DOWN": 1440}},
]
write_cfg(HANDWRITTEN)

print("== plan: ranks devices, explains why, skips the settled and the disabled ==")
r = run("plan", "--json")
plan = json.loads(r.stdout)
names = [p["device"] for p in plan]
ok(set(names[:2]) == {"gateway", "charger"}, "the two devices a shipped fix fits rank first", names)
ok("decided" not in names, "a fully hand-decided device is not asked about")
ok("old-dns" not in names, "a disabled device is skipped")
ok("wifi" in names and plan[names.index("wifi")]["open"] == ["consent", "drill"],
   "a bespoke ask-first entry only gets consent + drill")
ok(plan[names.index("box-disk")]["open"] == ["nag"], "an alert-only device only gets the nag question")
ok(any("shipped remediation" in w for w in plan[0]["why"]), "the why names the reason")

print("== next: one device-shaped question, with the device's own nouns ==")
q = json.loads(run("next", "--json").stdout)
ok(q["kind"] == "class" and q["device"] in ("gateway", "charger"), "the first question is a class question for a top-ranked device")
q = json.loads(run("next", "--device", "gateway", "--json").stdout)
ok(q["id"] == "gateway:class" and "openclaw-gateway" in q["ask"], "gateway's class question names its unit")
ok("UNIT_INACTIVE" in q["ask"], "the question lists the codes the check can emit")
q2 = json.loads(run("next", "--device", "media-server", "--json").stdout)
ok(q2["id"] == "media-server:class" and "192.168.1.58" in q2["ask"], "--device picks a device")

print("== answer: grammar, applicability, verbatim record ==")
ok(run("answer", "gateway", "class", "maybe?", expect=64).returncode == 64, "an unreadable answer is rejected")
ok(run("answer", "box-disk", "class", "fix", expect=2).returncode == 2, "a kind that does not apply is rejected")
run("answer", "gateway", "class", "fix — it's only my own gateway")
q = json.loads(run("next", "--device", "gateway", "--json").stdout)
ok(q["kind"] == "how" and "systemd:openclaw-gateway is the obvious one" in q["ask"],
   "the how question proposes the shipped systemd fix for a systemd device")
run("answer", "gateway", "how", "systemd:openclaw-gateway")
run("answer", "gateway", "drill", "freely")
run("answer", "media-server", "class", "ask")
q = json.loads(run("next", "--device", "media-server", "--json").stdout)
ok(q["kind"] == "how" and "runs on 192.168.1.58, not this box" in q["ask"] and "ssh:<user@host>" in q["ask"],
   "the how question for a service on another host says local restarts do not apply", q["ask"])
r = run("answer", "media-server", "how", "docker:navidrome", expect=64)
ok(r.returncode == 64 and "runs on 192.168.1.58" in r.stderr,
   "docker: for a service on another host is refused at answer time (C3)", r.stderr)
run("answer", "media-server", "how", "ssh:media@192.168.1.58")
run("answer", "media-server", "how", "script:remediations/restart-media.sh")
run("answer", "media-server", "host", "severs — that outlet feeds the router too")
run("answer", "media-server", "consent", "household: it's the living room")
run("answer", "media-server", "announce", "Heads-up: the media box is restarting, the TV will blip for a minute.")
run("answer", "media-server", "drill", "ok")
run("answer", "media-server", "nag", "daily")
run("answer", "nas", "class", "tell")
run("answer", "charger", "class", "fix")
q = json.loads(run("next", "--device", "charger", "--json").stdout)
ok("ha:<the switch to turn on> is the natural fit" in q["ask"], "the how question proposes the HA fix for an HA device")
run("answer", "charger", "how", "ha:switch.charger")
run("answer", "charger", "drill", "never")
run("answer", "box-disk", "nag", "digest")
run("answer", "wifi", "consent", "admin")
run("answer", "wifi", "drill", "never — one reset took the LAN down")
answers = json.loads((DEPLOY / "interview.json").read_text())
ok(answers["devices"]["media-server"]["host"]["text"] == "severs — that outlet feeds the router too",
   "answers are recorded verbatim")
plan = json.loads(run("plan", "--json").stdout)
ok(plan == [], "nothing left to ask once every device is answered", plan)

print("== fill: draft by default, services.json untouched ==")
before = CFG.read_text()
r = run("fill")
ok(CFG.read_text() == before, "fill without --apply leaves services.json byte-identical")
ok((DEPLOY / "services.draft.json").exists(), "a draft is written next to the config")
ok("kept as" in r.stdout and "WIFI_WEDGED" in r.stdout, "a hand-set ask entry is reported as kept, not changed")
ok("ssh-forced-command" not in r.stdout and "SSH_TARGET" not in r.stdout,
   "a superseded answer leaves no trace in the fill")
ok("lint:" in r.stdout and "wifi: remediations.WIFI_WEDGED" in r.stdout and "is not a file" in r.stdout,
   "the draft is linted too: a hand-set entry whose script is missing is reported as advice", r.stdout)

print("== fill --apply: the mappings ==")
r = run("fill", "--apply")
ok(len(list(DEPLOY.glob("services.json.backup-*"))) == 1, "a backup of services.json is kept")
ok("lint:" in r.stdout and r.stdout.index("applied") < r.stdout.index("lint:"),
   "fill --apply lints the written config (C6)", r.stdout)
gw = svc("gateway")
ok(gw["remediations"] == {c: "remediations/templates/restart-systemd-unit.sh" for c in ("PORT_DEAD", "UNIT_INACTIVE", "WEDGED")},
   "class=fix + how=systemd wires every fixable code to the shipped template", gw["remediations"])
ok(gw["params"]["REMEDIATION_KEY"] == "gateway" and gw["params"]["UNIT"] == "openclaw-gateway",
   "params the template needs are added; the existing UNIT is reused")
ms = svc("media-server")
ok(ms["remediations"]["SERVICE_DOWN"] == {"ask": "remediations/restart-media.sh", "consent": "household",
                                          "announce": "Heads-up: the media box is restarting, the TV will blip for a minute."},
   "class=ask wires an ask-first entry carrying the consent scope", ms["remediations"])
ok(ms["remediations"]["API_ERROR"] == ms["remediations"]["SERVICE_DOWN"], "all fixable codes get the same fix")
ok(ms["remediations"]["AUTH_FAILED"] is None, "an unfixable layer becomes tell-only (null)")
ok("HOST_DOWN" not in ms["remediations"] and ms["_interview"]["remediations.HOST_DOWN"]["omit"],
   "host=severs leaves the code OUT of the map (on-demand class) and records the omission")
ok(ms["consent_notes"] == "it's the living room", "the admin's words after the keyword become consent_notes")
ok(ms["realert_minutes_by_code"] == {"LAN_UNREACHABLE": 1440, "TRANSPORT_BLIND": 1440}, "nag=daily -> 1440 on the chronic codes")
ok("LAN_UNREACHABLE" not in ms["remediations"], "chronic codes are not written as remediations by the nag answer")
na = svc("nas")
ok(na["remediations"] == {"HOST_DOWN": None, "PORT_CLOSED": None}, "class=tell nulls every code", na["remediations"])
ch = svc("charger")
ok(ch["remediations"]["VALUE_LOW"] == "remediations/templates/ha-service-call.sh"
   and ch["remediations"]["ENTITY_BLIND"] is None and ch["params"]["HA_ENTITY"] == "switch.charger",
   "HA device: state codes get the turn_on template, the blind sensor stays tell-only")
ok("HA_URL" not in r.stdout, "no 'set by hand' note when HA_URL/HA_TOKEN_FILE are already in params")
bd = svc("box-disk")
ok(bd["realert_minutes_by_code"] == {"LOW_SPACE": 1440} and bd["notify_by_code"] == {"LOW_SPACE": "digest"},
   "nag=digest -> daily realert + digest routing")
ok(svc("wifi")["remediations"] == {"WIFI_WEDGED": {"ask": "remediations/wifi-reset.sh"}},
   "the hand-set ask entry was NOT given a consent field")
ok(svc("wifi")["_interview"]["doctrine.drill"]["value"] == "never", "drill is recorded as a doctrine-only decision")
ok("remediations" not in svc("decided") or svc("decided")["remediations"] == {"DNS_DOWN": None, "UPSTREAM_DOWN": None},
   "a hand-decided device is untouched")
ok("_interview" not in svc("decided"), "no provenance map on a device the interview never touched")
r = run("fill")
ok("nothing to fill" in r.stdout, "fill is idempotent")

print("== the config the interview wrote still loads in the engine ==")
r = subprocess.run([sys.executable, str(DEPLOY / "engine" / "selfheal.py"), "--once"],
                   capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "SELFHEAL_GW_OVERRIDE": "127.0.0.1",
                                                         "SELFHEAL_GRACE_MINUTES": "0"},
                   cwd=str(DEPLOY), timeout=120)
ok(r.returncode == 0 and (DEPLOY / "state" / "state.json").exists(),
   "selfheal.py --once exits 0 on the filled config", r.stderr[-300:])

print("== manual config wins: a hand edit is never overwritten ==")
data = cfg()
s = next(x for x in data["services"] if x["name"] == "media-server")
s["realert_minutes_by_code"]["LAN_UNREACHABLE"] = 720          # the admin changed their mind by hand
CFG.write_text(json.dumps(data, indent=2) + "\n")
run("answer", "media-server", "nag", "hourly")
r = run("fill", "--device", "media-server", "--overwrite", "--apply")
ms = svc("media-server")
ok(ms["realert_minutes_by_code"]["LAN_UNREACHABLE"] == 720, "a hand-edited value survives --overwrite")
ok(ms["realert_minutes_by_code"]["TRANSPORT_BLIND"] == 60, "the interview's own untouched fill is revised")
ok("hand-set" in r.stdout, "the kept field is reported as hand-set")
ok(run("fill", "--overwrite", expect=2).returncode == 2, "--overwrite without --device is refused")

print("== reask: re-interview ONE device ==")
r = run("reask", "nas")
ok("open again: class" in r.stdout, "reask reopens the device's class question")
plan = json.loads(run("plan", "--json").stdout)
ok([p["device"] for p in plan] == ["nas"], "only the re-asked device is open again", plan)
ok("nas" in json.loads((DEPLOY / "interview.json").read_text())["reask"], "the redo is recorded")
ok(len(json.loads((DEPLOY / "interview.json").read_text())["archived"]) == 1, "its old answers are archived, not deleted")
run("answer", "nas", "class", "ask")
run("answer", "nas", "how", "script:remediations/nas-restart.sh")
run("answer", "nas", "host", "none")
run("answer", "nas", "consent", "named:sam")
run("answer", "nas", "drill", "ok")
r = run("fill", "--apply")
na = svc("nas")
ok(na["remediations"]["PORT_CLOSED"] == {"ask": "remediations/nas-restart.sh", "consent": "named:sam"},
   "the re-interview replaced the interview's earlier null", na["remediations"])
ok(na["remediations"]["HOST_DOWN"] is None, "host=none keeps HOST_DOWN tell-only")
ok("nas" not in json.loads((DEPLOY / "interview.json").read_text())["reask"], "the redo closes once applied")
ok(svc("gateway")["remediations"]["WEDGED"] == "remediations/templates/restart-systemd-unit.sh",
   "other devices were not touched by the re-interview")

print("== fix/ask refusals ==")
write_cfg(HANDWRITTEN)
(DEPLOY / "interview.json").unlink()
run("answer", "nas", "class", "fix")
r = run("answer", "nas", "how", "docker:nas", expect=64)
ok(r.returncode == 64 and "ask-first" in r.stderr, "docker fix is refused for the auto class, with the reason")

print("== retire: a device failing for a week ranks first ==")
state_dir = DEPLOY / "state"
state_dir.mkdir(exist_ok=True)
old = (datetime.now(timezone.utc) - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
(state_dir / "state.json").write_text(json.dumps({"keys": {
    # the incident began 9 days ago; the hourly re-page moved last_transition an hour ago
    "box-disk": {"status": "awaiting_approval", "consecutive_failures": 9, "last_status_code": "VOLUME_ABSENT",
                 "attempts": [], "last_alert": recent, "last_transition": recent, "first_failed_at": old},
    # an older engine's record: a 9-day-old last_transition and no first_failed_at proves nothing
    "nas": {"status": "awaiting_approval", "consecutive_failures": 9, "last_status_code": "PORT_CLOSED",
            "attempts": [], "last_alert": old, "last_transition": old}}}))
plan = json.loads(run("plan", "--json").stdout)
names = [p["device"] for p in plan]
ok(plan[0]["device"] == "box-disk" and plan[0]["open"][0] == "retire", "retire comes first for a long-failing device", plan[0])
ok(any("failing now" in w for w in plan[0]["why"]), "live trouble is the stated reason")
ok(f"down since {old[:10]} with nothing done" in plan[0]["why"],
   "the why dates the incident from first_failed_at, not the re-paged last_transition", plan[0]["why"])
ok("nas" in names and "retire" not in plan[names.index("nas")]["open"]
   and not any("down since" in w for w in plan[names.index("nas")]["why"]),
   "no first_failed_at -> no retire question, however old last_transition is", plan[names.index("nas")])
ok(run("answer", "nas", "retire", "keep", expect=2).returncode == 2, "...and a retire answer for it is refused as not applicable")
q = json.loads(run("next", "--json").stdout)
ok(q["kind"] == "retire" and "VOLUME_ABSENT" in q["ask"], "the retire question names the live finding")
ok(f"since {old[:10]}" in q["ask"], "the retire question dates the incident from first_failed_at", q["ask"])
run("answer", "box-disk", "retire", "retire — that drive is gone")
run("fill", "--apply")
ok(svc("box-disk")["enabled"] is False, "retire=retire -> enabled: false")

print("== retire=keep: a doctrine-only decision, recorded once ==")
(state_dir / "state.json").write_text(json.dumps({"keys": {
    "charger": {"status": "awaiting_approval", "consecutive_failures": 9, "last_status_code": "VALUE_LOW",
                "attempts": [], "last_alert": recent, "last_transition": recent, "first_failed_at": old}}}))
q = json.loads(run("next", "--device", "charger", "--json").stdout)
ok(q["kind"] == "retire", "a second long incident gets its own retire question", q)
run("answer", "charger", "retire", "keep — it's unplugged for the winter, not gone")
r = run("fill", "--apply")
ok("applied" in r.stdout and svc("charger")["_interview"]["doctrine.retire"]["value"] == "keep",
   "retire=keep is recorded as a doctrine-only decision", r.stdout)
ok(svc("charger").get("enabled", True) is True, "retire=keep leaves the device enabled")
backups = sorted(p.name for p in DEPLOY.glob("services.json.backup-*"))
before = CFG.read_text()
r = run("fill", "--apply")
ok("nothing to fill" in r.stdout and "applied" not in r.stdout,
   "a second fill --apply after retire=keep has nothing to do", r.stdout)
ok(sorted(p.name for p in DEPLOY.glob("services.json.backup-*")) == backups, "...and makes no second backup")
ok(CFG.read_text() == before, "...and leaves services.json byte-identical")

print("== doctrine draft quotes the admin ==")
r = run("doctrine")
ok("| gateway | auto |" in r.stdout or "| gateway | undecided |" in r.stdout, "doctrine lists every service's class")
ok("that drive is gone" in r.stdout, "retirement list quotes the answer verbatim")
ok("not covered by the per-device interview" in r.stdout, "house-level sections are honestly marked uncovered")

print("== digest delivery of one question is opt-in and lock-safe ==")
digest = DEPLOY / "state" / "digest.jsonl"
run("next", "--digest", str(digest))
line = json.loads(digest.read_text().splitlines()[-1])
ok(line["system"] == "interview" and (":class)" in line["message"] or ":how)" in line["message"]),
   "the digest line carries the question id and text", line)

# ---- apply-time safety checks ------------------------------------------------------------

print("== apply-time safety checks: a wrong auto fix never reaches services.json ==")
HA = {"HA_URL": "http://192.168.1.35:8123", "HA_TOKEN_FILE": "/etc/ha.env"}
SAFETY = [
    # C1: one hand-set auto string, the other codes undecided
    {"name": "half-wired", "check": "checks/templates/check-systemd-unit.sh",
     "params": {"CHECK_KEY": "half-wired", "UNIT": "half"},
     "remediations": {"UNIT_INACTIVE": "remediations/templates/restart-systemd-unit.sh"}},
    # C2: class fix with a script that does not exist / lives outside remediations/
    {"name": "c2-missing", "check": "checks/templates/check-http.sh",
     "params": {"CHECK_KEY": "c2-missing", "HTTP_URL": "http://127.0.0.1:8081/health"}},
    {"name": "c2-outside", "check": "checks/templates/check-http.sh",
     "params": {"CHECK_KEY": "c2-outside", "HTTP_URL": "http://127.0.0.1:8082/health"}},
    # C3: the same restart answer for a service on this box and on another host
    {"name": "local-web", "check": "checks/templates/check-http.sh",
     "params": {"CHECK_KEY": "local-web", "HTTP_URL": "http://localhost:8080/health", "HOST_IP": "127.0.0.1"}},
    {"name": "media-server", "check": "checks/templates/check-http.sh",
     "params": {"CHECK_KEY": "media-server", "HTTP_URL": "http://192.168.1.58:4533/ping", "HOST_IP": "192.168.1.58"}},
    # C4: a host actuator through HA on a device without HA params
    {"name": "nas", "check": "checks/templates/check-tcp-port.sh",
     "params": {"CHECK_KEY": "nas", "TCP_HOST": "192.168.1.60", "TCP_PORT": "445"}},
    # C5: an entity the check expects OFF
    {"name": "heater", "check": "checks/templates/check-ha-entity.sh",
     "params": dict(HA, CHECK_KEY="heater", ENTITY_ID="switch.heater", EXPECT_STATE="off")},
    # ssh: a service on another host
    {"name": "pi-svc", "check": "checks/templates/check-http.sh",
     "params": {"CHECK_KEY": "pi-svc", "HTTP_URL": "http://192.168.1.70:9000/", "HOST_IP": "192.168.1.70"}},
    # outlet: a dark-host actuator, HA params present
    {"name": "tv-box", "check": "checks/templates/check-http.sh",
     "params": dict(HA, CHECK_KEY="tv-box", HTTP_URL="http://192.168.1.80:8008/", HOST_IP="192.168.1.80")},
    # a pinned argument on an ask-first script
    {"name": "arg-box", "check": "checks/templates/check-http.sh",
     "params": {"CHECK_KEY": "arg-box", "HTTP_URL": "http://127.0.0.1:8083/"}},
    # a hand-set ask entry whose script is missing: the lint's advice case
    {"name": "wifi", "check": "checks/check-wifi.sh", "params": {},
     "remediations": {"WIFI_WEDGED": {"ask": "remediations/wifi-reset.sh"}}},
]
write_cfg(SAFETY)
(DEPLOY / "interview.json").unlink(missing_ok=True)
ANS = DEPLOY / "interview.json"


def set_param(name, **kv):
    """The admin sets params by hand."""
    data = cfg()
    next(x for x in data["services"] if x["name"] == name)["params"].update(kv)
    CFG.write_text(json.dumps(data, indent=2) + "\n")


print("-- C1: an inferred auto class does not auto-wire the remaining codes --")
plan = {p["device"]: p for p in json.loads(run("plan", "--json").stdout)}
ok(plan["half-wired"]["open"][:2] == ["class", "how"] and any("inferred" in w for w in plan["half-wired"]["why"]),
   "a device with one hand-set auto entry is asked its class explicitly, and plan says why", plan.get("half-wired"))
run("answer", "half-wired", "how", "systemd:half")
r = run("fill", "--device", "half-wired", "--apply")
ok(svc("half-wired")["remediations"] == {"UNIT_INACTIVE": "remediations/templates/restart-systemd-unit.sh"}
   and "! " in r.stdout and "NOT auto-wired" in r.stdout and "inferred" in r.stdout and "nothing to fill" in r.stdout,
   "how= without an explicit class answer writes no auto entry, and says why", r.stdout)
run("answer", "half-wired", "class", "fix")
run("fill", "--device", "half-wired", "--apply")
ok(svc("half-wired")["remediations"] == {c: "remediations/templates/restart-systemd-unit.sh" for c in ("PORT_DEAD", "UNIT_INACTIVE", "WEDGED")},
   "once the admin says fix, the remaining codes are auto-wired", svc("half-wired")["remediations"])

print("-- C2: an auto script must exist and sit under remediations/ --")
run("answer", "c2-missing", "class", "fix")
run("answer", "c2-missing", "how", "script:remediations/not-there.sh")
r = run("fill", "--device", "c2-missing", "--apply")
rem = svc("c2-missing").get("remediations", {})
ok("SERVICE_DOWN" not in rem and "API_ERROR" not in rem and "is not a file" in r.stdout,
   "a script that does not exist is skipped, with a note", (rem, r.stdout))
run("answer", "c2-outside", "class", "fix")
run("answer", "c2-outside", "how", "script:bin/outside-restart.sh")
r = run("fill", "--device", "c2-outside", "--apply")
ok(svc("c2-outside")["remediations"]["SERVICE_DOWN"] == {"ask": "bin/outside-restart.sh"}
   and "wired ask-first instead" in r.stdout,
   "an auto script outside remediations/ is downgraded to an ask dict, with a note", r.stdout)

print("-- C3: systemd:/docker: restart THIS box; refused for a service on another host --")
r = run("answer", "media-server", "how", "systemd:navidrome", expect=64)
ok(r.returncode == 64 and "runs on 192.168.1.58" in r.stderr and "ssh:<user@host>" in r.stderr,
   "systemd: for a remote http device is refused at answer time, pointing at ssh:", r.stderr)
# the fill-time backstop: an answer recorded while the device still looked local
ans = json.loads(ANS.read_text())
ans["devices"]["media-server"] = {
    "class": {"key": "fix", "value": None, "text": "fix", "at": "2026-01-01T00:00:00Z", "question": ""},
    "how": {"key": "systemd", "value": "navidrome", "text": "systemd:navidrome", "at": "2026-01-01T00:00:00Z", "question": ""}}
ANS.write_text(json.dumps(ans))
r = run("fill", "--device", "media-server", "--apply")
rem = svc("media-server").get("remediations", {})
ok("SERVICE_DOWN" not in rem and "API_ERROR" not in rem and "restarts something on this box" in r.stdout,
   "...and fill refuses it again when the answer is already recorded (backstop)", (rem, r.stdout))
run("answer", "local-web", "class", "fix")
run("answer", "local-web", "how", "systemd:local-web")
r = run("fill", "--device", "local-web", "--apply")
ok(svc("local-web")["remediations"]["SERVICE_DOWN"] == "remediations/templates/restart-systemd-unit.sh"
   and svc("local-web")["params"]["UNIT"] == "local-web",
   "the same answer for a loopback device is written", r.stdout)

print("-- C4: a fix whose by-hand params are missing is not written --")
run("answer", "nas", "class", "ask")
run("answer", "nas", "how", "none")
run("answer", "nas", "host", "ha:switch.nas_outlet")
r = run("fill", "--device", "nas", "--apply")
rem = svc("nas").get("remediations", {})
ok("HOST_DOWN" not in rem and rem.get("PORT_CLOSED", 1) is None and "not written until params.HA_URL and params.HA_TOKEN_FILE" in r.stdout
   and "mode-600 file" in r.stdout,
   "ha: without HA_URL/HA_TOKEN_FILE in params is not written; the note says what to set", (rem, r.stdout))
set_param("nas", **HA)
r = run("fill", "--device", "nas", "--apply")
ok(svc("nas")["remediations"]["HOST_DOWN"] == {"ask": "remediations/templates/ha-service-call.sh"}
   and svc("nas")["params"]["HA_ENTITY"] == "switch.nas_outlet" and "HA_URL" not in r.stdout,
   "once the params are set by hand, the next fill writes it", r.stdout)

print("-- C5: turn_on is the wrong direction for an entity expected OFF --")
run("answer", "heater", "class", "fix")
run("answer", "heater", "how", "ha:switch.heater_relay")
r = run("fill", "--device", "heater", "--apply")
h = svc("heater")
ok(h["remediations"]["VALUE_LOW"] == "remediations/templates/ha-service-call.sh"
   and h["remediations"]["VALUE_CRITICAL"] == "remediations/templates/ha-service-call.sh"
   and "STATE_MISMATCH" not in h["remediations"] and "wrong direction" in r.stdout and "EXPECT_STATE=off" in r.stdout,
   "ha: on a device with EXPECT_STATE=off skips STATE_MISMATCH with a note and wires the rest", (h["remediations"], r.stdout))

print("-- ssh: a forced-command key on the other host --")
run("answer", "pi-svc", "class", "fix")
r = run("answer", "pi-svc", "how", "ssh:nouser", expect=64)
ok(r.returncode == 64 and "user@host" in r.stderr, "ssh: wants user@host", r.stderr)
run("answer", "pi-svc", "how", "ssh:pi@192.168.1.70.")
r = run("fill", "--device", "pi-svc", "--apply")
rem = svc("pi-svc").get("remediations", {})
ok("SERVICE_DOWN" not in rem and "not written until params.SSH_KEY" in r.stdout,
   "ssh: needs SSH_KEY set by hand first", (rem, r.stdout))
set_param("pi-svc", SSH_KEY="/etc/cranston/pi-key")
r = run("fill", "--device", "pi-svc", "--apply")
p = svc("pi-svc")
ok(p["remediations"]["SERVICE_DOWN"] == "remediations/templates/ssh-forced-command.sh"
   and p["remediations"]["API_ERROR"] == "remediations/templates/ssh-forced-command.sh"
   and p["params"]["SSH_TARGET"] == "pi@192.168.1.70" and p["params"]["REMEDIATION_KEY"] == "pi-svc",
   "ssh: wires ssh-forced-command.sh with SSH_TARGET (trailing punctuation stripped)", p)

print("-- outlet: cycles power, so ask-first only --")
run("answer", "tv-box", "class", "fix")
r = run("answer", "tv-box", "how", "outlet:switch.tv", expect=64)
ok(r.returncode == 64 and CATALOG["fixes"]["outlet"]["refuse"] in r.stderr,
   "outlet: for class fix is refused at answer time with the catalogue's reason", r.stderr)
r = run("answer", "tv-box", "host", "outlet:light.tv", expect=64)
ok(r.returncode == 64 and "switch.<outlet>" in r.stderr, "outlet: must name a switch.* entity", r.stderr)
run("answer", "tv-box", "how", "none")
run("answer", "tv-box", "host", "outlet:switch.tv_outlet.")
run("answer", "tv-box", "consent", "household")
r = run("fill", "--device", "tv-box", "--apply")
tv = svc("tv-box")
ok(tv["remediations"]["HOST_DOWN"] == {"ask": "remediations/templates/ha-outlet-cycle.sh", "consent": "household"}
   and tv["params"]["OUTLET_ENTITY"] == "switch.tv_outlet" and tv["params"]["REMEDIATION_KEY"] == "tv-box",
   "outlet: as the host answer -> an ask dict with OUTLET_ENTITY (trailing punctuation stripped)", tv)

print("-- script:<path> <arg>: a pinned argument, ask-first only --")
run("answer", "arg-box", "class", "fix")
r = run("answer", "arg-box", "how", "script:remediations/restart-media.sh outlet-7", expect=64)
ok(r.returncode == 64 and "pinned argument" in r.stderr, "a pinned arg on an auto-class string is refused", r.stderr)
run("answer", "arg-box", "class", "ask")
run("answer", "arg-box", "how", "script:remediations/restart-media.sh because the docker one is slow")
ok("arg" not in json.loads(ANS.read_text())["devices"]["arg-box"]["how"],
   "a word of explanation after the path is not mistaken for a pinned arg")
r = run("answer", "arg-box", "how", "script:remediations/restart-media.sh outlet-7 — the TV strip")
ok("arg=outlet-7" in r.stdout and json.loads(ANS.read_text())["devices"]["arg-box"]["how"]["arg"] == "outlet-7",
   "the arg is recorded and echoed", r.stdout)
r = run("fill", "--device", "arg-box", "--apply")
ok(svc("arg-box")["remediations"]["SERVICE_DOWN"] == {"ask": "remediations/restart-media.sh", "arg": "outlet-7"},
   "script:<path> <arg> pins the argument on the ask-first entry", svc("arg-box")["remediations"])

print("-- C6: the lint after --apply --")
ok("lint:" in r.stdout and "wifi: remediations.WIFI_WEDGED" in r.stdout and "is not a file" in r.stdout
   and r.returncode == 0,
   "a hand-set entry whose script is missing is reported as advice, exit 0", r.stdout)
data = cfg()
next(x for x in data["services"] if x["name"] == "wifi")["remediations"]["WIFI_DOWN"] = \
    "remediations/templates/ha-outlet-cycle.sh"          # a hand miswire of the ask-first-only template
next(x for x in data["services"] if x["name"] == "wifi")["remediations"]["WIFI_ODD"] = \
    {"ask": "remediations/restart-media.sh", "arg": "-rf"}
CFG.write_text(json.dumps(data, indent=2) + "\n")
run("answer", "arg-box", "drill", "ok")
r = run("fill", "--device", "arg-box", "--apply")
ok("WIFI_DOWN" in r.stdout and "ask-first only" in r.stdout, "the lint flags an auto string on the ask-first-only outlet template", r.stdout)
ok("WIFI_ODD" in r.stdout and "pinned arg '-rf'" in r.stdout, "the lint flags a malformed pinned arg", r.stdout)
ok(r.returncode == 0, "...as advice: hand-set problems do not fail the fill")
ok("Traceback" not in r.stdout + r.stderr, "no tracebacks")

print("== add: devices enter the config through the interview alone ==")
write_cfg([])                                    # a populated home, an empty config
(DEPLOY / "interview.json").unlink(missing_ok=True)
(DEPLOY / "remediations" / "lab-restart.sh").write_text("#!/bin/bash\nexit 0\n")
ok(json.loads(run("plan", "--json").stdout) == [], "nothing to ask on an empty config")
ok(run("add", "bad name", "tcp", expect=64).returncode == 64, "a bad service name is refused")
ok(run("add", "nas", "mainframe", expect=64).returncode == 64, "an unknown kind is refused")
r = run("add", "nas", "tcp")
ok("staged nas" in r.stdout and "[nas:setup]" in r.stdout and "TCP_HOST" in r.stdout and "TCP_PORT" in r.stdout,
   "add stages the device and asks its setup question naming the required params")
ok(run("add", "nas", "tcp", expect=2).returncode == 2, "adding a staged name twice is refused")
plan = json.loads(run("plan", "--json").stdout)
ok(plan and plan[0]["device"] == "nas" and plan[0]["open"] == ["setup"]
   and any("newly added" in w for w in plan[0]["why"]), "a staged device shows in plan with only setup open", plan)
ok(run("answer", "nas", "setup", "TCP_HOST=192.168.1.60 CAP_MAX=3", expect=64).returncode == 64,
   "setup refuses a key the check does not document")
ok(run("answer", "nas", "setup", "SELFHEAL_ROOT=/x", expect=64).returncode == 64, "setup refuses an engine-owned key")
r = run("answer", "nas", "setup", "TCP_HOST=192.168.1.60 it's the NAS")
ok("still needed for nas: TCP_PORT" in r.stdout, "a partial setup says what is still needed")
run("answer", "nas", "setup", "TCP_HOST=192.168.1.60 TCP_PORT=445")
q = json.loads(run("next", "--device", "nas", "--json").stdout)
ok(q["kind"] == "extras" and "TCP_TIMEOUT" in q["ask"], "extras comes next, listing the optional params")
r = run("answer", "nas", "extras", "CAP_MAX=3 TCP_TIMEOUT=5")
ok("note: CAP_MAX" in r.stdout, "an undocumented extras key is kept with a note")
q = json.loads(run("next", "--device", "nas", "--json").stdout)
ok(q["kind"] == "class" and "192.168.1.60:445" in q["ask"], "the class question uses the setup answers as its nouns")
run("answer", "nas", "class", "ask")
run("answer", "nas", "how", "script:remediations/lab-restart.sh nas")
run("answer", "nas", "host", "none")
run("answer", "nas", "consent", "household — everyone's photos")
q = json.loads(run("next", "--device", "nas", "--json").stdout)
ok(q["kind"] == "announce", "household consent on an ask-first fix asks for the announcement")
ok(run("answer", "nas", "announce", "🚨 NAS restart PORT_CLOSED", expect=64).returncode == 64,
   "an alert-shaped announcement is refused")
ok(run("answer", "nas", "announce", "Reply 'heal nas' to approve", expect=64).returncode == 64,
   "mechanics in an announcement are refused")
run("answer", "nas", "announce", "Heads-up: the NAS is restarting, photos will blip for a minute.")
run("answer", "nas", "drill", "ok")
run("add", "pi-dns", "dns")
run("answer", "pi-dns", "setup", "RESOLVER_IP=192.168.1.2")
run("answer", "pi-dns", "extras", "none")
run("answer", "pi-dns", "class", "fix")
run("answer", "pi-dns", "how", "script:remediations/lab-restart.sh")
run("answer", "pi-dns", "drill", "freely")
q = json.loads(run("next", "--device", "pi-dns", "--json").stdout)
ok(q["kind"] == "nag" and "UPSTREAM_DOWN" in q["ask"], "the WAN code gets a nag question")
run("answer", "pi-dns", "nag", "digest")
run("add", "root-disk", "disk")
run("answer", "root-disk", "setup", "MOUNT_PATH=/")
run("answer", "root-disk", "extras", "none")
q = json.loads(run("next", "--device", "root-disk", "--json").stdout)
ok(q["kind"] == "local", "a disk device is asked whether it is on this box")
run("answer", "root-disk", "local", "yes")
run("answer", "root-disk", "nag", "digest")
ok("(staged - not in services.json yet)" in run("status").stdout, "status marks staged devices")
r = run("fill")
ok(len(cfg()["services"]) == 0 and "+ service nas" in r.stdout, "a draft fill writes nothing to services.json")
r = run("fill", "--apply")
services = {s["name"]: s for s in cfg()["services"]}
ok(set(services) == {"nas", "pi-dns", "root-disk"}, "apply writes the three added services", sorted(services))
nas = services["nas"]
ok(nas["check"] == "checks/templates/check-tcp-port.sh" and nas["params"]["CHECK_KEY"] == "nas"
   and nas["params"]["TCP_PORT"] == "445" and nas["params"]["CAP_MAX"] == "3"
   and nas["params"]["REMEDIATION_KEY"] == "nas", "the service carries CHECK_KEY, setup, extras and fix params", nas["params"])
ok(nas["remediations"]["PORT_CLOSED"] == {"ask": "remediations/lab-restart.sh", "arg": "nas", "consent": "household",
                                          "announce": "Heads-up: the NAS is restarting, photos will blip for a minute."},
   "the ask-first entry carries consent and the announcement", nas["remediations"])
ok(nas["remediations"]["HOST_DOWN"] is None and nas["consent_notes"] == "everyone's photos", "host=none and consent_notes landed")
ok(nas["_interview"]["check"]["why"] == "add tcp" and nas["_interview"]["params.TCP_PORT"]["why"] == "setup",
   "provenance records the add and the setup answers")
pi = services["pi-dns"]
ok(pi["remediations"] == {"DNS_DOWN": "remediations/lab-restart.sh", "UPSTREAM_DOWN": None}
   and pi["notify_by_code"] == {"UPSTREAM_DOWN": "digest"} and pi["realert_minutes_by_code"] == {"UPSTREAM_DOWN": 1440},
   "the WAN code is tell-only, routed to the digest, nagged daily", pi)
ok(services["root-disk"].get("local") is True, "local=yes -> local: true")
ok(json.loads((DEPLOY / "interview.json").read_text()).get("added") == {}, "nothing stays staged after apply")
ok(json.loads(run("plan", "--json").stdout) == [], "nothing left to ask")
ok(run("add", "nas", "tcp", expect=2).returncode == 2, "adding a name already in the config is refused")
write_cfg([{"name": "stub", "check": "checks/templates/check-dns.sh", "params": {"CHECK_KEY": "stub"}}])
(DEPLOY / "interview.json").unlink(missing_ok=True)
plan = json.loads(run("plan", "--json").stdout)
ok(plan and plan[0]["open"] == ["setup"], "a hand-written entry missing RESOLVER_IP is asked for it first", plan)
run("answer", "stub", "setup", "RESOLVER_IP=10.0.0.53")
run("answer", "stub", "class", "tell")
run("answer", "stub", "nag", "hourly")
r = run("fill", "--apply")
ok(svc("stub")["params"]["RESOLVER_IP"] == "10.0.0.53", "setup fills the missing param on a hand-written entry")
ok("Traceback" not in r.stdout + r.stderr, "no tracebacks in the add flow")

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"{PASS} passed, {len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print(f"ALL {PASS} ASSERTIONS PASSED")
