"""Operator/watchdog stop: wind the run down and close it, instead of killing it.

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
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..infra.stop_request import consume_stop_request, read_stop_request

_WIND_DOWN = (
    "[OPERATOR STOP — the run is being stopped. Report what you have NOW: "
    "your findings so far, what is unfinished, and your retrospective. Do "
    "not start new work.]"
)

_LIVE = ("Working", "FollowUp", "Revising")


class StopMixin:
    """Stop-request handling for a node; state lives on ``self._stop``."""

    def _stop_tick(self) -> str:
        """Raw notice text for the entry node, "" when there is nothing new
        (the caller marks it as an adda notice).

        Idempotent and cheap: one small file read until a request is seen.
        Only the node that runs a turn (the one holding the run's start
        time) acts; a worker node's checkpoints leave the request alone.
        """
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
            self._record_intervention(
                "OPERATOR_STOP", "(run)",
                f"stop requested by {req['by']}"
                + (f": {req['reason']}" if req["reason"] else "")
                + f"; winding down {wound or 'no delegations'}, "
                f"grace {req['grace_s']:g}s",
            )
            return (
                f"[OPERATOR STOP — requested by {req['by']}"
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
                    "[OPERATOR STOP — grace expired; cancelled "
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
                        _WIND_DOWN)
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
                        "operator stop: cancelled after the "
                        f"{self._stop['grace_s']:g}s grace without a "
                        "report; no retrospective was given"),
                    started_at=e.get("started_at") or now, completed_at=now,
                    status="CANCELLED",
                    tokens_in=0, tokens_out=0, cost_usd=None,
                )
        ids = sorted(d for d, _ in cancelled)
        if ids:
            self._record_intervention(
                "OPERATOR_STOP_CANCELLED", "(run)",
                f"cancelled after grace, no report: {', '.join(ids)}")
        return ids

    def _stop_refusal(self) -> str | None:
        """The ``Delegate`` refusal while a stop is active, else None."""
        if not any(n._stop is not None for n in self._stop_nodes()):
            return None
        return (
            "operator stop: new delegations are refused. This delegation "
            "was NOT started. Wait() for any still reporting, then call "
            "Done().")

    def _stop_consume(self) -> None:
        run_dir = self._current_run_dir
        if run_dir is not None:
            consume_stop_request(run_dir)
