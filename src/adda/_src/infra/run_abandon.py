"""A run that stopped waiting for its nodes.

When a run is interrupted (Ctrl-C, a timeout, a crash) a node thread can be
stuck in a call that cannot be cancelled, such as an LLM read. The run sets
each node's abandon flag, stops waiting for the graph after a bounded grace,
and records what it left behind. Every thread that later reaches a tool call
or a wait loop raises ``RunAbandoned`` and ends. It derives from
``BaseException`` so a backend's ``except Exception`` cannot turn it into a
tool error the model would read.
"""
from __future__ import annotations

import threading


class RunAbandoned(BaseException):
    """The run no longer waits for this thread; end it."""


_stop = threading.Event()
_blocked = 0
_blocked_lock = threading.Lock()


def request_stop() -> None:
    """Tell every node and backend in this process to stop making calls."""
    _stop.set()


def reset_stop() -> None:
    """Clear the signal and the blocked-call count at the start of a run."""
    global _blocked
    _stop.clear()
    with _blocked_lock:
        _blocked = 0


def stop_requested() -> bool:
    return _stop.is_set()


def blocked_calls() -> int:
    with _blocked_lock:
        return _blocked


def raise_if_stopped(who: str = "") -> None:
    """End the calling thread, with no new call, once the run is abandoned.

    Backends call this before each model call and each tool call; a node
    calls it through ``Node._raise_if_abandoned``. Each refusal is counted.
    """
    global _blocked
    if _stop.is_set():
        with _blocked_lock:
            _blocked += 1
        raise RunAbandoned(who)
