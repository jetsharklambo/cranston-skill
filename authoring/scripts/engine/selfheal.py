#!/usr/bin/env python3
"""selfheal.py - Cranston's self-healing monitor orchestrator (core v2).

Framework-agnostic: stdlib only, no agent-framework imports. Runs one cycle per
invocation from SYSTEM cron (never the agent's own scheduler) so the home keeps
being monitored when the agent is down. Per cycle:

  1. run each enabled service's health check (read-only probe)
  2. step a per-key anti-flap state machine
  3. run allowlisted remediations through the configured approval gate
     (argv prefix - no shell strings anywhere), verify by re-running the check
  4. route messages: page via the configured alert sink, or append to the
     daily digest, per the notification policy

State machine per key: ok -> failing (after fail_threshold consecutive
failures) -> healing (auto remediation, capped per window, cooldown between
attempts) -> escalated (cap hit; a human must approve) or awaiting_approval
(no auto remediation exists). Recovery from any state holds its checkmark for
recovery_hold_minutes so flapping services collapse to one message.

v2 over the proven v1 engine (cranston, 2026-07..10):
  - per-service "params" exported as environment to checks/remediations/hooks
  - "paths" config section; relative paths resolve against the install root
  - pluggable alert_sink and approval_gate (argv lists) - the messaging
    channel and the 2FA gate belong to the host-framework adapter
  - blind-root suppression: when a service's root key has a finding, its
    subkeys are unknowable, not healthy - they are skipped, exactly like the
    network gate skips remote services (fixes the false-recovered-while-blind
    gap observed live 2026-10-02)
  - tunables pending_ttl_hours / post_outage_grace_minutes live in config

Single-instance via cron flock + fcntl lock on <state_dir>/.lock; state writes
are atomic and shared with approve-heal.py.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent      # engine/
ROOT = BASE.parent                          # install root


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def log(msg):
    print(f"{iso(now())} {msg}", flush=True)


def resolve(p, root=ROOT):
    p = Path(p)
    return p if p.is_absolute() else root / p


def argvify(v, root=ROOT):
    """Normalize a configured command edge to an argv list.
    A string names one file: .sh and .py get their interpreter prepended,
    anything else is executed directly. A list is taken as-is, with any
    element containing '/' resolved against the install root."""
    if v is None:
        return []
    if isinstance(v, str):
        path = str(resolve(v, root))
        if path.endswith(".sh"):
            return ["bash", path]
        if path.endswith(".py"):
            return ["python3", path]
        return [path]
    return [str(resolve(x, root)) if "/" in str(x) else str(x) for x in v]


def local_network_up(config_gw=None):
    """True unless our own default gateway is unreachable.

    When the gateway does not answer, every remote probe fails for ONE reason -
    our own NIC - and reporting each as a separate DOWN is exactly the
    misdiagnosis this guards against. Beyond an unreachable gateway those
    services are UNKNOWABLE, not failing: they must be skipped, not stepped.

    Gateway resolution order: SELFHEAL_GW_OVERRIDE env (tests win), then the
    deployment's defaults.gateway_ip, then `ip route show default`. Set
    gateway_ip on platforms where reading the route table doesn't work -
    on Android/Termux the `ip` binary EXISTS but gets "permission denied"
    on the netlink socket, and Android keeps default routes in per-network
    tables anyway, so the main table reads empty even with root.

    A FAILED route-table read fails OPEN: "couldn't read the table" is not
    "no default route". Only a CLEAN read with no route counts as down -
    treating a refused read as down made the gate fail closed and silent
    (every remote service skipped forever, nothing alerting).

    Returns (up: bool, gw: str|None).
    """
    gw = os.environ.get("SELFHEAL_GW_OVERRIDE") or config_gw
    if not gw:
        try:
            r = subprocess.run(["ip", "route", "show", "default"],
                               capture_output=True, text=True, timeout=5)
        except Exception:
            return True, None  # fail OPEN: can't run the tool -> don't suppress
        if r.returncode != 0:
            return True, None  # fail OPEN: tool present but refused the read
        out = r.stdout.split()
        gw = out[out.index("via") + 1] if "via" in out else None
    if not gw:
        return False, None  # clean read, genuinely no default route
    for _ in range(2):
        try:
            if subprocess.run(["ping", "-c", "1", "-W", "2", gw],
                              capture_output=True, timeout=5).returncode == 0:
                return True, gw
        except Exception:
            pass
    return False, gw


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        aside = path.with_suffix(path.suffix + f".corrupt-{now().strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(path, aside)
            log(f"WARN: {path.name} corrupt ({e}); moved aside to {aside.name}")
        except OSError:
            pass
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def default_record():
    return {
        "status": "ok",
        "consecutive_failures": 0,
        "last_status_code": None,
        "attempts": [],
        "last_alert": None,
        "last_transition": iso(now()),
    }


class SelfHeal:
    def __init__(self, config_path):
        self.config = load_json(Path(config_path), None)
        if self.config is None:
            print(f"FATAL: cannot read config {config_path}", file=sys.stderr)
            sys.exit(2)
        self.defaults = self.config.get("defaults", {})
        paths = self.config.get("paths", {})

        self.state_dir = resolve(paths.get("state_dir", "state"))
        self.state_file = self.state_dir / "state.json"
        self.pending_file = self.state_dir / "pending-approvals.json"
        self.lock_file = self.state_dir / ".lock"
        self.audit_log = resolve(paths.get("audit_log", "state/audit.log"))
        self.digest_file = resolve(paths.get("digest_file", "state/digest.jsonl"))
        self.digest_lock = Path(str(self.digest_file) + ".lock")
        # Edges: the messaging channel and the 2FA gate are adapter concerns.
        self.alert_sink = argvify(paths.get("alert_sink", "bin/send-alert.sh"))
        self.approval_gate = argvify(paths.get("approval_gate"))

        self.pending_ttl_hours = self.defaults.get("pending_ttl_hours", 6)
        # After the local network comes BACK, hold off on remote services: on
        # mains restore the router answers minutes before the other hosts
        # finish booting - without grace every remote service pages DOWN then
        # "recovered". Env override is test-only.
        self.grace_minutes = int(os.environ.get(
            "SELFHEAL_GRACE_MINUTES",
            self.defaults.get("post_outage_grace_minutes", 6)))

        self.alerts = []
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state = load_json(self.state_file, {"keys": {}})
        self.pending = load_json(self.pending_file, {})

    # -- config helpers -----------------------------------------------------

    def opt(self, svc, key):
        return svc.get(key, self.defaults[key])

    def check_env(self, svc):
        """Environment for this service's check, remediations and hooks:
        the process env + the service's params block + engine exports."""
        env = dict(os.environ)
        for k, v in svc.get("params", {}).items():
            env[str(k)] = str(v)
        # legacy container-list shorthand (predates params; kept working)
        if "containers_auto" in svc:
            env["SELFHEAL_CONTAINERS_AUTO"] = " ".join(svc["containers_auto"])
        if "containers_watch" in svc:
            env["SELFHEAL_CONTAINERS_WATCH"] = " ".join(svc["containers_watch"])
        env["SELFHEAL_ROOT"] = str(ROOT)
        env["SELFHEAL_STATE_DIR"] = str(self.state_dir)
        env["SELFHEAL_AUDIT_LOG"] = str(self.audit_log)
        return env

    # -- checks -------------------------------------------------------------

    def run_check(self, svc):
        """Return {key: finding} for this service ({} = all healthy)."""
        timeout = self.opt(svc, "check_timeout_seconds")
        try:
            r = subprocess.run(
                ["bash", str(resolve(svc["check"]))],
                capture_output=True, text=True, timeout=timeout,
                env=self.check_env(svc),
            )
        except subprocess.TimeoutExpired:
            return {svc["name"]: {"status": "CHECK_ERROR", "layer": "selfheal",
                                  "detail": f"health check timed out after {timeout}s"}}
        except OSError as e:
            return {svc["name"]: {"status": "CHECK_ERROR", "layer": "selfheal",
                                  "detail": f"health check failed to run: {e}"}}
        if r.returncode == 0:
            return {}
        findings = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                f = json.loads(line)
                findings[f["key"]] = f
            except Exception:
                pass
        if r.returncode != 1 or not findings:
            findings.setdefault(svc["name"], {
                "status": "CHECK_ERROR", "layer": "selfheal",
                "detail": f"check exited {r.returncode} with unparseable output: "
                          f"{(r.stdout or r.stderr)[:200].strip()}"})
        return findings

    # -- remediation --------------------------------------------------------

    def run_remediation(self, svc, script, arg):
        """Run a remediation through the approval gate as argv - never a shell
        string. Gate contract: gate_argv + [script_path] (+ [arg])."""
        argv = (self.approval_gate or ["bash"]) + [str(resolve(script))]
        if arg:
            argv.append(arg)
        env = self.check_env(svc)
        env.update(SELFHEAL_AUTOMATION="true", SELFHEAL_CALLER="selfheal.py")
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=180, env=env)
            return r.returncode, (r.stdout + r.stderr).strip()[-300:]
        except subprocess.TimeoutExpired:
            return -1, "remediation timed out after 180s"

    # -- forensic hooks -------------------------------------------------------

    def run_hook(self, svc, hook_key):
        """Run an optional read-only forensics script; return its last stdout
        line as a short summary (empty string if none/failed)."""
        script = svc.get(hook_key)
        if not script:
            return ""
        try:
            r = subprocess.run(["bash", str(resolve(script))], capture_output=True,
                               text=True, timeout=45, env=self.check_env(svc))
            lines = r.stdout.strip().splitlines()
            summary = lines[-1] if lines else ""
            log(f"hook {Path(script).name}: rc={r.returncode} {summary}")
            return summary
        except Exception as e:
            log(f"WARN: hook {hook_key} failed: {e}")
            return ""

    # -- alerting -----------------------------------------------------------

    def alert(self, rec, message, route="immediate", system="selfheal"):
        """Queue a user-facing message. route='immediate' pages via the alert
        sink this cycle; route='digest' lands in the daily digest instead.
        Both stamp last_alert, so realert_due throttles digest lines exactly
        like pages."""
        rec["last_alert"] = iso(now())
        if route == "digest":
            self.digest_line(system, message)
            log(f"DIGEST: {message}")
        else:
            self.alerts.append(message)
            log(f"ALERT: {message}")

    def digest_line(self, system, message):
        entry = {"system": system, "message": message, "timestamp": iso(now())}
        try:
            self.digest_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.digest_lock, "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                with open(self.digest_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
        except Exception as e:
            # fail LOUD: a broken digest must not silently eat findings
            log(f"WARN: digest write failed ({e}); paging immediately instead")
            self.alerts.append(message)

    def flush_alerts(self):
        if not self.alerts:
            return
        try:
            subprocess.run(self.alert_sink + self.alerts, timeout=30, check=False)
        except Exception as e:
            log(f"WARN: alert sink failed: {e}")

    def notify_class(self, svc, code, degraded, has_ask):
        """'immediate' or 'digest' for a finding. Ask-first findings are always
        immediate (the page carries the approval prompt and each re-send
        renews the pending approval). Explicit notify_by_code wins; otherwise
        degraded findings digest and everything else pages."""
        if has_ask:
            return "immediate"
        cls = svc.get("notify_by_code", {}).get(code)
        if cls in ("immediate", "digest"):
            return cls
        return "digest" if degraded else "immediate"

    def flap_suffix(self, rec):
        f = rec.get("flap") or {}
        n = f.get("count", 0)
        if not n:
            return ""
        first = (f.get("first_at") or "")[11:16]
        return f" (flapped {n}x since {first}Z)" if first else f" (flapped {n}x)"

    def realert_due(self, rec, svc, code=None):
        """Has this key been quiet long enough to alert again?

        A service may override the window for individual status codes via
        realert_minutes_by_code - some findings are chronic-but-known and
        deserve a daily nag, while a real outage of the same service still
        wants the hourly default."""
        if not rec["last_alert"]:
            return True
        minutes = svc.get("realert_minutes_by_code", {}).get(code) if code else None
        if minutes is None:
            minutes = self.opt(svc, "realert_minutes")
        return now() - parse_iso(rec["last_alert"]) > timedelta(minutes=minutes)

    def demote_ask(self, rec, svc, code, route):
        """Ask-first demotion: an unanswered ask must stop paging eventually.

        The reference deployment's life audit measured 439 ask-first pages
        across 7 conditions producing 15 approvals (~3%) - an unanswered ask
        re-paging on the realert clock is indistinguishable from spam. After
        ask_demote_after immediate pages of the same code with no approval,
        further re-pages route to the daily digest. The pending entry keeps
        renewing, so approval still works the whole time; a different code on
        the key, or a recovery, resets the counter."""
        if route != "immediate":
            return route
        if rec.get("ask_code") != code:
            rec["ask_code"] = code
            rec["ask_pages"] = 0
        rec["ask_pages"] = rec.get("ask_pages", 0) + 1
        limit = svc.get("ask_demote_after", self.defaults.get("ask_demote_after", 3))
        if limit and rec["ask_pages"] > limit:
            log(f"ask for this key paged {rec['ask_pages'] - 1}x with no approval "
                f"-> demoting to digest (ask_demote_after={limit})")
            return "digest"
        return route

    # -- pending approvals ----------------------------------------------------

    def add_pending(self, key, code, script, arg, consent=None):
        self.pending[key] = {
            "status_code": code,
            "script": str(resolve(script)),
            "arg": arg,
            "consent": consent or "admin",
            "created": iso(now()),
            "expires": iso(now() + timedelta(hours=self.pending_ttl_hours)),
        }

    # -- state machine --------------------------------------------------------

    def step(self, svc, key, finding):
        rec = self.state["keys"].setdefault(key, default_record())
        threshold = self.opt(svc, "fail_threshold")

        if finding is None:  # healthy
            # Release a held-back checkmark once health has lasted the hold
            # window. Flap collapse: recoveries wait recovery_hold_minutes; a
            # re-failure inside the window suppresses the pair and bumps a
            # flap counter that the next real page carries as a suffix.
            hold = timedelta(minutes=svc.get(
                "recovery_hold_minutes", self.defaults.get("recovery_hold_minutes", 10)))
            flap = rec.get("flap")
            if flap and flap.get("pending_since"):
                try:
                    settled = now() - parse_iso(flap["pending_since"]) >= hold
                except Exception:
                    settled = True
                if settled:
                    n = flap.get("count", 0)
                    suffix = f" (flapped {n}x before settling)" if n else ""
                    self.alert(rec,
                               f"✅ {key} recovered ({flap['ctx']}).{flap.get('extra', '')}{suffix}",
                               route=rec.get("notify_class", "immediate"), system=key)
                    rec.pop("flap", None)
            if rec["status"] != "ok" and rec["consecutive_failures"] >= threshold:
                ctx = rec["last_status_code"] or "previous failure"
                forensics = self.run_hook(svc, "on_recover_forensics")
                extra = f" {forensics}" if forensics else ""
                self.pending.pop(key, None)
                if rec.get("notify_class") == "digest":
                    # digest-class recoveries are already batched - no hold-off
                    self.alert(rec, f"✅ {key} recovered ({ctx}).{extra}",
                               route="digest", system=key)
                else:
                    prev = rec.get("flap") or {}
                    rec["flap"] = {"pending_since": iso(now()), "ctx": ctx, "extra": extra,
                                   "count": prev.get("count", 0),
                                   "first_at": prev.get("first_at") or iso(now())}
            if rec["status"] != "ok":
                rec["last_transition"] = iso(now())
            rec["status"] = "ok"
            rec["consecutive_failures"] = 0
            # a recovery ends the ask thread: the next incident pages fresh
            rec.pop("ask_pages", None)
            rec.pop("ask_code", None)
            return

        flap = rec.get("flap")
        if flap and flap.get("pending_since"):
            # re-failed while a checkmark was held back: suppress the pair
            flap["count"] = flap.get("count", 0) + 1
            flap.setdefault("first_at", flap["pending_since"])
            flap.pop("pending_since", None)
            log(f"{key}: re-failed within recovery hold - flap #{flap['count']}, ✅ suppressed")
        rec["consecutive_failures"] += 1
        code = finding["status"]
        rec["last_status_code"] = code
        if rec["consecutive_failures"] < threshold:
            log(f"{key}: {code} ({rec['consecutive_failures']}/{threshold}, absorbing)")
            return
        if rec["consecutive_failures"] == threshold:
            # first confirmed failure: capture evidence while the incident is live
            self.run_hook(svc, "on_fail_forensics")

        remediation = svc.get("remediations", {}).get(code, None)
        detail = finding.get("detail", "")
        layer = finding.get("layer", "?")
        arg = key.split("/", 1)[1] if "/" in key else None
        # A check may declare severity:"degraded" when the service itself is
        # provably fine and only our view of it is broken. Headlining that as
        # "DOWN" turns a router setting into a phantom outage.
        degraded = finding.get("severity") == "degraded"
        icon = "⚠️" if degraded else "\U0001f6a8"
        word = "DEGRADED" if degraded else "DOWN"

        if rec["status"] == "escalated":
            if self.realert_due(rec, svc, code):
                route = self.demote_ask(rec, svc, code, "immediate")
                self.alert(rec, f"\U0001f6a8 {key} STILL DOWN ({code}) — auto-restart cap "
                                f"reached earlier. Reply 'heal {key}' to run the fix."
                                f"{self.flap_suffix(rec)}", route=route, system=key)
            return

        if isinstance(remediation, str):
            self.step_auto(svc, key, rec, code, layer, detail, remediation, arg)
        else:
            ask_script = remediation.get("ask") if isinstance(remediation, dict) else None
            # An ask-first entry may pin a fixed argument for its script so one
            # remediation can serve several services without a service/arg key.
            if isinstance(remediation, dict) and remediation.get("arg"):
                arg = remediation["arg"]
            # A fresh transition into failure pages immediately - except
            # mid-flap, where every re-confirmation is a "fresh" transition
            # and the realert window gates re-pages like any chronic failure.
            flapping = rec.get("flap", {}).get("count", 0) > 0
            if ((rec["status"] != "awaiting_approval" and not flapping)
                    or self.realert_due(rec, svc, code)):
                if ask_script:
                    # consent scope is separate from approval: "whose evening
                    # does this ruin" travels with the proposal so the adapter
                    # can route/phrase it (admin | named:<person> | household)
                    consent = remediation.get("consent") if isinstance(remediation, dict) else None
                    scope = ""
                    if consent and consent != "admin":
                        who = consent.split(":", 1)[-1] if consent.startswith("named:") else "the household"
                        scope = f" This affects {who} — give them a heads-up."
                    proposal = (f"Proposed fix: {Path(ask_script).name} (ask-first). "
                                f"Reply 'heal {key}' to approve.{scope}")
                    self.add_pending(key, code, ask_script, arg, consent)
                else:
                    proposal = "No safe automatic fix known — manual intervention needed."
                route = self.notify_class(svc, code, degraded, bool(ask_script))
                if ask_script:
                    route = self.demote_ask(rec, svc, code, route)
                rec["notify_class"] = route
                self.alert(rec, f"{icon} {key} {word} — layer: {layer}. {detail}. {proposal}"
                                f"{self.flap_suffix(rec)}", route=route, system=key)
                rec["status"] = "awaiting_approval"
                rec["last_transition"] = iso(now())

    def step_auto(self, svc, key, rec, code, layer, detail, script, arg):
        window = timedelta(hours=self.opt(svc, "attempt_window_hours"))
        rec["attempts"] = [a for a in rec["attempts"] if now() - parse_iso(a) < window]
        max_attempts = self.opt(svc, "max_attempts")

        if len(rec["attempts"]) >= max_attempts:
            rec["status"] = "escalated"
            rec["last_transition"] = iso(now())
            self.add_pending(key, code, script, arg)
            # realert-gated: during a flap storm a key can re-escalate every
            # few minutes (attempt history survives recoveries by design); the
            # escalated branch re-nags STILL DOWN on the same schedule anyway.
            if self.realert_due(rec, svc, code):
                route = self.demote_ask(rec, svc, code, "immediate")
                self.alert(rec, f"\U0001f6a8 {key}: {detail}. Auto-restart cap reached "
                                f"({max_attempts} per {self.opt(svc, 'attempt_window_hours')}h). "
                                f"Reply 'heal {key}' to run {Path(script).name}."
                                f"{self.flap_suffix(rec)}", route=route, system=key)
            return

        cooldown = timedelta(minutes=self.opt(svc, "cooldown_minutes"))
        if rec["attempts"] and now() - parse_iso(rec["attempts"][-1]) < cooldown:
            log(f"{key}: {code} but in cooldown, skipping remediation this cycle")
            return

        rec["status"] = "healing"
        rec["attempts"].append(iso(now()))
        attempt_n = len(rec["attempts"])
        log(f"{key}: {code} -> running {Path(script).name} (attempt {attempt_n}/{max_attempts})")
        rc, output = self.run_remediation(svc, script, arg)

        time.sleep(self.opt(svc, "verify_delay_seconds"))
        still_failing = key in self.run_check(svc)

        if rc == 0 and not still_failing:
            # Routine successes go to the digest when the service says so or
            # when the key is mid-flap; a first-time fix of a quiet service
            # still pages.
            routine = (svc.get("notify_by_code", {}).get(code) == "digest"
                       or rec.get("flap", {}).get("count", 0) > 0)
            self.alert(rec, f"\U0001f527 {key}: {code} at {layer} — auto-remediated "
                            f"(attempt {attempt_n}/{max_attempts}). Verified healthy."
                            f"{self.flap_suffix(rec)}",
                       route="digest" if routine else "immediate", system=key)
            rec["status"] = "ok"
            rec["consecutive_failures"] = 0
        else:
            why = f"exit {rc}" if rc != 0 else "verification still failing"
            rec["notify_class"] = "immediate"
            self.alert(rec, f"\U0001f6a8 {key}: {code}. Remediation {Path(script).name} ran "
                            f"({why}). {output[-150:] if rc != 0 else detail}"
                            f"{self.flap_suffix(rec)}")
            rec["status"] = "failing"
        rec["last_transition"] = iso(now())

    # -- main cycle -----------------------------------------------------------

    def expire_pending(self):
        for key in list(self.pending):
            try:
                if now() > parse_iso(self.pending[key]["expires"]):
                    del self.pending[key]
            except Exception:
                del self.pending[key]

    def network_grace_active(self, net_up):
        """Track network down/up transitions in state['_network'] and return
        True while the post-outage grace window is open."""
        net = self.state.setdefault("_network", {})
        if not net_up:
            net.setdefault("down_since", iso(now()))
            net.pop("restored_at", None)
            return False
        if net.get("down_since"):
            net["last_down_since"] = net.pop("down_since")
            net["restored_at"] = iso(now())
            log(f"LOCAL NETWORK RESTORED (was down since {net['last_down_since']}) - "
                f"post-outage grace {self.grace_minutes} min for remote services")
        restored = net.get("restored_at")
        if restored:
            try:
                if now() - parse_iso(restored) < timedelta(minutes=self.grace_minutes):
                    return True
            except Exception:
                pass
            net.pop("restored_at", None)
        return False

    def run(self):
        net_up, gw = local_network_up(self.defaults.get("gateway_ip"))
        grace = self.network_grace_active(net_up)
        if not net_up or grace:
            enabled = [s for s in self.config["services"] if s.get("enabled", True)]
            skipped = [s["name"] for s in enabled if not s.get("local")]
            local = [s["name"] for s in enabled if s.get("local")]
            if not net_up:
                gwtxt = f"gw {gw} unreachable" if gw else "no default route"
                log(f"LOCAL NETWORK DOWN ({gwtxt}) - running local-only services "
                    f"{local}; skipped (unknowable, not failing) {skipped}")
            else:
                until = parse_iso(self.state["_network"]["restored_at"]) + \
                    timedelta(minutes=self.grace_minutes)
                log(f"POST-OUTAGE GRACE (until {until.strftime('%H:%M:%S')}Z) - remote hosts "
                    f"are still booting; running local-only services {local}; skipped {skipped}")
        for svc in self.config["services"]:
            if not svc.get("enabled", True):
                continue
            if (not net_up or grace) and not svc.get("local"):
                continue  # skip entirely: don't step the state machine, don't
                          # burn a failure count, and thus emit no spurious
                          # 'recovered' later
            findings = self.run_check(svc)
            prefix = svc["name"]
            known = {k for k in self.state["keys"]
                     if k == prefix or k.startswith(prefix + "/")}
            known |= set(findings)
            # Blind-root suppression: when the service's ROOT key has a
            # finding (unreachable, daemon down, check error), its subkeys are
            # unknowable - stepping them healthy fabricates recoveries while
            # the view is broken. Subkeys that DID produce findings still step.
            root_blind = prefix in findings
            skipped_subkeys = 0
            for key in sorted(known):
                if root_blind and key != prefix and key not in findings:
                    skipped_subkeys += 1
                    continue
                self.step(svc, key, findings.get(key))
            if root_blind and skipped_subkeys:
                log(f"{prefix}: root finding ({findings[prefix]['status']}) - "
                    f"{skipped_subkeys} subkey(s) skipped as unknowable")
        self.expire_pending()
        save_json(self.state_file, self.state)
        save_json(self.pending_file, self.pending)
        self.flush_alerts()


def main():
    ap = argparse.ArgumentParser(description="selfheal orchestrator (one cycle per run)")
    ap.add_argument("--config", default=str(ROOT / "services.json"))
    ap.add_argument("--once", action="store_true", help="run one cycle (default; accepted for clarity)")
    args = ap.parse_args()

    sh = SelfHeal(args.config)
    with open(sh.lock_file, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another selfheal instance holds the lock; exiting")
            return 0
        sh.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
