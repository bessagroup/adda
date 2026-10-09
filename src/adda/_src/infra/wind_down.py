"""The wind-down gate: what a node may do after the budget is spent.

One process-wide state, set by the entry node when the clock reaches
``wind_down_at`` (``nodes/wind_down.py``) and read at the one place every
tool call passes: ``_wrap_closure`` for adda tools, the PreToolUse hook on the
Claude backend and ``_guard_native`` on the OpenAI-compatible backend for the
native ones. The gate refuses with an ERROR that names the rule; it never
kills anything. The state is off until ``begin``, so a run with no budget, and
a plain Claude Code arm that never installs the hook, are not touched.

Two refusals:

* a tool that is not on ``ALLOWED`` (it would start new work);
* any tool but the node's end-of-work call once the node has made
  ``limit`` allowed calls since the wind-down began.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Tools that read, wait, or write up what exists. Everything else is closed.
ALLOWED: frozenset[str] = frozenset({
    "Wait", "RecallHistory", "ReadNote", "WriteNote", "ReadProblemStatement",
    "QueryStore", "OracleStatus", "HypothesisList", "HypothesisUpdate",
    "MilestoneList", "MilestoneSet", "WriteDeliverable", "WriteCell",
    "ShowNotebook", "ReportEvals", "Done",
    "Read", "Write", "Edit", "Glob", "Grep", "BashOutput", "KillShell",
})

CLOSED_TEXT = (
    "ERROR: wind-down rule: the budget is spent, so {tool} is closed. "
    "Start nothing new. Allowed now: Wait, Read, Write, Edit, WriteNote, the "
    "store and notebook write-up tools, and {end}. Save what exists, then "
    "{end_action}.")

LIMIT_TEXT = (
    "ERROR: wind-down rule: you have used {used} of your {limit} tool calls "
    "since the wind-down began. Only {end} is allowed now. {end_action}.")

_lock = threading.Lock()
_active = False
_suspended = 0
_limit = 50
_calls: dict[str, int] = {}
REVIEW_KEY = "close-out review"


def publish_eval_stop(debug_dir: Path, epoch: float) -> None:
    """Tell the metered evaluator, which may run in a campaign process with no
    run clock, the epoch after which it refuses new evaluations."""
    path = Path(debug_dir) / "run_config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["eval_stop_epoch"] = epoch
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError):
        pass


def begin(limit: int) -> None:
    global _active, _limit
    with _lock:
        _active, _limit = True, int(limit)
        _calls.clear()


def end() -> None:
    global _active
    with _lock:
        _active = False
        _calls.clear()


def active() -> bool:
    return _active


def suspended() -> bool:
    return _suspended > 0


@contextmanager
def suspend() -> Iterator[None]:
    """The close-out review (one reproduction gate, one critic review) is the
    wind-down's own work: its tools are not closed, so the critic can audit
    with the tools it has. They are still counted, together, against the same
    ``limit`` (key ``REVIEW_KEY``), so the review has a bound too."""
    global _suspended
    with _lock:
        _suspended += 1
    try:
        yield
    finally:
        with _lock:
            _suspended -= 1


def calls() -> dict[str, int]:
    with _lock:
        return dict(_calls)


def _bare(tool: str) -> str:
    return tool.rsplit("__", 1)[-1] if tool.startswith("mcp__") else tool


def check(key: str, tool: str, *, can_call_done: bool) -> str | None:
    """None when the call may run (and is counted), else the ERROR text.

    ``key`` names the node whose calls are counted: its delegation id, or
    ``entry``. ``can_call_done`` says which end-of-work call the text names.
    """
    if not _active:
        return None
    name = _bare(tool)
    end_call = "Done()" if can_call_done else "your final report"
    end_action = ("call Done() with your summary" if can_call_done else
                  "write your final report and end your turn")
    with _lock:
        if _suspended:
            key = REVIEW_KEY
        elif name not in ALLOWED:
            return CLOSED_TEXT.format(
                tool=name, end=end_call, end_action=end_action)
        used = _calls.get(key, 0)
        if used >= _limit and (name != "Done" or _suspended):
            return LIMIT_TEXT.format(
                used=used, limit=_limit, end=end_call,
                end_action=end_action[0].upper() + end_action[1:])
        _calls[key] = used + 1
    return None
