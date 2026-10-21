import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from shorturl.codec import code_for_row_id, encode_base62

# Spelled out here on purpose: digits, then upper case, then lower case.
DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def decode(code: str) -> int:
    """Independent base62 decoder used to check the encoder by round trip."""
    value = 0
    for char in code:
        value = value * 62 + DIGITS.index(char)
    return value


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, "0"),
        (9, "9"),
        (10, "A"),  # first letter after the digits
        (35, "Z"),
        (36, "a"),  # lower case starts after upper case
        (61, "z"),  # largest single digit
        (62, "10"),  # first carry
        (125, "21"),  # 2 * 62 + 1
        (3844, "100"),  # 62 ** 2
        (62**3, "1000"),
    ],
)
def test_encode_base62_known_values(number, expected):
    assert encode_base62(number) == expected


def test_encode_base62_rejects_negative_numbers():
    with pytest.raises(ValueError, match="negative"):
        encode_base62(-1)


def test_generated_codes_start_at_four_characters():
    # Row 1 is offset by 62 ** 3 (= "1000"), so the first code is "1001".
    assert code_for_row_id(1) == "1001"
    assert code_for_row_id(61) == "100z"
    assert code_for_row_id(62) == "1010"


def test_code_length_grows_exactly_at_the_four_to_five_character_boundary():
    # 62 ** 3 + 14_538_007 == 62 ** 4 - 1, the last four-character code.
    assert code_for_row_id(14_538_007) == "zzzz"
    assert code_for_row_id(14_538_008) == "10000"


def test_code_for_row_id_rejects_ids_below_one():
    for bad_id in (0, -5):
        with pytest.raises(ValueError, match="start at 1"):
            code_for_row_id(bad_id)


def test_distinct_row_ids_never_share_a_code_and_decode_back():
    ids = range(1, 5001)
    codes = [code_for_row_id(row_id) for row_id in ids]
    assert len(set(codes)) == len(codes)
    assert [decode(code) for code in codes] == [62**3 + row_id for row_id in ids]
