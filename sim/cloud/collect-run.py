#!/usr/bin/env python3
"""collect-run.py - build a sim/report.py run dir from the cloud drill's
real-time artifacts, so the drill and the soak share one gate checker.

  collect-run.py --stub-log F --scenario F --start ISO --out DIR \
                 [--deploy DIR] [--run-log F] [--admin-chat 1001] \
                 [--announce-chat 2001] [--since-offset BYTES]

Maps the stub Bot API's request log (each line {"path","chat_id","text",
"status","ts"}) into phone.jsonl ({"ts_sim","chat","text","via"}): only
status-200 requests count as delivered; via = announce for the announce chat,
digest when the text opens with the digest header, else page. Event times in
the scenario ('+MM:SS' offsets) are rewritten to absolute ISO against
--start, which is the drill driver's t0 - the same epoch the stub's ts live
on. metrics.json carries the chat ids and headline counts; the ledgers are
written empty because G4 is soak-only (report.py skips it in --mode drill).
Exits 0 after printing where everything landed; any missing input is a loud
exit 2 naming the file.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

FMT = "%Y-%m-%dT%H:%M:%SZ"
DIGEST_HEADER = "⚠️ Daily catch-up"


def die(msg):
    print(f"collect-run: {msg}", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub-log", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--start", required=True, help="drill t0, ISO UTC (%%Y-%%m-%%dT%%H:%%M:%%SZ)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--deploy", default=None, help="copies services.json + final queue/digest")
    ap.add_argument("--run-log", default=None, help="the drainer's SELFHEAL_LOG_FILE")
    ap.add_argument("--admin-chat", default="1001")
    ap.add_argument("--announce-chat", default="2001")
    ap.add_argument("--since-offset", type=int, default=0,
                    help="ignore stub-log bytes before this offset (pre-traffic sends)")
    a = ap.parse_args()

    try:
        start = datetime.strptime(a.start, FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        die(f"--start {a.start!r} is not ISO UTC ({FMT})")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    # phone.jsonl from the stub log
    if not Path(a.stub_log).is_file():
        die(f"stub log missing: {a.stub_log}")
    phone = []
    with open(a.stub_log, "rb") as f:
        f.seek(a.since_offset)
        raw = f.read().decode("utf-8", "replace")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("status") != 200:
            continue                      # refused sends are retried, not delivered
        ts = r.get("ts")
        if not ts:
            die("stub log line has no 'ts' - stub_botapi.py predates the ts field")
        chat, text = str(r.get("chat_id", "")), r.get("text", "")
        via = ("announce" if chat == a.announce_chat
               else "digest" if text.startswith(DIGEST_HEADER) else "page")
        phone.append({"ts_sim": ts, "chat": chat, "text": text, "via": via})
    with open(out / "phone.jsonl", "w") as f:
        for m in phone:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    # scenario with '+MM:SS' offsets rewritten to absolute ISO on the drill epoch
    if not Path(a.scenario).is_file():
        die(f"scenario missing: {a.scenario}")
    with open(out / "scenario.jsonl", "w") as f:
        for line in open(a.scenario):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            e = json.loads(s)
            t = e.get("t", "")
            if not e.get("meta") and t.startswith("+"):
                mm, ss = t.lstrip("+").split(":")
                e["t"] = (start + timedelta(minutes=int(mm), seconds=int(ss))).strftime(FMT)
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # ledgers: empty by design - G4 is a compressed-soak instrument and
    # report.py --mode drill does not run it
    (out / "queued-ledger.jsonl").write_text("")
    (out / "digest-ledger.jsonl").write_text("")

    if a.run_log and Path(a.run_log).is_file():
        (out / "run.log").write_text(Path(a.run_log).read_text())
    else:
        (out / "run.log").write_text("")

    finalq = finald = 0
    if a.deploy:
        dep = Path(a.deploy)
        svc = dep / "services.json"
        if svc.is_file():
            (out / "services.json").write_text(svc.read_text())
        q = dep / "state" / "alert-pending.json"
        if q.is_file():
            (out / "final-queue.json").write_text(q.read_text())
            try:
                finalq = len(json.loads(q.read_text()).get("alerts", []))
            except Exception:
                pass
        d = dep / "state" / "digest.jsonl"
        if d.is_file():
            (out / "final-digest.jsonl").write_text(d.read_text())
            finald = sum(1 for l in d.read_text().splitlines() if l.strip())

    admin = [m for m in phone if m["chat"] == a.admin_chat]
    metrics = {
        "admin_chat": a.admin_chat,
        "announce_chat": a.announce_chat,
        "mode": "drill",
        "start": a.start,
        "messages": len(phone),
        "admin_messages": len(admin),
        "pages": sum(1 for m in admin if m["via"] == "page"),
        "digest_msgs": sum(1 for m in admin if m["via"] == "digest"),
        "announces": sum(1 for m in phone if m["via"] == "announce"),
        "final_queue_len": finalq,
        "final_digest_len": finald,
    }
    with open(out / "metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)

    print(f"collect-run: {len(phone)} delivered messages -> {out}/phone.jsonl; "
          f"metrics: {metrics['pages']} pages, {metrics['digest_msgs']} digests, "
          f"{metrics['announces']} announces; run dir ready for "
          f"sim/report.py --run {out} --mode drill")


if __name__ == "__main__":
    main()
