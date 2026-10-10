"""The output-token budget: generated (output) tokens, summed over every node
of the run (entry node, specialists, critic, validators).

It is one budget among those a config may set (``budget`` wall, ``token_budget``,
``eval_budget``); wall and tokens end the run together, evaluations only
send notices (``runtime/time_rules.py``). Input
tokens are not counted: the multi-agent design re-reads context on purpose and
is not charged for it.

One process-wide counter. Every backend reports the final output count of each
model call (``record``) as the call ends. The count is never estimated: a
backend that reports no usage raises :class:`TokenUsageMissing`. ``record`` is
idempotent per call key, so a count reported twice for one call counts once.
The same module holds the wall deadline the plain Claude Code arm is stopped at
(``set_wall``), so that arm checks every budget it has in one place.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenUsageMissing(RuntimeError):
    """A model call ended without an output-token count."""


_lock = threading.Lock()
_budget: int | None = None
_used = 0
_seen: dict[str, int] = {}
_listeners: list[Callable[[], None]] = []
_stop: dict | None = None
_wall: tuple[float, float] | None = None


def parse(cfg: dict) -> int | None:
    """The ``token_budget`` of a study config, or ``None`` when it sets none.

    Raises ``ValueError`` for the removed ``budget_clock`` key and for a
    ``token_budget`` that is not a positive integer. ``budget`` (wall) and
    ``token_budget`` may both be set: each is enforced.
    """
    if "budget_clock" in cfg:
        raise ValueError(
            "budget_clock was removed: the wall and token budgets "
            "are enforced together. Delete budget_clock; set budget (wall clock), "
            "token_budget, or both")
    tokens = cfg.get("token_budget")
    if tokens is None:
        return None
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise ValueError(
            "token_budget must be a positive integer number of output "
            f"tokens; got {tokens!r}")
    return tokens


def configure(budget: int | None, seed: int = 0,
              wall: tuple[float, float] | None = None) -> None:
    """Start the counter for one run. ``seed`` is the count already spent when
    a run resumes. ``None`` switches the token budget off. ``wall`` is
    ``(run start epoch, wall budget seconds)`` for the plain arm's stop."""
    global _budget, _used, _stop, _wall
    with _lock:
        _budget = budget
        _used = int(seed) if budget else 0
        _seen.clear()
        _listeners.clear()
        _stop = None
        _wall = wall


def enabled() -> bool:
    return _budget is not None


def budget() -> int | None:
    return _budget


def used() -> int:
    return _used


def trigger() -> str | None:
    """The budget the plain arm has reached, ``"tokens"`` or ``"wall"``;
    ``None`` while both are within bounds."""
    if _budget is not None and _used >= _budget:
        return "tokens"
    if _wall is not None and time.time() - _wall[0] >= _wall[1]:
        return "wall"
    return None


def exhausted() -> bool:
    return trigger() is not None


def note_stop(**extra: object) -> dict:
    """Record that a backend stopped its session at a budget (the plain
    Claude Code arm: it has no wind-down, so the operator ends it). Keeps the
    first record's count at the stop, tokens over the budget and the budget
    that triggered it; later calls add their extra fields."""
    global _stop
    with _lock:
        if _stop is None:
            _stop = {"output_tokens_used": _used,
                     "tokens_over": max(0, _used - (_budget or 0)),
                     "budget_trigger": trigger()}
        _stop.update(extra)
        return dict(_stop)


def stop_info() -> dict | None:
    return dict(_stop) if _stop is not None else None


def on_change(callback: Callable[[], None]) -> None:
    with _lock:
        _listeners.append(callback)


def remove_listener(callback: Callable[[], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


def record(key: str, output_tokens: int | None,
           who: str = "a model call") -> int:
    """Count the final output tokens of one model call, once per ``key``.

    A later report for the same key replaces the earlier one (the count only
    grows). ``None`` raises: under this clock a call with no count cannot be
    measured and is never guessed. Returns the tokens newly counted.
    """
    if _budget is None:
        return 0
    if output_tokens is None:
        raise TokenUsageMissing(
            f"token_budget needs the backend to report output tokens, and "
            f"{who} reported none. The run stops here rather than estimate. "
            "Use a backend that reports usage, or remove token_budget.")
    global _used
    with _lock:
        before = _seen.get(key, 0)
        if output_tokens <= before:
            return 0
        _seen[key] = int(output_tokens)
        added = int(output_tokens) - before
        _used += added
        listeners = list(_listeners)
    for cb in listeners:
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass
    return added
