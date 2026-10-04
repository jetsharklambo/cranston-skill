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

Paths: --config (default <install-root>/services.json), --answers (default
<config dir>/interview.json); live state is read from the config's
paths.state_dir when present. Stdlib only; python3 3.9+.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent          # onboard/
ROOT = BASE.parent                              # install root
CATALOG = json.loads((BASE / "catalog.json").read_text())
FIXABLE = set(CATALOG["fixable_layers"])

# ask order within one device: a retirement question may make the rest moot
KINDS = ["retire", "class", "how", "host", "consent", "drill", "nag"]

GRAMMAR = {
    "retire":  r"^(retire|keep)\b",
    "class":   r"^(fix|ask|tell)\b",
    "how":     r"^(?:(systemd|docker|ha|script):(\S+)|(none)\b)",
    "host":    r"^(?:(ha|script):(\S+)|(none|severs)\b)",
    "consent": r"^(?:(named):(\S+)|(admin|household)\b)",
    "drill":   r"^(freely|ok|never)\b",
    "nag":     r"^(daily|hourly|digest)\b",
}

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

    def will_have_fix(self):
        """A remediation exists or is about to be written from an answer."""
        if self.ans("class") == "tell" and self.ans("host") not in ("ha", "script"):
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
        return bool(host and host in ("ha", "script"))

    def will_have_ask_without_consent(self):
        if any(isinstance(v, dict) and "consent" not in v for v in self.rem.values()):
            return True
        if self.ans("consent"):
            return False
        if self.effective_class() == "ask" and self.ans("how") not in (None, "none"):
            return True
        return self.ans("host") in ("ha", "script")

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
        options = ("systemd:<unit> · docker:<container> (ask-first only) · "
                   "ha:<switch entity> (turn_on only) · script:<path> · none")
        df = self.tpl.get("default_fix") if self.tpl else None
        if df:
            if df.get("from_param") and df["from_param"] in self.params:
                options = f"{df['kind']}:{self.params[df['from_param']]} is the obvious one; or {options}"
            else:
                options = f"{df['kind']}:<the switch to turn on> is the natural fit here; or {options}"
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
    the admin's words are kept verbatim for the doctrine."""
    m = re.match(GRAMMAR[kind], text.strip(), re.IGNORECASE)
    if not m:
        return None
    groups = [g for g in m.groups() if g]
    key = groups[0].lower()
    value = groups[1] if len(groups) > 1 else None
    return key, value


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
    key, value = parsed
    if args.kind == "how" and key != "none":
        fix = CATALOG["fixes"][key]
        cls = dev.effective_class()
        if cls and cls not in fix["classes"]:
            die(f"{key}: {fix['refuse']}", 64)
    record = {"key": key, "value": value, "text": args.text.strip(), "at": iso(now()),
              "question": dev.question(args.kind)["ask"]}
    dep.answers["devices"].setdefault(dev.name, {})[args.kind] = record
    dep.save_answers()
    fresh = Device(dep, svc)
    nxt = fresh.open_kinds()
    print(f"recorded {dev.name}:{args.kind} = {key}" + (f" ({value})" if value else ""))
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
    its own earlier fill on a device that is not being redone."""
    svc, rem, prov, params = dev.svc, dev.rem, dev.prov, dict(dev.svc.get("params", {}))
    changes, kept, notes = [], [], []

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
        fix = CATALOG["fixes"][fix_key]
        script = fix["script"].format(value=value)
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
        for k, v in fix["params"].items():
            want_param(k, v.format(value=value), why)
        for need in fix.get("needs", []):
            if need not in params and not (need == "HA_TOKEN_FILE" and "HA_TOKEN" in params):
                notes.append(f"params.{need} must be set by hand for {Path(script).name} "
                             f"(never put a token in the config - point at a mode-600 file)")

    cls = dev.effective_class()
    a_class, a_how, a_host = dev.ans("class"), dev.ans("how"), dev.ans("host")

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
            fix = CATALOG["fixes"][a_how]
            if cls not in fix["classes"]:
                notes.append(f"how={a_how} refused for class={cls}: {fix['refuse']}")
            else:
                wire_fix(a_how, dev.ans_value("how"), cls, dev.undecided(*FIXABLE),
                         f"class={cls}, how={a_how}")
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
        elif a_host in ("ha", "script"):
            value = dev.ans_value("host")
            if a_host == "ha" and a_how == "ha" and dev.ans_value("how") != value:
                notes.append(f"host=ha:{value} conflicts with how=ha:{dev.ans_value('how')} - one "
                             f"HA_ENTITY per service; give the host actuator its own service or a script")
            else:
                wire_fix(a_host, value, "ask", dev.undecided("host"), f"host={a_host} (ask-first)")

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


def cmd_fill(dep, args):
    if args.overwrite and not args.device:
        die("--overwrite needs --device: the interview only revises its own fills, one device at a time")
    if args.device:
        targets = [Device(dep, dep.service(args.device), redo=args.overwrite)]
    else:
        targets = devices(dep)
    total, any_kept, written = 0, False, []
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
        print("verify: python3 engine/selfheal.py --once   and read state/state.json")
    else:
        draft = dep.config_path.with_name(dep.config_path.stem + ".draft.json")
        save_json(draft, dep.config)
        print(f"wrote {total} change(s) to {draft} - services.json untouched; re-run with --apply to write it")


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
