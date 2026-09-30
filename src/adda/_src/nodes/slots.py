"""Run-wide cap on how many worker sessions are awake at once.

Each awake session is a live CLI process holding memory, so the cap is a host
safety bound (``runtime.max_awake_nodes``, counting the strategizer, which
always holds one slot of its own and is never queued). Workers share the
remaining ``max_awake_nodes - 1`` slots.

Slots are keyed by delegation id, not by thread: the tool calls a worker makes
run on other threads than the one that started its session.

Grants are FIFO, except that a session giving a slot back only to avoid a
deadlock (see :meth:`yielded`) re-enters ahead of new spawns.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["AwakeSlots"]


class AwakeSlots:
    def __init__(self, max_awake_nodes: int) -> None:
        self.max_awake_nodes = max(2, int(max_awake_nodes))
        self.capacity = self.max_awake_nodes - 1
        self._cond = threading.Condition()
        self._held: set[str] = set()
        self._priority: deque[str] = deque()
        self._fifo: deque[str] = deque()

    def holds(self, did: str) -> bool:
        with self._cond:
            return did in self._held

    def reserve(self, did: str, *, priority: bool = False) -> str | None:
        """Take a slot now, or join the queue.

        Returns None when the slot was granted, else the reason the caller is
        QUEUED. Follow a queued reserve with :meth:`wait_granted`.
        """
        with self._cond:
            if did in self._held:
                return None
            if len(self._held) < self.capacity and not self._priority \
                    and not self._fifo:
                self._held.add(did)
                return None
            (self._priority if priority else self._fifo).append(did)
            return (f"too many nodes working "
                    f"({len(self._held) + 1}/{self.max_awake_nodes}: "
                    f"{len(self._held)} workers + the strategizer)")

    def wait_granted(self, did: str) -> None:
        with self._cond:
            self._cond.wait_for(lambda: did in self._held)

    def release(self, did: str) -> None:
        with self._cond:
            if did not in self._held:
                # Never granted (e.g. cancelled while queued): leave the queue.
                for q in (self._priority, self._fifo):
                    if did in q:
                        q.remove(did)
                return
            self._held.discard(did)
            while len(self._held) < self.capacity and (
                    self._priority or self._fifo):
                nxt = (self._priority or self._fifo).popleft()
                self._held.add(nxt)
            self._cond.notify_all()

    @contextmanager
    def yielded(self, did: str) -> Iterator[None]:
        """Give ``did``'s slot back for the duration, then re-take it ahead of
        new spawns. A no-op when ``did`` holds none."""
        if not self.holds(did):
            yield
            return
        self.release(did)
        try:
            yield
        finally:
            self.reserve(did, priority=True)
            self.wait_granted(did)
