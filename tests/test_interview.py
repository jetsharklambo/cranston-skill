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
        ok((SCRIPTS / fix["script"]).exists(), f"fix '{key}' points at a shipped remediation")

print("== catalog questions cite real interview.md questions ==")
interview_md = (REPO / "authoring" / "references" / "interview.md").read_text()
for kind, q in CATALOG["questions"].items():
    for u in q["u"].split():
        ok(re.search(rf"^## {u}\.", interview_md, re.M) is not None, f"{kind} cites {u} (exists in interview.md)")
ok("onboard/interview.py" in interview_md, "interview.md documents the tool")

# ---- a throwaway deployment ----------------------------------------------------------

tmp = Path(tempfile.mkdtemp(prefix="cranston-interview-"))
DEPLOY = tmp / "deploy"
shutil.copytree(SCRIPTS, DEPLOY)
TOOL = DEPLOY / "onboard" / "interview.py"
CFG = DEPLOY / "services.json"


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
     "remediations": {"DNS_DOWN": None, "UPSTREAM_DOWN": None}},
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
r = run("answer", "media-server", "how", "docker:navidrome", expect=0)
ok(r.returncode == 0, "docker fix accepted for ask-first")
run("answer", "media-server", "how", "script:remediations/restart-media.sh")
run("answer", "media-server", "host", "severs — that outlet feeds the router too")
run("answer", "media-server", "consent", "household: it's the living room")
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
ok("docker" not in r.stdout, "a superseded answer leaves no trace in the fill")

print("== fill --apply: the mappings ==")
r = run("fill", "--apply")
ok(len(list(DEPLOY.glob("services.json.backup-*"))) == 1, "a backup of services.json is kept")
gw = svc("gateway")
ok(gw["remediations"] == {c: "remediations/templates/restart-systemd-unit.sh" for c in ("PORT_DEAD", "UNIT_INACTIVE", "WEDGED")},
   "class=fix + how=systemd wires every fixable code to the shipped template", gw["remediations"])
ok(gw["params"]["REMEDIATION_KEY"] == "gateway" and gw["params"]["UNIT"] == "openclaw-gateway",
   "params the template needs are added; the existing UNIT is reused")
ms = svc("media-server")
ok(ms["remediations"]["SERVICE_DOWN"] == {"ask": "remediations/restart-media.sh", "consent": "household"},
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

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"{PASS} passed, {len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print(f"ALL {PASS} ASSERTIONS PASSED")
