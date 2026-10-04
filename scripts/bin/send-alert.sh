#!/bin/bash
# send-alert.sh "line1" ["line2" ...] - the default alert sink: merge alert
# lines into a pending-alert file that bin/notify-alerts.sh drains. Lock +
# merge + exact-line dedupe, so concurrent writers (the engine, a critical-path
# cron, a stats collector) can't clobber each other.
#
# This is only the QUEUE half. The DELIVERY half ships next to it:
#   bin/notify-alerts.sh   every-minute system cron: header from the worst line
#                          present, one message, delete exactly the delivered
#                          lines on success, keep the queue on failure
#   bin/send-telegram.sh   the sender it uses by default (Bot API, DNS fallback,
#                          4096-char chunking); any SELFHEAL_NOTIFY_CMD works
# Queue-side dedupe (here) cannot see what was already delivered, so the
# drainer also keeps a sent-text window (~30 min, <file>.sent.json) and drops
# a line whose exact text was delivered within it. The reference deployment's
# worst day ever was 530 deliveries from a looping sender; storm days repeat
# one text.
#
# Channels: with SELFHEAL_ALERT_CHANNEL=announce each line is queued as the
# object {"text": <line>, "channel": "announce"} instead of a plain string -
# the drainer routes those to TG_ANNOUNCE_CHAT_ID (household announcements,
# no alert header). Page lines stay plain strings; old queues are unchanged.
#
# SELFHEAL_ALERT_FILE must be the SAME value for the writer (the engine's cron
# line, which runs this sink) and the drainer's cron line - two values are two
# queues, one of them never drained. The default suits Linux; Termux has no
# /tmp (see references/hardware.md).
#
# Locking is python fcntl on <file>.lock - no flock binary needed (macOS
# lacks one). Writers and the drainer share the same lock file.

ALERT_FILE="${SELFHEAL_ALERT_FILE:-/tmp/selfheal-alert-pending.json}"

[ $# -ge 1 ] || exit 0

python3 - "$ALERT_FILE" "${SELFHEAL_ALERT_CHANNEL:-}" "$@" <<'PY'
import fcntl, json, os, sys
from datetime import datetime, timezone

path, channel, new = sys.argv[1], sys.argv[2], sys.argv[3:]
if channel == "announce":
    new = [{"text": a, "channel": "announce"} for a in new]
with open(path + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    alerts = []
    try:
        with open(path) as f:
            alerts = json.load(f).get("alerts", [])
    except Exception:
        pass
    alerts.extend(a for a in new if a not in alerts)
    data = {
        "alerts": alerts,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
PY
