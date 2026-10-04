"""A request, on disk, that a run wind down and close instead of being killed.

SIGTERM unwinds nothing in a run, so a kill loses every agent's real
retrospective. A stop request is the graceful alternative: whoever wants the
run to end (the watchdog ahead of its deadline, the viewer's Stop button)
writes ``debug/stop_request.json``; the entry node notices it at its next
checkpoint, asks workers to report what they have, and closes through its
normal retrospective round.

Like the operator channel this is small JSON in the run's own ``debug/``
directory — the requester is a different process — and nothing here raises on
a missing directory or a malformed file.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..runtime import terminal

__all__ = [
    "DEFAULT_GRACE_S",
    "write_stop_request",
    "read_stop_request",
    "consume_stop_request",
]

_NAME = "stop_request.json"
_CONSUMED = "stop_request.consumed.json"

# How long workers get to report before they are cancelled, when the
# requester names none.
DEFAULT_GRACE_S = 120.0


def _path(run_dir: Path | str, name: str = _NAME) -> Path:
    return Path(run_dir) / "debug" / name


def write_stop_request(
    run_dir: Path | str,
    *,
    by: str,
    reason: str = "",
    grace_s: float = DEFAULT_GRACE_S,
    termination: str | None = None,
) -> bool:
    """Ask the run in ``run_dir`` to stop. False when it could not be written.

    ``termination`` names the terminal value the run closes with once it has
    wound down; left out, the run closes STOPPED. A backstop that asks for its
    own wind-down passes its halt's value, so analysis can still tell them apart.
    """
    path = _path(run_dir)
    payload = {
        "requested_at": time.time(),
        "by": by,
        "reason": reason,
        "grace_s": float(grace_s),
        "termination": termination,
    }
    tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False
    return True


def read_stop_request(
    run_dir: Path | str, *, since: float | None = None,
) -> dict[str, Any] | None:
    """The pending request, or None.

    A request stamped before ``since`` (this run's start) is a leftover from
    an earlier run of the same directory — a resumed run must not stop the
    moment it starts — and is ignored.
    """
    try:
        data = json.loads(_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        at = float(data.get("requested_at"))
        grace = float(data.get("grace_s", DEFAULT_GRACE_S))
    except (TypeError, ValueError):
        return None
    if since is not None and at < since:
        return None
    termination = data.get("termination")
    return {
        "requested_at": at,
        "by": str(data.get("by") or "unknown"),
        "reason": str(data.get("reason") or ""),
        "grace_s": max(grace, 0.0),
        "termination": (
            termination if termination in terminal.TERMINATIONS else None),
    }


def consume_stop_request(run_dir: Path | str) -> None:
    """Mark the request honoured, so a later resume does not trip on it."""
    try:
        _path(run_dir).replace(_path(run_dir, _CONSUMED))
    except OSError:
        pass
