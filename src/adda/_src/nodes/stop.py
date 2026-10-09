"""A stop: wind the run down and close it, instead of killing or halting it.

SIGTERM unwinds nothing, so a killed run loses every agent's real
retrospective. A stop request (``infra/stop_request.py``) is noticed by the
entry node at its checkpoints — the start of each turn, every tool result it
receives, and each tick of a blocking ``Wait`` — and answered in order:

1. every live delegation is told to report what it has and finish;
2. new delegations are refused;
3. once the workers have reported (or ``grace_s`` has passed, whereupon the
   stragglers are cancelled and recorded as such), ``Done()`` skips the
   milestone, first-call, reproduction and critic gates and takes the
   retrospective round, closing STOPPED — UNGATED, never reviewed, resumable.

Whoever asks — the watchdog ahead of its deadline, the viewer, or a backstop
(time, USD, repeated errors) that would otherwise jump straight to END — goes
through this one path. A backstop closes with its own termination value rather
than STOPPED, and only if the wind-down overruns its grace plus one closing
allowance does the hard halt still fire, so a halt can never run on forever.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..infra.stop_request import (
    DEFAULT_GRACE_S,
    consume_stop_request,
    read_stop_request,
    write_stop_request,
)
from ..runtime import terminal

TIME_SECTION = (
    "- TIME: the run is ending before the work was finished. (a) DIAGNOSIS: "
    "was the problem too hard for the resources allotted, or was the work "
    "inefficient (your strategy, the tools, the problem setup)? Pick one and "
    "say why. (b) WHERE IT WENT: where the time (or spend) went, citing "
    "evidence from your own work — turns, waits, evaluations, retries. "
    "(c) WHAT WOULD HAVE AVOIDED IT (required): concrete changes, each "
    "attributed to who can make it — your own strategy, the tools/harness, or "
    "the problem setup (budget, evaluator cost, statement)."
)


CRASH_SECTION = (
    "- TIME: the previous process of this run was lost before it could "
    "close. (a) DIAGNOSIS: what do you think brought it down (a resource "
    "limit such as memory, a node loss, the evaluator or a tool, your own "
    "work) and did anything in your work foreshadow it? Say what you know "
    "and what you are inferring. (b) WHERE IT WENT: what the run had spent "
    "its time on up to that point, citing your own work. (c) WHAT WOULD HAVE "
    "AVOIDED IT (required): concrete changes, each attributed to who can "
    "make it — your own strategy, the tools/harness, or the problem setup."
)


def time_section(stop: dict | None) -> str:
    return CRASH_SECTION if (stop or {}).get("termination") == (
        terminal.CRASHED) else TIME_SECTION


def stop_headline(stop: dict | None) -> str:
    """Why the run is ending, stated plainly. A run out of time is told it did
    not finish on time; any other stop names its own cause."""
    stop = stop or {}
    reason = stop.get("reason") or ""
    if stop.get("termination") == terminal.CRASHED:
        return ("The previous process of this run crashed or was killed"
                + (f" ({reason})" if reason else "")
                + "; this resume only collects what you can still report.")
    if stop.get("by") == "watchdog" or stop.get("termination") == (
            terminal.BACKSTOP_TIME):
        return ("The hard time cap is being reached"
                + (f" ({reason})" if reason else "")
                + "; the run did not finish on time.")
    return "The run is being stopped" + (f": {reason}." if reason else ".")


def wind_down_notice(stop: dict | None) -> str:
    return (
        f"[RUN STOP — {stop_headline(stop)} Report what you have NOW: your "
        "findings so far, what is unfinished, and your retrospective. Do not "
        "start new work. Your retrospective must include this bullet:\n"
        f"{time_section(stop)}]"
    )


_LIVE = ("Working", "Revising")


class StopMixin:
    """Stop-request handling for a node; state lives on ``self._stop``."""

    def _stop_tick(self) -> str:
        """Raw notice text for the entry node, "" when there is nothing new
        (the caller marks it as an adda notice).

        Idempotent and cheap: one small file read until a request is seen.
        Only the node that runs a turn (the one holding the run's start
        time) acts; a worker node's checkpoints leave the request alone.
        """
        self._time_backstop_tick()
        stop = self._stop
        if stop is None:
            run_dir = self._current_run_dir
            if run_dir is None or self._run_start is None:
                return ""
            req = read_stop_request(run_dir, since=self._run_start)
            if req is None:
                return ""
            stop = self._stop = {
                **req,
                "deadline": time.time() + req["grace_s"],
                "cancelled": False,
            }
            wound = self._stop_wind_down()
            if req.get("termination") == terminal.CRASHED:
                self._log_lost_workers()
            self._record_intervention(
                "RUN_STOP", "(run)",
                f"stop requested by {req['by']}"
                + (f": {req['reason']}" if req["reason"] else "")
                + f"; winding down {wound or 'no delegations'}, "
                f"grace {req['grace_s']:g}s",
            )
            return (
                f"[RUN STOP — requested by {req['by']}"
                + (f" ({req['reason']})" if req["reason"] else "")
                + ". New delegations are refused. "
                + (f"Winding down {', '.join(wound)}: Wait() for their "
                   "reports, then " if wound else "")
                + "call Done() with what you have — it will skip the "
                "usual gates and ask only for your retrospective.]")
        if not stop["cancelled"] and time.time() >= stop["deadline"]:
            cancelled = self._stop_cancel_stragglers()
            if cancelled:
                return (
                    "[RUN STOP — grace expired; cancelled "
                    f"{', '.join(cancelled)} (no report). Call Done() now.]")
        return ""

    def _stop_nodes(self) -> list[Any]:
        return [self, *getattr(self, "_peers", {}).values()]

    def _stop_wind_down(self) -> list[str]:
        """Tell every live delegation to report now. Returns their ids."""
        ids: list[str] = []
        for n in self._stop_nodes():
            with n._registry_lock:
                live = [d for d, e in n._registry.items()
                        if e.get("status") in _LIVE]
            ids.extend(live)
        for did in ids:
            for n in self._stop_nodes():
                with n._pending_worker_msgs_lock:
                    n._pending_worker_msgs.setdefault(did, []).append(
                        wind_down_notice(self._stop))
        return sorted(ids)

    def _stop_cancel_stragglers(self) -> list[str]:
        """Past the grace: detach what has not reported, and say so.

        No placeholder retrospective is written for a straggler — it never
        gave one, and the record must say that rather than invent it.
        """
        self._stop["cancelled"] = True
        cancelled: list[str] = []
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        for n in self._stop_nodes():
            with n._registry_lock:
                for did, e in n._registry.items():
                    if e.get("status") in _LIVE:
                        e["status"] = "Cancelled"
                        cancelled.append((did, e))
        for did, e in cancelled:
            if self._delegation_log is not None:
                self._delegation_log.record(
                    id=did,
                    from_node=(
                        self._name if e.get("parent") in (None, "entry")
                        else e["parent"]),
                    to_node=e.get("target") or "",
                    task=str(e.get("task", "")),
                    hypothesis_ids=list(e.get("hypothesis_ids") or []),
                    deliverable=(
                        "run stop: cancelled after the "
                        f"{self._stop['grace_s']:g}s grace without a "
                        "report; no retrospective was given"),
                    started_at=e.get("started_at") or now, completed_at=now,
                    status="CANCELLED",
                    tokens_in=0, tokens_out=0, cost_usd=None,
                )
        ids = sorted(d for d, _ in cancelled)
        if ids:
            self._record_intervention(
                "RUN_STOP_CANCELLED", "(run)",
                f"cancelled after grace, no report: {', '.join(ids)}")
        return ids

    def _stop_refusal(self) -> str | None:
        """The ``Delegate`` refusal while a stop is active, else None."""
        if not any(n._stop is not None for n in self._stop_nodes()):
            return None
        return (
            "run stop: new delegations are refused. This delegation "
            "was NOT started. Wait() for any still reporting, then call "
            "Done().")

    def _stop_consume(self) -> None:
        run_dir = self._current_run_dir
        if run_dir is not None:
            consume_stop_request(run_dir)

    def _request_wind_down(
        self, *, reason: str, termination: str, run_dir: Any = None,
        grace_s: float = DEFAULT_GRACE_S,
    ) -> bool:
        """A backstop asks for the graceful path instead of halting. False when
        it cannot (no run directory, or the file cannot be written), in which
        case the caller halts as it always did."""
        run_dir = Path(run_dir) if run_dir else self._current_run_dir
        if run_dir is None or self._run_start is None:
            return False
        return write_stop_request(
            run_dir, by="backstop", reason=reason, grace_s=grace_s,
            termination=termination)

    def _wind_down_overdue(self) -> bool:
        """The grace for the workers plus an equal one for the entry node's own
        retrospective round has passed: the hard halt may fire."""
        stop = self._stop
        return bool(stop) and time.time() > stop["deadline"] + stop["grace_s"]

    def _stop_termination(self) -> str:
        return (self._stop or {}).get("termination") or terminal.STOPPED

    def _recorded_retrospective_ids(self) -> set[str]:
        import json

        run_dir = self._current_run_dir
        have: set[str] = set()
        if run_dir is None:
            return have
        try:
            with open(run_dir / "debug" / "retrospectives.jsonl",
                      encoding="utf-8") as f:
                for line in f:
                    try:
                        have.add(str(json.loads(line).get("source_id")))
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError:
            pass
        return have

    def _missing_retrospectives(self) -> list[str]:
        """Delegations that ended without a recorded retrospective."""
        run_dir = self._current_run_dir
        if run_dir is None:
            return []
        have = self._recorded_retrospective_ids()
        ids: set[str] = set()
        for n in self._stop_nodes():
            with n._registry_lock:
                ids.update(n._registry)
        missing = sorted(ids - have)
        if "DONE" not in have:
            missing.append("DONE")
        return missing

    def _log_lost_workers(self) -> None:
        """A resume after a crash: delegations the delegation log last saw
        RUNNING died with the process. They are named, never given invented
        retrospective text."""
        log = getattr(self, "_delegation_log", None)
        if log is None:
            return
        have = self._recorded_retrospective_ids()
        lost = sorted(
            r["id"] for r in log.query_all()
            if r.get("status") == "RUNNING" and r["id"] not in have)
        if lost:
            self._record_intervention(
                "RETROSPECTIVES_MISSING", "(run)",
                "process lost: no retrospective from "
                f"{', '.join(lost)} (they were running when the process "
                "died; none is synthesized)")

    def _log_missing_retrospectives(self, why: str) -> None:
        missing = self._missing_retrospectives()
        if missing:
            self._record_intervention(
                "RETROSPECTIVES_MISSING", "(run)",
                f"{why}; no retrospective from: {', '.join(missing)}")
