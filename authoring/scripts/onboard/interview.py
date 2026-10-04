#!/usr/bin/env python3
"""interview.py - the per-device interview: fill the gaps in a hand-written
services.json, one device at a time.

Manual config is the default. This tool never runs on its own and never asks
about anything the admin already decided. It reads services.json, works out
per device which decisions are still OPEN (a finding code with no entry in
`remediations`, an ask-first fix with no consent scope, a chronic code with no
nag cadence, a device that has been failing for a week...), ranks the devices
by how much a wrong default would cost, and hands the agent ONE device-shaped
question at a time. Answers are recorded verbatim; `fill` maps them onto
config fields and shows the diff. Nothing touches services.json without
--apply, and a field the admin set by hand is never overwritten: the tool may
only revise fills it made itself (tracked in the service's `_interview` map),
and only for a device named with `--device D --overwrite`.

  plan                         rank the devices that still need a decision, and why
  next [--device D] [--digest FILE]
                               the next question (one device, one kind); --digest
                               appends it to the daily digest instead of printing
  answer D KIND "the words"    record the admin's answer verbatim (keyword first)
  fill [--device D] [--overwrite] [--apply]
                               map answers -> config; writes <config>.draft.json,
                               or services.json itself with --apply (backup kept)
  reask D                      archive D's answers so its questions come back
  status                       answered / open, per device
  doctrine [--out FILE]        draft the house-doctrine sections the answers cover

Device types come from onboard/catalog.json (which codes a check template
emits, at which layer, and which shipped fixes fit). A bespoke check the
catalog does not know is still handled for what the config shows (consent on
its ask-first entries, drill policy) - its codes have to be listed in
`remediations` by hand (null = tell-only).

`fill` also refuses to write a remediation the engine would run wrongly: an
auto-class string needs the admin's explicit `fix` (never a class inferred
from one hand-set entry), must point at an existing script under
remediations/ (the gate's auto-pass covers nothing else), may not restart
something on this box for a device that lives on another host, needs its
by-hand params (HA_URL, a token FILE, an SSH key) present first, and never
turns ON an entity the check expects OFF. Each refusal is a `!` line; after
--apply a structural lint of the written config is printed.

Paths: --config (default <install-root>/services.json), --answers (default
<config dir>/interview.json); live state is read from the config's
paths.state_dir when present. Stdlib only; python3 3.9+.
"""

import argparse
import fcntl
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent          # onboard/
ROOT = BASE.parent                              # install root
CATALOG = json.loads((BASE / "catalog.json").read_text())
FIXABLE = set(CATALOG["fixable_layers"])
FIXES = CATALOG["fixes"]

# ask order within one device: a retirement question may make the rest moot
KINDS = ["retire", "class", "how", "host", "consent", "drill", "nag"]

# The one argument a remediation may take. Keep the text identical to
# engine/selfheal.py's ARG_RE: a pinned "arg" the engine would refuse is a
# config bug the interview must never write.
ARG_RE = r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}"


def _kinds(question):
    """The fix kinds the catalog lets a question accept (grammar alternation)."""
    return "|".join(k for k, f in FIXES.items() if question in f.get("questions", ["how"]))


GRAMMAR = {
    "retire":  r"^(retire|keep)\b",
    "class":   r"^(fix|ask|tell)\b",
    "how":     rf"^(?:({_kinds('how')}):(\S+)|(none)\b)",
    "host":    rf"^(?:({_kinds('host')}):(\S+)|(none|severs)\b)",
    "consent": r"^(?:(named):(\S+)|(admin|household)\b)",
    "drill":   r"^(freely|ok|never)\b",
    "nag":     r"^(daily|hourly|digest)\b",
}
# what an admin's sentence leaves stuck to a value: "ha:switch.x." -> switch.x
VALUE_STRIP_LEAD, VALUE_STRIP_TAIL = "'\"([{", ".,;:!?)]}'\""
# a pinned arg must be the last word, or set off from the admin's words by one of these
ARG_SEPARATORS = "-—–:;,|(\"'"
# a `needs` param satisfied by an alternative the admin set instead
ALT_PARAMS = {"HA_TOKEN_FILE": ("HA_TOKEN",)}

