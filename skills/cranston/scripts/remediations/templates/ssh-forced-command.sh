#!/bin/bash
# ssh-forced-command.sh - FIXED-CONTENT remediation: trigger ONE forced
# command on ANOTHER host over SSH. This box holds a private key whose
# matching authorized_keys line on the remote host PINS the command, so the
# most this script can ever make that host do is run that one command -
# whatever request travels with the connection. Auto-class safe when the
# pinned command is itself fail-safe (a restart, a remount): nothing here can
# cut power or run arbitrary remote shell.
#
# Remote setup (once, on the target host, one line in ~<user>/.ssh/authorized_keys):
#   command="/usr/local/bin/restart-x.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 AAAA...the-key... cranston@this-box
# With command="..." sshd ignores whatever command the client asks for and
# runs the pinned one; the client's request (our single argument, when the
# engine passes one) reaches it only as $SSH_ORIGINAL_COMMAND, which the
# pinned script may read (e.g. to pick one of its allowlisted targets) or
# ignore. Make the key on this box with
#   ssh-keygen -t ed25519 -N '' -f <SSH_KEY>
# and keep it mode 600: this script refuses a key anyone else can read. The
# remote host key must already be in known_hosts (StrictHostKeyChecking=yes
# never learns a new one - `ssh-keyscan -p <port> <host> >> ~/.ssh/known_hosts`
# once, by hand, after checking the fingerprint).
#
# Params:
#   REMEDIATION_KEY      required
#   SSH_TARGET           required - user@host
#   SSH_KEY              required - path to the private key (must exist, mode *00)
#   SSH_PORT             default 22
#   SSH_CONNECT_TIMEOUT  default 10 (seconds)
#   SSH_KNOWN_HOSTS      optional - a known_hosts file holding the target's key
#                        (default: ssh's own)
#   VERIFY_URL           optional - poll it with curl until a 2xx answers
#   VERIFY_TCP           optional - host:port to poll until a TCP connect succeeds
#                        At least one VERIFY_* should be set: without any, the
#                        script acts but cannot claim success and exits 1
#                        ("unverified") - the engine's own re-check decides.
#   VERIFY_TRIES         default 6, VERIFY_SLEEP default 5
#   CAP_MAX              default 3 per CAP_WINDOW_H (default 6)
#
# Argument: optional, a single token the engine/gate already validated as
# [A-Za-z0-9][A-Za-z0-9._@:-]{0,63}; passed to ssh as the remote command,
# argv only - no string is ever composed.
#
# Exit codes: 0 fixed+verified | 1 failed/unverified | 64 refused | 75 cap.
set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/remediation.sh"

: "${REMEDIATION_KEY:?set REMEDIATION_KEY in params}"
TARGET="${SSH_TARGET:-}"
ARG="${1:-}"

rem_begin "$REMEDIATION_KEY" "ssh-forced-command-${TARGET:-?}"
rem_cap "${CAP_MAX:-3}" "${CAP_WINDOW_H:-6}"

# ---- pre-flight: refuse anything we cannot run safely -------------------------
[ $# -le 1 ] || rem_refuse "takes at most one argument, got $#"
if [ -n "$ARG" ]; then
    case "$ARG" in -*|*/*|*[[:space:]]*) rem_refuse "malformed argument '$ARG'" ;; esac
fi
[ -n "$TARGET" ] || rem_refuse "SSH_TARGET not set in params (user@host)"
case "$TARGET" in
    -*|*[[:space:]]*|*/*) rem_refuse "SSH_TARGET '$TARGET' is not a user@host" ;;
    *@?*) : ;;
    *) rem_refuse "SSH_TARGET '$TARGET' is not a user@host" ;;
esac
PORT="${SSH_PORT:-22}"
case "$PORT" in ''|*[!0-9]*) rem_refuse "SSH_PORT '$PORT' is not a number" ;; esac
CTO="${SSH_CONNECT_TIMEOUT:-10}"
case "$CTO" in ''|*[!0-9]*) rem_refuse "SSH_CONNECT_TIMEOUT '$CTO' is not a number" ;; esac

[ -n "${SSH_KEY:-}" ] || rem_refuse "SSH_KEY not set in params (path to the private key)"
[ -f "$SSH_KEY" ] || rem_refuse "SSH_KEY $SSH_KEY: no such file"
PERMS=$(stat -c %a "$SSH_KEY" 2>/dev/null || stat -f %Lp "$SSH_KEY" 2>/dev/null)
case "$PERMS" in
    *00) : ;;
    *) rem_refuse "SSH_KEY $SSH_KEY must be mode 600 (is ${PERMS:-unreadable}) - a readable key is a borrowed key" ;;
esac

VURL="${VERIFY_URL:-}"
VTCP="${VERIFY_TCP:-}"
VTCP_HOST=""; VTCP_PORT=""
if [ -n "$VTCP" ]; then
    VTCP_HOST="${VTCP%:*}"; VTCP_PORT="${VTCP##*:}"
    case "$VTCP_PORT" in ''|*[!0-9]*) rem_refuse "VERIFY_TCP '$VTCP' must be host:port" ;; esac
    [ -n "$VTCP_HOST" ] && [ "$VTCP_HOST" != "$VTCP" ] || rem_refuse "VERIFY_TCP '$VTCP' must be host:port"
fi

# ---- act: argv only ----------------------------------------------------------
set -- -i "$SSH_KEY" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "ConnectTimeout=$CTO"
[ -n "${SSH_KNOWN_HOSTS:-}" ] && set -- "$@" -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS"
set -- "$@" "$TARGET"
[ -n "$ARG" ] && set -- "$@" "$ARG"

ssh "$@" </dev/null
rc=$?
if [ $rc -ne 0 ]; then
    rem_result fail "ssh rc=$rc target=$TARGET"
    echo "ssh to $TARGET exited $rc (255 = could not connect or key refused)"
    exit 1
fi

# ---- verify: never claim success unverified ------------------------------------
if [ -z "$VURL" ] && [ -z "$VTCP" ]; then
    rem_result fail "ran on $TARGET but unverified by script (no VERIFY_URL / VERIFY_TCP)"
    echo "unverified by script; the engine's re-check decides - set VERIFY_URL or VERIFY_TCP in params"
    exit 1
fi

verify_url() { curl -fs -o /dev/null --max-time 8 "$VURL" 2>/dev/null; }
verify_tcp() {
    python3 - "$VTCP_HOST" "$VTCP_PORT" <<'PY'
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close()
except OSError:
    raise SystemExit(1)
PY
}
verify() {
    if [ -n "$VURL" ]; then verify_url || return 1; fi
    if [ -n "$VTCP" ]; then verify_tcp || return 1; fi
    return 0
}

if rem_verify "${VERIFY_TRIES:-6}" "${VERIFY_SLEEP:-5}" verify; then
    rem_result ok "target=$TARGET${ARG:+ arg=$ARG} verified=${VURL:+url}${VTCP:+ tcp}"
    exit 0
fi
rem_result fail "ssh accepted (rc=0) but ${VURL:+$VURL }${VTCP:+$VTCP }did not come back within the poll window"
echo "command ran but the service did not come back within the poll window; the next monitor cycle is the authority"
exit 1
