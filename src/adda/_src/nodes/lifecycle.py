"""Run lifecycle: unrecoverable-condition detectors (USD budget, repeated errors,
time backstop) and the resumable checkpoint-halt. A mixin on the strategizer."""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..runtime import terminal
from ._constants import backstop_enabled, run_backstop_multiple

if TYPE_CHECKING:
    from langgraph.types import Command


class LifecycleMixin:
    def _halt_resumable(
        self,
        state: Any,
        *,
        reason: str,
        termination: str,
        status: str = "halted",
        extra_update: dict | None = None,
    ):
        """Checkpoint-and-halt cleanly on an unrecoverable condition.

        Instead of crashing, write ``debug/run_status.json`` (so tooling and
        the human can see the run is resumable) and return a ``Command`` to
        ``END`` whose ``last_report`` is prefixed with a HALTED banner.  The
        durable SqliteSaver checkpoint + the persisted ``thread_id`` are what
        make the run resumable via ``AgenticRun(resume_from=...)`` — no new
        serialized state is introduced here.

        ``termination`` names WHICH unrecoverable condition fired. It is
        carried on the state so the close path records it verbatim instead of
        inferring an outcome from the HALTED banner — which matched none of the
        old banner patterns and so read as GATED, logging every backstop kill
        as a validated success.
        """
        import json as _json

        from langchain_core.messages import AIMessage
        from langgraph.graph import END
        from langgraph.types import Command

        from ..runtime import features as _features

        run_dir = state.get("run_dir")
        thread_id = None
        if run_dir:
            debug_dir = Path(run_dir) / "debug"
            tid_path = debug_dir / "thread_id"
            try:
                if tid_path.exists():
                    thread_id = tid_path.read_text().strip()
            except OSError:
                thread_id = None
            try:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / "run_status.json").write_text(
                    _json.dumps(
                        {
                            "status": status,
                            "reason": reason,
                            "resumable": True,
                            "thread_id": thread_id,
                            "outcome": terminal.UNGATED,
                            "termination": termination,
                            "reviewed": False,
                            "arms": _features.arm_config(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass

        # Preserve the latest conclusion below the banner.
        prior_text = ""
        for _m in reversed(state["messages"]):
            if isinstance(_m, AIMessage):
                prior_text = str(_m.content)
                break

        banner = f"## ⚠ HALTED (resumable) — {reason}\n\n"
        update = {
            "messages": [],
            "done": True,
            "last_report": banner + (prior_text or "(no prior report)"),
            "token_totals": dict(self._token_totals),
            "error_counts": dict(self._error_counts),
            "outcome": terminal.UNGATED,
            "termination": termination,
            "reviewed": False,
        }
        if extra_update:
            update.update(extra_update)
        return Command(goto=END, update=update)

    def _halt_tallies(self, state: Any) -> dict:
        with self._registry_lock:
            _total_new = len(self._registry)
            _evals_new = sum(e["evals"] for e in self._registry.values())
        return {
            "total_delegations": state["total_delegations"] + _total_new,
            "evals_used": state.get("evals_used", 0) + _evals_new,
        }

    def _close_after_wind_down_error(self, state: Any, exc: Exception):
        """The turn that was to close a wind-down raised. The run closes anyway,
        with the halt's own termination, and says which retrospectives it lacks."""
        reason = (f"the wind-down turn failed ({type(exc).__name__}: "
                  f"{str(exc)[:200]})")
        self._log_missing_retrospectives(reason)
        return self._halt_resumable(
            state, reason=reason, termination=self._stop_termination(),
            extra_update=self._halt_tallies(state))

    def _trip(
        self, state: Any, *, drift: dict, reason: str, termination: str,
        tallies: dict,
    ) -> Command | None:
        """A backstop condition holds. Ask the run to wind down — every agent
        gives its retrospective, the run closes with ``termination`` — instead of
        jumping to END with none. While the wind-down is in progress this
        returns None (the condition keeps holding; that is expected). Only a
        wind-down that overruns its grace, or one that cannot be asked for,
        falls through to the hard halt, so a halt is always bounded."""
        if self._stop is None:
            self._record_science_drift(drift)
            if self._request_wind_down(
                reason=reason, termination=termination,
                run_dir=state.get("run_dir")):
                self._record_intervention(
                    "BACKSTOP_WIND_DOWN", "(run)",
                    f"{reason}; winding down for retrospectives, closing "
                    f"{termination}")
                return None
        elif not self._wind_down_overdue():
            return None
        else:
            self._record_science_drift({**drift, "wind_down": "overran"})
            self._log_missing_retrospectives(
                f"{reason}; the wind-down overran its grace")
        return self._halt_resumable(
            state, reason=reason, termination=termination,
            extra_update=tallies)

    def _time_backstop_due(
        self, budget: float | None, start: float | None,
    ) -> tuple[dict, str] | None:
        """``(drift, reason)`` once the run is past ``run_backstop_multiple`` x
        its time budget, else None."""
        if not backstop_enabled() or budget is None or start is None:
            return None
        mult = run_backstop_multiple()
        elapsed = time.time() - start
        if elapsed <= budget * mult:
            return None
        with self._registry_lock:
            abandoned = [d for d, e in self._registry.items()
                         if e["status"] == "Working"]
        return (
            {"error_type": "RUN_BACKSTOP", "elapsed": elapsed,
             "budget": budget, "multiple": mult, "abandoned": abandoned},
            f"time backstop: {int(mult)}x budget exceeded "
            f"({elapsed:.0f}s / {budget:.0f}s)")

    def _time_backstop_tick(self) -> None:
        """Ask for the time-backstop wind-down from outside the turn loop.

        A turn on a CLI backend is one long session, so the check between
        turns never runs while a delegation, a ``Wait`` or a gate review is in
        flight. This is called from the stop checkpoints (every tool result,
        every ``Wait`` tick) and from a timer thread, and does what the
        between-turn check does: one stop request, one BACKSTOP_WIND_DOWN row.
        """
        if self._stop is not None or self._run_start is None:
            return
        due = self._time_backstop_due(self._budget_seconds, self._run_start)
        if due is None:
            return
        drift, reason = due
        with self._backstop_tick_lock:
            if self._stop is not None or self._backstop_requested:
                return
            self._backstop_requested = True
        self._record_science_drift(drift)
        if self._request_wind_down(
                reason=reason, termination=terminal.BACKSTOP_TIME):
            self._record_intervention(
                "BACKSTOP_WIND_DOWN", "(run)",
                f"{reason}; winding down for retrospectives, closing "
                f"{terminal.BACKSTOP_TIME}")

    def _start_backstop_timer(self) -> None:
        """One daemon thread per run that fires the time backstop at its
        deadline even when no checkpoint is reached."""
        import threading

        budget, start = self._budget_seconds, self._run_start
        if (self._backstop_timer is not None or not backstop_enabled()
                or budget is None or start is None):
            return
        delay = start + budget * run_backstop_multiple() - time.time()

        def _fire() -> None:
            try:
                self._time_backstop_tick()
            except Exception:  # noqa: BLE001
                pass

        t = threading.Timer(max(delay, 0.0) + 0.5, _fire)
        t.daemon = True
        self._backstop_timer = t
        t.start()

    def _check_unrecoverable(self, state: Any, budget: float | None, start: float | None) -> Command | None:
        """Return a halt Command if an unrecoverable condition is met, else None.
        Extracted verbatim from __call__ (USD ceiling → repeated errors → time
        backstop)."""

        # (1) USD cost ceiling. Hard, resumable (raise budget_usd and resume).
        # Inactive under ollama (no cost data): warn once, never halt.
        _budget_usd = self._budget_usd
        if _budget_usd is not None and _budget_usd > 0:
            _spent = self._token_totals.get("total_cost_usd") or 0.0
            if not self._cost_observed:
                if (
                    getattr(self, "_turn_count", 0) >= 1
                    and not self._usd_inactive_warned
                ):
                    self._usd_inactive_warned = True
                    self._record_science_drift({
                        "error_type": "USD_BUDGET_INACTIVE",
                        "budget_usd": _budget_usd,
                        "note": "no per-call cost reported (e.g. ollama); "
                                "USD ceiling treated as inactive",
                    })
            elif _spent >= _budget_usd:
                return self._trip(
                    state,
                    drift={
                        "error_type": "USD_BACKSTOP",
                        "spent_usd": _spent,
                        "budget_usd": _budget_usd,
                    },
                    reason=(
                        f"USD budget exhausted "
                        f"(${_spent:.4f} / ${_budget_usd:.4f})"
                    ),
                    termination=terminal.BACKSTOP_USD,
                    tallies=self._halt_tallies(state),
                )

        # (2) Repeated errors: a target failing N times in a row (genuine
        # worker EXCEPTIONS, not REVISE loops or poor results — those reset the
        # streak on any success) is not going to self-heal by looping the
        # strategizer at it again. The default is deliberately CONSERVATIVE: a
        # legitimate run "stuck" in the scientific process loops on critic
        # verdicts and slow delegations, none of which count here — only hard
        # consecutive crashes do. Knob: max_consecutive_errors (config.yaml
        # runtime block; F3DASM_MAX_CONSECUTIVE_ERRORS overrides); 0 disables.
        from ..runtime.settings import get_int
        _max_err = get_int("max_consecutive_errors", 12)
        if _max_err > 0:
            with self._registry_lock:
                _stuck = [
                    (t, n) for t, n in self._consecutive_errors.items()
                    if n >= _max_err
                ]
            if _stuck:
                _t, _n = _stuck[0]
                _down = _t in self._unreachable_targets
                return self._trip(
                    state,
                    drift={
                        "error_type": ("BACKEND_UNAVAILABLE" if _down
                                       else "REPEATED_ERRORS"),
                        "target": _t,
                        "consecutive": _n,
                    },
                    reason=(
                        f"backend endpoint unavailable: {_t} failed {_n}x "
                        "consecutively" if _down else
                        f"repeated errors: {_t} failed {_n}x consecutively"
                    ),
                    termination=(terminal.BACKEND_UNAVAILABLE if _down
                                 else terminal.REPEATED_ERRORS),
                    tallies=self._halt_tallies(state),
                )

        # (3) Time backstop: past run_backstop_multiple x the (soft) time
        # budget, bound runaway cost. Now resumable (raise budget + resume).
        due = self._time_backstop_due(budget, start)
        if due is not None:
            if self._backstop_requested and self._stop is None:
                return None
            drift, reason = due
            return self._trip(
                state, drift=drift, reason=reason,
                termination=terminal.BACKSTOP_TIME,
                tallies=self._halt_tallies(state))

        return None
