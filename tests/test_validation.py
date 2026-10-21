import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from shorturl.validation import (
    ValidationError,
    canonical_host,
    validate_custom_code,
    validate_target_url,
)

OWN_HOST = "short.test"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com/path?query=1#frag",
        "HTTPS://Example.COM/Upper",
        "http://example.com:8080/x",
        "http://short.test.evil.example/",  # looks like us, is not us
        "http://notshort.test/",
        "http://short.test@evil.example/",  # user-info trick: the real host is evil.example
    ],
)
def test_accepts_ordinary_http_and_https_urls_unchanged(url):
    assert validate_target_url(url, OWN_HOST) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "//example.com/no-scheme",
        "example.com/no-scheme",
    ],
)
def test_rejects_non_http_schemes(url):
    with pytest.raises(ValidationError, match="only http and https"):
        validate_target_url(url, OWN_HOST)


@pytest.mark.parametrize("url", ["http://", "https:///path", "http:example.com", "http://user@/x"])
def test_rejects_urls_without_a_host(url):
    with pytest.raises(ValidationError, match="host"):
        validate_target_url(url, OWN_HOST)


@pytest.mark.parametrize("url", [None, ""])
def test_rejects_missing_url(url):
    with pytest.raises(ValidationError, match="required"):
        validate_target_url(url, OWN_HOST)


@pytest.mark.parametrize("url", [123, ["http://example.com"], {"url": "x"}, True])
def test_rejects_non_string_url(url):
    with pytest.raises(ValidationError, match="must be a string"):
        validate_target_url(url, OWN_HOST)


def test_url_length_limit_is_inclusive_at_2048_characters():
    prefix = "http://example.com/"
    at_limit = prefix + "a" * (2048 - len(prefix))
    assert len(at_limit) == 2048
    assert validate_target_url(at_limit, OWN_HOST) == at_limit
    with pytest.raises(ValidationError, match="longer than 2048"):
        validate_target_url(at_limit + "a", OWN_HOST)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a b",
        "http://example.com/a\tb",
        "http://example.com/a\r\nSet-Cookie: x=1",  # header injection attempt
        "http://example.com/\x00",
        "http://example.com/\x7f",
        " http://example.com/",  # urllib strips this, a Location header must not carry it
        "http://short.test\\@evil.example/",  # browsers read the backslash as a slash
    ],
)
def test_rejects_whitespace_control_characters_and_backslashes(url):
    with pytest.raises(ValidationError, match="whitespace"):
        validate_target_url(url, OWN_HOST)


def test_rejects_non_ascii_urls():
    with pytest.raises(ValidationError, match="ASCII"):
        validate_target_url("http://example.com/café", OWN_HOST)


@pytest.mark.parametrize("url", ["http://example.com:99999/", "http://example.com:abc/"])
def test_rejects_invalid_ports(url):
    with pytest.raises(ValidationError, match="malformed"):
        validate_target_url(url, OWN_HOST)


@pytest.mark.parametrize(
    "url",
    [
        "http://short.test/abc",
        "https://SHORT.TEST/abc",  # host names are case-insensitive
        "http://short.test./abc",  # a trailing dot is the same host
        "http://short.test:9999/abc",  # the port is deliberately ignored
        "http://evil.example@short.test/abc",  # the real host is after the '@'
    ],
)
def test_rejects_links_back_to_the_service_host(url):
    with pytest.raises(ValidationError, match="points back"):
        validate_target_url(url, OWN_HOST)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://127.0.0.1:8080/x",
        "http://127.5.6.7/x",  # the whole 127.0.0.0/8 block is loopback
        "http://[::1]/x",
        "http://0.0.0.0/x",
    ],
)
def test_every_loopback_spelling_counts_as_the_local_service(url):
    with pytest.raises(ValidationError, match="points back"):
        validate_target_url(url, "localhost")


def test_loopback_url_is_fine_when_the_service_has_a_public_name():
    url = "http://localhost:3000/dev"
    assert validate_target_url(url, OWN_HOST) == url


def test_canonical_host_folds_case_trailing_dot_and_loopback_only():
    assert canonical_host("Example.COM.") == "example.com"
    assert canonical_host("127.0.0.1") == canonical_host("::1") == "localhost"
    assert canonical_host("128.0.0.1") == "128.0.0.1"  # just outside 127.0.0.0/8


@pytest.mark.parametrize("code", ["abc", "my-link_1", "UPPER", "a" * 32, "0000", "-_-"])
def test_accepts_valid_custom_codes_unchanged(code):
    assert validate_custom_code(code) == code


@pytest.mark.parametrize(
    "code",
    ["", "ab", "a" * 33, "has space", "dot.dot", "slash/x", "café", "new\nline", "abc\n"],
)
def test_rejects_malformed_custom_codes(code):
    with pytest.raises(ValidationError, match="3-32 characters"):
        validate_custom_code(code)


@pytest.mark.parametrize("code", ["shorten", "stats"])
def test_rejects_custom_codes_that_would_shadow_service_routes(code):
    with pytest.raises(ValidationError, match="reserved"):
        validate_custom_code(code)


@pytest.mark.parametrize("code", [None, 123, ["abc"]])
def test_rejects_non_string_custom_codes(code):
    with pytest.raises(ValidationError, match="must be a string"):
        validate_custom_code(code)
