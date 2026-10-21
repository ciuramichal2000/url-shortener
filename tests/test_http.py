import http.client
import json
import os
import socket
import sys
import threading
from typing import Any, NamedTuple, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from shorturl.server import MAX_BODY_BYTES, create_server

# 2023-11-14T22:13:20Z, a well-known epoch value, and exactly one day later.
START_TIME = 1_700_000_000
ONE_DAY = 86_400


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Response(NamedTuple):
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class Client:
    """Talks to a running server over a real socket; never follows redirects."""

    def __init__(self, server, clock: FakeClock) -> None:
        self.server = server
        self.clock = clock
        self.host, self.port = server.server_address[:2]
        self.base_url = server.base_url

    def connection(self, timeout: float = 5) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def request(self, method: str, path: str, body: Optional[bytes] = None, headers=None) -> Response:
        conn = self.connection()
        try:
            conn.request(method, path, body=body, headers=headers or {})
            return self.read(conn)
        finally:
            conn.close()

    @staticmethod
    def read(conn: http.client.HTTPConnection) -> Response:
        response = conn.getresponse()
        return Response(response.status, response.headers, response.read())

    def get(self, path: str) -> Response:
        return self.request("GET", path)

    def post_raw(self, body: bytes) -> Response:
        return self.request("POST", "/shorten", body, {"Content-Type": "application/json"})

    def shorten(self, url: Any, custom_code: Any = None, **extra: Any) -> Response:
        payload: dict[str, Any] = {"url": url, **extra}
        if custom_code is not None:
            payload["custom_code"] = custom_code
        return self.post_raw(json.dumps(payload).encode())


@pytest.fixture
def start_server():
    started = []

    def start(**options: Any) -> Client:
        clock = FakeClock(START_TIME)
        server = create_server(clock=clock, **options)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        started.append((server, thread))
        return Client(server, clock)

    yield start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def client(start_server) -> Client:
    return start_server()


def assert_json_error(response: Response, status: int) -> str:
    assert response.status == status
    assert response.headers["Content-Type"] == "application/json"
    message = response.json()["error"]
    assert isinstance(message, str) and message
    return message


# --- the main flow ---------------------------------------------------------


def test_shorten_returns_first_base62_code_and_matching_short_url(client):
    response = client.shorten("https://example.com/some/long/page")
    assert response.status == 201
    assert response.json() == {
        "code": "1001",  # row 1 -> 62 ** 3 + 1
        "short_url": f"http://127.0.0.1:{client.port}/1001",
        "url": "https://example.com/some/long/page",
    }


def test_following_a_short_link_redirects_with_302_and_counts_each_click(client):
    client.shorten("https://example.com/landing?ref=1#top")
    for _ in range(2):
        response = client.get("/1001")
        assert response.status == 302
        assert response.headers["Location"] == "https://example.com/landing?ref=1#top"
    stats = client.get("/stats/1001").json()
    assert stats["clicks"] == 2


def test_query_string_on_a_short_link_is_ignored_but_the_click_still_counts(client):
    client.shorten("https://example.com/")
    assert client.get("/1001?utm_source=newsletter").status == 302
    assert client.get("/stats/1001").json()["clicks"] == 1


def test_stats_report_target_clicks_and_creation_time_without_counting_a_click(client):
    client.shorten("https://example.com/a")
    client.clock.now = START_TIME + ONE_DAY
    client.shorten("https://example.com/b")

    first = client.get("/stats/1001")
    again = client.get("/stats/1001")
    second = client.get("/stats/1002")

    assert first.json() == again.json() == {
        "code": "1001",
        "url": "https://example.com/a",
        "clicks": 0,
        "created_at": "2023-11-14T22:13:20Z",
    }
    assert second.json()["created_at"] == "2023-11-15T22:13:20Z"


def test_unknown_code_is_404_for_redirect_and_stats(client):
    assert_json_error(client.get("/zzzz"), 404)
    assert_json_error(client.get("/stats/zzzz"), 404)


def test_concurrent_visitors_are_all_counted(client):
    client.shorten("https://example.com/busy")
    results: list[int] = []

    def visit() -> None:
        results.append(client.get("/1001").status)

    threads = [threading.Thread(target=visit) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [302] * 20
    assert client.get("/stats/1001").json()["clicks"] == 20


# --- custom codes ----------------------------------------------------------


def test_custom_code_is_used_and_redirects(client):
    response = client.shorten("https://example.com/sale", custom_code="summer-sale")
    assert response.status == 201
    assert response.json()["code"] == "summer-sale"
    assert response.json()["short_url"].endswith("/summer-sale")
    assert client.get("/summer-sale").headers["Location"] == "https://example.com/sale"


def test_duplicate_custom_code_is_409_and_does_not_overwrite(client):
    client.shorten("https://example.com/first", custom_code="taken")
    clash = client.shorten("https://example.com/second", custom_code="taken")
    assert "taken" in assert_json_error(clash, 409)
    assert client.get("/taken").headers["Location"] == "https://example.com/first"


def test_null_custom_code_means_generate_one(client):
    response = client.post_raw(b'{"url": "https://example.com/", "custom_code": null}')
    assert response.status == 201
    assert response.json()["code"] == "1001"


@pytest.mark.parametrize(
    "code", ["ab", "x" * 33, "has space", "dot.dot", "shorten", "stats", "", 12345]
)
def test_invalid_custom_codes_are_400(client, code):
    response = client.post_raw(
        json.dumps({"url": "https://example.com/", "custom_code": code}).encode()
    )
    assert_json_error(response, 400)


def test_custom_code_equal_to_an_upcoming_generated_code_never_collides(client):
    # "1002" is what row 2 would generate. It is taken as a custom code by
    # row 1, so the generator must move past it instead of failing or reusing it.
    client.shorten("https://example.com/custom", custom_code="1002")
    codes = [client.shorten(f"https://example.com/{n}").json()["code"] for n in range(2)]
    assert codes == ["1003", "1004"]
    assert client.get("/1002").headers["Location"] == "https://example.com/custom"


# --- validation over HTTP --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/",
        "example.com",
        "http://example.com/a b",
        "http://example.com/x\r\nSet-Cookie: a=b",
    ],
)
def test_unsafe_urls_are_rejected_and_nothing_is_stored(client, url):
    assert_json_error(client.shorten(url), 400)
    assert client.get("/stats/1001").status == 404


