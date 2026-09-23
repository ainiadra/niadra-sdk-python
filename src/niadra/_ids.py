"""UUIDv7 (RFC 9562) for idempotency keys minted on the client.

Time-ordered ids keep the server's deduplication table append-mostly. Python only ships
`uuid.uuid7` from 3.14 on, so the SDK carries its own.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> uuid.UUID:
    """A UUIDv7 with a 12-bit counter that keeps ids monotonic within one millisecond."""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            ms = _last_ms
            _counter = (_counter + 1) & 0xFFF
            if _counter == 0:
                ms += 1
        else:
            _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF
        _last_ms = ms
        counter = _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | counter << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def new_key() -> str:
    return str(uuid7())
