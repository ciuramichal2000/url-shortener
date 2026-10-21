"""Input rules for target URLs and custom codes.

The redirect target is copied into a ``Location`` header, so anything that
could break out of that header (whitespace, control characters, non-ASCII) is
refused up front instead of being escaped later.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import SplitResult, urlsplit

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
# Path segments the service itself answers on; a link must never shadow them.
RESERVED_CODES = frozenset({"shorten", "stats"})

_CUSTOM_CODE = re.compile(r"[A-Za-z0-9_-]{3,32}")


class ValidationError(ValueError):
    """The caller supplied a value the service refuses to store."""


def canonical_host(hostname: str) -> str:
    """Normalise a host so that equivalent spellings compare equal.

    Lower-cases, drops a trailing dot and folds every loopback or wildcard
    spelling (``localhost``, ``127.0.0.1``, ``127.0.0.2``, ``::1``, ``0.0.0.0``)
    into ``localhost``, because a link to any of them from a local service is
    still a loop. Decimal/hex IP spellings and DNS aliases are not resolved.
    """
    host = hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback or address.is_unspecified:
            return "localhost"
    except ValueError:
        pass  # not an IP literal, so it is an ordinary host name
    return host


def _parse(url: str) -> SplitResult:
    """Run ``urlsplit`` plus the port check, which urllib only does lazily."""
    try:
        parts = urlsplit(url)
        parts.port  # property access raises ValueError for a bad port
    except ValueError as error:
        raise ValidationError("url is malformed (bad host or port)") from error
    return parts


def validate_target_url(url: object, own_host: str) -> str:
    """Return ``url`` unchanged if it is safe to redirect to, else raise.

    ``own_host`` is the host the service is reachable under; links back to it
    are refused so a short link can never redirect into the shortener itself.
    Only the host is compared, never the port: a different local service on
    another port is refused too, which is the safe side of the trade-off.
    """
    if url is None or url == "":
        raise ValidationError("url is required")
    if not isinstance(url, str):
        raise ValidationError("url must be a string")
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"url is longer than {MAX_URL_LENGTH} characters")

    parts = _parse(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ValidationError("only http and https URLs are allowed")
    if not url.isascii():
        raise ValidationError("url must be ASCII; percent-encode or use punycode")
    # Backslashes are refused because browsers read "http://a\@b" as host "a",
    # while urllib reads it as host "b"; the disagreement could hide a loop.
    if any(char <= " " or char in "\x7f\\" for char in url):
        raise ValidationError(
            "url must not contain whitespace, control characters or backslashes"
        )
    if not parts.hostname:
        raise ValidationError("url must include a host")
    if canonical_host(parts.hostname) == canonical_host(own_host):
        raise ValidationError("url points back at this service")
    return url


def validate_custom_code(code: object) -> str:
    """Return ``code`` unchanged if it is an acceptable custom short code."""
    if not isinstance(code, str):
        raise ValidationError("custom_code must be a string")
    if _CUSTOM_CODE.fullmatch(code) is None:
        raise ValidationError(
            "custom_code must be 3-32 characters: letters, digits, '-' or '_'"
        )
    if code in RESERVED_CODES:
        raise ValidationError("custom_code is reserved")
    return code
