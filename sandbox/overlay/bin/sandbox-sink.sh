#!/bin/bash
# Sandbox alert sink: stands in for a Telegram/Signal sender by appending
# every page to state/outbox.log. One line per alert, timestamped.
OUT="${SELFHEAL_STATE_DIR:-$(cd "$(dirname "$0")/.." && pwd)/state}/outbox.log"
for line in "$@"; do
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >> "$OUT"
done
