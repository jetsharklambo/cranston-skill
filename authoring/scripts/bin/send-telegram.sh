#!/bin/bash
# send-telegram.sh "<text>" - the direct Telegram sender: one message (several,
# when the text is longer than the Bot API's 4096-character limit) to the
# pinned chat. Exit 0 only when EVERY chunk got HTTP 2xx.
#
# This is the delivery primitive the alert path builds on:
#   bin/notify-alerts.sh   drains the pending-alert queue through it (default)
#   bin/flush-digest.sh    SELFHEAL_NOTIFY_CMD points at it
#   the admin              test page:  . /etc/cranston.env && bin/send-telegram.sh "test page"
#
# Env (read from the environment; the installer keeps the secrets in ONE
# mode-600 file, /etc/cranston.env, written as `export TG_BOT_TOKEN=...`
# lines and sourced by every cron line - never in services.json):
#   TG_BOT_TOKEN  required - the bot's token (from @BotFather)
#   TG_CHAT_ID    required - the numeric chat id to page
#   TG_API_IP     optional - DNS-outage fallback: when the normal send fails,
#                 retry once with --resolve api.telegram.org:443:<ip>. The one
#                 outage a home most needs to be paged about is its own
#                 resolver dying, and that is exactly when the name won't resolve.
#   TG_API        optional - Bot API base URL (default https://api.telegram.org;
#                 tests point it at a stub on 127.0.0.1)
#
# Exit: 0 delivered; 1 a chunk failed (curl -fsS: any non-2xx or transport
# error is a failure - a silent `curl -s` would turn a dead bot into a quiet
# night); 64 missing credentials or empty text, reason on stdout.
#
# Plain text, no parse_mode: alert lines quote arbitrary check output, and a
# stray '_' or '*' must never turn a page into an HTTP 400. Several arguments
# are joined with newlines, so the script also works directly as a tiny
# paths.alert_sink (no queue, no sent-text window - fine for a test install).
set -u

usage() { echo "send-telegram: $1"; exit 64; }

[ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] \
    || usage "TG_BOT_TOKEN and TG_CHAT_ID must be set (source the mode-600 creds file in the cron line)"
[ $# -ge 1 ] || usage "usage: send-telegram.sh \"<text>\""
TEXT=$(printf '%s\n' "$@")

API="${TG_API:-https://api.telegram.org}/bot${TG_BOT_TOKEN}/sendMessage"
TMPD=$(mktemp -d) || exit 1
trap 'rm -rf "$TMPD"' EXIT

# Chunk at line boundaries. Length is measured in UTF-16 code units - the
# stricter count, so an emoji-heavy page (every 🚨 is two units) still fits.
# One file per chunk; curl reads each with text@file, so the message never
# passes through a shell string or the argv limit.
N=$(TEXT="$TEXT" python3 - "$TMPD" <<'PY'
import os, sys

LIMIT = 4096
outdir = sys.argv[1]
text = os.environ["TEXT"].replace("\r\n", "\n")


def ulen(s):
    return len(s.encode("utf-16-le")) // 2


def head(s, limit):
    """Longest prefix of s that fits in `limit` UTF-16 units."""
    n = 0
    for i, ch in enumerate(s):
        n += 2 if ord(ch) > 0xFFFF else 1
        if n > limit:
            return i
    return len(s)


chunks, cur, cur_len = [], [], 0
for line in text.split("\n"):
    while ulen(line) > LIMIT:             # a single over-long line: hard cut
        if cur:
            chunks.append("\n".join(cur)); cur, cur_len = [], 0
        cut = head(line, LIMIT)
        chunks.append(line[:cut]); line = line[cut:]
    add = ulen(line) if not cur else cur_len + 1 + ulen(line)
    if cur and add > LIMIT:
        chunks.append("\n".join(cur)); cur, cur_len = [], 0
        add = ulen(line)
    cur.append(line); cur_len = add
if cur:
    chunks.append("\n".join(cur))

n = 0
for c in chunks:
    if not c.strip():                     # the Bot API rejects empty text
        continue
    with open(os.path.join(outdir, "chunk.%03d" % n), "w") as f:
        f.write(c)
    n += 1
print(n)
PY
) || exit 1
[ "$N" -gt 0 ] 2>/dev/null || usage "refusing to send an empty message"

post() {   # post <chunk-file> [extra curl args...] -> 0 iff the Bot API answered 2xx
    local f="$1"; shift
    local code
    code=$(curl -fsS --max-time 15 -o /dev/null -w '%{http_code}' "$@" "$API" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" --data-urlencode "text@$f") || return 1
    case "$code" in 2[0-9][0-9]) return 0 ;; *) return 1 ;; esac
}

send_chunk() {
    post "$1" && return 0
    [ -n "${TG_API_IP:-}" ] || return 1
    # Retry once past DNS. The retry can duplicate a page whose first attempt
    # reached Telegram but whose answer was lost; a duplicate beats a missed page.
    post "$1" --resolve "api.telegram.org:443:${TG_API_IP}"
}

i=0
for f in "$TMPD"/chunk.*; do
    i=$((i + 1))
    send_chunk "$f" || { echo "send-telegram: delivery failed (chunk $i of $N)" >&2; exit 1; }
done
exit 0
