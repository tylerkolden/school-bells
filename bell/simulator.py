"""Safe local HTTP paging receiver used by the Docker test environment."""

from __future__ import annotations

import argparse
import html
import json
import logging
import threading
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 64 * 1024


class EventStore:
    """Bounded, thread-safe collection of simulated page events."""

    def __init__(self, maximum: int = 100) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maximum)
        self._lock = threading.Lock()

    def add(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.appendleft(event)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)


class SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: EventStore) -> None:
        super().__init__(address, SimulatorHandler)
        self.store = store


class SimulatorHandler(BaseHTTPRequestHandler):
    server: SimulatorServer
    protocol_version = "HTTP/1.1"

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: object) -> None:
        self._send(
            status,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            "application/json; charset=utf-8",
        )

    def _dashboard(self) -> bytes:
        rows = []
        for event in self.server.store.snapshot():
            payload = event["payload"]
            rows.append(
                "<tr>"
                f"<td>{html.escape(event['received_at'])}</td>"
                f"<td>{html.escape(str(payload.get('event', 'Page')))}</td>"
                f"<td>{html.escape(str(payload.get('zone', 'unknown')))}</td>"
                f"<td>{html.escape(str(payload.get('sound', 'unknown')))}</td>"
                f"<td><code>{html.escape(event['idempotency_key'])}</code></td>"
                "</tr>"
            )
        content = "".join(rows) or '<tr><td colspan="5">No simulated pages received yet.</td></tr>'
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="3">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local Bell Receiver</title><style>
body{{font:16px system-ui,sans-serif;margin:0;background:#f5f3ee;color:#17221d}}
main{{max-width:1100px;margin:3rem auto;padding:0 1rem}} .card{{background:white;border-radius:14px;
padding:1.5rem;box-shadow:0 8px 30px #17221d18}} table{{width:100%;border-collapse:collapse}}
th,td{{padding:.75rem;text-align:left;border-bottom:1px solid #d9dedb}} th{{color:#496158}}
.badge{{display:inline-block;padding:.3rem .65rem;border-radius:999px;background:#dff4e8;color:#146238}}
button{{padding:.65rem 1rem;border:0;border-radius:8px;background:#8b2d35;color:white;cursor:pointer}}
code{{font-size:.8rem}} p{{line-height:1.5;color:#496158}}
</style></head><body><main><span class="badge">Safe simulator online</span>
<h1>Local bell receiver</h1><p>This records delivery metadata only. It never sends multicast,
contacts phones, or plays audio on the host.</p><section class="card"><table><thead><tr><th>Received</th>
<th>Event</th><th>Zone</th><th>Sound</th><th>Idempotency key</th></tr></thead>
<tbody>{content}</tbody></table><form method="post" action="/clear"><p><button type="submit">
Clear events</button></p></form></section></main></body></html>""".encode()

    def do_HEAD(self) -> None:
        if urlsplit(self.path).path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
        elif path == "/events":
            events = self.server.store.snapshot()
            self._json(HTTPStatus.OK, {"count": len(events), "events": events})
        elif path == "/":
            self._send(HTTPStatus.OK, self._dashboard(), "text/html; charset=utf-8")
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/clear":
            self.server.store.clear()
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path != "/page":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content_type = self.headers.get_content_type()
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if content_type != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
            return
        if not 0 <= length <= MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request is too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
            return
        event = {
            "received_at": datetime.now(UTC).isoformat(),
            "idempotency_key": self.headers.get("Idempotency-Key", ""),
            "payload": payload,
        }
        self.server.store.add(event)
        LOGGER.info("simulated_page_received", extra={"event": payload})
        self._json(HTTPStatus.ACCEPTED, {"accepted": True})

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("simulator_http", extra={"client": self.client_address[0], "detail": format % args})


def create_server(host: str = "127.0.0.1", port: int = 9000) -> SimulatorServer:
    return SimulatorServer((host, port), EventStore())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = create_server(args.host, args.port)
    LOGGER.info("simulator_started", extra={"host": args.host, "port": args.port})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
