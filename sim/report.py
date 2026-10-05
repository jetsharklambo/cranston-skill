#!/usr/bin/env python3
"""report.py - hard messaging gates over a sim/soak.py run. Exit 1 on breach.

  python3 sim/report.py --run sim/.run/out [--scenario F] [--mode soak|drill]
                        [--sent-window 30] [--emit-judge-input F]

Gates (G1-G10; the advisory judge NEVER gates - --emit-judge-input only
prepares its input). A scenario whose meta says "gates": "invariants"
(generated traffic) runs the invariant subset: G1 (mean only, <=4/day), G3
coverage, G4, G5, G6, G8. --mode drill (the real-time cloud drill) runs
G3/G5/G6/G8 with the drill's compressed dedupe window via --sent-window
(G4 is soak-only; sim/cloud/collect-run.py builds the drill's run dir).

Calibration: thresholds below were pinned after real canonical runs; the
observed canonical numbers are recorded in sim/README.md's gate table.
"""
import argparse
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the repo tree
SIM = Path(__file__).resolve().parent
sys.path.insert(0, str(SIM))
import soak  # noqa: E402  (load_scenario / gw_windows / START / parse_iso)

MIN = timedelta(minutes=1)
HOST_SVC = {"192.168.1.3": "truenas-smb", "192.168.1.4": "plex"}
ORDER = ["\U0001f6a8", "⚠", "\U0001f527", "✅"]
CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:_[A-Z0-9]+)+\b")

# G1 thresholds. "full" was calibrated against the canonical run (observed:
# mean ~0.6/day, max 4/day) and pinned at the plan's budgets, which encode the
# reference deployment's bad era (9+/day) as the failure to catch.
G1_LIMITS = {"full": (3.0, 8), "invariants": (4.0, 10)}


def subject(e):
    ev = e.get("ev")
    if "svc" in e:
        s = e["svc"]
        return "plex" if s.startswith("plex") else s
    if "key" in e:
        return e["key"]
    if ev in ("dns_wedge", "dns_ok", "dns_down", "dns_up", "wan_down", "wan_up"):
        return "pihole-dns"
    if ev == "cert_set":
        return "truenas-cert"
    if ev == "disk_fill":
        return "truenas-disk"
    if ev in ("host_down", "host_up", "flap"):
        return HOST_SVC.get(e.get("host"))
    return None


