#!/usr/bin/env python3
"""Tiny fake Home Assistant for template tests.

  stub-ha.py <port> <entity_id> <state> <age_minutes>

GET  /api/states/<entity_id>          -> {"state": ..., "last_changed": now-age}
POST /api/services/<domain>/turn_on   -> 200 []  (and the stored state becomes "on")
Anything else -> 404. No auth check (the templates send a token; we ignore it).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

port, entity = int(sys.argv[1]), sys.argv[2]
state = {"value": sys.argv[3], "age_min": float(sys.argv[4])}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == f"/api/states/{entity}":
            lc = datetime.now(timezone.utc) - timedelta(minutes=state["age_min"])
            self._send(200, {"entity_id": entity, "state": state["value"],
                             "last_changed": lc.strftime("%Y-%m-%dT%H:%M:%S+00:00")})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/services/") and self.path.endswith("/turn_on"):
            state["value"] = "on"
            state["age_min"] = 0
            self._send(200, [])
        else:
            self._send(404, {"message": "not found"})


HTTPServer(("127.0.0.1", port), H).serve_forever()
