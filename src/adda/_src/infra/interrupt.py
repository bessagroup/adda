"""SIGINT for hung work, never a kill.

In the wind-down (``nodes/wind_down.py``) a Bash command or evaluation that
does not finish is sent SIGINT: the tool call returns "interrupted", the
agent's turn continues, and the agent can save what exists. Only processes that
one node owns are signalled, each found by something adda recorded and never by
a name pattern: the shells of an OpenAI-compatible adapter's ``_BashSession``
(recorded pid, start time checked), and the processes below a Bash-tool shell of a
Claude CLI session, carrying its unique ``ADDA_SESSION_TOKEN``. The CLI process
and every MCP server it started are never signalled, so the turn is not ended
and no tool server dies.

A store flusher holds the store's lock while it writes; the signal is sent
while holding that lock, so no process is interrupted inside a store write.
"""
from __future__ import annotations

import os
import signal
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

SESSION_TOKEN_ENV = "ADDA_SESSION_TOKEN"
STORE_LOCK_WAIT_S = 10.0

_lock = threading.Lock()
_in_flight: dict[int, Any] = {}
_store_locks: set[str] = set()


def new_token() -> str:
    return uuid.uuid4().hex


def register_store_lock(path: str | os.PathLike) -> None:
    """A store's flush lock; ``interrupt_all`` holds it while it signals."""
    with _lock:
        _store_locks.add(str(path))


@contextmanager
def in_flight(adapter: Any) -> Iterator[None]:
    """``adapter`` is inside a call whose processes ``interrupt_all`` may signal."""
    key = id(adapter)
    with _lock:
        _in_flight[key] = adapter
    try:
        yield
    finally:
        with _lock:
            _in_flight.pop(key, None)


def interrupt_pids(pids: list[int]) -> list[dict]:
    """SIGINT these pids and their descendants, deepest first. Returns what
    was signalled as ``{"pid", "name"}`` rows."""
    try:
        import psutil
    except ImportError:
        return []
    procs: list[Any] = []
    for pid in pids:
        try:
            root = psutil.Process(pid)
            procs.extend(root.children(recursive=True)[::-1])
            procs.append(root)
        except Exception:  # noqa: BLE001
            continue
    return _signal(procs)


_SHELLS = {"bash", "zsh", "sh", "dash", "fish"}


def _argv(cfg: Any) -> list[str]:
    """The command line a stdio MCP server config launches."""
    if not isinstance(cfg, dict) or not cfg.get("command"):
        return []
    return [str(cfg["command"]), *[str(x) for x in cfg.get("args") or []]]


def _carries(p: Any, token: str) -> bool:
    """True when ``p`` carries ``token``. A process whose environment cannot be
    read (macOS returns it empty for system binaries such as ``/bin/bash``) counts as
    carrying it: the callers only ask about processes that already sit below
    the token-verified CLI, so a readable environment can only exclude."""
    try:
        env = p.environ()
        return not env or env.get(SESSION_TOKEN_ENV) == token
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__ == "AccessDenied"


def interrupt_session(token: str | None,
                      mcp_servers: dict | None = None) -> list[dict]:
    """SIGINT what a Bash-tool shell of the CLI session that carries ``token``
    runs.

    Signalled: a shell that is a direct child of the CLI, and everything below
    it, each carrying ``token``. Never signalled: the CLI itself, and any
    process of an MCP server, i.e. a direct child of the CLI whose command line
    is exactly one of the configured ``mcp_servers`` launches, with its whole
    subtree. Both rules are checked, so a server that shares the token is
    still left alone.
    """
    if not token:
        return []
    try:
        import psutil
        me = psutil.Process(os.getpid())
        cli = [p for p in me.children()
               if p.environ().get(SESSION_TOKEN_ENV) == token]
    except Exception:  # noqa: BLE001
        return []
    servers = [a for a in (_argv(c) for c in (mcp_servers or {}).values()) if a]
    mine: list[Any] = []
    for c in cli:
        try:
            kids = c.children()
        except Exception:  # noqa: BLE001
            continue
        for k in kids:
            try:
                if k.cmdline() in servers or k.name() not in _SHELLS:
                    continue
                if not _carries(k, token):
                    continue
                below = k.children(recursive=True)
            except Exception:  # noqa: BLE001
                continue
            mine.extend(p for p in below[::-1] if _carries(p, token))
            mine.append(k)
    return _signal(mine)


def _signal(procs: list[Any]) -> list[dict]:
    hit: list[dict] = []
    for p in procs:
        try:
            name = p.name()
            p.send_signal(signal.SIGINT)
            hit.append({"pid": p.pid, "name": name})
        except Exception:  # noqa: BLE001
            continue
    return hit


def interrupt_all() -> list[dict]:
    """Interrupt the own processes of every adapter in a call. Best effort:
    one failure never stops the rest. Returns what was signalled."""
    with _lock:
        adapters = list(_in_flight.values())
    hit: list[dict] = []
    with _holding_store_locks():
        for a in adapters:
            try:
                hit.extend(a.interrupt())
            except Exception:  # noqa: BLE001
                continue
    return hit


@contextmanager
def _holding_store_locks() -> Iterator[None]:
    """Take every registered store lock (bounded wait). A lock not won in time
    is skipped rather than left to block the wind-down."""
    with _lock:
        paths = sorted(_store_locks)
    held: list[Any] = []
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        paths = []
    for path in paths:
        fl = FileLock(path)
        try:
            fl.acquire(timeout=STORE_LOCK_WAIT_S)
            held.append(fl)
        except (Timeout, OSError):
            continue
    try:
        yield
    finally:
        for fl in held:
            try:
                fl.release()
            except Exception:  # noqa: BLE001
                pass
