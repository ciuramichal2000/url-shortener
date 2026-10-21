"""Base62 encoding of row ids into short, URL-safe codes.

Deriving the code from the database row id (instead of drawing random strings)
means two links can never be issued the same generated code, and no
"generate, check, retry" loop is needed.
"""
from __future__ import annotations

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)

# Added to the row id before encoding so generated codes start at four
# characters ("1001") instead of one ("1"), which looks less like a counter.
CODE_OFFSET = BASE**3


def encode_base62(number: int) -> str:
    """Return ``number`` written in base62 using ``ALPHABET`` (no padding)."""
    if number < 0:
        raise ValueError("cannot encode a negative number")
    if number == 0:
        return ALPHABET[0]
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, BASE)
        digits.append(ALPHABET[remainder])
    return "".join(reversed(digits))


def code_for_row_id(row_id: int) -> str:
    """Return the generated short code for the link stored under ``row_id``."""
    if row_id < 1:
        raise ValueError("row ids start at 1")
    return encode_base62(CODE_OFFSET + row_id)
