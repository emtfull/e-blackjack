from http.server import BaseHTTPRequestHandler
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.rooms import handle_get, handle_post  # noqa: E402


class handler(BaseHTTPRequestHandler):
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

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        room_id = (query.get("room") or ["machine-2"])[0]
        player_id = (query.get("playerId") or [None])[0]
        result = handle_get(room_id, player_id)
        self._json(200 if result.get("ok") else 404, result)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "Bad JSON."})
            return
        result = handle_post(body)
        self._json(200, result)

    def log_message(self, *args):
        pass
