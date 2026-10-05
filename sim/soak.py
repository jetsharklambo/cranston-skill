#!/usr/bin/env python3
"""soak.py - compressed-clock soak layer of the home-simulation suite.

Drives the REAL engine (deploy/engine/selfheal.py, loaded via importlib with
its module clock patched - the tests/test_engine.py pattern) against the
simulated family home that sim/casa/up-casa.sh builds. Checks, remediations,
the alert sink, the drainer (bin/notify-alerts.sh, with its test-only
SELFHEAL_NOW override), the digest flusher and approve-heal.py all run as the
real scripts/subprocesses; only the ENGINE's notion of time and the drainer's
sent-window clock are simulated.

One loop iteration = one simulated minute:
  1. apply scenario actions due at this minute (casa-ctl / approve-heal)
  2. on the 2-minute cron grid, run one engine cycle (sh.SelfHeal(cfg).run())
     and ledger-snapshot the alert queue + digest (every NEW entry, sim-ts)
  3. if the queue file exists, run the real drainer with SELFHEAL_NOW=<sim t>
  4. at 05:25 run the real flush-digest.sh (daily catch-up slot)
  5. advance: minute-by-minute while anything is hot; otherwise jump the sim
     clock to the next wake (hot/cold scheduler - see sim/README.md for the
     equivalence argument; --cron-faithful disables the jumps)

Outputs in --out: phone.jsonl (delivered messages: ts_sim/chat/text/via),
queued-ledger.jsonl, digest-ledger.jsonl, metrics.json, transcript.log,
run.log (the drainer/flusher's own log lines, incl. the within-window-drop
lines gate G4 reconciles), tg-requests.jsonl (every stub Bot API request,
delivered or not), scenario.jsonl (the scenario actually run), and final
copies of state/pending/queue/digest.

Usage:
  python3 sim/soak.py --scenario sim/scenarios/family-home-90d.jsonl --out sim/.run/out
  python3 sim/soak.py --gen-seed 42 --days 45 --out sim/.run/out-seeded
  python3 sim/soak.py --selftest
"""
import argparse
import atexit
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time as _time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the repo tree

SIM = Path(__file__).resolve().parent
REPO = SIM.parent
FMT = "%Y-%m-%dT%H:%M:%SZ"
# Sim epoch: deliberately in the FUTURE so engine-written timestamps (pending
# expiries, sent-window stamps) are never "expired" against the few scripts
# that read the real clock (approve-heal.py's TTL check). 2027-01-04 is a
# Monday; D01 == that day.
START = datetime(2027, 1, 4, 0, 0, tzinfo=timezone.utc)
GW_IP = "192.168.1.1"
MIN = timedelta(minutes=1)
REAL_SLEEP = _time.sleep  # keep a handle before the engine's time.sleep is patched


def iso(dt):
    return dt.strftime(FMT)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_t(s):
    """'DNN HH:MM' (soak sim time), '+MM:SS' (offset from START - the
    real-time cloud drill's scenario format), or ISO UTC (what the drill's
    collect-run.py rewrites event times to)."""
    s = s.strip()
    m = re.fullmatch(r"D(\d+)\s+(\d{1,2}):(\d{2})", s)
    if m:
        return START + timedelta(days=int(m[1]) - 1, hours=int(m[2]),
                                 minutes=int(m[3]))
    m = re.fullmatch(r"\+(\d+):(\d{2})", s)
    if m:
        return START + timedelta(minutes=int(m[1]), seconds=int(m[2]))
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", s)
    if m:
        return parse_iso(s)
    raise ValueError(f"bad scenario time {s!r} (want 'DNN HH:MM', '+MM:SS' or ISO UTC)")


def load_scenario(path):
    """-> (meta dict, events list sorted by time, each with '_t')."""
    meta = {"name": Path(path).stem, "gates": "full"}
    events = []
    for raw in open(path):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        e = json.loads(raw)
        if e.get("meta"):
            meta.update(e)
            continue
        e["_t"] = parse_t(e["t"])
        events.append(e)
    events.sort(key=lambda e: e["_t"])
    return meta, events


