"""Same-role delegations serialize on the shared worker adapter's own lock
(ClaudeAdapter/OpenAICompatibleAdapter.copy() returns self), but Delegate()
used to reply "Delegation started" and log a RUNNING row with started_at =
DISPATCH time regardless -- silently misrepresenting a delegation that will
actually sit blocked behind another same-role delegation as having started
immediately (run 20260928T141126: D003 dispatched at 14:14:38 while D002 --
same role -- was still running; D003's session only began a second after
D002's ended).

This suite exercises Delegate()'s own truthfulness at dispatch time and Wait
(block=False)'s status report while queued -- both driven purely from the
delegation REGISTRY (status=="Working" for the same target), independent of
any real backend lock, so a slow/blocking stub worker is enough to reproduce
the scenario headlessly.
"""
from __future__ import annotations

import threading

import pytest

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra.delegation_log import DelegationLog
from adda._src.nodes import Node


@pytest.fixture(autouse=True)
def _isolate_from_milestone_gate():
    """Exercise delegation-queueing mechanics only, not the process backlog
    gate (test_milestones.py's own concern) -- same isolation test_nodes.py
    uses for the same reason."""
    from adda._src.runtime import settings
    settings.configure({"milestones_enabled": False})
    yield
    settings.configure({})


class _Stub:
    def __init__(self):
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return "## Report\n### Actions taken\nnone\n### Conclusions\nok\n### Numbers\nn: 0"


class _BlockingWorker:
    """No .copy() method -- Delegate() reuses this SAME instance for every
    delegation to its target (worker_template.copy() if hasattr(...) else
    worker_template), exactly mirroring ClaudeAdapter/OpenAICompatibleAdapter
    sharing one adapter object across same-role delegations. A real _lock
    (same attribute name/semantics real backends use) actually serializes
    concurrent invoke() calls, and on_session_start fires the instant it is
    acquired -- mirroring ClaudeAdapter.invoke/OpenAICompatibleAdapter.invoke
    closely enough to exercise delegation.py's queueing logic for real."""

    def __init__(self):
        self.last_usage: dict = {}
        self.closure_tools: dict = {}
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.entered = threading.Event()

    def invoke(self, messages, on_session_start=None):
        with self._lock:
            if on_session_start is not None:
                on_session_start()
            self.entered.set()
            self.release.wait(timeout=5)
            return "## Report\n### Actions taken\nnone\n### Conclusions\nok\n### Numbers\nn: 0"


def _node(tmp_path, worker):
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "Wait"})
        description = "strategizer"

    class B(Agent):
        role = "implementer"
        description = "implementer"

    spec = Graph(
        nodes={"strategizer": A(), "implementer": B()},
        edges=(Edge("strategizer", "implementer"),), entry="strategizer")
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    return Node(
        _Stub(), name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": worker}, notes_dir=notes,
        delegation_log=DelegationLog(tmp_path / "dlog.jsonl"))


def test_second_same_role_delegation_is_reported_queued(tmp_path):
    worker = _BlockingWorker()
    n = _node(tmp_path, worker)
    tools = n.adapter.closure_tools

    out1 = tools["Delegate"]("implementer", "task A", "a report")
    assert "Delegation started" in out1
    assert worker.entered.wait(timeout=2), "D001 never reached invoke()"

    # D001 is genuinely inside invoke() (blocked), still "Working" in the
    # registry -- a second dispatch to the SAME role must be truthful.
    out2 = tools["Delegate"]("implementer", "task B", "a report")
    assert "QUEUED behind D001" in out2, out2
    assert "same-role delegations run one at a time" in out2

    worker.release.set()


def test_queued_delegations_registry_entry_has_no_session_start_yet(tmp_path):
    worker = _BlockingWorker()
    n = _node(tmp_path, worker)
    tools = n.adapter.closure_tools

    tools["Delegate"]("implementer", "task A", "a report")
    worker.entered.wait(timeout=2)
    tools["Delegate"]("implementer", "task B", "a report")

    entry = n._registry["D002"]
    assert entry["session_started_at"] is None
    assert entry["queued_behind"] == ["D001"]
    # started_at (dispatch time) is still recorded -- only session_started_at
    # is withheld until the session actually begins.
    assert entry["started_at"] is not None

    worker.release.set()


def test_wait_block_false_reports_queued_state(tmp_path):
    worker = _BlockingWorker()
    n = _node(tmp_path, worker)
    tools = n.adapter.closure_tools

    tools["Delegate"]("implementer", "task A", "a report")
    worker.entered.wait(timeout=2)
    tools["Delegate"]("implementer", "task B", "a report")

    status = tools["Wait"]("D002", block=False)
    assert status.startswith("QUEUED behind D001"), status
    assert "has not actually started yet" in status

    worker.release.set()


def test_session_started_at_is_patched_once_the_queued_delegation_actually_runs(tmp_path):
    """Once D001 finishes and D002's own thread reaches worker.invoke(),
    _mark_session_started_if_queued patches session_started_at (registry AND
    delegation_log) even though it was None at dispatch."""
    worker = _BlockingWorker()
    n = _node(tmp_path, worker)
    tools = n.adapter.closure_tools

    tools["Delegate"]("implementer", "task A", "a report")
    worker.entered.wait(timeout=2)
    tools["Delegate"]("implementer", "task B", "a report")
    assert n._registry["D002"]["session_started_at"] is None

    worker.release.set()  # let D001 finish
    n._threads["D001"].join(timeout=5)
    # D002's thread now proceeds into invoke() (release is already set, so
    # it returns immediately) -- give it a moment to run.
    n._threads["D002"].join(timeout=5)

    assert n._registry["D002"]["session_started_at"] is not None

    records = {r["id"]: r for r in n._delegation_log.query_all()}
    assert records["D002"]["session_started_at"] is not None


def test_not_queued_when_no_same_role_delegation_is_running(tmp_path):
    """A dispatch to a role with NOTHING currently Working is the ordinary,
    not-queued case -- no false positive."""
    n = _node(tmp_path, _Stub())
    tools = n.adapter.closure_tools

    out = tools["Delegate"]("implementer", "task A", "a report", wait=True)
    assert "QUEUED" not in out
