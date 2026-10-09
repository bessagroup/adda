"""The wall-clock rules: the declared budget ``B`` is the real limit.

Thresholds, each a fraction of ``B`` measured from the run start:

* ``budget_warn_from`` (0.75), then every ``budget_warn_every`` (0.05): a
  one-line notice with the minutes left, to the entry node and to running
  delegations.
* ``delegation_cutoff_at`` (0.90): no new delegations; running ones continue.
* ``wind_down_at`` (1.0): the wind-down begins (``nodes/wind_down.py``).
  Nothing is killed. Code refuses new delegations and new metered evaluations,
  each node saves what it has, the entry node writes the deliverable, and the
  run closes after one reproduction gate and one critic review.

Three more knobs bound the wind-down by counts, never by time:
``wind_down_tool_calls`` (50 tool calls per node), ``wind_down_turns`` (forced
turns per step, 2), ``wind_down_interrupt_after_s`` (300 s before work that has
not finished gets SIGINT).

With no ``budget`` configured every threshold is off. The knobs live in the
study's ``runtime:`` block (``settings.py``); this module holds their defaults,
their validation, the text of each notice, and the clock the tests replace.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import settings

DEFAULTS: dict[str, float] = {
    "budget_warn_from": 0.75,
    "budget_warn_every": 0.05,
    "delegation_cutoff_at": 0.90,
    "wind_down_at": 1.0,
}

WIND_DOWN_DEFAULTS: dict[str, float] = {
    "wind_down_tool_calls": 50,
    "wind_down_turns": 2,
    "wind_down_interrupt_after_s": 300.0,
}

REMOVED: dict[str, str] = {
    "run_backstop_multiple": "wind_down_at",
    "delegate_cutoff_multiple": "delegation_cutoff_at",
    "wrapup_at": "wind_down_at",
    "graceful_stop_at": "wind_down_at",
    "hard_stop_at": "wind_down_at",
    "stop_retrospective_s": "wind_down_turns",
}

NOTICE, CUTOFF, WIND_DOWN = "notice", "cutoff", "wind_down"


def now() -> float:
    return time.time()


def _number(key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"runtime: {key} must be a number, got {value!r}")
    return float(value)


def validate(cfg: dict, explicit: dict) -> None:
    """Reject a removed knob, a non-number, or thresholds out of order.

    Raises ``ValueError`` at start so a study never runs under a schedule it
    did not ask for.
    """
    merged = {**cfg, **explicit}
    removed = sorted(set(merged) & set(REMOVED))
    if removed:
        raise ValueError("; ".join(
            f"runtime: {k} was removed. The wall-clock budget is now the "
            f"limit and the run winds down at it: set {REMOVED[k]} "
            f"(see the runtime reference)"
            for k in removed))
    vals = {k: _number(k, merged.get(k, d)) for k, d in DEFAULTS.items()}
    if not (0 < vals["budget_warn_from"] < vals["delegation_cutoff_at"]
            < vals["wind_down_at"]):
        raise ValueError(
            "runtime: the time thresholds must satisfy 0 < budget_warn_from < "
            "delegation_cutoff_at < wind_down_at; got "
            + ", ".join(f"{k}={vals[k]:g}" for k in DEFAULTS if k != "budget_warn_every"))
    if vals["budget_warn_every"] <= 0:
        raise ValueError("runtime: budget_warn_every must be > 0")
    for k, d in WIND_DOWN_DEFAULTS.items():
        if _number(k, merged.get(k, d)) <= 0:
            raise ValueError(f"runtime: {k} must be > 0")


@dataclass(frozen=True)
class TimeRules:
    budget: float
    warn_from: float
    warn_every: float
    cutoff_at: float
    wind_down_at: float

    @classmethod
    def from_settings(cls, budget: float | None) -> TimeRules | None:
        if not budget or budget <= 0:
            return None
        return cls(
            budget=float(budget),
            warn_from=settings.get_float("budget_warn_from", 0.75),
            warn_every=settings.get_float("budget_warn_every", 0.05),
            cutoff_at=settings.get_float("delegation_cutoff_at", 0.90),
            wind_down_at=settings.get_float("wind_down_at", 1.0),
        )

    def schedule(self) -> list[tuple[str, float]]:
        """Every threshold as ``(name, fraction)``, in time order. A notice
        that falls on the cutoff is the cutoff's own line."""
        out: list[tuple[str, float]] = []
        k = 0
        while True:
            f = round(self.warn_from + k * self.warn_every, 6)
            if f >= self.wind_down_at - 1e-9:
                break
            if abs(f - self.cutoff_at) > 1e-9:
                out.append((f"{NOTICE}:{f:g}", f))
            k += 1
        out.append((CUTOFF, self.cutoff_at))
        out.append((WIND_DOWN, self.wind_down_at))
        return sorted(out, key=lambda x: x[1])

    def at(self, phase: str) -> float:
        """Seconds after the run start at which ``phase`` begins."""
        for name, frac in self.schedule():
            if name == phase:
                return self.budget * frac
        raise KeyError(phase)

    def due(self, elapsed: float) -> list[str]:
        return [n for n, f in self.schedule() if elapsed >= self.budget * f]

    # -- text, always in minutes --------------------------------------

    def budget_min(self) -> str:
        return _mins(self.at(WIND_DOWN))

    def _clock(self, elapsed: float) -> str:
        end = self.at(WIND_DOWN)
        if elapsed < end:
            return (f"{_left(end - elapsed)} left before the wind-down at "
                    f"{self.budget_min()}.")
        return (f"the wind-down began at {self.budget_min()}; "
                f"{_left(elapsed - end, over=True)} ago.")

    def cutoff_refusal(self, elapsed: float) -> str:
        return (
            "No new delegations: " + self._clock(elapsed)
            + " This delegation was NOT started. Wait() for the running "
            "ones, then write your deliverables and call Done().")

    def notice(self, phase: str, elapsed: float, *, can_call_done: bool) -> str:
        end = ("call Done()" if can_call_done
               else "report what you have and return")
        clock = self._clock(elapsed)
        if phase == CUTOFF:
            return (
                "No new delegations: " + clock + " Running ones continue. "
                + ("Wait() for them, then write your deliverables."
                   if can_call_done else
                   "Finish the step you are on and report."))
        pct = _pct(elapsed / self.budget)
        return f"Time: {pct}% of the budget used; {clock} Plan so you can {end}."


def _pct(frac: float) -> str:
    return f"{100 * frac:.0f}"


def _mins(seconds: float) -> str:
    return f"{round(seconds / 60, 1):g} min"


def _left(seconds: float, over: bool = False) -> str:
    seconds = max(seconds, 0.0)
    if seconds < 60:
        return "under 1 min"
    return f"{int(seconds // 60)} min"
