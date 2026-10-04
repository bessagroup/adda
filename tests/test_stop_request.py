"""A stop request winds the run down and closes it STOPPED, not killed.

SIGTERM unwinds nothing, so a killed run lost every agent's real
retrospective. These cover the node side: noticing the request at each
checkpoint, winding live delegations down before cancelling anyone, refusing
new work, and closing through the retrospective round as STOPPED.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra.delegation_log import DelegationLog
from adda._src.infra.stop_request import (
    read_stop_request,
    write_stop_request,
)
from adda._src.nodes import Node
from adda._src.runtime import terminal


class _Stub:
    def __init__(self) -> None:
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return ""


def _node(tmp_path: Path) -> tuple[Node, Path, dict]:
    run_dir = tmp_path / "runs" / "T1"

    class S(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "Wait", "Delegate"})
        description = "s"

    class W(Agent):
        description = "w"

    spec = Graph(
        nodes={"strategizer": S(), "worker": W()},
        edges=(Edge("strategizer", "worker"),),
        entry="strategizer",
    )
    dlog = DelegationLog(run_dir / "debug" / "delegation_log.jsonl")
    notes_dir = run_dir / "debug" / "strategizer_notes"
    notes_dir.mkdir(parents=True)
    node = Node(
        _Stub(), name="strategizer", outgoing=["worker"], spec=spec,
        worker_adapters={"worker": _Stub()}, notes_dir=notes_dir,
        delegation_log=dlog,
    )
    node._run_start = time.time() - 60
    return node, run_dir, node._build_routing_closures()


def _live(node: Node, did: str = "D001") -> None:
    with node._registry_lock:
        node._registry[did] = {
            "status": "Working", "parent": "entry", "result": None,
            "evals": 0, "hypothesis_ids": [], "target": "worker",
            "started_at": "2026-01-01T00:00:00+00:00",
        }


_RETRO = "### Retrospective\n- BLOCKED: none\n- FRICTION: none"


def test_no_request_changes_nothing(tmp_path):
    node, _, tools = _node(tmp_path)
    assert node._stop_tick() == ""
    assert node._stop is None
    assert node._stop_refusal() is None


def test_request_while_idle_closes_stopped_through_the_retrospective(tmp_path):
    node, run_dir, tools = _node(tmp_path)
    write_stop_request(run_dir, by="viewer", reason="enough", grace_s=5)

    first = tools["Done"]("Stopping with what I have.")
    # No milestone / first-call-warning / reproduction gate: straight to the
    # retrospective, which names the real reason.
    assert "STOPPED" in first and "Retrospective" in first, first
    assert "WARNING: first Done()" not in first

    out = tools["Done"](_RETRO)
    assert out.endswith("Run complete."), out
    assert node._route["kind"] == "done"
    assert node._route["termination"] == terminal.STOPPED
    assert node._route["outcome"] == terminal.UNGATED
    assert node._route["reviewed"] is False
    assert "STOPPED" in node._route["summary"]
    # Honoured: renamed, so a resume does not trip on it.
    assert not (run_dir / "debug" / "stop_request.json").exists()
    assert (run_dir / "debug" / "stop_request.consumed.json").exists()


def test_stopped_is_censored_never_gated():
    assert terminal.STOPPED in terminal.TERMINATIONS
    assert terminal.STOPPED in terminal.HALT_TERMINATIONS
    assert terminal.resolve(
        terminal.GATED, terminal.STOPPED, True)[0] == terminal.UNGATED


def test_request_mid_delegation_winds_down_before_cancelling(tmp_path):
    node, run_dir, tools = _node(tmp_path)
    _live(node)
    write_stop_request(run_dir, by="watchdog", grace_s=60)

    notice = node._drain_notifications()
    assert "OPERATOR STOP" in notice and "D001" in notice, notice
    # The worker was asked to report, not cancelled.
    with node._pending_worker_msgs_lock:
        queued = node._pending_worker_msgs["D001"]
    assert any("Report what you have NOW" in m for m in queued), queued
    assert node._registry["D001"]["status"] == "Working"

    # Done() waits for the report rather than closing over it.
    held = tools["Done"]("closing")
    assert "still reporting" in held and "D001" in held, held

    # The worker reports; its report is its retrospective, and Done proceeds.
    with node._registry_lock:
        node._registry["D001"].update({"status": "Done", "result": "partial"})
    assert "Retrospective" in tools["Done"]("closing")
    assert node._registry["D001"]["status"] == "Done"


def test_stragglers_past_the_grace_are_cancelled_and_recorded(tmp_path):
    node, run_dir, tools = _node(tmp_path)
    _live(node)
    write_stop_request(run_dir, by="watchdog", reason="deadline", grace_s=0)

    node._stop_tick()           # first sight: wind down
    out = node._stop_tick()     # grace (0s) already over: cancel
    assert "cancelled D001" in out, out
    assert node._registry["D001"]["status"] == "Cancelled"
    rows = [r for r in node._delegation_log.query_all() if r["id"] == "D001"]
    assert rows and rows[-1]["status"] == "CANCELLED"
    assert "operator stop" in rows[-1]["deliverable"]
    assert "no retrospective" in rows[-1]["deliverable"]
    # Not pending any more, so Done() proceeds to the retrospective.
    assert "Retrospective" in tools["Done"]("closing")


def test_new_delegation_is_refused_while_stopping(tmp_path):
    node, run_dir, tools = _node(tmp_path)
    write_stop_request(run_dir, by="viewer")
    out = tools["Delegate"]("worker", "do a thing", "a report")
    assert "operator stop" in out and "NOT started" in out, out
    # Not an ERROR return: a refusal by design is not a tool failure.
    assert not out.lstrip().startswith("ERROR:")
    assert node._registry == {}


def test_blocking_wait_returns_early_once_on_first_sight(tmp_path):
    node, run_dir, tools = _node(tmp_path)
    _live(node)
    # A live thread so Wait sees something it could wait for.
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    node._threads["D001"] = t
    try:
        # Request lands while Wait is already blocking.
        threading.Timer(
            0.5, write_stop_request, (run_dir,),
            {"by": "viewer", "grace_s": 60}).start()
        started = time.monotonic()
        out = tools["Wait"]()
        assert time.monotonic() - started < 20, "Wait did not return early"
        assert "OPERATOR STOP" in out and "woken early" in out, out
    finally:
        stop.set()


def test_stale_request_from_an_earlier_run_is_ignored(tmp_path):
    node, run_dir, tools = _node(tmp_path)
    path = run_dir / "debug" / "stop_request.json"
    path.write_text(json.dumps({
        "requested_at": node._run_start - 3600, "by": "viewer",
        "reason": "", "grace_s": 5}), encoding="utf-8")
    assert read_stop_request(run_dir) is not None
    assert read_stop_request(run_dir, since=node._run_start) is None
    assert node._stop_tick() == ""
    assert node._stop is None


def test_worker_node_without_a_run_start_leaves_the_request_alone(tmp_path):
    node, run_dir, _ = _node(tmp_path)
    node._run_start = None
    write_stop_request(run_dir, by="viewer")
    assert node._stop_tick() == ""
    assert node._stop is None


def test_malformed_request_is_ignored(tmp_path):
    _, run_dir, _ = _node(tmp_path)
    (run_dir / "debug" / "stop_request.json").write_text("{not json")
    assert read_stop_request(run_dir) is None
    assert read_stop_request(tmp_path / "nowhere") is None


def test_a_stopped_close_is_recorded_stopped_and_resumable(tmp_path):
    """run_status.json says STOPPED (not a verdict on the work), keeps the
    UNGATED outcome beside it, and marks the run resumable."""
    import logging

    from adda._src.runtime.agent_runtime import AgenticRun, _RunContext

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    debug_dir = run_dir / "debug"
    debug_dir.mkdir(parents=True)
    ctx = _RunContext(
        ts="T", run_dir=run_dir, debug_dir=debug_dir,
        notes_dir=debug_dir / "strategizer_notes",
        workspace_dir=run_dir / "workspace",
        problem="x", problem_sha256="a", live_problem_sha256="a",
        resume_from=None, start_time=0.0, thread_id="th",
        log=logging.getLogger("test_stop_close"),
        log_handler=logging.NullHandler(),
        delegation_log=__import__("unittest.mock").mock.MagicMock(),
        canonical_cfg={}, study_cfg={}, initial_state={}, graph_config={},
    )
    run._finalize_run(ctx, {
        "last_report": "stopped", "outcome": "UNGATED",
        "termination": "stopped", "reviewed": False, "token_totals": {}})
    status = json.loads((debug_dir / "run_status.json").read_text())
    assert status["status"] == "STOPPED"
    assert status["outcome"] == "UNGATED"
    assert status["termination"] == "stopped"
    assert status["resumable"] is True
