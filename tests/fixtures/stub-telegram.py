#!/usr/bin/env python3
"""Stub Telegram Bot API for drilling gates/gate-telegram-confirm.sh.

Usage: STUB_MODE=<mode> python3 stub-telegram.py <port>
Modes:
  approve   - after the gate sends its prompt, serve a reply "approve <nonce>"
              from the pinned chat (nonce parsed from the prompt text)
  deny      - serve "deny <nonce>" from the pinned chat
  wrong     - serve "approve ffffffff" (bad nonce) from the pinned chat
  stranger  - serve the correct "approve <nonce>" but from a DIFFERENT chat id
  silence   - never serve a reply (gate must time out and refuse)

The pinned chat id the gate expects is 123456789 (test env file).
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

MODE = os.environ.get("STUB_MODE", "silence")
STATE = {"nonce": None, "served": False, "update_id": 100}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if "/sendMessage" in self.path:
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode()
            m = re.search(r"approve\+?\s?([0-9a-f]{8})", body) or \
                re.search(r"approve%20([0-9a-f]{8})", body)
            if m:
                STATE["nonce"] = m.group(1)
            self._json({"ok": True, "result": {"message_id": 1}})
        else:
            self._json({"ok": False})

    def do_GET(self):
        if "/getUpdates" not in self.path:
            self._json({"ok": False})
            return
        result = []
        if STATE["nonce"] and not STATE["served"] and MODE != "silence":
            chat = 999999 if MODE == "stranger" else 123456789
            text = {"approve": f"approve {STATE['nonce']}",
                    "deny": f"deny {STATE['nonce']}",
                    "wrong": "approve ffffffff",
                    "stranger": f"approve {STATE['nonce']}"}[MODE]
            STATE["update_id"] += 1
            STATE["served"] = True
            result = [{"update_id": STATE["update_id"],
                       "message": {"chat": {"id": chat}, "text": text}}]
        self._json({"ok": True, "result": result})


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
