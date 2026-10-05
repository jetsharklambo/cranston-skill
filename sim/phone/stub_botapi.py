#!/usr/bin/env python3
"""stub_botapi.py --port P --log FILE --mode-file FILE - a stand-in Telegram
Bot API for the simulation suite (lifted from tests/test_delivery.sh's inline
stub; kept standalone so sim/soak.py and the cloud drill share one recorder).

Behavior:
  * every POST is recorded as ONE JSON line in --log:
      {"path": ..., "chat_id": ..., "text": ..., "status": 200|500}
    ("status" is the one field added over the test_delivery original, so a
    harness can tell a delivered request from one the 500 mode rejected -
    bin/send-telegram.sh retries failures, and the retries are requests too.)
  * the --mode-file selects the answer: content "500" -> HTTP 500 with a
    Telegram-style error body; anything else (or an unreadable file) -> HTTP
    200 with {"ok": true}. casa-ctl's `tg-mode ok|500` writes this file.
  * GET / answers 200 so a harness can poll for readiness.
  * send-telegram.sh chunks long texts at the 4096-unit limit; each chunk is
    its own POST and therefore its own log line - by design (the recorder
    mirrors what the Bot API would have seen, not what the sender meant).

stdlib only; binds 127.0.0.1 only.
"""
import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--mode-file", required=True)
    args = ap.parse_args()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(200, {"ok": True, "result": "stub"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            q = parse_qs(self.rfile.read(n).decode("utf-8", "replace"),
                         keep_blank_values=True)
            try:
                mode = open(args.mode_file).read().strip()
            except Exception:
                mode = "ok"
            status = 500 if mode == "500" else 200
            rec = {"path": self.path,
                   "chat_id": (q.get("chat_id") or [""])[0],
                   "text": (q.get("text") or [""])[0],
                   "status": status,
                   # real receipt time, for harnesses that key deliveries on
                   # the wall clock (the cloud drill); the soak ignores it
                   "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            with open(args.log, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if status == 500:
                self._reply(500, {"ok": False, "error_code": 500,
                                  "description": "stub: Internal Server Error"})
            else:
                self._reply(200, {"ok": True, "result": {"message_id": 1}})

    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