RETIRE_DAYS = int(os.environ.get("INTERVIEW_RETIRE_DAYS", "7"))
OMIT = object()   # "decided: leave this code out of the map" (on-demand class)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def die(msg, code=2):
    print(f"interview.py: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        die(f"cannot read {path}: {e}")


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def resolve(p, root):
    """A config path the way the engine resolves it: relative to the install root."""
    p = Path(p)
    return (p if p.is_absolute() else root / p).resolve()


def under_remediations(path, root):
    """True when `path` sits under <root>/remediations/ - the only place the
    approval gate's auto-pass covers; an auto string anywhere else can never
    run unattended (and an auto string IS a request to run unattended)."""
    try:
        return Path(path).resolve().is_relative_to((root / "remediations").resolve())
    except (OSError, ValueError):
        return False


def clean_value(v):
    """Strip what an admin's sentence leaves stuck to a value ("ha:switch.x." -> switch.x)."""
    if not v:
        return v
    return v.lstrip(VALUE_STRIP_LEAD).rstrip(VALUE_STRIP_TAIL)


def host_of(value):
    """The host part of a params value: a URL's hostname, else the value itself
    (a bare address, [v6] brackets dropped)."""
    if not value:
        return None
    if "://" in value:
        try:
            return urlsplit(value).hostname
        except ValueError:
            return None
    return value.strip("[]") or None


def is_local_address(host):
    """True when `host` names THIS box: loopback, or an address the kernel
    routes to itself. The UDP connect sends nothing - it only asks the routing
    table which source address it would use, and that is the target itself
    exactly when the target is one of our own addresses. A name that does not
    resolve counts as remote: the restart would run here, blind."""
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h in ("localhost", "localhost.localdomain"):
        return True
    try:
        addrs = [str(ipaddress.ip_address(h))]
    except ValueError:
        try:
            addrs = sorted({i[4][0] for i in socket.getaddrinfo(h, None)})
        except OSError:
            return False
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_loopback:
            return True
        s = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((a, 9))
            if s.getsockname()[0] == a:
                return True
        except OSError:
            pass
        finally:
            s.close()
    return False


def has_param(params, need):
    return need in params or any(alt in params for alt in ALT_PARAMS.get(need, ()))


def fix_params(kind, value):
    return {k: v.format(value=value) for k, v in FIXES[kind]["params"].items()}


# ---- the deployment ---------------------------------------------------------

class Deployment:
    def __init__(self, config_path, answers_path=None):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        self.config = load_json(self.config_path, None)
        if self.config is None:
            die(f"no config at {self.config_path} - write services.json first "
                f"(the interview fills gaps in a config; it does not invent one)")
        self.answers_path = (Path(answers_path).resolve() if answers_path
                             else self.root / "interview.json")
        self.answers = load_json(self.answers_path, {"version": 1, "devices": {}, "archived": []})
        state_dir = self.config.get("paths", {}).get("state_dir", "state")
        state_dir = Path(state_dir) if Path(state_dir).is_absolute() else self.root / state_dir
        self.state = load_json(state_dir / "state.json", {"keys": {}}).get("keys", {})
        self.pending = load_json(state_dir / "pending-approvals.json", {})

    def services(self):
        return [s for s in self.config.get("services", []) if s.get("enabled", True)]

    def service(self, name):
        for s in self.config.get("services", []):
            if s.get("name") == name:
                return s
        die(f"no service named '{name}' in {self.config_path}")

    def device_answers(self, name):
        return self.answers["devices"].get(name, {})

    def save_answers(self):
        save_json(self.answers_path, self.answers)


# ---- one device's view --------------------------------------------------------

class Device:
    """Everything the interview knows about one service: its type, which
    decisions the config already holds, which the answers hold, and which are
    still open."""

    def __init__(self, dep, svc, redo=False):
        self.dep = dep
        self.svc = svc
        self.name = svc["name"]
        self.params = {k: str(v) for k, v in svc.get("params", {}).items()}
        self.rem = svc.get("remediations") or {}
        self.prov = svc.get("_interview") or {}
        self.tpl = CATALOG["templates"].get(svc.get("check", ""))
        self.answers = dep.device_answers(self.name)
        # redo: this device is being re-interviewed (`reask`, or `fill --device
        # D --overwrite`). Its interview-made fills count as open again; fields
        # the admin set by hand never do.
        self.redo = redo or self.name in dep.answers.get("reask", [])
        if self.tpl:
            self.codes = dict(self.tpl["codes"])
        else:
            # bespoke check: the admin's remediations map IS the code universe
            self.codes = {c: "unknown" for c in self.rem}
        self.live = self._live()

    # -- facts --------------------------------------------------------------

    def layer(self, code):
        return self.codes.get(code, "unknown")

    def codes_at(self, *layers):
        return sorted(c for c, l in self.codes.items() if l in layers)

    @property
    def fixable_codes(self):
        return sorted(c for c, l in self.codes.items() if l in FIXABLE)

    def current(self, path):
        """(present, value) of a config field by dotted path."""
        top, _, sub = path.partition(".")
        if top == "doctrine":
            return False, None          # doctrine-only decisions have no config field
        container = self.svc.get(top) if sub else self.svc
        key = sub or top
        if isinstance(container, dict) and key in container:
            return True, container[key]
        return False, None

    def mine(self, path):
        """True when the interview wrote this field itself AND it still holds
        what the interview wrote. A value the admin hand-edited afterwards is
        the admin's, whatever the provenance says."""
        e = self.prov.get(path)
        if not e:
            return False
        if path.startswith("doctrine."):
            return True
        present, val = self.current(path)
        if e.get("omit"):
            return not present
        return present and val == e.get("value")

    def is_open(self, path):
        """A field is open when nobody decided it, or when this device is being
        redone and the earlier decision was the interview's own."""
        if self.redo and self.mine(path):
            return True
        present, _ = self.current(path)
        return not present and not self.mine(path)

    def decided(self, code):
        return not self.is_open(f"remediations.{code}")

    def undecided(self, *layers):
        return [c for c in (self.codes_at(*layers) if layers else sorted(self.codes))
                if not self.decided(c)]

    def ans(self, kind):
        a = self.answers.get(kind)
        return a["key"] if a else None

    def ans_value(self, kind):
        a = self.answers.get(kind)
        return a.get("value") if a else None

    def effective_class(self):
        """The admin's stance on this device: an answer wins; otherwise what the
        config already shows for its fixable codes."""
        if self.ans("class"):
            return self.ans("class")
        # a bespoke check has no layer map: every listed code speaks for it
        codes = self.fixable_codes if self.tpl else sorted(self.codes)
        values = [self.rem[c] for c in codes if c in self.rem and self.decided(c)]
        if any(isinstance(v, str) for v in values):
            return "fix"
        if any(isinstance(v, dict) for v in values):
            return "ask"
        if codes and all(self.decided(c) for c in codes):
            return "tell"
        return None

    def target_text(self):
        if not self.tpl:
            return f"bespoke check {self.svc.get('check', '?')}"
        if self.tpl.get("target_text"):
            return self.tpl["target_text"]
        parts = [self.params[p] for p in self.tpl["target"] if p in self.params]
        return ":".join(parts) if parts else "target not in params"

    def host_text(self):
        hp = self.tpl.get("host_param") if self.tpl else None
        return self.params.get(hp, "no host address in params") if hp else "n/a"

    def has_host_question(self):
        return bool(self.tpl and self.tpl.get("host_param") in self.params
                    and self.codes_at("host"))

    def target_host(self):
        """Where this device's service runs, from the template's
        target_host_param (a name or an ordered list; a URL counts by its
        hostname). None when the catalog has no idea (a local check)."""
        tp = self.tpl.get("target_host_param") if self.tpl else None
        for name in ([tp] if isinstance(tp, str) else (tp or [])):
            h = host_of(self.params.get(name))
            if h:
                return h
        return None

    def is_remote(self):
        """True when the device's target is another host - a restart on THIS
        box cannot be its fix. None when the target host is unknown."""
        h = self.target_host()
        if h is None:
            return None
        return not is_local_address(h)

    def will_have_fix(self):
        """A remediation exists or is about to be written from an answer."""
        if self.ans("class") == "tell" and self.ans("host") not in FIXES:
            # every open fixable code is about to become null; only fixes the
            # interview may not touch (hand-set) remain
            return any(v is not None and not self.is_open(f"remediations.{c}")
                       for c, v in self.rem.items())
        if any(v is not None for v in self.rem.values()):
            return True
        how = self.ans("how")
        if how and how != "none":
            return True
        host = self.ans("host")
        return bool(host and host in FIXES)

    def will_have_ask_without_consent(self):
        if any(isinstance(v, dict) and "consent" not in v for v in self.rem.values()):
            return True
        if self.ans("consent"):
            return False
        if self.effective_class() == "ask" and self.ans("how") not in (None, "none"):
            return True
        return self.ans("host") in FIXES

    def class_is_inferred(self):
        """The auto class came from a hand-set string, not from an answer. It
        may steer the host and consent questions, but `fill` will not wire more
        codes to auto on its strength - the admin says `fix` for the device."""
        return self.effective_class() == "fix" and not self.ans("class")

    def _live(self):
        """What the engine's state says right now: failing keys, pending asks,
        and when the root key's current incident began (`since`).

        `since` is the record's `first_failed_at`: the engine sets it on the
        first failing cycle, never moves it while the incident lasts, and drops
        it when the key returns to ok. `last_transition` is NOT a substitute -
        every re-page of an awaiting_approval key moves it (hourly by default),
        so an incident measured from it never looks a week old. A record
        without `first_failed_at` (state written by an older engine) has no
        `since`, and the retire question does not apply to it."""
        keys = {k: v for k, v in self.dep.state.items()
                if k == self.name or k.startswith(self.name + "/")}
        bad = {k: v for k, v in keys.items() if v.get("status") != "ok"}
        pending = [k for k in self.dep.pending if k == self.name or k.startswith(self.name + "/")]
        since = None
        root = keys.get(self.name)
        if (root and root.get("status") in ("awaiting_approval", "escalated", "failing")
                and root.get("first_failed_at")):
            try:
                since = parse_iso(root["first_failed_at"])
            except Exception:
                since = None
        return {"bad": bad, "pending": pending, "since": since}

    # -- which questions apply, and which are still open ---------------------

    def applies(self, kind):
        if kind == "retire":
            return self.live["since"] is not None
        if kind == "class":
            return bool(self.fixable_codes)
        if kind == "how":
            return bool(self.fixable_codes)
        if kind == "host":
            return self.has_host_question()
        if kind == "consent":
            return self.will_have_fix() or any(isinstance(v, dict) for v in self.rem.values())
        if kind == "drill":
            return self.will_have_fix()
        if kind == "nag":
            return bool(self.codes_at("chronic"))
        return False

    def open_kinds(self):
        """Kinds still worth asking, in ask order. An answered kind is never
        re-asked; a decision the config already holds is never asked."""
        out = []
        cls = self.effective_class()
        if self.applies("retire") and not self.ans("retire"):
            age = now() - self.live["since"]
            if age >= timedelta(days=RETIRE_DAYS):
                out.append("retire")
        if self.applies("class") and cls is None:
            out.append("class")
        elif (self.applies("class") and self.class_is_inferred()
                and self.undecided(*FIXABLE)):
            # one hand-set auto string must not auto-wire the rest: confirm it
            out.append("class")
        if (self.applies("how") and cls in ("fix", "ask") and not self.ans("how")
                and self.undecided(*FIXABLE)):
            out.append("how")
        if (self.applies("host") and cls in ("fix", "ask") and not self.ans("host")
                and self.undecided("host")):
            out.append("host")
        if self.applies("consent") and not self.ans("consent") and self.will_have_ask_without_consent():
            out.append("consent")
        if self.applies("drill") and not self.ans("drill") and self.is_open("doctrine.drill"):
            out.append("drill")
        if self.applies("nag") and not self.ans("nag"):
            if any(self.is_open(f"realert_minutes_by_code.{c}") for c in self.codes_at("chronic")):
                out.append("nag")
        return out

    # -- priority -------------------------------------------------------------

    def priority(self):
        """Score + reasons: the devices where a wrong default costs most come
        first. Live trouble beats everything; then devices a shipped fix would
        unlock; then undecided classes; then noise risks."""
        score, why = 0, []
        if self.live["pending"]:
            score += 100
            why.append("awaiting your approval right now")
        elif self.live["bad"]:
            code = next(iter(self.live["bad"].values())).get("last_status_code") or "failing"
            score += 100
            why.append(f"failing now ({code})")
        if "retire" in self.open_kinds():
            score += 60
            why.append(f"down since {iso(self.live['since'])[:10]} with nothing done")
        if self.tpl and self.tpl.get("default_fix") and self.effective_class() is None:
            score += 40
            why.append("a shipped remediation fits this device type")
        und = self.undecided(*FIXABLE)
        if und and self.effective_class() is None:
            score += 30
            why.append(f"no fix/ask/tell decision for {', '.join(und)}")
        elif und and self.class_is_inferred():
            score += 30
            why.append(f"its auto class is inferred from a hand-set entry - confirm fix/ask "
                       f"before {', '.join(und)} are wired the same way")
        if self.will_have_ask_without_consent() and not self.ans("consent"):
            score += 25
            why.append("an ask-first fix with no consent scope")
        if "host" in self.open_kinds():
            score += 20
            why.append(f"its host ({self.host_text()}) can go dark")
        if "nag" in self.open_kinds():
            score += 10
            why.append(f"{', '.join(self.codes_at('chronic'))} will nag at the default cadence")
        if not self.tpl and not self.rem:
            why.append("bespoke check with an empty remediations map - list its codes (null = tell) and I can ask about them")
        return score, why

    # -- the question text ------------------------------------------------------

    def question(self, kind):
        q = CATALOG["questions"][kind]
        fixable = ", ".join(self.fixable_codes) or "nothing fixable"
        options = " · ".join(f["option"] for k, f in FIXES.items()
                             if "how" in f.get("questions", ["how"])) + " · none"
        df = self.tpl.get("default_fix") if self.tpl else None
        if df:
            if df.get("from_param") and df["from_param"] in self.params:
                options = f"{df['kind']}:{self.params[df['from_param']]} is the obvious one; or {options}"
            else:
                options = f"{df['kind']}:<the switch to turn on> is the natural fit here; or {options}"
        if kind == "how" and self.is_remote():
            options += (f" ({self.name} runs on {self.target_host()}, not this box: systemd:/docker: "
                        f"do not apply - ssh:<user@host> runs a forced-command key there)")
        since = self.live["since"]
        root = self.dep.state.get(self.name, {})
        fields = {
            "name": self.name,
            "target": self.target_text(),
            "codes": ", ".join(sorted(self.codes)) or "whatever its check emits",
            "fixable": fixable,
            "options": options,
            "host": self.host_text(),
            "hostcodes": ", ".join(self.codes_at("host")) or "HOST_DOWN",
            "chronic": ", ".join(self.codes_at("chronic")) or "its degraded codes",
            "status": root.get("last_status_code") or root.get("status") or "failing",
            "since": iso(since)[:10] if since else "a while",
        }
        return {
            "id": f"{self.name}:{kind}",
            "device": self.name,
            "kind": kind,
            "u": q["u"],
            "ask": q["ask"].format(**fields),
            "accepts": q["accepts"],
            "maps_to": q["maps_to"].format(**fields),
        }


def devices(dep):
    return [Device(dep, s) for s in dep.services()]


def ranked(dep):
    ds = [d for d in devices(dep) if d.open_kinds()]
    ds.sort(key=lambda d: (-d.priority()[0], -len(d.open_kinds()), d.name))
    return ds


# ---- answers --------------------------------------------------------------------

def parse_answer(kind, text):
    """Keyword-first grammar: the first token(s) decide the mapping; the rest of
    the admin's words are kept verbatim for the doctrine. Returns (key, value,
    arg): the value with stray punctuation stripped ("ha:switch.x." ->
    switch.x); `arg` only for `script:<path> <arg>` - an ARG_RE-shaped word
    right after the path that is the last word or is set off from the admin's
    words by punctuation (so "script:x.sh because ..." pins nothing)."""
    text = text.strip()
    m = re.match(GRAMMAR[kind], text, re.IGNORECASE)
    if not m:
        return None
    groups = [g for g in m.groups() if g]
    key = groups[0].lower()
    value = clean_value(groups[1]) if len(groups) > 1 else None
    if len(groups) > 1 and not value:
        return None
    arg = None
    if key == "script" and kind in ("how", "host"):
        rest = text[m.end():].split(None, 1)
        if rest and re.fullmatch(ARG_RE, rest[0]) and (len(rest) == 1 or rest[1][0] in ARG_SEPARATORS):
            arg = rest[0]
    return key, value, arg


def cmd_answer(dep, args):
    if args.kind not in KINDS:
        die(f"unknown question kind '{args.kind}' (one of {', '.join(KINDS)})")
    svc = dep.service(args.device)
    dev = Device(dep, svc)
    if not dev.applies(args.kind):
        die(f"'{args.kind}' does not apply to {dev.name} "
            f"({dev.target_text()}: codes {', '.join(sorted(dev.codes)) or 'none listed'})")
    parsed = parse_answer(args.kind, args.text)
    if not parsed:
        die(f"could not read that as a '{args.kind}' answer. Accepted: "
            f"{CATALOG['questions'][args.kind]['accepts']}", 64)
    key, value, arg = parsed
    if args.kind in ("how", "host") and key in FIXES:
        # refuse now what `fill` would refuse later, with the same reasons
        fix = FIXES[key]
        if fix.get("value_re") and not re.match(fix["value_re"], value):
            die(f"{key}:{value} - the value must look like {fix['value_hint']}", 64)
        cls = "ask" if args.kind == "host" else dev.effective_class()
        if cls and cls not in fix["classes"]:
            die(f"{key}: {fix['refuse']}", 64)
        if arg and cls == "fix":
            die(f"{key}:{value} {arg}: an auto-class string cannot carry a pinned argument - "
                f"answer 'ask' for {dev.name}, or bake the target into the script", 64)
        if fix.get("local_only") and dev.is_remote():
            die(f"{key}: {Path(fix['script']).name} restarts something on THIS box, but {dev.name} "
                f"runs on {dev.target_host()} - use ssh:<user@host> (a forced-command key there) "
                f"or a script", 64)
    record = {"key": key, "value": value, "text": args.text.strip(), "at": iso(now()),
              "question": dev.question(args.kind)["ask"]}
    if arg:
        record["arg"] = arg
    dep.answers["devices"].setdefault(dev.name, {})[args.kind] = record
    dep.save_answers()
    fresh = Device(dep, svc)
    nxt = fresh.open_kinds()
    print(f"recorded {dev.name}:{args.kind} = {key}" + (f" ({value})" if value else "")
          + (f" arg={arg}" if arg else ""))
    if nxt:
        print(f"next for {dev.name}: {nxt[0]}   (python3 {sys.argv[0]} next --device {dev.name})")
    else:
        print(f"{dev.name}: nothing more to ask - run `fill` to see what it maps to")


def cmd_reask(dep, args):
    """Re-interview ONE device: archive its answers (never deleted - the
    doctrine quotes them) and mark it redo, so the questions the interview
    itself once settled come back. Hand-set fields stay closed."""
    svc = dep.service(args.device)
    old = dep.answers["devices"].pop(svc["name"], None)
    if old:
        dep.answers["archived"].append({"device": svc["name"], "archived_at": iso(now()), "answers": old})
    reask = dep.answers.setdefault("reask", [])
    if svc["name"] not in reask:
        reask.append(svc["name"])
    dep.save_answers()
    d = Device(dep, svc)
    print(f"{svc['name']}: {len(old or {})} answer(s) archived; open again: "
          f"{', '.join(d.open_kinds()) or 'nothing (every decision on it was set by hand)'}")
    if d.prov:
        print(f"  its {len(d.prov)} interview-made field(s) stay in services.json until the next "
              f"`fill --apply` replaces them")


# ---- fill: answers -> config ----------------------------------------------------

class Change:
    def __init__(self, path, old, new, why, present):
        self.path, self.old, self.new, self.why, self.present = path, old, new, why, present


def fmt(v):
    if v is OMIT:
        return "<omitted: on-demand>"
    return json.dumps(v, ensure_ascii=False)


def plan_device(dev):
    """Return (changes, kept, notes). `changes` are writes the rules allow;
    `kept` are fields the interview wanted but may not touch: set by hand, or
    its own earlier fill on a device that is not being redone. `notes` are the
    `!` lines: every write a safety check refused or downgraded says so here -
    nothing unsafe is left out quietly."""
    svc, rem, prov, params = dev.svc, dev.rem, dev.prov, dict(dev.svc.get("params", {}))
    root = dev.dep.root
    changes, kept, notes = [], [], []
    cls = dev.effective_class()
    a_class, a_how, a_host = dev.ans("class"), dev.ans("how"), dev.ans("host")

    def want(path, new, why):
        """Register one desired write, applying the overwrite rules."""
        top, _, sub = path.partition(".")
        container = svc.get(top) if sub else svc
        key = sub if sub else top
        present = isinstance(container, dict) and key in container
        old = container.get(key) if present else None
        if present and old == new:
            return
        if present and not dev.is_open(path):
            kept.append((path, old, new, "hand-set" if not dev.mine(path)
                         else f"an earlier fill; `reask {dev.name}` or --overwrite to revise"))
            return
        changes.append(Change(path, old, new, why, present))

    def want_param(k, v, why):
        path = f"params.{k}"
        if k in params and str(params[k]) == str(v):
            return
        if k in params and not dev.is_open(path):
            kept.append((path, params[k], v, "hand-set" if not dev.mine(path) else "an earlier fill"))
            return
        changes.append(Change(path, params.get(k), v, why, k in params))

    def wire_fix(fix_key, value, cls, codes, why, arg=None):
        """Wire one fix to `codes` - after the apply-time safety checks. A
        check that fails leaves a `!` note and skips the write (or downgrades
        it to ask-first); the engine runs whatever is in services.json, so
        the interview must never be the one that put a wrong auto fix there."""
        fix = FIXES[fix_key]
        script = fix["script"].format(value=value)
        label = Path(script).name
        if not codes:
            return
        # C1: an auto string needs the admin's explicit `fix` - a class
        # inferred from one hand-set entry does not auto-wire the rest
        if cls == "fix" and a_class != "fix":
            notes.append(f"{why}: {', '.join(codes)} NOT auto-wired - class=fix was inferred from a "
                         f"hand-set entry, not answered; `answer {dev.name} class fix` (or ask) first")
            return
        # C2: the script must exist, and an auto string must sit under
        # remediations/ - the gate's auto-pass covers nothing else
        target = resolve(script, root)
        if not target.is_file():
            notes.append(f"{why}: not written - {script} is not a file (looked at {target}); "
                         f"put the script there first, then fill again")
            return
        if cls == "fix" and not under_remediations(target, root):
            notes.append(f"{why}: {script} is outside {root / 'remediations'}/ - the gate's auto-pass "
                         f"only covers remediations/; wired ask-first instead")
            cls = "ask"
        if cls == "fix" and arg:
            notes.append(f"{why}: not written - an auto-class string cannot carry the pinned argument "
                         f"'{arg}'; answer `ask`, or bake the target into the script")
            return
        # C3: a restart on THIS box is no fix for a service on another host
        if fix.get("local_only") and dev.is_remote():
            notes.append(f"{why}: not written - {label} restarts something on this box, but {dev.name} "
                         f"runs on {dev.target_host()}; use ssh:<user@host> (a forced-command key "
                         f"there) or a script")
            return
        # C4: the by-hand params first - never a token in the config
        missing = [n for n in fix.get("needs", []) if not has_param(params, n)]
        if missing:
            notes.append(f"{why}: not written until params.{' and params.'.join(missing)} are set by hand "
                         f"for {label} (never put a token in the config - point at a mode-600 file); "
                         f"then run fill again")
            return
        # C5: a fix that leaves the entity ON is the wrong direction for a
        # STATE_MISMATCH whose expected state is anything else
        expect = params.get("EXPECT_STATE")
        if (fix.get("ends_state") and "STATE_MISMATCH" in codes
                and expect is not None and str(expect) != fix["ends_state"]):
            codes = [c for c in codes if c != "STATE_MISMATCH"]
            notes.append(f"{why}: STATE_MISMATCH skipped - EXPECT_STATE={expect} but {label} leaves the "
                         f"entity {fix['ends_state']} (the wrong direction); null it by hand or choose a script")
            if not codes:
                return
        if cls == "fix":
            new = script
        else:
            new = {"ask": script}
            a = fix.get("arg")
            if a:
                new["arg"] = a.format(value=value)
            if arg:
                new["arg"] = arg
            if dev.ans("consent"):
                c, who = dev.ans("consent"), dev.ans_value("consent")
                new["consent"] = f"named:{who}" if c == "named" else c
        for code in codes:
            want(f"remediations.{code}", new, why)
        want_param("REMEDIATION_KEY", dev.name, why)
        for k, v in fix_params(fix_key, value).items():
            want_param(k, v, why)

    # retire ------------------------------------------------------------------
    if dev.ans("retire") == "retire":
        want("enabled", False, "retire=retire")
    elif dev.ans("retire") == "keep" and dev.is_open("doctrine.retire"):
        # doctrine-only, like drill: recorded once, so a later fill has nothing to do
        changes.append(Change("_interview.doctrine.retire", None, "keep", "retire=keep", False))

    # class / how -------------------------------------------------------------
    if a_class == "tell":
        for code in dev.undecided():
            want(f"remediations.{code}", None, "class=tell")
    elif cls in ("fix", "ask"):
        if a_how == "none":
            for code in dev.undecided(*FIXABLE):
                want(f"remediations.{code}", None, "how=none")
        elif a_how:
            fix = FIXES[a_how]
            if cls not in fix["classes"]:
                notes.append(f"how={a_how} refused for class={cls}: {fix['refuse']}")
            else:
                wire_fix(a_how, dev.ans_value("how"), cls, dev.undecided(*FIXABLE),
                         f"class={cls}, how={a_how}", arg=dev.answers["how"].get("arg"))
        # the layers no fix can touch: tell-only, now that the stance is known
        for code in dev.undecided("credential", "upstream", "hardware", "sensor", "inventory"):
            want(f"remediations.{code}", None, f"class={cls} (unfixable layer)")
        # host ----------------------------------------------------------------
        if a_host == "none":
            for code in dev.undecided("host"):
                want(f"remediations.{code}", None, "host=none")
        elif a_host == "severs":
            for code in dev.undecided("host"):
                want(f"remediations.{code}", OMIT, "host=severs (on-demand only; never-touch list)")
        elif a_host in FIXES:
            value = dev.ans_value("host")
            # one value per param per service: a how= and a host= fix that both
            # set e.g. HA_ENTITY would overwrite each other
            clash = None
            if a_how in FIXES:
                ph, pt = fix_params(a_how, dev.ans_value("how")), fix_params(a_host, value)
                clash = next((k for k in pt if k in ph and ph[k] != pt[k]), None)
            if clash:
                notes.append(f"host={a_host}:{value} conflicts with how={a_how}:{dev.ans_value('how')} - "
                             f"both set params.{clash}, one per service; give the host actuator its own "
                             f"service or a script")
            else:
                wire_fix(a_host, value, "ask", dev.undecided("host"), f"host={a_host} (ask-first)",
                         arg=dev.answers["host"].get("arg"))

    # consent -----------------------------------------------------------------
    if dev.ans("consent"):
        c, who = dev.ans("consent"), dev.ans_value("consent")
        scope = f"named:{who}" if c == "named" else c
        for code, v in rem.items():
            if isinstance(v, dict) and ("consent" not in v or dev.is_open(f"remediations.{code}")):
                want(f"remediations.{code}", dict(v, consent=scope), f"consent={scope}")
        text = dev.answers["consent"]["text"]
        rest = re.sub(GRAMMAR["consent"], "", text, flags=re.IGNORECASE).strip(" -—:,.")
        if rest:
            want("consent_notes", rest, f"consent={scope}")

    # nag ---------------------------------------------------------------------
    if dev.ans("nag"):
        minutes = {"daily": 1440, "hourly": 60, "digest": 1440}[dev.ans("nag")]
        for code in dev.codes_at("chronic"):
            want(f"realert_minutes_by_code.{code}", minutes, f"nag={dev.ans('nag')}")
            if dev.ans("nag") == "digest":
                want(f"notify_by_code.{code}", "digest", "nag=digest")

    # drill: doctrine only --------------------------------------------------------
    if dev.ans("drill") and dev.is_open("doctrine.drill"):
        changes.append(Change("_interview.doctrine.drill", None, dev.ans("drill"),
                              f"drill={dev.ans('drill')}", False))

    return changes, kept, notes


def apply_changes(svc, changes):
    """Write the changes into the service and record each in its `_interview`
    provenance map (field -> when, from which answer, and the value written -
    so a later hand edit is recognizable as the admin's)."""
    stamp = iso(now())
    prov = svc.setdefault("_interview", {})
    for ch in changes:
        if ch.path.startswith("_interview."):
            prov[ch.path[len("_interview."):]] = {"at": stamp, "why": ch.why, "value": ch.new}
            continue
        top, _, sub = ch.path.partition(".")
        if ch.new is OMIT:
            if sub and isinstance(svc.get(top), dict):
                svc[top].pop(sub, None)
            prov[ch.path] = {"at": stamp, "why": ch.why, "omit": True}
            continue
        if sub:
            svc.setdefault(top, {})[sub] = ch.new
        else:
            svc[top] = ch.new
        prov[ch.path] = {"at": stamp, "why": ch.why, "value": ch.new}


def lint_config(config, root):
    """Structural lint of a services.json, the engine's view: every remediation
    path (auto string or ask-first "ask") resolves to a file, an auto string
    sits under remediations/ and is not an ask-first-only template, every
    ask-first entry has "ask", a pinned "arg" is ARG_RE-shaped, every service
    has name and check. Advice, not enforcement: the engine keeps running on a
    config with these problems - badly. Returns [(service, field, problem)]."""
    problems = []
    ask_only = {resolve(f["script"], root): k for k, f in FIXES.items()
                if "{value}" not in f["script"] and "fix" not in f["classes"]}
    for i, svc in enumerate(config.get("services", [])):
        if not isinstance(svc, dict):
            problems.append((f"services[{i}]", "", "not an object"))
            continue
        name = svc.get("name")
        label = name if isinstance(name, str) and name else f"services[{i}]"
        if not (isinstance(name, str) and name):
            problems.append((label, "name", "missing or not a string"))
        if not (isinstance(svc.get("check"), str) and svc["check"]):
            problems.append((label, "check", "missing or not a string"))
        rem = svc.get("remediations")
        if rem is None:
            continue
        if not isinstance(rem, dict):
            problems.append((label, "remediations", "must be an object: code -> entry"))
            continue
        for code, v in rem.items():
            field = f"remediations.{code}"
            if v is None:
                continue
            if isinstance(v, str):
                path = resolve(v, root)
                if not path.is_file():
                    problems.append((label, field, f"auto script {v} is not a file ({path})"))
                elif not under_remediations(path, root):
                    problems.append((label, field, f"auto script {v} is outside remediations/ - "
                                                   f"the gate's auto-pass never runs it"))
                elif path in ask_only:
                    problems.append((label, field, f"{Path(v).name} is ask-first only (it refuses the "
                                                   f"auto path) - wire it as {{\"ask\": ...}}"))
                continue
            if isinstance(v, dict):
                ask = v.get("ask")
                if not (isinstance(ask, str) and ask):
                    problems.append((label, field, "an ask-first entry needs \"ask\": <script path>"))
                elif not resolve(ask, root).is_file():
                    problems.append((label, field, f"ask script {ask} is not a file ({resolve(ask, root)})"))
                if "arg" in v and not (isinstance(v["arg"], str) and re.fullmatch(ARG_RE, v["arg"])):
                    problems.append((label, field, f"pinned arg {v['arg']!r} must match {ARG_RE} - "
                                                   f"the engine ignores it with a WARN"))
                c = v.get("consent")
                if c is not None and not (c in ("admin", "household")
                                          or (isinstance(c, str) and c.startswith("named:") and len(c) > 6)):
                    problems.append((label, field, f"consent {c!r} is not admin | household | named:<person>"))
                continue
            problems.append((label, field, f"{type(v).__name__} is not a remediation entry "
                                           f"(a script path, {{\"ask\": ...}} or null)"))
    return problems


def report_lint(dep, where, written_fields):
    """Print the lint of the config just written; a problem in a field THIS
    fill wrote is a bug in the interview (the checks above should make it
    impossible) and fails the command - the file stays written, backup kept."""
    problems = lint_config(dep.config, dep.root)
    entries = sum(len(s.get("remediations") or {}) for s in dep.config.get("services", [])
                  if isinstance(s, dict))
    if not problems:
        print(f"lint: ok - {len(dep.config.get('services', []))} service(s), {entries} remediation "
              f"entr{'y' if entries == 1 else 'ies'}, every path resolves")
        return
    print(f"lint: {len(problems)} problem(s) in {where} (advice: the engine keeps running, badly, "
          f"until they are fixed by hand)")
    for svc_name, field, msg in problems:
        print(f"  ! {svc_name}: {field}: {msg}")
    fatal = [p for p in problems if (p[0], p[1]) in written_fields]
    if fatal:
        die(f"{len(fatal)} lint problem(s) sit in field(s) this fill just wrote - the safety checks "
            f"should make that impossible; keep the backup and report it", 1)


def cmd_fill(dep, args):
    if args.overwrite and not args.device:
        die("--overwrite needs --device: the interview only revises its own fills, one device at a time")
    if args.device:
        targets = [Device(dep, dep.service(args.device), redo=args.overwrite)]
    else:
        targets = devices(dep)
    total, any_kept, written, written_fields = 0, False, [], set()
    for dev in targets:
        changes, kept, notes = plan_device(dev)
        if not (changes or kept or notes):
            continue
        print(dev.name)
        for ch in changes:
            mark = "~" if ch.present else "+"
            old = f"{fmt(ch.old)} -> " if ch.present else ""
            print(f"  {mark} {ch.path} = {old}{fmt(ch.new)}   ({ch.why})")
        for path, old, new, why in kept:
            any_kept = True
            print(f"  = {path} kept as {fmt(old)}   ({why}; the interview wanted {fmt(new)})")
        for n in notes:
            print(f"  ! {n}")
        if changes:
            apply_changes(dev.svc, changes)
            total += len(changes)
            written.append(dev.name)
            written_fields |= {(dev.name, ch.path) for ch in changes}
    if not total:
        print("nothing to fill" + (" (fields the interview may not touch were left alone)" if any_kept else
                                   " - every answer is already in the config"))
        return
    if args.apply:
        backup = dep.config_path.with_name(
            dep.config_path.name + ".backup-" + now().strftime("%Y%m%d%H%M%S"))
        shutil.copy2(dep.config_path, backup)
        save_json(dep.config_path, dep.config)
        # a redo is complete once its fills are in the config
        reask = dep.answers.get("reask", [])
        if any(n in reask for n in written):
            dep.answers["reask"] = [n for n in reask if n not in written]
            dep.save_answers()
        print(f"applied {total} change(s) to {dep.config_path} (backup: {backup.name})")
        report_lint(dep, dep.config_path, written_fields)
        print("verify: python3 engine/selfheal.py --once   and read state/state.json")
    else:
        draft = dep.config_path.with_name(dep.config_path.stem + ".draft.json")
        save_json(draft, dep.config)
        print(f"wrote {total} change(s) to {draft} - services.json untouched; re-run with --apply to write it")
        report_lint(dep, draft, written_fields)


# ---- plan / next / status -----------------------------------------------------------

def cmd_plan(dep, args):
    ds = ranked(dep)
    disabled = len(dep.config.get("services", [])) - len(dep.services())
    if args.json:
        print(json.dumps([{"device": d.name, "score": d.priority()[0], "open": d.open_kinds(),
                           "why": d.priority()[1]} for d in ds], indent=2))
        return
    if not ds:
        print("nothing to ask: every device's decisions are in the config"
              + (f" ({disabled} disabled service(s) skipped)" if disabled else ""))
        return
    print(f"{len(ds)} device(s) with open decisions, most pressing first"
          + (f" ({disabled} disabled skipped)" if disabled else "") + ":")
    for i, d in enumerate(ds, 1):
        score, why = d.priority()
        print(f"{i:>2}. {d.name}  [{score}]  open: {', '.join(d.open_kinds())}")
        for w in why:
            print(f"      - {w}")
    settled = [d for d in devices(dep) if not d.open_kinds()]
    if settled:
        print(f"settled (nothing to ask): {', '.join(d.name for d in settled)}")


def cmd_next(dep, args):
    if args.device:
        d = Device(dep, dep.service(args.device))
        if not d.open_kinds():
            print(f"{d.name}: nothing open")
            return
    else:
        ds = ranked(dep)
        if not ds:
            print("nothing to ask: every device's decisions are in the config")
            return
        d = ds[0]
    q = d.question(d.open_kinds()[0])
    if args.digest:
        line = {"system": "interview", "timestamp": iso(now()),
                "message": f"({q['id']}) {q['ask']} Answer with: {q['accepts']}"}
        path = Path(args.digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            with open(path, "a") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"queued {q['id']} for the daily digest ({path})")
        return
    if args.json:
        print(json.dumps(q, indent=2, ensure_ascii=False))
        return
    print(f"[{q['id']}] ({q['u']})")
    print(q["ask"])
    print(f"  accepts: {q['accepts']}")
    print(f"  maps to: {q['maps_to']}")
    print(f"  record:  python3 {sys.argv[0]} answer {d.name} {q['kind']} \"<the admin's words>\"")


def cmd_status(dep, args):
    rows = []
    for d in devices(dep):
        answered = {k: f"{v['key']}" + (f":{v['value']}" if v.get("value") else "")
                    for k, v in d.answers.items()}
        rows.append({"device": d.name, "class": d.effective_class(), "answered": answered,
                     "open": d.open_kinds(), "filled": sorted(d.prov)})
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        ans = ", ".join(f"{k}={v}" for k, v in r["answered"].items()) or "-"
        print(f"{r['device']}: class={r['class'] or 'undecided'}  answered: {ans}  "
              f"open: {', '.join(r['open']) or 'none'}  filled: {len(r['filled'])} field(s)")
    arch = dep.answers.get("archived", [])
    if arch:
        print(f"archived re-interviews: {len(arch)}")


# ---- doctrine draft ----------------------------------------------------------------

def cmd_doctrine(dep, args):
    ds = devices(dep)
    # a device the interview just retired is disabled now - its answer still counts
    everything = [Device(dep, s) for s in dep.config.get("services", [])]
    out = []
    out.append(f"# House doctrine — DRAFT")
    out.append(f"Drafted by cranston onboard/interview.py on {iso(now())[:10]} from the "
               f"per-device interview. Reviewed and owned by <admin>. The agent does not "
               f"edit this file unasked.")
    out.append("")
    out.append("## Remediation class per service (U1, U2)")
    out.append("| Service | Class | Notes |")
    out.append("|---|---|---|")
    never = []
    for d in ds:
        cls = {"fix": "auto", "ask": "ask-first", "tell": "watch-only"}.get(d.effective_class(), "undecided")
        note = d.answers.get("class", {}).get("text", "")
        if d.ans("host") == "severs":
            never.append(f"{d.name}'s host ({d.host_text()}): \"{d.answers['host']['text']}\"")
            cls += " (host: on-demand only)"
        out.append(f"| {d.name} | {cls} | {note} |")
    for s in dep.config.get("services", []):
        if not s.get("enabled", True):
            out.append(f"| {s['name']} | disabled | |")
    out.append("")
    out.append("## Drill policy (U3)")
    drills = [(d.name, d.answers["drill"]["key"], d.answers["drill"]["text"]) for d in ds if d.ans("drill")]
    if drills:
        for name, key, text in drills:
            out.append(f"- {name}: {dict(freely='drill freely', ok='only with an explicit OK each time', never='never drill')[key]} — \"{text}\"")
    else:
        out.append("none recorded")
    out.append("")
    out.append("## Never-touch list (U2, U12)")
    out.extend(f"- {n}" for n in never) if never else out.append("none recorded")
    out.append("")
    out.append("## Consent map (U10, U11)")
    cons = [(d.name, d.answers["consent"]["text"]) for d in ds if d.ans("consent")]
    out.extend(f"- {n}: \"{t}\"" for n, t in cons) if cons else out.append("none recorded")
    out.append("(U11 voice/guest exposure: not covered by the per-device interview)")
    out.append("")
    out.append("## Alert budget (U4, U5)")
    nags = [(d.name, d.answers["nag"]["key"], ", ".join(d.codes_at("chronic"))) for d in ds if d.ans("nag")]
    out.extend(f"- {n}: {c} remind {k}" for n, k, c in nags) if nags else out.append("no nag cadences recorded")
    out.append("(U4 pages/day budget: not covered by the per-device interview — ask it in conversation)")
    out.append("")
    out.append("## Retirement list (U13)")
    ret = [(d.name, d.answers["retire"]["text"]) for d in everything if d.ans("retire") == "retire"]
    out.extend(f"- {n}: \"{t}\"" for n, t in ret) if ret else out.append("none recorded")
    out.append("")
    for sec in ("Actor doctrine (U7)", "Energy and charging policy (U6)",
                "Known-compromises register (U8, U9)", "Standing hands-on list (U15, U14)",
                "Dark-house channel (U16)", "Paid-reliability stance (U17)"):
        out.append(f"## {sec}")
        out.append("not covered by the per-device interview — see references/interview.md")
        out.append("")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)


# ---- main ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="per-device interview: fill the gaps in services.json")
    ap.add_argument("--config", default=str(ROOT / "services.json"))
    ap.add_argument("--answers", default=None, help="answers store (default <config dir>/interview.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="rank devices with open decisions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("next", help="the next question")
    p.add_argument("--device")
    p.add_argument("--digest", help="append the question to this digest .jsonl instead of printing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("answer", help="record the admin's answer verbatim")
    p.add_argument("device")
    p.add_argument("kind", help=", ".join(KINDS))
    p.add_argument("text")
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("fill", help="map answers onto the config")
    p.add_argument("--device")
    p.add_argument("--overwrite", action="store_true",
                   help="let this device's earlier interview fills be replaced (never hand-set fields)")
    p.add_argument("--apply", action="store_true", help="write services.json (default: a draft next to it)")
    p.set_defaults(fn=cmd_fill)

    p = sub.add_parser("reask", help="archive one device's answers so its questions come back")
    p.add_argument("device")
    p.set_defaults(fn=cmd_reask)

    p = sub.add_parser("status", help="answered / open per device")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("doctrine", help="draft the doctrine sections the answers cover")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_doctrine)

    args = ap.parse_args()
    dep = Deployment(args.config, args.answers)
    args.fn(dep, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
