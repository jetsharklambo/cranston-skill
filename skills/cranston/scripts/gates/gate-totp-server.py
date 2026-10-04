#!/usr/bin/env python3
"""gate-totp-server.py - single-file, stdlib-only TOTP verifier (RFC 6238).

Runs on a SECOND always-on device (HA box, NAS, spare Pi, an old phone under
Termux) - never on the box the agent lives on, where the secret would be
readable by the thing the gate exists to restrain. No Docker, no pip.

Endpoints (and nothing else - no setup, no QR, no secret read-back):
    POST /verify  {"code": "123456"}  ->  {"ok": true|false}
    GET  /health                      ->  ok

Usage:
    gate-totp-server.py --mint                      # print a new secret + otpauth:// URI
    gate-totp-server.py [secret-file] [port]        # serve (defaults below)

Setup, once:
    1. On this device:  python3 gate-totp-server.py --mint
       - put the base32 secret in ~/.cranston-totp-secret, chmod 600
       - scan/enter the otpauth:// URI in the admin's authenticator app
    2. Run this script at boot (cron @reboot, a systemd unit, or Termux:Boot).
    3. On the monitor box: GATE_TOTP_URL=http://<this-device>:8766 and
       "approval_gate": "gates/gate-totp-remote.sh".

Security properties: +-1 time-step tolerance; constant-time compare; lockout
(5 failures/minute -> refuse everything for 30 minutes); refuses to start if
the secret file is group/world-readable. GATE_TOTP_NOW overrides the clock
FOR TESTS ONLY - never set it on a real deployment.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_SECRET_FILE = os.path.expanduser("~/.cranston-totp-secret")
DEFAULT_PORT = 8766
STEP = 30
DIGITS = 6
LOCKOUT_FAILS = 5          # failures within LOCKOUT_WINDOW...
LOCKOUT_WINDOW = 60
LOCKOUT_SECONDS = 1800     # ...refuse everything this long


def totp(secret_b32, t, step_offset=0):
    s = secret_b32.strip().upper().replace(" ", "").rstrip("=")
    key = base64.b32decode(s + "=" * ((8 - len(s) % 8) % 8), casefold=True)
    counter = int(t) // STEP + step_offset
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(code).zfill(DIGITS)


def mint():
    raw = secrets.token_bytes(20)
    b32 = base64.b32encode(raw).decode().rstrip("=")
    print(f"secret (put in {DEFAULT_SECRET_FILE}, chmod 600):\n  {b32}\n")
    print("authenticator-app enrolment URI:\n"
          f"  otpauth://totp/cranston?secret={b32}&issuer=cranston&digits={DIGITS}&period={STEP}")
    return 0


class State:
    fails = []            # timestamps of recent failures
    locked_until = 0.0


def check_code(secret, code):
    nw = float(os.environ.get("GATE_TOTP_NOW", time.time()))
    if time.time() < State.locked_until:
        return False, "locked out"
    good = any(hmac.compare_digest(totp(secret, nw, o), code) for o in (-1, 0, 1))
    if not good:
        t = time.time()
        State.fails = [f for f in State.fails if t - f < LOCKOUT_WINDOW] + [t]
        if len(State.fails) >= LOCKOUT_FAILS:
            State.locked_until = t + LOCKOUT_SECONDS
            return False, "locked out"
        return False, "bad code"
    State.fails = []
    return True, "ok"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--mint":
        return mint()
    secret_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SECRET_FILE
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    st = os.stat(secret_file)
    if st.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        sys.exit(f"refusing to start: {secret_file} is group/world-accessible - chmod 600 it")
    secret = open(secret_file).read().strip()
    totp(secret, time.time())  # fail fast on a malformed secret

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, '"ok"')
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self):
            if self.path != "/verify":
                self._send(404, '{"error": "not found"}')
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                code = str(body.get("code", ""))
            except Exception:
                self._send(400, '{"ok": false, "error": "bad request"}')
                return
            if not (code.isdigit() and len(code) == DIGITS):
                self._send(400, '{"ok": false, "error": "malformed code"}')
                return
            ok, why = check_code(secret, code)
            self._send(200 if ok else 403, json.dumps({"ok": ok, "why": why}))

    print(f"gate-totp-server: verifying on 0.0.0.0:{port} (secret: {secret_file})")
    HTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
