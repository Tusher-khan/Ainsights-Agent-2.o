"""Rate-limit storage behind one small interface.

`hit(name, limit, window_seconds)` records one event and returns False when the limit is already reached.
The shipped store keeps counters in this process's memory, which is correct for a single worker only:
with several workers or machines each one counts separately, so the effective limit is multiplied.
To share counters, implement the same `hit`/`reset` methods on a shared store (Redis `INCR`+`EXPIRE`, or a
sliding window in a sorted set) and call `set_store(...)` at start-up. No Redis store is bundled or tested here.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Protocol


class RateStore(Protocol):
    def hit(self, name: str, limit: int, window_seconds: int) -> bool: ...
    def reset(self) -> None: ...


class MemoryRateStore:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, name: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            window = self._hits[name]
            while window and now - window[0] > window_seconds:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_store: RateStore = MemoryRateStore()


def get_store() -> RateStore:
    return _store


def set_store(store: RateStore) -> None:
    global _store
    _store = store
