"""HTTP front end: routing, JSON parsing, size limits and status codes.

This module only translates between HTTP and ``LinkStore``. The rules about
what may be stored live in ``validation`` and ``store``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .store import CodeTakenError, LinkStore
from .validation import ValidationError, validate_custom_code, validate_target_url

MAX_BODY_BYTES = 4096
# A refused body is read and dropped (up to this many bytes) before answering.
# Closing a socket that still holds unread data makes the OS reset the
# connection, and the client may then never see the 413 we sent.
MAX_DRAIN_BYTES = 1 << 20
DEFAULT_TIMEOUT_SECONDS = 10.0

_LOG = logging.getLogger("shorturl")
_CODE_PATH = re.compile(r"/([A-Za-z0-9_-]{1,32})")
_STATS_PATH = re.compile(r"/stats/([A-Za-z0-9_-]{1,32})")
_DIGITS = re.compile(r"[0-9]{1,12}")


class _HttpError(Exception):
    """An error that maps straight to a JSON error response."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def format_timestamp(epoch_seconds: float) -> str:
    """Render an epoch time as UTC ISO-8601 with a ``Z`` suffix."""
    moment = datetime.fromtimestamp(epoch_seconds, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Handler(BaseHTTPRequestHandler):
    """Serves ``POST /shorten``, ``GET /<code>`` and ``GET /stats/<code>``."""

    server: ShortenerServer
    server_version = "shorturl"

    @property
    def timeout(self) -> float:  # type: ignore[override]
        """Per-socket timeout so a stalled client cannot hold a thread forever."""
        return self.server.request_timeout

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        stats_match = _STATS_PATH.fullmatch(path)
        code_match = _CODE_PATH.fullmatch(path)
        if path == "/shorten":
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "use POST /shorten"},
                {"Allow": "POST"},
            )
        elif stats_match:
            self._send_stats(stats_match.group(1))
        elif code_match:
            self._redirect(code_match.group(1))
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            if self.path.split("?", 1)[0] != "/shorten":
                raise _HttpError(HTTPStatus.NOT_FOUND, "not found")
            payload = self._read_json_object()
            url = validate_target_url(payload.get("url"), self.server.own_host)
            custom_code = payload.get("custom_code")
            if custom_code is not None:
                validate_custom_code(custom_code)
            link = self.server.store.add(url, custom_code)
        except _HttpError as error:
            self._send_json(error.status, {"error": error.message})
        except ValidationError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except CodeTakenError:
            self._send_json(HTTPStatus.CONFLICT, {"error": "custom_code is already taken"})
        except TimeoutError:
            self._send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "timed out reading the body"})
        else:
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "code": link.code,
                    "short_url": f"{self.server.base_url}/{link.code}",
                    "url": link.url,
                },
            )

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Answer protocol-level failures (bad request line, 501) as JSON, not HTML."""
        self._send_json(HTTPStatus(code), {"error": message or HTTPStatus(code).phrase})

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.info("%s %s", self.address_string(), format % args)

    def _redirect(self, code: str) -> None:
        url = self.server.store.resolve(code)
        if url is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown code"})
            return
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_stats(self, code: str) -> None:
        link = self.server.store.stats(code)
        if link is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown code"})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "code": link.code,
                "url": link.url,
                "clicks": link.clicks,
                "created_at": format_timestamp(link.created_at),
            },
        )

    def _read_json_object(self) -> dict[str, Any]:
        """Read the request body, enforcing the size limit before trusting it."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _HttpError(HTTPStatus.LENGTH_REQUIRED, "Content-Length header is required")
        if _DIGITS.fullmatch(raw_length) is None:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "Content-Length is not a number")
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self._discard_body(length)
            raise _HttpError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"body is larger than {MAX_BODY_BYTES} bytes",
            )
        raw = self.rfile.read(length)
        if len(raw) < length:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "body is shorter than Content-Length")
        try:
            payload = json.loads(raw)
        except ValueError:  # covers both invalid UTF-8 and invalid JSON syntax
            raise _HttpError(HTTPStatus.BAD_REQUEST, "body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return payload

    def _discard_body(self, length: int) -> None:
        remaining = min(length, MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class ShortenerServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` that carries the store and its public identity."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: LinkStore,
        base_url: Optional[str],
        request_timeout: float,
    ) -> None:
        super().__init__(address, _Handler)
        self.store = store
        self.request_timeout = request_timeout
        # The port is only known after binding, which matters for port 0.
        host, port = self.server_address[:2]
        self.base_url = (base_url or f"http://{host}:{port}").rstrip("/")
        own_host = urlsplit(self.base_url).hostname
        if not own_host:
            super().server_close()
            raise ValueError("base_url must be an absolute http(s) URL with a host")
        self.own_host: str = own_host

    def server_close(self) -> None:
        """Stop listening and close the database."""
        super().server_close()
        self.store.close()


def create_server(
    db_path: str = ":memory:",
    host: str = "127.0.0.1",
    port: int = 0,
    base_url: Optional[str] = None,
    clock: Callable[[], float] = time.time,
    request_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ShortenerServer:
    """Build a bound but not yet serving server; call ``serve_forever`` on it.

    ``port=0`` lets the OS pick a free port (read it from ``server_address``).
    ``base_url`` is the public address used in ``short_url`` and for the
    self-loop check; it defaults to the address the server is bound to.
    """
    store = LinkStore(db_path, clock)
    try:
        return ShortenerServer((host, port), store, base_url, request_timeout)
    except BaseException:
        store.close()
        raise
