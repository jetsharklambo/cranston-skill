#!/bin/bash
# send-alert.sh "line1" ["line2" ...] - the default alert sink: merge alert
# lines into a pending-alert file that the adapter's delivery cron sends and
# deletes. Lock + merge + exact-line dedupe, so concurrent writers (the
# engine, a critical-path cron, a stats collector) can't clobber each other.
#
# Delivery-contract note for adapters: the SENDER should also keep a short
# sent-text window (skip a send whose exact text was delivered within ~30
# min). The reference deployment's worst day ever was 530 deliveries from a
# looping sender, and storm days repeat one text - queue-side dedupe (here)
# cannot see what was already delivered.
#
# This is only the QUEUE half. The adapter owns delivery (e.g. OpenClaw's
# notify-alerts.sh: every minute, derive the header from the worst line
# present, send via the Bot API with a DNS fallback, delete on HTTP 2xx).
#
# Locking is python fcntl on <file>.lock - no flock binary needed (macOS
# lacks one). Writers and the deliverer must share the same lock file.

ALERT_FILE="${SELFHEAL_ALERT_FILE:-/tmp/selfheal-alert-pending.json}"

[ $# -ge 1 ] || exit 0

python3 - "$ALERT_FILE" "$@" <<'PY'
import fcntl, json, os, sys
from datetime import datetime, timezone

path, new = sys.argv[1], sys.argv[2:]
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
