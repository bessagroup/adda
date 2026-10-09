"""The wall-clock rules on the nodes: notices, the delegation cutoff and the
start of the wind-down (``runtime/time_rules.py`` holds the thresholds and the
notice text; ``nodes/wind_down.py`` runs the wind-down).

Only the entry node holds the run clock (``_run_start``); every other node
reaches it through ``_time_entry``. The same code serves every node and both
backends. A timer thread per threshold fires the notice even when the entry
node sits in one long CLI turn; the checkpoints call the same idempotent tick,
so a late timer changes nothing.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..infra import pending_notices, wind_down
from ..runtime import time_rules as _rules
from .stop import _LIVE


class TimeRulesMixin:
    # ── the clock ────────────────────────────────────────────────────────────

    def _time_entry(self) -> Any | None:
        """The node that holds the run clock, from any node."""
        for n in self._stop_nodes():
            if n._run_start is not None and n._time_rules_ensure() is not None:
                return n
        return None

    def _time_rules_ensure(self) -> Any | None:
        """The rules for this node's clock, built once from the settings."""
        if self._time_rules is None and self._run_start is not None:
            self._time_rules = _rules.TimeRules.from_settings(
                self._budget_seconds)
        return self._time_rules

    def _time_rules_start(self) -> None:
        """Entry node, once per run: read the rules and arm one timer each."""
        if self._time_armed:
            return
        rules = self._time_rules_ensure()
        if rules is None:
            return
        self._time_armed = True
        for phase, _frac in rules.schedule():
            delay = self._run_start + rules.at(phase) - _rules.now()
            t = threading.Timer(max(delay, 0.0) + 0.2, self._time_timer_fire)
            t.daemon = True
            self._time_timers.append(t)
            t.start()

    def _time_timer_fire(self) -> None:
        try:
            self._time_rules_tick()
        except Exception:  # noqa: BLE001
            pass

    def _time_rules_cancel(self) -> None:
        """The run is over: no timer may act on a later run in this process."""
        self._time_closed = True
        wind_down.end()
        for t in self._time_timers:
            t.cancel()
        self._time_timers.clear()

    def _time_elapsed(self) -> float | None:
        entry = self._time_entry()
        if entry is None or entry._run_start is None:
            return None
        return _rules.now() - entry._run_start

    # ── the thresholds ───────────────────────────────────────────────────────

    def _time_rules_tick(self) -> None:
        """Fire every threshold the clock has passed, once. Idempotent.

        When several are due at once (a late checkpoint, a resume) only the
        latest fires: its notice states the rule that holds now.
        """
        entry = self._time_entry()
        if entry is None:
            return
        if entry is not self:
            entry._time_rules_tick()
            return
        rules = self._time_rules_ensure()
        if rules is None or self._time_closed:
            return
        elapsed = _rules.now() - self._run_start
        with self._time_lock:
            due = [p for p in rules.due(elapsed) if p not in self._time_fired]
            self._time_fired.update(due)
        if due:
            self._time_fire(due[-1], elapsed)

    def _time_fire(self, phase: str, elapsed: float) -> None:
        from ..runtime import features

        rules = self._time_rules
        if phase == _rules.WIND_DOWN:
            self._record_intervention(
                "TIME_WIND_DOWN", "(run)",
                f"{elapsed:.0f}s of {rules.budget:.0f}s: the wind-down begins",
                fault="observation")
            self._wind_down_begin(elapsed)
            return
        self._record_intervention(
            "TIME_" + phase.split(":")[0].upper(), "(run)",
            f"{elapsed:.0f}s of {rules.budget:.0f}s: "
            + rules.notice(phase, elapsed, can_call_done=True),
            fault="observation")
        if features.enabled("budget_notes"):
            self._time_broadcast(phase, elapsed)

    def _time_broadcast(self, phase: str, elapsed: float) -> None:
        """The notice to the entry node and to every running delegation."""
        rules = self._time_rules
        entry_text = rules.notice(phase, elapsed, can_call_done=True)
        worker_text = rules.notice(phase, elapsed, can_call_done=False)
        with self._notifications_lock:
            self._notifications.append(f"[TIME — {entry_text}]")
        run_dir = self._current_run_dir
        if run_dir is None:
            return
        for n in self._stop_nodes():
            with n._registry_lock:
                live = [d for d, e in n._registry.items()
                        if e.get("status") in _LIVE]
            for did in live:
                pending_notices.post(
                    Path(run_dir) / "debug", did, f"[TIME — {worker_text}]")

    def _time_cutoff_refusal(self) -> str | None:
        """The ``Delegate`` refusal once the cutoff has passed, on any node.

        Always enforced when a budget is declared; ``budget_notes`` only
        decides whether the earlier notices are sent.
        """
        entry = self._time_entry()
        if entry is None:
            return None
        elapsed = _rules.now() - entry._run_start
        rules = entry._time_rules
        if elapsed < rules.at(_rules.CUTOFF):
            return None
        self._record_intervention(
            "DELEGATE_CUTOFF", "(refused)",
            f"new delegation refused at {elapsed:.0f}s of "
            f"{rules.budget:.0f}s (cutoff at {rules.at(_rules.CUTOFF):.0f}s)",
            fault="observation")
        return "ERROR: " + rules.cutoff_refusal(elapsed)
