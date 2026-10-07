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


class RunAbandoned(BaseException):
    """The run no longer waits for this thread; end it."""