class Report:
    def __init__(self, run, scenario, mode, sent_window):
        self.run = Path(run)
        self.mode = mode
        self.window = sent_window
        self.metrics = json.load(open(self.run / "metrics.json"))
        self.admin = self.metrics["admin_chat"]
        self.ann = self.metrics["announce_chat"]
        self.phone = [json.loads(l) for l in open(self.run / "phone.jsonl")]
        for m in self.phone:
            m["_t"] = soak.parse_iso(m["ts_sim"])
        self.q_ledger = [json.loads(l) for l in open(self.run / "queued-ledger.jsonl")]
        self.d_ledger = [json.loads(l) for l in open(self.run / "digest-ledger.jsonl")]
        self.meta, self.events = soak.load_scenario(
            scenario or self.run / "scenario.jsonl")
        self.gateset = self.meta.get("gates", "full")
        try:
            self.svc_cfg = json.load(open(self.run / "services.json"))
        except Exception:
            self.svc_cfg = {"services": [], "defaults": {}}
        self.grace = self.svc_cfg.get("defaults", {}).get("post_outage_grace_minutes", 6)
        self.results = []

    # -- helpers ---------------------------------------------------------------

    def admin_msgs(self):
        return [m for m in self.phone if m["chat"] == self.admin]

    def day(self, m):
        return (m["_t"] - soak.START).days + 1

    def res(self, gate, status, detail=""):
        self.results.append((gate, status, detail))

    def dropped_count(self):
        n = 0
        try:
            for line in open(self.run / "run.log"):
                m = re.search(r"dropped (\d+) within-window repeat", line)
                if m:
                    n += int(m.group(1))
        except FileNotFoundError:
            pass
        return n

    def final_queue_len(self):
        try:
            txt = (self.run / "final-queue.json").read_text().strip()
            return len(json.loads(txt).get("alerts", [])) if txt else 0
        except Exception:
            return 0

    def final_digest_len(self):
        try:
            return sum(1 for l in open(self.run / "final-digest.jsonl") if l.strip())
        except FileNotFoundError:
            return 0

    # -- gates -----------------------------------------------------------------

    def g1_frequency(self):
        days = self.metrics["days"]
        per_day = {}
        for m in self.admin_msgs():
            per_day[self.day(m)] = per_day.get(self.day(m), 0) + 1
        mean = sum(per_day.values()) / max(1, days)
        mx = max(per_day.values()) if per_day else 0
        mean_max, max_max = G1_LIMITS[self.gateset]
        ok = mean <= mean_max and (self.gateset == "invariants" or mx <= max_max)
        self.res("G1 frequency", "PASS" if ok else "FAIL",
                 f"mean {mean:.2f}/day (max allowed {mean_max}), "
                 f"busiest day {mx} (max allowed {max_max})")

    def g2_baseline(self):
        wk = [m for m in self.admin_msgs() if self.day(m) <= 7]
        nondig = [m for m in wk if m["via"] != "digest"]
        dig_days = {}
        for m in wk:
            if m["via"] == "digest":
                dig_days[self.day(m)] = dig_days.get(self.day(m), 0) + 1
        in_gw = []
        for t0, t1 in soak.gw_windows(self.events, self.grace):
            in_gw += [m for m in wk if t0 <= m["_t"] <= t1]
        ok = len(nondig) <= 2 and all(v <= 1 for v in dig_days.values()) and not in_gw
        self.res("G2 baseline week", "PASS" if ok else "FAIL",
                 f"{len(nondig)} non-digest msgs (<=2), digest/day max "
                 f"{max(dig_days.values()) if dig_days else 0} (<=1), "
                 f"{len(in_gw)} msgs in gw windows (=0)")

    def g3_coverage(self):
        fails = []
        checked = 0
        for e in self.events:
            exp = e.get("expect")
            if not exp:
                continue
            checked += 1
            t = e["_t"]
            within = timedelta(minutes=e.get("within_min", 15))
            svc = subject(e)
            pages = [m for m in self.admin_msgs() if m["via"] == "page"
                     and svc and svc in m["text"]]
            digs = [m for m in self.admin_msgs() if m["via"] == "digest"
                    and svc and svc in m["text"]]
            page_hit = any(t <= m["_t"] <= t + within for m in pages)
            dig_budget = max(timedelta(hours=29), within)
            dig_hit = any(t <= m["_t"] <= t + dig_budget for m in digs)
            ok = True
            if exp == "page":
                ok = page_hit
            elif exp == "digest":
                ok = dig_hit
            elif exp == "notify":
                ok = page_hit or dig_hit
            elif exp == "silent":
                span = timedelta(minutes=e.get(
                    "dur_min", int(e.get("n", 0)) * int(e.get("period_min", 0))))
                t1 = t + span + timedelta(minutes=self.grace + 14)
                if e["ev"] in ("gw_down", "flap") and (
                        e.get("host") in (None, soak.GW_IP)):
                    ok = not any(t <= m["_t"] <= t1 for m in self.admin_msgs())
                else:
                    ok = not any(t <= m["_t"] <= t + timedelta(hours=2)
                                 for m in pages + digs)
            elif exp == "announce":
                anns = [m for m in self.phone if m["via"] == "announce"
                        and t - 2 * MIN <= m["_t"] <= t + within]
                ok = len(anns) >= 1
            elif exp == "heal-ok":
                ok = any(h.get("id") == e.get("id") and h["rc"] == 0
                         and "HEALTHY" in h.get("output", "")
                         for h in self.metrics.get("heals", []))
            elif exp == "all-clear":
                exempt = set(e.get("except", []))
                bad = {k: v for k, v in
                       self.metrics.get("final_state_status", {}).items()
                       if v != "ok" and k.split("/")[0] not in exempt}
                ok = not bad
                if bad:
                    fails.append(f"end-state not clear: {bad}")
                    continue
            if not ok:
                fails.append(f"{e.get('id', e['t'])} expect={exp} svc={svc}")
        self.res("G3 coverage", "PASS" if not fails else "FAIL",
                 f"{checked} expectations" + (f"; missed: {fails}" if fails else ""))

    def g4_no_silent_drops(self):
        delivered = 0
        for m in self.phone:
            if m["via"] == "page":
                delivered += max(0, len(m["text"].splitlines()) - 1)
            elif m["via"] == "announce":
                delivered += len(m["text"].splitlines())
        dropped = self.dropped_count()
        finalq = self.final_queue_len()
        q = len(self.q_ledger)
        ok_q = q == delivered + dropped + finalq
        flushed = sum(1 for m in self.phone if m["via"] == "digest"
                      for l in m["text"].splitlines() if l.startswith("  • "))
        finald = self.final_digest_len()
        d = len(self.d_ledger)
        ok_d = d == flushed + finald
        self.res("G4 zero silent drops", "PASS" if ok_q and ok_d else "FAIL",
                 f"queue: {q} queued = {delivered} delivered + {dropped} "
                 f"window-dropped + {finalq} still queued "
                 f"({'ok' if ok_q else 'MISMATCH'}); digest: {d} = {flushed} "
                 f"flushed + {finald} pending ({'ok' if ok_d else 'MISMATCH'})")

    def g5_headers(self):
        bad = []
        for m in self.phone:
            if m["via"] != "page":
                continue
            lines = m["text"].splitlines()
            head, body = lines[0], lines[1:]
            if "Cranston:" not in head:
                continue  # chunk continuation of a long message
            worst = next((o for o in ORDER if any(o in b for b in body)), "")
            if worst and not head.startswith(worst):
                bad.append(f"{m['ts_sim']}: head {head!r} worst {worst!r}")
        self.res("G5 header severity", "PASS" if not bad else "FAIL",
                 f"{sum(1 for m in self.phone if m['via'] == 'page')} page msgs"
                 + (f"; bad: {bad[:3]}" if bad else ""))

    def _consent_heals(self):
        consented = set()
        for s in self.svc_cfg.get("services", []):
            for r in (s.get("remediations") or {}).values():
                if isinstance(r, dict):
                    c = str(r.get("consent") or "")
                    if c == "household" or c.startswith("named:"):
                        consented.add(s["name"])
        return [e for e in self.events if e.get("ev") == "admin_heal"
                and e.get("key", "").split("/")[0] in consented]

    def g6_announce(self):
        anns = [m for m in self.phone if m["via"] == "announce"]
        expected = len(self._consent_heals())
        admin_texts = {m["text"] for m in self.admin_msgs()}
        leaked = [m for m in anns if m["text"] in admin_texts]
        ugly = [m for m in anns
                if CODE_RE.search(m["text"])
                or ("Cranston:" in m["text"].splitlines()[0]
                    and "alert" in m["text"].splitlines()[0])]
        ok = len(anns) == expected and not leaked and not ugly
        self.res("G6 announce channel", "PASS" if ok else "FAIL",
                 f"{len(anns)} announce msgs (expected {expected}); "
                 f"{len(leaked)} leaked to admin chat; {len(ugly)} with "
                 f"codes/headers")

    def g7_gate_windows(self):
        wins = soak.gw_windows(self.events, self.grace)
        bad = []
        for t0, t1 in wins:
            for m in self.admin_msgs():
                if t0 <= m["_t"] <= t1 and "box-disk" not in m["text"]:
                    bad.append(f"{m['ts_sim']}: {m['text'][:60]!r}")
        self.res("G7 network-gate silence",
                 "PASS" if not bad else "FAIL",
                 f"{len(wins)} gw windows" + (f"; leaked: {bad[:3]}" if bad else ""))

    def g8_dedupe(self):
        w = timedelta(minutes=self.window)
        seen = {}
        close = []
        spaced = 0
        for m in self.phone:
            key = (m["chat"], m["text"])
            if key in seen:
                gap = m["_t"] - seen[key]
                if timedelta(0) <= gap < w:
                    close.append(f"{m['ts_sim']} gap {gap} {m['text'][:50]!r}")
                elif gap <= timedelta(minutes=240):
                    spaced += 1
            seen[key] = m["_t"]
        need_spaced = self.gateset == "full" and self.mode == "soak"
        ok = not close and (spaced >= 1 or not need_spaced)
        self.res("G8 dedupe window", "PASS" if ok else "FAIL",
                 f"0 repeats inside {self.window} min required, found {len(close)}"
                 + (f" {close[:2]}" if close else "")
                 + f"; {spaced} legitimate repeats >= window delivered"
                 + ("" if spaced or not need_spaced else " (expected >= 1)"))

    def _ask_incidents(self):
        """(down_event, heal_event) pairs for ask-first services."""
        asks = []
        heals = [e for e in self.events if e.get("ev") == "admin_heal"]
        ask_svcs = set()
        for s in self.svc_cfg.get("services", []):
            if any(isinstance(r, dict) and "ask" in r
                   for r in (s.get("remediations") or {}).values()):
                ask_svcs.add(s["name"])
        for e in self.events:
            if e.get("ev") == "svc_down" and e.get("svc") in ask_svcs:
                heal = next((h for h in heals if h.get("key") == e["svc"]
                             and h["_t"] > e["_t"]), None)
                asks.append((e, heal))
        return asks

    def g9_ask_discipline(self):
        pairs = self._ask_incidents()
        if not pairs:
            self.res("G9 ask discipline", "SKIP", "no ask incidents in scenario")
            return
        fails, details = [], []
        for down, heal in pairs:
            key = down["svc"]
            t0 = down["_t"]
            t1 = heal["_t"] if heal else t0 + timedelta(hours=24)
            pages = [m for m in self.admin_msgs() if m["via"] == "page"
                     and key in m["text"] and "\U0001f6a8" in m["text"]
                     and t0 <= m["_t"] <= t1]
            digs = [d for d in self.d_ledger if key in d["text"]
                    and t0 <= soak.parse_iso(d["ts_sim"]) <= t1]
            hrec = next((h for h in self.metrics.get("heals", [])
                         if h["key"] == key and heal
                         and h["ts_sim"] == soak.iso(heal["_t"])), None)
            details.append(f"{down.get('id')}: {len(pages)} immediate pages, "
                           f"{len(digs)} digest re-nags")
            if len(pages) > 4:
                fails.append(f"{down.get('id')}: {len(pages)} immediate pages > 4")
            if (t1 - t0) > timedelta(hours=5) and not digs:
                fails.append(f"{down.get('id')}: no digest re-nags despite "
                             f"{t1 - t0} unattended")
            if heal is None:
                fails.append(f"{down.get('id')}: never healed (pending left to rot)")
            elif not hrec or not hrec.get("pending_before") or hrec["rc"] != 0:
                fails.append(f"{down.get('id')}: heal did not find a live pending "
                             f"approval or failed ({hrec})")
        self.res("G9 ask discipline", "PASS" if not fails else "FAIL",
                 "; ".join(details) + (f"; FAIL: {fails}" if fails else ""))

    def g10_tg_outage(self):
        outages = [e for e in self.events
                   if e.get("ev") == "tg_mode" and e.get("mode") == "500"]
        if not outages:
            self.res("G10 sender outage", "SKIP", "no tg outage in scenario")
            return
        fails, info = [], []
        for e in outages:
            t0 = e["_t"]
            t1 = t0 + timedelta(minutes=e.get("dur_min", 60))
            queued = {q["text"] for q in self.q_ledger if q["kind"] == "page"
                      and t0 <= soak.parse_iso(q["ts_sim"]) <= t1}
            info.append(f"{e.get('id')}: {len(queued)} texts queued in window")
            for text in queued:
                hits = [m for m in self.admin_msgs()
                        if text in m["text"]
                        and t0 <= m["_t"] <= t1 + timedelta(hours=3)]
                if len(hits) != 1:
                    fails.append(f"{text[:50]!r} delivered {len(hits)}x (want 1)")
        self.res("G10 sender outage", "PASS" if not fails else "FAIL",
                 "; ".join(info) + (f"; FAIL: {fails}" if fails else ""))

    # -- judge input -----------------------------------------------------------

    def emit_judge_input(self, path):
        sev = {"\U0001f6a8": "critical", "⚠": "degraded",
               "\U0001f527": "fixed", "✅": "recovery"}
        seen = set()
        with open(path, "w") as f:
            for m in self.phone:
                key = (m["chat"], m["text"])
                if key in seen:
                    continue
                seen.add(key)
                klass = next((v for k, v in sev.items() if k in m["text"]),
                             "announce" if m["via"] == "announce" else "info")
                lines = m["text"].splitlines()
                header = lines[0] if m["via"] == "page" and "Cranston:" in lines[0] else ""
                svc_hits = [(e, abs(m["_t"] - e["_t"])) for e in self.events
                            if subject(e) and subject(e) in m["text"]
                            and abs(m["_t"] - e["_t"]) < timedelta(hours=48)]
                incident = min(svc_hits, key=lambda p: p[1])[0].get("id", "") \
                    if svc_hits else ""
                f.write(json.dumps({"text": m["text"], "chat": m["chat"],
                                    "via": m["via"], "header": header,
                                    "severity_class": klass,
                                    "incident_id": incident},
                                   ensure_ascii=False) + "\n")

    # -- driver ----------------------------------------------------------------

    def run_gates(self):
        if self.mode == "drill":
            # G4's exact ledger equation is a compressed-soak instrument (the
            # soak snapshots the queue between its own drains); the real-time
            # drill proves delivery liveness through G3 coverage plus its own
            # per-message assertions instead.
            gates = [self.g3_coverage, self.g5_headers,
                     self.g6_announce, self.g8_dedupe]
        elif self.gateset == "invariants":
            gates = [self.g1_frequency, self.g3_coverage, self.g4_no_silent_drops,
                     self.g5_headers, self.g6_announce, self.g8_dedupe]
        else:
            gates = [self.g1_frequency, self.g2_baseline, self.g3_coverage,
                     self.g4_no_silent_drops, self.g5_headers, self.g6_announce,
                     self.g7_gate_windows, self.g8_dedupe,
                     self.g9_ask_discipline, self.g10_tg_outage]
        for g in gates:
            try:
                g()
            except Exception as e:
                self.res(g.__name__, "FAIL", f"gate crashed: {type(e).__name__}: {e}")
        width = max(len(n) for n, _, _ in self.results)
        print(f"== sim report: {self.meta.get('name')} (gates={self.gateset}, "
              f"mode={self.mode})")
        failed = 0
        for name, status, detail in self.results:
            if status == "FAIL":
                failed += 1
            print(f"  {status:4} {name:<{width}}  {detail}")
        print(f"== {'ALL GATES PASS' if not failed else f'{failed} GATE(S) FAILED'}")
        return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--scenario", default=None,
                    help="default: the run dir's scenario.jsonl copy")
    ap.add_argument("--mode", choices=["soak", "drill"], default="soak")
    ap.add_argument("--sent-window", type=int, default=30,
                    help="G8 window in minutes (the drill uses 4)")
    ap.add_argument("--emit-judge-input", default=None)
    args = ap.parse_args()
    r = Report(args.run, args.scenario, args.mode, args.sent_window)
    rc = r.run_gates()
    if args.emit_judge_input:
        r.emit_judge_input(args.emit_judge_input)
        print(f"judge input -> {args.emit_judge_input}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
