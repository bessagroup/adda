"""The wind-down: how a run ends when the first of its wall or token budgets is spent.

The budgets are the real limit and nothing is killed. At ``wind_down_at`` x
the first of those budgets to reach it, the entry node (which holds the run clock) begins the wind-down:

* W0, the freeze. The gate in ``infra/wind_down.py`` refuses every tool that
  would start new work (``Delegate``, ``Bash``, ``RunNotebook`` ...) with an
  ERROR that names the rule, and the metered evaluator refuses new
  evaluations. Running work finishes.
* W1, the drain. Every node is told the three steps: let running work finish,
  save every result not yet stored (naming the file and how it was produced),
  then end (a worker reports, the entry node calls ``Done``). The entry
  node's turns are driven by :meth:`_wind_down_route`: at most
  ``wind_down_turns`` forced turns per step. Each node has
  ``wind_down_tool_calls`` tool calls; past them only its end call is allowed.
  The counts bound the wind-down, not the clock.
* W2, the close (``FeedbackTools._close_wound_down``). The entry node writes
  the deliverable from what exists. ``Done`` then runs the reproduction gate
  ONCE and ONE critic review, with no rework. Both results are recorded with
  the deliverable and nobody edits it afterwards.
* W3, the retrospectives, as in every other close.

Work that has not finished after ``wind_down_interrupt_after_s``, or after one
forced turn of waiting, gets SIGINT, only to processes the node owns
(``infra/interrupt.py``); the tool returns "interrupted" and the node saves
what it has.

The record is ``debug/wind_down.json``; ``run_status.json`` carries
``wind_down_turns``, ``overrun_s`` and ``deliverables_missing``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..infra import interrupt, pending_notices, wind_down
from ..runtime import settings
from ..runtime import time_rules as _rules
from .stop import _LIVE

RECORD_FILE = "wind_down.json"

HUB_NOTICE = (
    "WIND-DOWN: the {budget} is spent. Nothing is killed. New "
    "delegations and new evaluations are refused, and Bash and the search "
    "tools are closed. Do now, in this order: (1) Wait() for the running "
    "delegations; (2) save every result not yet stored: write a note naming "
    "each file and how it was produced; (3) write the deliverable from what "
    "exists, then call Done() with your summary. Mark unfinished work as "
    "unfinished; claim nothing that did not run. You have {limit} tool calls. "
    "After Done(), the reproduction gate runs once and the critic reviews "
    "once. There is no rework.")

WORKER_NOTICE = (
    "WIND-DOWN: the {budget} is spent. Nothing is killed. "
    "Start nothing new: Bash and the search tools are closed. Do now, in "
    "this order: (1) let the work that runs finish; (2) save every result "
    "not yet stored: write a file or note that names each file and how it "
    "was produced; (3) write your final report and end your turn. Mark "
    "unfinished work as unfinished; claim nothing that did not run. You have "
    "{limit} tool calls.")

WAIT_TURN = (
    "WIND-DOWN, step 1 of 3 (turn {n}): these delegations still run: {ids}. "
    "Call Wait() for them.{tail}")
WAIT_TAIL_INTERRUPT = (
    " Work that has not finished is now interrupted (SIGINT); its report "
    "will say so.")
SAVE_TURN = (
    "WIND-DOWN, step 2 of 3: save every result not yet stored. Write a note "
    "that names each file and how it was produced. Then go to step 3.")
DONE_TURN_NO_DONE = (
    "WIND-DOWN, step 3 of 3 (forced turn {n} of {k}): write the deliverable "
    "from what exists, then end your turn. Mark unfinished work as "
    "unfinished; claim nothing that did not run. The reproduction "
    "gate runs once and the critic reviews once. There is no rework.")
DONE_TURN = (
    "WIND-DOWN, step 3 of 3 (forced turn {n} of {k}): write the deliverable "
    "from what exists, then call Done() with your summary. Mark unfinished "
    "work as unfinished; claim nothing that did not run. The reproduction "
    "gate runs once and the critic reviews once. There is no rework.")
PENDING_REFUSAL = (
    "WIND-DOWN: {n} delegation(s) still run: {ids}. Call Wait() for them, "
    "then call Done() again.")
REVIEW_PREFACE = (
    "[WIND-DOWN REVIEW — the budget is spent. This is the only review "
    "this run gets. There is no rework: judge what exists.]\n\n")


def _without_done(text: str) -> str:
    """The close texts ask for a final call to ``Done()``; a node that does
    not hold it is asked to reply instead."""
    return (text.replace("Call Done() ONE more time with",
                         "Reply ONE more time with")
            .replace("then call Done() again", "then end your turn again"))


class WindDownMixin:
    """State lives on the entry node (``_wd_*``); every other node reaches it
    through ``_time_entry``."""

    # ── the state ────────────────────────────────────────────────────────────

    def _wind_down_entry(self) -> Any | None:
        entry = self._time_entry()
        return entry if entry is not None and (
            entry._wd_started_at is not None) else None

    def _wind_down_active(self) -> bool:
        return self._wind_down_entry() is not None

    def _wind_down_record(self, **fields: Any) -> None:
        """Merge ``fields`` into ``debug/wind_down.json`` (atomic)."""
        run_dir = self._current_run_dir
        if run_dir is None:
            return
        path = Path(run_dir) / "debug" / RECORD_FILE
        try:
            have = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            have = {}
        have.update(fields)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(have, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # ── W0: begin ────────────────────────────────────────────────────────────

    def _wind_down_begin(self, prog: _rules.Progress) -> None:
        """Entry node, once: freeze, tell every node, arm the interrupt."""
        with self._time_lock:
            if self._wd_started_at is not None:
                return
            self._wd_started_at = _rules.now()
        rules = self._time_rules
        limit = int(settings.get_float("wind_down_tool_calls", 50))
        wind_down.begin(limit)
        if self._current_run_dir is not None:
            wind_down.publish_eval_stop(
                Path(self._current_run_dir) / "debug", self._wd_started_at)
        fields = {"budget": rules.budget_phrase(), "limit": limit}
        with self._notifications_lock:
            self._notifications.append("[" + (
                HUB_NOTICE if self._holds("Done") else WORKER_NOTICE
            ).format(**fields) + "]")
        run_dir = self._current_run_dir
        live: list[str] = []
        if run_dir is not None:
            for n in self._stop_nodes():
                with n._registry_lock:
                    ids = [d for d, e in n._registry.items()
                           if e.get("status") in _LIVE]
                for did in ids:
                    pending_notices.post(
                        Path(run_dir) / "debug", did,
                        "[" + WORKER_NOTICE.format(**fields) + "]")
                live.extend(ids)
        after = settings.get_float("wind_down_interrupt_after_s", 300)
        t = threading.Timer(after, self._wind_down_interrupt_timer)
        t.daemon = True
        self._time_timers.append(t)
        t.start()
        self._wind_down_record(
            started_at=self._wd_started_at,
            elapsed_at_start=round(_rules.now() - self._run_start, 1),
            budget_trigger=prog.driver, budget_s=rules.wall,
            token_budget=rules.tokens, eval_budget=rules.evals,
            progress=prog.by_kind(),
            tool_call_limit=limit,
            running_at_start=sorted(live), forced_turns=0, interrupted=[])

    # ── W1: drain ────────────────────────────────────────────────────────────

    def _wind_down_interrupt_timer(self) -> None:
        try:
            if not self._time_closed and not wind_down.suspended():
                self._wind_down_interrupt("not finished "
                    f"{settings.get_float('wind_down_interrupt_after_s', 300):g}"
                    " s after the wind-down began")
        except Exception:  # noqa: BLE001
            pass

    def _wind_down_interrupt(self, why: str) -> list[dict]:
        """SIGINT the work in flight that each node owns. Recorded, never
        silent; the nodes continue with "save results"."""
        hit = interrupt.interrupt_all()
        if hit:
            self._record_intervention(
                "WIND_DOWN_INTERRUPT", "(run)",
                f"{why}; SIGINT to {[(h['pid'], h['name']) for h in hit]}",
                fault="observation")
            have = self._wind_down_read().get("interrupted", [])
            self._wind_down_record(interrupted=[*have, *hit])
        return hit

    def _wind_down_read(self) -> dict:
        run_dir = self._current_run_dir
        if run_dir is None:
            return {}
        try:
            return json.loads((Path(run_dir) / "debug" / RECORD_FILE)
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _wind_down_route(self, state: Any, ai_msg: Any) -> Any | None:
        """The entry node's turn ended during the wind-down: say which step
        comes next, at most ``wind_down_turns`` times per step. Returns a
        ``Command`` that re-prompts, or None when the turn is to be routed as
        usual (the close was accepted)."""
        if self._wd_started_at is None:
            return None
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        from .tools.routing.feedback import FeedbackTools

        k = int(settings.get_float("wind_down_turns", 2))

        def _say(text: str, *, forced: bool = True) -> Command:
            if forced:
                self._wd_turns += 1
                self._wind_down_record(forced_turns=self._wd_turns)
            return Command(goto=self._name, update={
                "messages": [ai_msg, HumanMessage(content=text)]})

        if self._awaiting_retro:
            FeedbackTools(self)._capture_retrospective(
                str(ai_msg.content), "")
            return None
        if self._route.get("kind") == "done":
            return None
        working = self._pending_delegations()
        if working:
            n = self._wd_step_turns["wait"] = self._wd_step_turns["wait"] + 1
            if n >= 2:
                self._wind_down_interrupt(
                    "one wind-down turn spent waiting for " + ", ".join(working))
            return _say(WAIT_TURN.format(
                n=n, ids=working,
                tail=WAIT_TAIL_INTERRUPT if n >= 2 else ""))
        if not self._wd_save_asked:
            self._wd_save_asked = True
            return _say(SAVE_TURN)
        n = self._wd_step_turns["done"] = self._wd_step_turns["done"] + 1
        if n <= k:
            return _say((DONE_TURN if self._holds("Done")
                         else DONE_TURN_NO_DONE).format(n=n, k=k))
        text = FeedbackTools(self)._close_wound_down(str(ai_msg.content), "")
        if not self._holds("Done"):
            text = _without_done(text)
        return _say(text, forced=False)

    def _wind_down_finish(self) -> None:
        """At the close: the facts ``run_status.json`` reports."""
        if self._wd_started_at is None:
            return
        missing = self._missing_deliverables({
            "study_dir": str(self._study_dir or "."),
            "required_deliverables": getattr(
                self, "_required_deliverables", None) or []})
        if missing:
            self._record_science_drift({
                "error_type": "DELIVERABLES_MISSING", "missing": missing})
        self._wind_down_record(
            deliverables_missing=missing, forced_turns=self._wd_turns,
            tool_calls=wind_down.calls(),
            closed_at=time.time())
