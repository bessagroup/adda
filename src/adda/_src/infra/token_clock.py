"""The output-token clock: the run budget counted in generated tokens.

With ``budget_clock: output_tokens`` the run's budget ``B`` is
``token_budget`` generated (output) tokens, summed over every node of the run
(entry node, specialists, critic, validators). Input tokens are not counted:
the multi-agent design re-reads context on purpose and is not charged for it.
The wall clock is still recorded; it just is not the limit.

One process-wide counter. Every backend reports the final output count of each
model call (``record``) as the call ends. The count is never estimated: a
backend that reports no usage raises :class:`TokenUsageMissing`. ``record`` is
idempotent per call key, so a count reported twice for one call counts once.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

WALL, OUTPUT_TOKENS = "wall", "output_tokens"
CLOCKS = (WALL, OUTPUT_TOKENS)


class TokenUsageMissing(RuntimeError):
    """A model call ended without an output-token count."""


_lock = threading.Lock()
_budget: int | None = None
_used = 0
_seen: dict[str, int] = {}
_listeners: list[Callable[[], None]] = []
_stop: dict | None = None


def parse(cfg: dict) -> int | None:
    """The token budget for a study config, or ``None`` on the wall clock.

    Raises ``ValueError`` for an unknown clock, for ``output_tokens`` without
    a positive integer ``token_budget``, for ``token_budget`` on the wall
    clock, and for a wall ``budget`` next to the token clock (two limits, one
    of them silently ignored).
    """
    clock = cfg.get("budget_clock", WALL)
    if clock not in CLOCKS:
        raise ValueError(
            f"budget_clock must be one of {', '.join(CLOCKS)}; got {clock!r}")
    tokens = cfg.get("token_budget")
    if clock == WALL:
        if tokens is not None:
            raise ValueError(
                "token_budget is set but budget_clock is 'wall': set "
                "budget_clock: output_tokens or remove token_budget")
        return None
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise ValueError(
            "budget_clock: output_tokens needs token_budget: a positive "
            f"integer number of output tokens; got {tokens!r}")
    if cfg.get("budget") is not None:
        raise ValueError(
            "budget (wall clock) and budget_clock: output_tokens are two "
            "limits: remove budget. The wall-clock safety limit for the "
            "external watchdog is its own explicit key, watchdog_wall_s")
    return tokens


def configure(budget: int | None, seed: int = 0) -> None:
    """Start the counter for one run. ``seed`` is the count already spent when
    a run resumes. ``None`` switches the token clock off."""
    global _budget, _used, _stop
    with _lock:
        _budget = budget
        _used = int(seed) if budget else 0
        _seen.clear()
        _listeners.clear()
        _stop = None


def enabled() -> bool:
    return _budget is not None


def budget() -> int | None:
    return _budget


def used() -> int:
    return _used


def exhausted() -> bool:
    """True once the count has reached the budget (never on the wall clock)."""
    return _budget is not None and _used >= _budget


def note_stop(**extra: object) -> dict:
    """Record that a backend stopped its session at the budget (the plain
    Claude Code arm: it has no wind-down, so the operator ends it). Keeps the
    first record's count at the stop and tokens over the budget; later calls
    add their extra fields."""
    global _stop
    with _lock:
        if _stop is None:
            _stop = {"output_tokens_used": _used,
                     "tokens_over": max(0, _used - (_budget or 0))}
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
            f"budget_clock: output_tokens needs the backend to report output "
            f"tokens, and {who} reported none. The run stops here rather than "
            "estimate. Use a backend that reports usage, or budget_clock: wall.")
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
