"""The budget rules: every budget a config sets is enforced at once.

A config may set ``budget`` (wall clock), ``token_budget`` (output tokens
summed over every node) and ``eval_budget`` (evaluations in the canonical
store). The schedule keys on the wall and token budgets only: the one furthest
along drives it (``Progress``). The evaluation budget only nudges (Elvis,
2026-10-09): it sends a notice from ``budget_warn_from``, then every
``budget_warn_every``, with the evaluations left. It does not count toward the
delegation cutoff, does not start the wind-down and never refuses an
evaluation.

Thresholds, each a fraction of a budget:

* ``budget_warn_from`` (0.75), then every ``budget_warn_every`` (0.05): a
  one-line notice with the wall and token shares used and what is left, to the
  entry node and to running delegations. The evaluation budget has its own
  notice on the same steps (``eval_notice``).
* ``delegation_cutoff_at`` (0.90): no new delegations; running ones continue.
* ``wind_down_at`` (1.0): the wind-down begins (``nodes/wind_down.py``).
  Nothing is killed. Code refuses new delegations and new metered evaluations,
  each node saves what it has, the entry node writes the deliverable, and the
  run closes after one reproduction gate and one critic review. Evaluations
  already running finish and are stored.

Three more knobs bound the wind-down by counts, never by time:
``wind_down_tool_calls`` (50 tool calls per node), ``wind_down_turns`` (forced
turns per step, 2), ``wind_down_interrupt_after_s`` (300 s before work that has
not finished gets SIGINT).

With none of the three configured every threshold is off. The knobs live in
the study's ``runtime:`` block (``settings.py``); this module holds their
defaults, their validation, the text of each notice, and the clock the tests
replace.
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
WALL, TOKENS = "wall", "tokens"


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
class Progress:
    """How far each budget that is set has run, as a fraction of that budget.

    The schedule keys on the wall and token budgets: ``fraction`` is the larger
    of the two (0 when neither is set) and ``driver`` names it (``"wall"`` or
    ``"tokens"``). ``evals`` only feeds the evaluation notices.
    """
    wall: float | None = None
    tokens: float | None = None
    evals: float | None = None

    def by_kind(self) -> dict[str, float]:
        """The wall and token fractions that are set."""
        return {k: f for k, f in ((WALL, self.wall), (TOKENS, self.tokens))
                if f is not None}

    @property
    def fraction(self) -> float:
        return max(self.by_kind().values(), default=0.0)

    @property
    def driver(self) -> str | None:
        """The budget furthest along (wall wins a tie, then tokens), or
        ``None`` when neither is set."""
        kinds = self.by_kind()
        return max(kinds, key=lambda k: kinds[k]) if kinds else None


@dataclass(frozen=True)
class TimeRules:
    wall: float | None
    tokens: int | None
    evals: int | None
    warn_from: float
    warn_every: float
    cutoff_at: float
    wind_down_at: float

    @classmethod
    def from_settings(cls, wall: float | None, tokens: int | None = None,
                      evals: int | None = None) -> TimeRules | None:
        """The rules for the budgets that are set; ``None`` when none is."""
        wall = float(wall) if wall and wall > 0 else None
        tokens = int(tokens) if tokens and tokens > 0 else None
        evals = int(evals) if evals and evals > 0 else None
        if wall is None and tokens is None and evals is None:
            return None
        return cls(
            wall=wall, tokens=tokens, evals=evals,
            warn_from=settings.get_float("budget_warn_from", 0.75),
            warn_every=settings.get_float("budget_warn_every", 0.05),
            cutoff_at=settings.get_float("delegation_cutoff_at", 0.90),
            wind_down_at=settings.get_float("wind_down_at", 1.0),
        )

    def progress(self, elapsed_s: float, tokens_used: float,
                 evals_used: float = 0) -> Progress:
        return Progress(
            wall=None if self.wall is None else elapsed_s / self.wall,
            tokens=None if self.tokens is None else tokens_used / self.tokens,
            evals=None if self.evals is None else evals_used / self.evals)

    @property
    def keyed(self) -> bool:
        """True when a wall or token budget is set, so the cutoff and the
        wind-down exist."""
        return self.wall is not None or self.tokens is not None

    def schedule(self) -> list[tuple[str, float]]:
        """Every threshold as ``(name, fraction)``, in time order. A notice
        that falls on the cutoff is the cutoff's own line."""
        out: list[tuple[str, float]] = []
        if not self.keyed:
            return out
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

    def fraction_of(self, phase: str) -> float:
        for name, frac in self.schedule():
            if name == phase:
                return frac
        raise KeyError(phase)

    def wall_at(self, phase: str) -> float:
        """Seconds after the run start at which ``phase`` begins on the wall
        budget."""
        return self.wall * self.fraction_of(phase)

    def due(self, fraction: float) -> list[str]:
        return [n for n, f in self.schedule() if fraction >= f]

    def eval_step(self, fraction: float) -> int | None:
        """The notice step the evaluation budget has reached, or ``None``
        below ``warn_from``. Step ``k`` is at ``warn_from + k * warn_every``;
        the steps go on past 100%."""
        if fraction < self.warn_from - 1e-9:
            return None
        return int((fraction - self.warn_from) / self.warn_every + 1e-9)

    def eval_notice(self, used: float) -> str:
        left = self.evals - used
        text = (f"Evaluations: {_pct(used / self.evals)}% of the budget used "
                + (f"({int(left):,} left)." if left >= 0
                   else f"({int(-left):,} over)."))
        return f"Budget: {text}"

    # -- text: every budget that is set, each in its own unit ---------------

    def _parts(self) -> list[tuple[str, str, float]]:
        """``(name, kind, budget)`` for each budget set."""
        out: list[tuple[str, str, float]] = []
        if self.wall is not None:
            out.append(("Time", WALL, self.wall))
        if self.tokens is not None:
            out.append(("Tokens", TOKENS, float(self.tokens)))
        return out

    @staticmethod
    def _amount(kind: str, value: float) -> str:
        if kind == TOKENS:
            return f"{int(value):,} output tokens"
        return _mins(value)

    def _left_text(self, kind: str, value: float) -> str:
        return _left(value) if kind == WALL else self._amount(
            kind, max(value, 0.0))

    def budget_phrase(self) -> str:
        return " and ".join(
            f"{_NAME[k]} budget of {self._amount(k, b * self.wind_down_at)}"
            for _n, k, b in self._parts())

    def _fractions(self, p: Progress):
        kinds = p.by_kind()
        for n, k, b in self._parts():
            yield n, k, b, kinds[k]

    def _clock(self, p: Progress) -> str:
        """The budgets' state: used share and what is left, per budget set."""
        parts = []
        over = p.fraction >= self.wind_down_at
        for n, k, b, f in self._fractions(p):
            left = b * self.wind_down_at - f * b
            if left > 0:
                parts.append(f"{n} {_pct(f)}% ({self._left_text(k, left)} left)")
            elif k == WALL:
                parts.append(f"{n} {_pct(f)}% ({_left(-left, over=True)} over)")
            else:
                parts.append(f"{n} {_pct(f)}% ({self._left_text(k, -left)} over)")
        text = "; ".join(parts)
        if over:
            return (f"the wind-down began at {self.budget_phrase()}, set by "
                    f"the {_NAME[p.driver]} budget. {text}.")
        first = ", when the first of them is reached" if len(
            self._parts()) > 1 else ""
        return f"{text}. The wind-down begins at {self.budget_phrase()}{first}."

    def cutoff_refusal(self, p: Progress, *, can_call_done: bool = True) -> str:
        then = ("Wait() for the running ones, then write your deliverables "
                "and call Done()." if can_call_done else
                "Let the running ones finish, then report what you have.")
        return ("No new delegations: " + self._clock(p)
                + " This delegation was NOT started. " + then)

    def notice(self, phase: str, p: Progress, *, can_call_done: bool) -> str:
        end = ("call Done()" if can_call_done
               else "report what you have and return")
        clock = self._clock(p)
        if phase == CUTOFF:
            return (
                "No new delegations: " + clock + " Running ones continue. "
                + ("Wait() for them, then write your deliverables."
                   if can_call_done else
                   "Finish the step you are on and report."))
        return f"Budget: {clock} Plan so you can {end}."


_NAME = {WALL: "time", TOKENS: "token"}


def _pct(frac: float) -> str:
    return f"{100 * frac:.0f}"


def _mins(seconds: float) -> str:
    return f"{round(seconds / 60, 1):g} min"


def _left(seconds: float, over: bool = False) -> str:
    seconds = max(seconds, 0.0)
    if seconds < 60:
        return "under 1 min"
    return f"{int(seconds // 60)} min"
