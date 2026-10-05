#!/usr/bin/env python3
"""fake-service.py <kind> <port> [path] - a stand-in home service on loopback.

kind=http: answers GET <path> with 200 (default /ping; anything else 404).
kind=tcp:  accepts and immediately closes TCP connections.
"""
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

kind, port = sys.argv[1], int(sys.argv[2])
ok_path = sys.argv[3] if len(sys.argv) > 3 else "/ping"


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == ok_path else 404)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, *a):
        pass


if kind == "http":
    ThreadingHTTPServer(("127.0.0.1", port), Ping).serve_forever()
else:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    while True:
        c, _ = s.accept()
        c.close()
