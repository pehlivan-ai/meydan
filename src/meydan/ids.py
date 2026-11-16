"""Ids made by meydan, not by the database: time-ordered UUIDs, version 7 (RFC 9562).

`ORDER BY id` is insertion order, which meydan relies on (a reply's bubbles go out in the order
they were written, all within one millisecond). Postgres has `uuidv7()` only from 18; making the
id here lets meydan run on 16 and 17 too.

Layout (RFC 9562, section 5.7): 48 bits of Unix time in milliseconds, the version (0b0111), 12
bits `rand_a`, the variant (0b10), 62 random bits `rand_b`.

Monotonic within a process (section 6.2, "Method 1: Fixed Bit-Length Dedicated Counter"):
`rand_a` is a 12-bit counter "positioned immediately after the embedded timestamp". With each
new millisecond it is seeded at random, its leftmost bit zero "for the sole purpose of guarding
against counter rollovers"; within the same millisecond it is incremented. When it rolls over,
or the clock steps back, the previous timestamp is reused and counted on ("the application MAY
reuse the previous timestamp and increment the previous counter"; "implementations MAY increment
the timestamp ahead of the actual time and reinitialize the counter"). An id is therefore always
greater than the one before it in the process.

Across processes the RFC guarantees no order, and neither does this: what has to be ordered (one
turn's bubbles, one conversation's writes) is written by one process.
"""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last = 0  # the last (milliseconds << 12 | counter) handed out


def uuid7() -> uuid.UUID:
    global _last
    with _lock:
        now = time.time_ns() // 1_000_000
        if now > _last >> 12:  # a new millisecond: seed the counter, leftmost bit zero
            _last = now << 12 | int.from_bytes(os.urandom(2), "big") & 0x7FF
        else:  # the same millisecond, or the clock stepped back: count on
            _last += 1
        seq = _last
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    return uuid.UUID(int=(seq >> 12) << 80 | 0x7 << 76 | (seq & 0xFFF) << 64 | 0b10 << 62 | rand_b)
