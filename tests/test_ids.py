"""Ids are time-ordered UUIDs (version 7) made by meydan: `ORDER BY id` is the order they were
made in, within a millisecond and when the clock steps back."""

import time
import uuid

from meydan import ids
from meydan.ids import uuid7


def test_an_id_is_a_version_7_uuid_carrying_the_time():
    before = time.time_ns() // 1_000_000
    made = uuid7()
    assert (made.version, made.variant) == (7, uuid.RFC_4122)
    assert before <= made.int >> 80 <= time.time_ns() // 1_000_000 + 1


def test_ids_made_one_after_another_always_increase_within_one_millisecond():
    made = [uuid7() for _ in range(5000)]  # far more than one millisecond can tell apart
    assert made == sorted(made) and len(set(made)) == len(made)


def test_a_clock_that_steps_back_does_not_reorder_ids(monkeypatch):
    first = uuid7()
    monkeypatch.setattr(ids.time, "time_ns", lambda: 0)  # 1970
    assert uuid7() > first


def test_a_new_millisecond_seeds_the_counter_at_random_with_its_leftmost_bit_zero(monkeypatch):
    ms = [10**12]
    monkeypatch.setattr(ids, "_last", 0)
    monkeypatch.setattr(ids.time, "time_ns", lambda: ms[0] * 1_000_000)
    first = uuid7()
    counter = first.int >> 64 & 0xFFF
    assert counter < 0x800  # room left to count on before it rolls over
    assert (uuid7().int >> 64 & 0xFFF) == counter + 1  # same millisecond: counted on
    ms[0] += 1
    assert (uuid7().int >> 80) == ms[0]  # a new millisecond: a new timestamp