def test_url_longer_than_2048_characters_is_400(client):
    assert_json_error(client.shorten("http://example.com/" + "a" * 2030), 400)


def test_links_back_to_the_service_are_rejected_without_consuming_a_code(client):
    own = f"http://127.0.0.1:{client.port}/1001"
    for looped in (own, f"http://localhost:{client.port}/x", "http://LOCALHOST./x"):
        message = assert_json_error(client.shorten(looped), 400)
        assert "back at this service" in message
    # Had a rejected request stored anything, this would not be row 1.
    assert client.shorten("https://example.com/real").json()["code"] == "1001"


def test_configured_public_base_url_drives_short_url_and_loop_check(start_server):
    client = start_server(base_url="https://sho.rt/")
    assert_json_error(client.shorten("https://sho.rt/abc"), 400)
    accepted = client.shorten("https://sho.rt.example.org/page")
    assert accepted.status == 201
    assert accepted.json()["short_url"] == "https://sho.rt/1001"


def test_base_url_without_a_host_is_refused():
    with pytest.raises(ValueError, match="base_url"):
        create_server(base_url="short.example")


# --- request framing and size limits ---------------------------------------


def test_body_of_exactly_the_limit_is_accepted_and_one_byte_more_is_413(client):
    def body_of_size(size: int) -> bytes:
        skeleton = json.dumps({"url": "https://example.com/", "pad": ""}).encode()
        padding = "x" * (size - len(skeleton))
        return json.dumps({"url": "https://example.com/", "pad": padding}).encode()

    at_limit = body_of_size(MAX_BODY_BYTES)
    assert len(at_limit) == MAX_BODY_BYTES
    assert client.post_raw(at_limit).status == 201

    over_limit = body_of_size(MAX_BODY_BYTES + 1)
    assert len(over_limit) == MAX_BODY_BYTES + 1
    assert "larger than" in assert_json_error(client.post_raw(over_limit), 413)


def test_a_much_larger_body_is_refused_with_413_and_the_client_still_gets_the_answer(client):
    huge = json.dumps({"url": "https://example.com/", "pad": "x" * 200_000}).encode()
    assert_json_error(client.post_raw(huge), 413)
    assert client.get("/stats/1001").status == 404  # nothing was stored


def test_missing_content_length_is_411(client):
    conn = client.connection()
    conn.putrequest("POST", "/shorten")
    conn.endheaders()
    assert_json_error(client.read(conn), 411)
    conn.close()


@pytest.mark.parametrize("value", ["abc", "-5", "1e3", "12 "])
def test_malformed_content_length_is_400(client, value):
    conn = client.connection()
    conn.putrequest("POST", "/shorten")
    conn.putheader("Content-Length", value)
    conn.endheaders()
    assert_json_error(client.read(conn), 400)
    conn.close()


def test_body_shorter_than_declared_is_400(client):
    conn = client.connection()
    conn.putrequest("POST", "/shorten")
    conn.putheader("Content-Length", "50")
    conn.endheaders(b'{"url": "x"}')
    conn.sock.shutdown(socket.SHUT_WR)  # no more bytes will ever arrive
    assert "shorter" in assert_json_error(client.read(conn), 400)
    conn.close()


def test_stalled_body_times_out_with_408(start_server):
    client = start_server(request_timeout=0.3)
    conn = client.connection()
    conn.putrequest("POST", "/shorten")
    conn.putheader("Content-Length", "50")
    conn.endheaders()  # promises 50 bytes, sends none
    assert_json_error(client.read(conn), 408)
    conn.close()


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"",
        b'{"url": "https://example.com/"',  # truncated
        b'{"url": "https://example.com/\xff"}',  # invalid UTF-8
        b'["https://example.com/"]',  # valid JSON, wrong shape
        b'"https://example.com/"',
        b"[" * 4000,  # unterminated deep nesting
        b"[" * 2048 + b"]" * 2048,  # balanced, deepest array a 4 KiB body can hold
    ],
)
def test_bodies_that_are_not_a_json_object_are_400(client, body):
    assert_json_error(client.post_raw(body), 400)


@pytest.mark.parametrize("payload", [{}, {"url": None}, {"url": 42}, {"url": ["http://a.example"]}])
def test_missing_or_non_string_url_is_400(client, payload):
    assert_json_error(client.post_raw(json.dumps(payload).encode()), 400)


# --- routing ---------------------------------------------------------------


def test_get_on_the_shorten_endpoint_is_405_and_names_the_allowed_method(client):
    response = client.get("/shorten")
    assert_json_error(response, 405)
    assert response.headers["Allow"] == "POST"


def test_post_to_any_other_path_is_404(client):
    response = client.request("POST", "/1001", b"{}", {"Content-Type": "application/json"})
    assert_json_error(response, 404)


def test_unsupported_method_gets_a_json_501(client):
    assert_json_error(client.request("PUT", "/shorten", b"{}"), 501)


@pytest.mark.parametrize("path", ["/", "/stats", "/stats/", "/a/b/c", "/%20", "/" + "a" * 33])
def test_paths_that_cannot_be_codes_are_404(client, path):
    assert_json_error(client.get(path), 404)
