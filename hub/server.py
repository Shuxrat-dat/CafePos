"""HTTP-API для касс. Все ответы JSON. Касса подписывает запросы своим ключом: Authorization: Bearer <ключ>."""
import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from .service import HubError

log = logging.getLogger("hub")

MAX_JSON = 2 * 1024 * 1024
MAX_DOCUMENT = 55 * 1024 * 1024   # Telegram принимает файлы до 50 МБ


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CafeHub"
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _reply(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _raw(self, limit):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HubError(400, "bad length") from None
            if length > limit:
                raise HubError(413, "too large")
            return self.rfile.read(length) if length else b""

        def _json(self):
            raw = self._raw(MAX_JSON)
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except ValueError:
                raise HubError(400, "bad json") from None
            if not isinstance(data, dict):
                raise HubError(400, "bad json")
            return data

        def _venue(self):
            header = self.headers.get("Authorization", "")
            venue = hub.store.venue_by_key(header[7:].strip() if header.startswith("Bearer ") else "")
            if not venue:
                raise HubError(401, "bad key")
            return venue

        def _handle(self, method):
            url = urlparse(self.path)
            route, query = url.path.rstrip("/"), parse_qs(url.query)
            try:
                if method == "GET" and route == "/health":
                    return self._reply(200, {"ok": True})
                if method == "POST" and route == "/v1/venues":
                    data = self._json()
                    return self._reply(200, hub.register(data.get("name"), data.get("lang"),
                                                         data.get("registration_token")))
                venue = self._venue()
                if method == "GET" and route == "/v1/status":
                    return self._reply(200, hub.status(venue, want_link=query.get("link") == ["1"]))
                if method == "POST" and route == "/v1/link":
                    return self._reply(200, hub.new_link(venue))
                if method == "POST" and route == "/v1/venue":
                    data = self._json()
                    hub.update_venue(venue, data.get("name"), data.get("lang"))
                    return self._reply(200, {"ok": True})
                if method == "POST" and route == "/v1/events":
                    data = self._json()
                    return self._reply(200, hub.send_event(venue, data.get("event"), data.get("text", "")))
                if method == "POST" and route == "/v1/snapshot":
                    return self._reply(200, hub.set_snapshot(venue, self._json().get("data")))
                if method == "POST" and route == "/v1/test":
                    return self._reply(200, hub.send_test(venue))
                if method == "POST" and route == "/v1/disconnect":
                    hub.disconnect(venue, self._json().get("chat_id"))
                    return self._reply(200, {"ok": True})
                if method == "POST" and route == "/v1/document":
                    content = self._raw(MAX_DOCUMENT)
                    name = unquote(self.headers.get("X-Filename", "file.bin"))
                    caption = unquote(self.headers.get("X-Caption", ""))
                    return self._reply(200, hub.send_document(venue, name, content, caption))
                raise HubError(404, "not found")
            except HubError as exc:
                self._reply(exc.status, {"error": exc.message})
            except Exception:
                log.exception("Ошибка обработки запроса %s %s", method, route)
                self._reply(500, {"error": "server error"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler
