#!/usr/bin/env python3
"""Static server plus a shared card-source store.

The mechanic page and the live table publish here. The official table,
often open on another port, reads the same store.
"""

import json
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = Path("/tmp/ebj-sources.json")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        if self.path.split("?", 1)[0] != "/api/sources":
            self.send_error(404)
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/sources":
            body = STORE.read_bytes() if STORE.exists() else b'{"machine":{"tables":{}},"player":{"sequence":[]},"lab":{"sequence":[]}}'
            self._send_json(200, body)
            return
        super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/sources":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            incoming = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        if not isinstance(incoming, dict):
            self.send_error(400)
            return
        current = {}
        if STORE.exists():
            try:
                current = json.loads(STORE.read_text())
            except json.JSONDecodeError:
                current = {}
        if not isinstance(current, dict):
            current = {}
        for key in ("machine", "player", "lab"):
            if key in incoming and isinstance(incoming[key], dict):
                current[key] = incoming[key]
        encoded = json.dumps(current).encode()
        STORE.write_bytes(encoded)
        self._send_json(200, b'{"ok":true}')

    def _send_json(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serve the lab and the shared card source.")
    parser.add_argument("port", nargs="?", type=int, default=8080)
    args = parser.parse_args()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
