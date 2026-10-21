import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from shorturl.store import CodeTakenError, LinkStore

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def decode(code: str) -> int:
    value = 0
    for char in code:
        value = value * 62 + DIGITS.index(char)
    return value


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock(1_700_000_000.0)


@pytest.fixture
def store(clock):
    link_store = LinkStore(":memory:", clock)
    yield link_store
    link_store.close()


def test_generated_codes_follow_row_order(store):
    first = store.add("http://example.com/one")
    second = store.add("http://example.com/two")
    # Rows 1 and 2 map to 62 ** 3 + 1 and 62 ** 3 + 2, i.e. "1001" and "1002".
    assert (first.code, second.code) == ("1001", "1002")


def test_same_url_added_twice_gets_two_distinct_codes(store):
    a = store.add("http://example.com/same")
    b = store.add("http://example.com/same")
    assert a.code != b.code
    assert store.resolve(a.code) == store.resolve(b.code) == "http://example.com/same"


def test_added_link_starts_with_zero_clicks_and_clock_time(store, clock):
    link = store.add("http://example.com/")
    assert link.clicks == 0
    assert link.created_at == 1_700_000_000.0
    clock.now = 1_700_000_500.0
    later = store.add("http://example.com/later")
    assert store.stats(link.code).created_at == 1_700_000_000.0
    assert store.stats(later.code).created_at == 1_700_000_500.0


def test_resolve_counts_each_click_and_stats_does_not(store):
    link = store.add("http://example.com/target")
    for _ in range(3):
        assert store.resolve(link.code) == "http://example.com/target"
    assert store.stats(link.code).clicks == 3
    assert store.stats(link.code).clicks == 3  # reading stats is not a click


def test_unknown_code_yields_none_everywhere(store):
    assert store.resolve("missing") is None
    assert store.stats("missing") is None


def test_clicks_are_tracked_per_link(store):
    a = store.add("http://example.com/a")
    b = store.add("http://example.com/b")
    store.resolve(a.code)
    store.resolve(a.code)
    store.resolve(b.code)
    assert (store.stats(a.code).clicks, store.stats(b.code).clicks) == (2, 1)


def test_custom_code_is_stored_verbatim(store):
    link = store.add("http://example.com/x", "promo-2026")
    assert link.code == "promo-2026"
    assert store.resolve("promo-2026") == "http://example.com/x"


def test_taken_custom_code_raises_and_keeps_the_original_link(store):
    store.add("http://example.com/original", "taken")
    with pytest.raises(CodeTakenError):
        store.add("http://example.com/intruder", "taken")
    assert store.stats("taken").url == "http://example.com/original"


def test_failed_custom_insert_does_not_disturb_generated_codes(store):
    store.add("http://example.com/original", "taken")  # row 1
    with pytest.raises(CodeTakenError):
        store.add("http://example.com/intruder", "taken")
    assert store.add("http://example.com/next").code == "1002"  # row 2


def test_custom_codes_are_case_sensitive(store):
    store.add("http://example.com/lower", "abc")
    store.add("http://example.com/upper", "ABC")
    assert store.resolve("abc") == "http://example.com/lower"
    assert store.resolve("ABC") == "http://example.com/upper"


def test_custom_code_that_equals_a_future_generated_code_is_skipped_not_clashed(store):
    # Rows 1 and 2 take the custom codes "1003" and "1004"; those are exactly
    # the codes rows 3 and 4 would generate, so ids 3 and 4 must be skipped
    # and the first generated link lands on row 5 -> "1005".
    store.add("http://example.com/custom-a", "1003")
    store.add("http://example.com/custom-b", "1004")
    generated = store.add("http://example.com/generated")
    assert generated.code == "1005"
    assert store.resolve("1003") == "http://example.com/custom-a"
    assert store.resolve("1004") == "http://example.com/custom-b"
    assert store.resolve("1005") == "http://example.com/generated"


def test_links_and_id_sequence_survive_reopening_the_database(tmp_path):
    path = str(tmp_path / "links.db")
    first = LinkStore(path)
    first.add("http://example.com/one")
    first.add("http://example.com/two")
    first.resolve("1001")
    first.close()

    reopened = LinkStore(path)
    try:
        assert reopened.stats("1001").clicks == 1
        assert reopened.stats("1002").url == "http://example.com/two"
        assert reopened.add("http://example.com/three").code == "1003"
    finally:
        reopened.close()


def test_concurrent_adds_get_exactly_the_next_unique_codes():
    link_store = LinkStore()
    codes: list[str] = []

    def add_many() -> None:
        for index in range(25):
            codes.append(link_store.add(f"http://example.com/{index}").code)

    threads = [threading.Thread(target=add_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    link_store.close()

    assert len(codes) == 200
    assert sorted(decode(code) for code in codes) == list(range(62**3 + 1, 62**3 + 201))