def expand(events):
    """Scenario events -> sorted atomic actions [{t, op, args, ev}] + end time.
    dur_min auto-emits the paired up/restore action; flap expands to toggles
    (down first, forced to end plugged)."""
    acts, end = [], None

    def add(t, op, args, e):
        acts.append({"t": t, "op": op, "args": [str(a) for a in args], "ev": e})

    for e in events:
        t, ev = e["_t"], e["ev"]
        dur = e.get("dur_min")
        up = (t + timedelta(minutes=dur)) if dur else None
        if ev == "svc_down":
            add(t, "ctl", ["stop", e["svc"]], e)
            if up:
                add(up, "ctl", ["start", e["svc"]], e)
        elif ev == "svc_up":
            add(t, "ctl", ["start", e["svc"]], e)
        elif ev == "host_down":
            add(t, "ctl", ["unplug", e["host"]], e)
            if up:
                add(up, "ctl", ["plug", e["host"]], e)
        elif ev == "host_up":
            add(t, "ctl", ["plug", e["host"]], e)
        elif ev == "gw_down":
            add(t, "ctl", ["unplug", GW_IP], e)
            if up:
                add(up, "ctl", ["plug", GW_IP], e)
        elif ev == "gw_up":
            add(t, "ctl", ["plug", GW_IP], e)
        elif ev == "flap":
            n, period = int(e["n"]), int(e["period_min"])
            for k in range(n):
                add(t + timedelta(minutes=k * period), "ctl",
                    ["unplug" if k % 2 == 0 else "plug", e["host"]], e)
            if n % 2 == 1:
                add(t + timedelta(minutes=n * period), "ctl", ["plug", e["host"]], e)
        elif ev == "dns_wedge":
            add(t, "ctl", ["dns-mode", "resolver", "wedged"], e)
        elif ev in ("dns_ok", "dns_down"):
            # dns_ok = the wedge clears but the resolver is STILL down until
            # something restarts it (the heal); dns_down = a restartable crash.
            # Same casa mode either way; the names carry scenario intent.
            add(t, "ctl", ["dns-mode", "resolver", "down"], e)
        elif ev == "dns_up":
            add(t, "ctl", ["dns-mode", "resolver", "up"], e)
        elif ev == "wan_down":
            add(t, "ctl", ["dns-mode", "all", "down"], e)
            if up:
                add(up, "ctl", ["dns-mode", "all", "up"], e)
        elif ev == "wan_up":
            add(t, "ctl", ["dns-mode", "all", "up"], e)
        elif ev == "disk_fill":
            add(t, "ctl", ["disk-fill", e["vol"], e["pct"]], e)
        elif ev == "cert_set":
            add(t, "ctl", ["cert-set", e["name"], e["days"]], e)
        elif ev == "tg_mode":
            add(t, "ctl", ["tg-mode", e["mode"]], e)
            if up and e["mode"] != "ok":
                add(up, "ctl", ["tg-mode", "ok"], e)
        elif ev == "admin_heal":
            add(t, "heal", [e["key"]], e)
        elif ev == "end":
            end = t
        else:
            raise ValueError(f"unknown scenario event {ev!r}")
    acts.sort(key=lambda a: a["t"])
    if end is None:
        end = (acts[-1]["t"] if acts else START) + timedelta(hours=30)
    return acts, end


def gw_windows(events, grace_min=6, slop_min=4):
    """Gateway-dark intervals [(t0, t1)] incl. post-outage grace + slop -
    shared with report.py's G2/G7."""
    wins = []
    for e in events:
        pad = timedelta(minutes=grace_min + slop_min)
        if e["ev"] == "gw_down" and e.get("dur_min"):
            wins.append((e["_t"], e["_t"] + timedelta(minutes=e["dur_min"]) + pad))
        elif e["ev"] == "flap" and e.get("host") == GW_IP:
            span = timedelta(minutes=int(e["n"]) * int(e["period_min"]))
            wins.append((e["_t"], e["_t"] + span + pad))
    return wins


def freeport():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


