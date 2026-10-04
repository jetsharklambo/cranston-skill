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
#                                 The window applies to pages AND announcements,
#                                 by text.
#   TG_ANNOUNCE_CHAT_ID           optional - a second chat for household
#                                 announcements. Entries send-alert.sh queued
#                                 under SELFHEAL_ALERT_CHANNEL=announce (objects
#                                 {"text": ..., "channel": "announce"}) are sent
#                                 to this chat as the bare texts, no "N alerts"
#                                 header, through the same sender with TG_CHAT_ID
#                                 overridden in its environment. Pages go first;
#                                 a failed announce send keeps only the announce
#                                 entries queued (exit 1). UNSET: announce texts
#                                 are folded into the page message and counted in
#                                 its header, so nothing is silently dropped.
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
# repeats, compose. Leaves $TMPD/msg + $TMPD/lines.json (the page message and
# exactly the raw queue entries it covers) and, when the announce chat is set
# (argv 4 non-empty), $TMPD/msg.ann + $TMPD/lines.ann.json for the announce
# entries; with it unset, announce texts fold into the page message. No msg
# files = nothing to send. Exit 2 = the queue is not JSON; left for a human.
python3 - "$ALERT_FILE" "$WINDOW" "$TMPD" "${TG_ANNOUNCE_CHAT_ID:-}" <<'PY'
import fcntl, json, os, sys
from datetime import datetime, timedelta, timezone

path, window, outdir, ann_chat = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
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
    # normalize: a plain string is a page line; {"channel": "announce",
    # "text": ...} is an announcement; any other dict with a text field pages
    # too. Anything else is malformed and falls off on the next queue rewrite.
    # Each entry keeps its RAW form paired with its text so removal in step 2
    # stays exact.
    entries = []
    for a in alerts:
        if isinstance(a, str) and a.strip():
            entries.append((a, a, False))
        elif isinstance(a, dict):
            text = a.get("text")
            if isinstance(text, str) and text.strip():
                entries.append((a, text, a.get("channel") == "announce"))

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

    kept, dropped, seen = [], 0, set()
    for raw, text, is_ann in entries:
        if text in recent:
            dropped += 1          # delivered within the window: noise, remove it
        elif text not in seen:
            seen.add(text)
            kept.append((raw, text, is_ann))
    if dropped:
        logs.append(f"dropped {dropped} within-window repeat(s) (window {window:g} min)")
    if len(kept) != len(alerts):
        # rewrite the queue without the dropped entries (delete it when empty)
        if kept:
            write_json(path, {"alerts": [r for r, _, _ in kept],
                              "timestamp": now.strftime(FMT)})
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

# announce chat unset: fold announce texts into the page message (counted in
# its header) so nothing is silently dropped
pages = [(r, t) for r, t, is_ann in kept if not is_ann or not ann_chat]
anns = [(r, t) for r, t, is_ann in kept if is_ann and ann_chat]
if pages:
    texts = [t for _, t in pages]
    worst = next((shown for needle, shown in ORDER if any(needle in t for t in texts)), "")
    head = f"Cranston: {len(texts)} alert" + ("s" if len(texts) != 1 else "")
    head = f"{worst} {head}" if worst else head
    with open(os.path.join(outdir, "msg"), "w") as f:
        f.write(head + "\n" + "\n".join(texts))
    with open(os.path.join(outdir, "lines.json"), "w") as f:
        json.dump([r for r, _ in pages], f)
if anns:
    # for the household: plain language, no alert-count header
    with open(os.path.join(outdir, "msg.ann"), "w") as f:
        f.write("\n".join(t for _, t in anns))
    with open(os.path.join(outdir, "lines.ann.json"), "w") as f:
        json.dump([r for r, _ in anns], f)
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
if [ ! -s "$TMPD/msg" ] && [ ! -s "$TMPD/msg.ann" ]; then exit 0; fi

# the page goes first: a failed page send keeps the whole queue and exits 1
if [ -s "$TMPD/msg" ]; then
    MESSAGE=$(cat "$TMPD/msg")
    ERR=$("$NOTIFY" "$MESSAGE" 2>&1 >/dev/null); rc=$?
    if [ "$rc" -ne 0 ]; then
        ERR=$(printf '%s' "$ERR" | tr '\n' ' ' | cut -c1-300)
        logline "send FAILED rc=$rc via $NOTIFY: ${ERR:-no output} - queue kept"
        exit 1
    fi
fi

# the announce message rides the same sender with the chat id overridden; a
# failure still removes the DELIVERED page entries below, logs, and exits 1
# so the announce entries retry next minute
ANN_FAILED=0
if [ -s "$TMPD/msg.ann" ]; then
    ANN_MSG=$(cat "$TMPD/msg.ann")
    ERR=$(TG_CHAT_ID="${TG_ANNOUNCE_CHAT_ID:-}" "$NOTIFY" "$ANN_MSG" 2>&1 >/dev/null); rc=$?
    if [ "$rc" -ne 0 ]; then
        ANN_FAILED=1
        ERR=$(printf '%s' "$ERR" | tr '\n' ' ' | cut -c1-300)
        logline "announce send FAILED rc=$rc via $NOTIFY: ${ERR:-no output} - announce entries kept"
    fi
fi

# Step 2 (under the lock): remove exactly the delivered raw entries - anything
# appended meanwhile stays queued - and stamp their TEXTS into the sent window.
# Takes one lines-file per delivered message.
set --
[ -s "$TMPD/msg" ] && set -- "$@" "$TMPD/lines.json"
[ -s "$TMPD/msg.ann" ] && [ "$ANN_FAILED" -eq 0 ] && set -- "$@" "$TMPD/lines.ann.json"
if [ "$#" -gt 0 ]; then
python3 - "$ALERT_FILE" "$@" <<'PY'
import fcntl, json, os, sys
from datetime import datetime, timezone

path = sys.argv[1]
sent_path = path + ".sent.json"
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
delivered = []
for lines_path in sys.argv[2:]:
    with open(lines_path) as f:
        delivered.extend(json.load(f))


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
        sent[a["text"] if isinstance(a, dict) else a] = now
    write_json(sent_path, sent)
PY
fi
[ -s "$TMPD/msg" ] && logline "sent via $NOTIFY: $(head -n 1 "$TMPD/msg")"
if [ -s "$TMPD/msg.ann" ] && [ "$ANN_FAILED" -eq 0 ]; then
    logline "sent announce via $NOTIFY: $(head -n 1 "$TMPD/msg.ann")"
fi
[ "$ANN_FAILED" -eq 0 ] || exit 1
exit 0
