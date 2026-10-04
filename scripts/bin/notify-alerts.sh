#!/bin/bash
# notify-alerts.sh - the DELIVERY half of alerting. bin/send-alert.sh (the
# default alert_sink) only QUEUES lines into a pending file; this script drains
# that queue. Run it from SYSTEM cron every minute - never from an agent's
# scheduler, because the pages that matter most are the ones about the agent:
#
#   * * * * *  . /etc/cranston.env; /opt/cranston/bin/notify-alerts.sh >> /var/log/cranston.log 2>&1
#
# (/etc/cranston.env is the installer's ONE mode-600 secrets file: `export
# TG_BOT_TOKEN=...`, `export TG_CHAT_ID=...` for the default sender and `export
# SELFHEAL_ALERT_FILE=...` so writer and drainer agree on the queue; add
# SELFHEAL_LOG_FILE=/var/log/cranston.log to the line for a status line per action.)
#
# Each run: take the queue's lock, read the pending lines, drop any line whose
# exact text was already delivered within the sent window, compose ONE message
# headed by the worst line present (🚨 > ⚠️ > 🔧 > ✅ > other, e.g.
# "🚨 Cranston: 2 alerts"), release the lock, deliver, then under the lock
# again remove exactly the delivered lines (lines the engine appended while
# the sender ran survive) and record them in the sent window. A failed
# delivery leaves the queue untouched and exits 1 - the next minute retries.
# Nothing to send -> exit 0, silently.
#
# Env:
#   SELFHEAL_ALERT_FILE           the queue (default /tmp/selfheal-alert-pending.json).
#                                 MUST be the value the engine's cron line gives
#                                 the writer: two values are two queues, one of
#                                 them never drained.
#   SELFHEAL_NOTIFY_CMD           executable taking the whole message as $1
#                                 (default: send-telegram.sh next to this script)
#   SELFHEAL_SENT_WINDOW_MINUTES  sent-text window, default 30 (0 disables). Kept
#                                 in <file>.sent.json as {text: iso-time}, pruned
#                                 every run. Queue-side dedupe cannot see what was
#                                 already delivered; the reference deployment's
#                                 worst day was 530 deliveries of one looping text.
#   SELFHEAL_LOG_FILE             optional - append one line per real action
#
# Locking is python fcntl on <file>.lock - the same lock send-alert.sh takes.
# The lock is NOT held while the sender runs: the engine's flush would queue
# behind a slow Bot API and hit its own 30 s sink timeout, losing the page.
# python3-inside-bash, stdlib only, bash 3.2 (macOS) compatible.
set -u

ALERT_FILE="${SELFHEAL_ALERT_FILE:-/tmp/selfheal-alert-pending.json}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NOTIFY="${SELFHEAL_NOTIFY_CMD:-$HERE/send-telegram.sh}"
WINDOW="${SELFHEAL_SENT_WINDOW_MINUTES:-30}"
LOG_FILE="${SELFHEAL_LOG_FILE:-}"