class Soak:
    def __init__(self, meta, events, out, faithful=False,
                 casa_run=None, sent_window=None, no_selfheal_now=False,
                 override_defaults=None, services_file=None):
        self.override_defaults = override_defaults or {}
        # a services.json to run INSTEAD of the casa overlay's - e.g. one the
        # interview built (sim/interview-lab.sh); copied in after up-casa.sh
        self.services_file = Path(services_file) if services_file else None
        self.meta, self.events = meta, events
        self.acts, self.end = expand(events)
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.faithful = faithful
        self.sent_window = sent_window
        self.no_selfheal_now = no_selfheal_now
        self.casa = Path(casa_run or SIM / ".run" / "casa")
        self.deploy = self.casa / "deploy"
        self.cfg = self.deploy / "services.json"
        self.state_file = self.deploy / "state" / "state.json"
        self.pending_file = self.deploy / "state" / "pending-approvals.json"
        self.queue_path = self.deploy / "state" / "alert-pending.json"
        self.digest_path = self.deploy / "state" / "digest.jsonl"
        self.stub_log = self.out / "tg-requests.jsonl"
        self.run_log = self.out / "run.log"
        self.t = START
        self.force_hot_until = START
        self.phone, self.q_ledger, self.d_ledger = [], [], []
        self.heals = []
        self.cycles = self.drains = self.flushes = 0
        self._q_base = Counter()
        self._d_base = Counter()
        self._stub_off = 0
        self.stub_proc = None
        self.transcript = None
        self.svc_cfg = None

    # -- world setup ----------------------------------------------------------

    def build(self):
        subprocess.run(["bash", str(SIM / "casa" / "up-casa.sh"),
                        "--run", str(self.casa)],
                       check=True, capture_output=True, text=True)
        atexit.register(self.teardown)
        for f in (self.stub_log, self.run_log):
            f.unlink(missing_ok=True)
        self.transcript = open(self.out / "transcript.log", "w")
        if self.services_file:
            shutil.copy(self.services_file, self.cfg)
        if self.override_defaults:  # test knob (e.g. a shorter cooldown)
            config = json.load(open(self.cfg))
            config.setdefault("defaults", {}).update(self.override_defaults)
            with open(self.cfg, "w") as f:
                json.dump(config, f, indent=2)
        config = json.load(open(self.cfg))
        self.defaults = config.get("defaults", {})
        self.svc_cfg = {s["name"]: s for s in config["services"]}

        port = freeport()
        self.stub_proc = subprocess.Popen(
            [sys.executable, str(SIM / "phone" / "stub_botapi.py"),
             "--port", str(port), "--log", str(self.stub_log),
             "--mode-file", str(self.casa / "tg-mode")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                REAL_SLEEP(0.05)
        else:
            raise RuntimeError("stub Bot API never came up")

        env = os.environ
        env["CASA_RUN"] = env["SANDBOX_RUN"] = str(self.casa)
        prefix = f"{self.casa}/bin:{self.casa}/lan"
        if not env["PATH"].startswith(prefix + ":"):  # idempotent across runs
            env["PATH"] = prefix + ":" + env["PATH"]
        env["SELFHEAL_GW_OVERRIDE"] = GW_IP
        env["SELFHEAL_ALERT_FILE"] = str(self.queue_path)
        env["TG_API"] = f"http://127.0.0.1:{port}"
        env["TG_BOT_TOKEN"] = "000000:FAKE-TOKEN"
        env["TG_CHAT_ID"] = "1001"
        # honor a caller-set value (incl. empty = announce chat unset) so the
        # G6 negative test can run without code changes
        env.setdefault("TG_ANNOUNCE_CHAT_ID", "2001")
        self.ann_chat = env["TG_ANNOUNCE_CHAT_ID"]

        spec = importlib.util.spec_from_file_location(
            "casa_selfheal", self.deploy / "engine" / "selfheal.py")
        self.sh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sh)
        self.sh.now = lambda: self.t
        self.sh.time.sleep = lambda s: None  # verify_delay is 0 anyway

    def teardown(self):
        if self.stub_proc:
            self.stub_proc.kill()
            self.stub_proc = None
        pids = self.casa / "lan" / "pids"
        if pids.is_dir():
            for p in pids.glob("*.pid"):
                try:
                    os.kill(int(p.read_text().strip()), 15)
                except (OSError, ValueError):
                    pass

    # -- ledgers / harvesting --------------------------------------------------

    @staticmethod
    def _multiset(items):
        return Counter(json.dumps(x, sort_keys=True, ensure_ascii=False) for x in items)

    def snap_queue(self, ledger=True):
        cur = read_json(self.queue_path, {}).get("alerts", [])
        cur_ms = self._multiset(cur)
        if ledger:
            for k, n in (cur_ms - self._q_base).items():
                entry = json.loads(k)
                text = entry["text"] if isinstance(entry, dict) else entry
                kind = ("announce" if isinstance(entry, dict)
                        and entry.get("channel") == "announce" else "page")
                for _ in range(n):
                    self.q_ledger.append({"ts_sim": iso(self.t), "kind": kind, "text": text})
        self._q_base = cur_ms

    def snap_digest(self, ledger=True):
        try:
            cur = [l for l in open(self.digest_path).read().splitlines() if l.strip()]
        except FileNotFoundError:
            cur = []
        cur_ms = Counter(cur)
        if ledger:
            for k, n in (cur_ms - self._d_base).items():
                e = json.loads(k)
                for _ in range(n):
                    self.d_ledger.append({"ts_sim": iso(self.t),
                                          "system": e.get("system", "?"),
                                          "text": e.get("message", "")})
        self._d_base = cur_ms

    def harvest(self):
        if not self.stub_log.exists():
            return
        with open(self.stub_log) as f:
            f.seek(self._stub_off)
            new = f.read()
            self._stub_off = f.tell()
        head_re = re.compile(r"^(?:\U0001f6a8|⚠️|\U0001f527|✅)? ?Cranston: \d+ alert")
        prev_via = None
        for line in new.splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("status") != 200:
                prev_via = None
                continue
            text = rec.get("text", "")
            chat = rec.get("chat_id", "")
            if self.ann_chat and chat == self.ann_chat:
                via = "announce"
            elif text.startswith("⚠️ Daily catch-up"):
                via = "digest"
            elif head_re.match(text.splitlines()[0] if text else ""):
                via = "page"
            else:
                # a >4096-char message is sent as several requests; a chunk
                # with neither header is the continuation of the previous one
                via = prev_via or "page"
            self.phone.append({"ts_sim": iso(self.t), "chat": chat,
                               "text": text, "via": via})
            prev_via = via

    # -- the moving parts ------------------------------------------------------

    def child_env(self, selfheal_now=True):
        env = dict(os.environ)
        if selfheal_now and not self.no_selfheal_now:
            env["SELFHEAL_NOW"] = iso(self.t)
        env["SELFHEAL_LOG_FILE"] = str(self.run_log)
        if self.sent_window is not None:
            env["SELFHEAL_SENT_WINDOW_MINUTES"] = str(self.sent_window)
        return env

    def run_cycle(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sh.SelfHeal(str(self.cfg)).run()
        out = buf.getvalue()
        if out.strip():
            self.transcript.write(f"--- cycle {iso(self.t)}\n{out}")
        self.cycles += 1
        self.snap_queue()
        self.snap_digest()

    def drain(self):
        r = subprocess.run(["bash", str(self.deploy / "bin" / "notify-alerts.sh")],
                           env=self.child_env(), capture_output=True, text=True,
                           timeout=120)
        if r.returncode == 2:
            raise RuntimeError(f"drainer says the queue is corrupt: {r.stderr}")
        self.drains += 1
        self.harvest()
        self.snap_queue(ledger=False)  # drains only remove; re-baseline

    def flush(self):
        env = self.child_env()
        env["SELFHEAL_DIGEST_FILE"] = str(self.digest_path)
        env["SELFHEAL_NOTIFY_CMD"] = str(self.deploy / "bin" / "send-telegram.sh")
        subprocess.run(["bash", str(self.deploy / "bin" / "flush-digest.sh")],
                       env=env, capture_output=True, text=True, timeout=120)
        self.flushes += 1
        self.harvest()
        self.snap_digest(ledger=False)  # flushes only remove; re-baseline

    def apply(self, act):
        self.transcript.write(f"=== {iso(self.t)} {act['op']} {' '.join(act['args'])}"
                              f" (event {act['ev'].get('id', '?')})\n")
        if act["op"] == "ctl":
            r = subprocess.run([str(self.casa / "bin" / "casa-ctl")] + act["args"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f"casa-ctl {act['args']} failed rc={r.returncode}: "
                                   f"{r.stdout}{r.stderr}")
        elif act["op"] == "heal":
            key = act["args"][0]
            pending_before = key in read_json(self.pending_file, {})
            r = subprocess.run([sys.executable,
                                str(self.deploy / "engine" / "approve-heal.py"), key],
                               env=dict(os.environ), capture_output=True, text=True,
                               timeout=300)
            self.transcript.write(r.stdout + (r.stderr or ""))
            self.heals.append({"id": act["ev"].get("id"), "key": key,
                               "ts_sim": iso(self.t), "rc": r.returncode,
                               "pending_before": pending_before,
                               "output": r.stdout.strip()[-400:]})
            self.snap_queue()   # the consent announce is queued by approve-heal
            self.snap_digest()
        # 3 minutes guarantees at least one 2-min-grid engine cycle sees the
        # changed world; a cycle that finds a failure keeps the loop hot via
        # consecutive_failures, so no longer window is needed.
        self.force_hot_until = max(self.force_hot_until, self.t + 3 * MIN)

    # -- hot/cold scheduler ----------------------------------------------------

    def _realert_minutes(self, key, rec):
        name = key.split("/", 1)[0]
        svc = self.svc_cfg.get(name, {})
        code = rec.get("last_status_code")
        by_code = svc.get("realert_minutes_by_code", {})
        if code in by_code:
            return by_code[code]
        return svc.get("realert_minutes", self.defaults.get("realert_minutes", 60))

    def _realert_wake(self, key, rec):
        la = rec.get("last_alert")
        if not la:
            return self.t + 2 * MIN
        return parse_iso(la) + timedelta(minutes=self._realert_minutes(key, rec),
                                         seconds=90)

    def hot(self, state):
        if self.t < self.force_hot_until or self.queue_path.exists():
            return True
        net = state.get("_network", {})
        ra = net.get("restored_at")
        grace = self.defaults.get("post_outage_grace_minutes", 6)
        if ra and self.t < parse_iso(ra) + timedelta(minutes=grace + 4):
            return True
        for key, rec in state.get("keys", {}).items():
            if not isinstance(rec, dict):
                continue
            if rec.get("status") == "healing":
                return True
            if rec.get("consecutive_failures", 0) > 0 and rec.get("status") == "ok":
                return True  # mid-absorption / mid-flap
            if rec.get("status") not in (None, "ok"):
                # steady failing/awaiting/escalated keys sleep until their next
                # timer (cooldown-gated retry or realert) is nearly due
                for w in self._key_wakes(key, rec):
                    if w <= self.t + 4 * MIN:
                        return True
        return False

    def _key_wakes(self, key, rec):
        wakes = [self._realert_wake(key, rec)]
        if rec.get("status") == "failing" and rec.get("attempts"):
            name = key.split("/", 1)[0]
            svc = self.svc_cfg.get(name, {})
            cd = svc.get("cooldown_minutes", self.defaults.get("cooldown_minutes", 30))
            try:
                wakes.append(parse_iso(rec["attempts"][-1])
                             + timedelta(minutes=cd, seconds=90))
            except Exception:
                wakes.append(self.t + 2 * MIN)
        return wakes

    def next_wake(self, state, next_act_t):
        cands = [self.end + MIN]
        if next_act_t:
            cands.append(next_act_t)
        for key, rec in state.get("keys", {}).items():
            if not isinstance(rec, dict):
                continue
            if rec.get("status") not in (None, "ok"):
                cands.extend(self._key_wakes(key, rec))
            ps = (rec.get("flap") or {}).get("pending_since")
            if ps:
                hold = self.defaults.get("recovery_hold_minutes", 10)
                cands.append(parse_iso(ps) + timedelta(minutes=hold, seconds=90))
        for entry in read_json(self.pending_file, {}).values():
            try:
                cands.append(parse_iso(entry["expires"]) + MIN)
            except Exception:
                pass
        if self.digest_path.exists() and self.digest_path.stat().st_size:
            flush_t = self.t.replace(hour=5, minute=25, second=0, microsecond=0)
            while flush_t <= self.t:
                flush_t += timedelta(days=1)
            cands.append(flush_t)
        wake = min(c for c in cands if c > self.t)
        return wake.replace(second=0, microsecond=0)

    # -- main loop -------------------------------------------------------------

    def run(self):
        t0 = _time.monotonic()
        self.build()
        i = 0
        guard = 0
        max_iter = int((self.end - START) / MIN) + 10000
        while self.t <= self.end:
            guard += 1
            if guard > max_iter:
                raise RuntimeError("soak loop guard tripped - scheduler bug?")
            while i < len(self.acts) and self.acts[i]["t"] <= self.t:
                self.apply(self.acts[i])
                i += 1
            state = read_json(self.state_file, {"keys": {}})
            is_hot = self.faithful or self.hot(state)
            if self.t.minute % 2 == 0 and is_hot:
                self.run_cycle()
            if self.queue_path.exists():
                self.drain()
            if (self.t.hour, self.t.minute) == (5, 25):
                self.flush()
            if is_hot or self.queue_path.exists():
                self.t += MIN
            else:
                nxt_act = self.acts[i]["t"] if i < len(self.acts) else None
                wake = self.next_wake(state, nxt_act)
                self.t = max(self.t + MIN, wake)
                self.force_hot_until = max(self.force_hot_until, self.t + 3 * MIN)
        self.finish(_time.monotonic() - t0)

    def finish(self, wall):
        # final copies for the reporter
        for src, name in ((self.state_file, "final-state.json"),
                          (self.pending_file, "final-pending.json"),
                          (self.queue_path, "final-queue.json"),
                          (self.digest_path, "final-digest.jsonl"),
                          (self.cfg, "services.json")):
            try:
                (self.out / name).write_text(Path(src).read_text())
            except FileNotFoundError:
                (self.out / name).write_text("")
        with open(self.out / "phone.jsonl", "w") as f:
            for m in self.phone:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        with open(self.out / "queued-ledger.jsonl", "w") as f:
            for m in self.q_ledger:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        with open(self.out / "digest-ledger.jsonl", "w") as f:
            for m in self.d_ledger:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        per_day = Counter((parse_iso(m["ts_sim"]) - START).days + 1
                          for m in self.phone if m["chat"] == os.environ["TG_CHAT_ID"])
        days = max(1, (self.end - START).days + 1)
        metrics = {
            "scenario": self.meta.get("name"),
            "gates": self.meta.get("gates", "full"),
            "mode": "cron-faithful" if self.faithful else "scheduler",
            "start": iso(START), "end": iso(self.end), "days": days,
            "cycles": self.cycles, "drains": self.drains, "flushes": self.flushes,
            "wall_seconds": round(wall, 1),
            "admin_chat": os.environ["TG_CHAT_ID"],
            "announce_chat": self.ann_chat,
            "admin_msgs": sum(per_day.values()),
            "admin_msgs_per_day": {str(d): n for d, n in sorted(per_day.items())},
            "mean_admin_per_day": round(sum(per_day.values()) / days, 3),
            "max_admin_per_day": max(per_day.values()) if per_day else 0,
            "pages": sum(1 for m in self.phone if m["via"] == "page"),
            "digest_msgs": sum(1 for m in self.phone if m["via"] == "digest"),
            "announces": sum(1 for m in self.phone if m["via"] == "announce"),
            "heals": self.heals,
            "final_state_status": {
                k: (v.get("status") if isinstance(v, dict) else "?")
                for k, v in read_json(self.state_file, {"keys": {}})
                .get("keys", {}).items()},
        }
        with open(self.out / "metrics.json", "w") as f:
            json.dump(metrics, f, indent=2)
        # the scenario as run, for report.py
        with open(self.out / "scenario.jsonl", "w") as f:
            f.write(json.dumps({"meta": True, **{k: v for k, v in self.meta.items()
                                                 if k != "meta"}}) + "\n")
            for e in self.events:
                f.write(json.dumps({k: v for k, v in e.items() if k != "_t"},
                                   ensure_ascii=False) + "\n")
        self.transcript.close()
        print(f"soak: {metrics['mode']} {days}d  cycles={self.cycles} "
              f"drains={self.drains} flushes={self.flushes} "
              f"admin_msgs={metrics['admin_msgs']} "
              f"(mean {metrics['mean_admin_per_day']}/day, max "
              f"{metrics['max_admin_per_day']})  announces={metrics['announces']} "
              f"wall={metrics['wall_seconds']}s  -> {self.out}")
        self.teardown()


def generate(seed, days, out_path):
    spec = importlib.util.spec_from_file_location(
        "gen_scenario", SIM / "scenarios" / "gen_scenario.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    lines = gen.generate(seed, days)
    Path(out_path).write_text("\n".join(lines) + "\n")
    return out_path


# -- selftest ------------------------------------------------------------------

CERT_DATE_RE = re.compile(r"[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4} GMT")


def masked_phone(path):
    """phone.jsonl lines with the one non-sim-deterministic substring (the
    real-clock openssl notAfter date inside cert alerts) masked."""
    out = []
    for line in open(path):
        out.append(CERT_DATE_RE.sub("CERTDATE", line.rstrip("\n")))
    return out


def run_once(scenario, out, faithful=False, days_cap=None):
    meta, events = load_scenario(scenario)
    if days_cap:
        cut = START + timedelta(days=days_cap)
        events = [e for e in events if e["_t"] < cut and e["ev"] != "end"]
        events.append({"ev": "end", "t": f"D{days_cap:02d} 23:50",
                       "_t": cut - timedelta(minutes=10)})
    s = Soak(meta, events, out, faithful=faithful)
    s.run()
    return s


# A dense 34-hour equivalence scenario: a page, a gateway blip, a degraded
# digest + its flush, and a held checkmark - so the mode comparison carries
# real deliveries (the canonical week-1 prefix is deliberately silent).
EQ_MINI = [
    '{"meta": true, "name": "eq-mini-34h", "gates": "invariants"}',
    '{"t": "D01 03:00", "ev": "svc_down", "svc": "plex"}',
    '{"t": "D01 06:00", "ev": "gw_down", "dur_min": 10}',
    '{"t": "D01 09:00", "ev": "svc_down", "svc": "plex-primary", "dur_min": 90}',
    '{"t": "D01 12:00", "ev": "host_down", "host": "192.168.1.3", "dur_min": 30}',
    '{"t": "D02 10:00", "ev": "end"}',
]


def compare_runs(label, out_a, out_b):
    a = [json.loads(l) for l in open(Path(out_a) / "phone.jsonl")]
    b = [json.loads(l) for l in open(Path(out_b) / "phone.jsonl")]
    ok = True
    if [m["text"] for m in a] != [m["text"] for m in b]:
        print(f"FAIL [{label}]: message texts differ between modes")
        ok = False
    else:
        for ma, mb in zip(a, b):
            dt = abs(parse_iso(ma["ts_sim"]) - parse_iso(mb["ts_sim"]))
            if dt > timedelta(minutes=2):
                print(f"FAIL [{label}]: {ma['text'][:60]!r} delivered {dt} apart")
                ok = False
    def stat(p):
        return {k: v.get("status") for k, v in
                read_json(Path(p) / "final-state.json", {}).get("keys", {}).items()}
    if stat(out_a) != stat(out_b):
        print(f"FAIL [{label}]: final statuses differ: "
              f"{stat(out_a)} vs {stat(out_b)}")
        ok = False
    print(f"   equivalence [{label}]: {'PASS' if ok else 'FAIL'} "
          f"({len(a)} vs {len(b)} delivered messages)")
    return ok


def selftest(eq_days):
    canonical = SIM / "scenarios" / "family-home-90d.jsonl"
    base = SIM / ".run" / "selftest"
    base.mkdir(parents=True, exist_ok=True)
    print("== selftest (a1): equivalence on a dense 34h scenario, "
          "cron-faithful vs scheduler")
    mini = base / "eq-mini.jsonl"
    mini.write_text("\n".join(EQ_MINI) + "\n")
    run_once(mini, base / "eq-mini-faithful", faithful=True)
    run_once(mini, base / "eq-mini-scheduler", faithful=False)
    ok = compare_runs("34h dense", base / "eq-mini-faithful",
                      base / "eq-mini-scheduler")
    print(f"== selftest (a2): equivalence, {eq_days}-day prefix of the canonical "
          f"scenario (quiet baseline), cron-faithful vs scheduler")
    run_once(canonical, base / "eq-faithful", faithful=True, days_cap=eq_days)
    run_once(canonical, base / "eq-scheduler", faithful=False, days_cap=eq_days)
    ok = compare_runs(f"{eq_days}d prefix", base / "eq-faithful",
                      base / "eq-scheduler") and ok

    print("== selftest (b): determinism, same --gen-seed twice -> byte-identical "
          "phone.jsonl (cert notAfter dates masked; see README)")
    gen_file = base / "gen-seed7.jsonl"
    generate(7, 14, gen_file)
    run_once(gen_file, base / "det-1")
    run_once(gen_file, base / "det-2")
    det = masked_phone(base / "det-1" / "phone.jsonl") == \
        masked_phone(base / "det-2" / "phone.jsonl")
    n = len(masked_phone(base / "det-1" / "phone.jsonl"))
    print(f"   determinism: {'PASS' if det else 'FAIL'} ({n} delivered messages)")
    return 0 if ok and det else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario")
    ap.add_argument("--gen-seed", type=int)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=str(SIM / ".run" / "out"))
    ap.add_argument("--casa-run", default=None,
                    help="casa world dir (default sim/.run/casa; rebuilt per run)")
    ap.add_argument("--services", default=None,
                    help="run this services.json instead of the casa overlay's (e.g. one the "
                         "interview built); copied into the deploy after the world is built")
    ap.add_argument("--cron-faithful", action="store_true",
                    help="run every 2-min cycle instead of the hot/cold scheduler")
    ap.add_argument("--sent-window", type=int, default=None,
                    help="SELFHEAL_SENT_WINDOW_MINUTES for the drainer (test knob)")
    ap.add_argument("--no-selfheal-now", action="store_true",
                    help="NEGATIVE-TEST: run the drainer on the real clock")
    ap.add_argument("--override-defaults", default=None,
                    help='JSON merged into the config "defaults" block (test knob)')
    ap.add_argument("--end-day", type=int, default=None,
                    help="truncate the scenario after this day (test knob)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--eq-days", type=int, default=7,
                    help="selftest equivalence window in days (default 7)")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.eq_days)

    if args.gen_seed is not None:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        scenario = generate(args.gen_seed, args.days, out / "scenario-generated.jsonl")
    elif args.scenario:
        scenario = args.scenario
    else:
        ap.error("need --scenario, --gen-seed or --selftest")

    meta, events = load_scenario(scenario)
    if args.end_day:
        cut = START + timedelta(days=args.end_day)
        events = [e for e in events if e["_t"] < cut and e["ev"] != "end"]
        events.append({"ev": "end", "t": f"D{args.end_day:02d} 23:50",
                       "_t": cut - timedelta(minutes=10)})
    Soak(meta, events, args.out, faithful=args.cron_faithful,
         casa_run=args.casa_run, sent_window=args.sent_window,
         no_selfheal_now=args.no_selfheal_now,
         override_defaults=json.loads(args.override_defaults)
         if args.override_defaults else None).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
