#!/usr/bin/env python3
"""Local multiplayer host for the official table and operator pages.

Gadgets on the same Wi-Fi can open http://<this-machine-ip>:8080/ and play
together. On Vercel the same room logic runs through /api/room with Redis.
"""

import json
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PLAYER = ROOT / "player"
sys.path.insert(0, str(PLAYER))

from lib.rooms import (  # noqa: E402
    handle_get,
    handle_post,
    handle_sources_get,
    handle_sources_post,
)

STATIC_ROUTES = {
    "/": PLAYER / "index.html",
    "/index.html": PLAYER / "index.html",
    "/lab.html": ROOT / "backend" / "index.html",
    "/mechanic-tables.html": ROOT / "backend" / "mechanic-tables.html",
    "/player-simulation.html": ROOT / "backend" / "player-simulation.html",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ebj/1.0"

    def log_message(self, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        ext = path.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/api/sources":
            self._json(200, handle_sources_get())
            return

        if route == "/api/room":
            room_id = (query.get("room") or ["machine-2"])[0]
            player_id = (query.get("playerId") or [None])[0]
            result = handle_get(room_id, player_id)
            self._json(200 if result.get("ok") else 404, result)
            return

        if route in STATIC_ROUTES:
            self._send_file(STATIC_ROUTES[route])
            return

        if route.startswith("/assets/"):
            name = route[len("/assets/"):]
            safe = (PLAYER / "assets" / name).resolve()
            base = (PLAYER / "assets").resolve()
            if str(safe).startswith(str(base)) and safe.exists():
                self._send_file(safe)
                return
            self.send_error(404)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "Bad JSON."})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": "Bad body."})
            return

        if route == "/api/sources":
            self._json(200, handle_sources_post(body))
            return

        if route == "/api/room":
            self._json(200, handle_post(body))
            return

        self.send_error(404)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Multiplayer blackjack server.")
    parser.add_argument("port", nargs="?", type=int, default=8080)
    args = parser.parse_args()
    print(f"e-blackjack server on http://0.0.0.0:{args.port}  (official table at /)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