logline() { [ -n "$LOG_FILE" ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) notify-alerts: $*" >> "$LOG_FILE"; }

[ -s "$ALERT_FILE" ] || exit 0

TMPD=$(mktemp -d) || exit 1
trap 'rm -rf "$TMPD"' EXIT

# Step 1 (under the lock): read, prune the sent window, drop within-window
# repeats, compose. Leaves $TMPD/msg (the message), $TMPD/lines.json (exactly
# the queue lines it contains) and $TMPD/log (status lines). No msg = nothing
# to send. Exit 2 = the queue is not JSON; it is left in place for a human.
python3 - "$ALERT_FILE" "$WINDOW" "$TMPD" <<'PY'
import fcntl, json, os, sys
from datetime import datetime, timedelta, timezone

path, window, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
sent_path = path + ".sent.json"
FMT = "%Y-%m-%dT%H:%M:%SZ"
# worst line decides the header; the needle for ⚠️ matches with or without
# the variation selector
ORDER = [("\U0001f6a8", "\U0001f6a8"), ("⚠", "⚠️"),
         ("\U0001f527", "\U0001f527"), ("✅", "✅")]
try:
    window = float(window)
except ValueError:
    window = 30.0
now = datetime.now(timezone.utc)
logs = []


def write_json(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


with open(path + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    try:
        with open(path) as f:
            alerts = json.load(f).get("alerts", [])
    except FileNotFoundError:
        alerts = []
    except Exception as e:
        print(f"queue {path} is not valid JSON ({e}) - left in place", file=sys.stderr)
        sys.exit(2)
    alerts = [a for a in alerts if isinstance(a, str) and a.strip()]

    # the sent window: {text: iso-time}, pruned to entries younger than the window
    try:
        with open(sent_path) as f:
            sent = json.load(f)
    except Exception:
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    recent = {}
    for text, ts in sent.items():
        try:
            age = now - datetime.strptime(str(ts), FMT).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if timedelta(0) <= age < timedelta(minutes=window):
            recent[text] = ts
    if recent != sent:
        write_json(sent_path, recent)

    lines, dropped = [], 0
    for a in alerts:
        if a in recent:
            dropped += 1          # delivered within the window: noise, remove it
        elif a not in lines:
            lines.append(a)
    if dropped:
        logs.append(f"dropped {dropped} within-window repeat(s) (window {window:g} min)")
    if len(lines) != len(alerts):
        # rewrite the queue without the dropped lines (delete it when empty)
        if lines:
            write_json(path, {"alerts": lines, "timestamp": now.strftime(FMT)})
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

if lines:
    worst = next((shown for needle, shown in ORDER if any(needle in l for l in lines)), "")
    head = f"Cranston: {len(lines)} alert" + ("s" if len(lines) != 1 else "")
    head = f"{worst} {head}" if worst else head
    with open(os.path.join(outdir, "msg"), "w") as f:
        f.write(head + "\n" + "\n".join(lines))
    with open(os.path.join(outdir, "lines.json"), "w") as f:
        json.dump(lines, f)
if logs:
    with open(os.path.join(outdir, "log"), "w") as f:
        f.write("\n".join(logs) + "\n")
PY
rc=$?
[ -f "$TMPD/log" ] && while IFS= read -r l; do logline "$l"; done < "$TMPD/log"
if [ "$rc" -ne 0 ]; then
    logline "queue $ALERT_FILE unreadable (rc=$rc) - left in place, nothing sent"
    exit 1
fi
[ -s "$TMPD/msg" ] || exit 0

MESSAGE=$(cat "$TMPD/msg")
ERR=$("$NOTIFY" "$MESSAGE" 2>&1 >/dev/null); rc=$?
if [ "$rc" -ne 0 ]; then
    ERR=$(printf '%s' "$ERR" | tr '\n' ' ' | cut -c1-300)
    logline "send FAILED rc=$rc via $NOTIFY: ${ERR:-no output} - queue kept"
    exit 1
fi

# Step 2 (under the lock): remove exactly the delivered lines - anything
# appended meanwhile stays queued - and stamp them into the sent window.
python3 - "$ALERT_FILE" "$TMPD/lines.json" <<'PY'
import fcntl, json, os, sys
from datetime import datetime, timezone

path, lines_path = sys.argv[1], sys.argv[2]
sent_path = path + ".sent.json"
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
with open(lines_path) as f:
    delivered = json.load(f)


def write_json(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


with open(path + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    try:
        with open(path) as f:
            alerts = json.load(f).get("alerts", [])
    except Exception:
        alerts = []
    remaining = [a for a in alerts if a not in delivered]
    if remaining:
        write_json(path, {"alerts": remaining, "timestamp": now})
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    try:
        with open(sent_path) as f:
            sent = json.load(f)
    except Exception:
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    for a in delivered:
        sent[a] = now
    write_json(sent_path, sent)
PY
logline "sent via $NOTIFY: $(head -n 1 "$TMPD/msg")"
exit 0
