#!/bin/bash
# flush-digest.sh - fallback flusher for the daily digest.
#
# The engine routes chronic/degraded findings and routine auto-fix successes
# into a JSONL digest file. The PRIMARY renderer is the deployment's daily
# summary (usually agent-composed) - but that path can die silently, so this
# script runs from SYSTEM cron shortly after the summary slot and sends
# anything still in the file through the adapter's direct text sender. Normal
# days it finds the file empty and exits silently. Digest lines must never
# rot unseen.
#
# Required env (set in the cron line by the installer):
#   SELFHEAL_DIGEST_FILE   the engine's digest_file path
#   SELFHEAL_NOTIFY_CMD    executable taking the message as $1 (adapter's
#                          direct sender, e.g. a Telegram bot-API script)
# Optional:
#   SELFHEAL_LOG_FILE      append one status line per real action

set -u

DIGEST="${SELFHEAL_DIGEST_FILE:?set SELFHEAL_DIGEST_FILE}"
NOTIFY="${SELFHEAL_NOTIFY_CMD:?set SELFHEAL_NOTIFY_CMD}"
LOG_FILE="${SELFHEAL_LOG_FILE:-}"

logline() { [ -n "$LOG_FILE" ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) flush-digest: $*" >> "$LOG_FILE"; }

[ -s "$DIGEST" ] || exit 0

# Build the message and atomically claim the lines, under the same lock the
# engine's appender and the primary renderer use (<digest>.lock, fcntl).
MESSAGE=$(python3 - "$DIGEST" <<'PY'
import fcntl, json, sys
from collections import defaultdict

path = sys.argv[1]
with open(path + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    by_system = defaultdict(list)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                by_system[e.get("system", "?")].append(e)
    except FileNotFoundError:
        raise SystemExit
    if not by_system:
        raise SystemExit
    out = ["⚠️ Daily issues digest (summary didn't run - fallback sender):", ""]
    for system, events in by_system.items():
        out.append(f"{system}:")
        for e in events:
            t = e.get("timestamp", "")[:16].replace("T", " ")
            out.append(f"  • {t}: {e.get('message', '')}")
    print("\n".join(out))
PY
)

[ -n "$MESSAGE" ] || exit 0

if "$NOTIFY" "$MESSAGE" >/dev/null 2>&1; then
    # clear under the lock; lines appended between build and clear survive
    python3 - "$DIGEST" "$MESSAGE" <<'PY'
import fcntl, json, sys
path, sent = sys.argv[1], sys.argv[2]
sent_msgs = set()
for line in sent.splitlines():
    line = line.strip()
    if line.startswith("•"):
        sent_msgs.add(line.split(": ", 1)[-1])
with open(path + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    kept = []
    try:
        with open(path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    if json.loads(raw).get("message", "") in sent_msgs:
                        continue
                except Exception:
                    pass
                kept.append(raw)
    except FileNotFoundError:
        raise SystemExit
    with open(path, "w") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
PY
    logline "sent fallback digest"
    exit 0
else
    logline "send FAILED - digest kept"
    exit 1
fi
