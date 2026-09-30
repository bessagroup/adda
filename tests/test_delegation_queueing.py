"""Delegate() must be truthful at dispatch time: a delegation that has to wait
for an awake-node slot (runtime.max_awake_nodes) is reported QUEUED, with the
reason, and its registry/log session_started_at stays None until it starts.
(Run 20260928T141126: D003 was logged as started while it sat blocked behind
another same-role delegation.)

Driven from the delegation REGISTRY with a slow stub worker; see also
tests/test_max_awake_nodes.py for the cap itself.
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

    def invoke(self, messages, on_session_start=None, on_session_end=None):
        with self._lock:
            if on_session_start is not None:
                on_session_start()
            self.entered.set()
            self.release.wait(timeout=5)
            return "## Report\n### Actions taken\nnone\n### Conclusions\nok\n### Numbers\nn: 0"


def _node(tmp_path, worker, max_awake=None):
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
    node = Node(
        _Stub(), name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": worker}, notes_dir=notes,
        delegation_log=DelegationLog(tmp_path / "dlog.jsonl"))
    if max_awake is not None:
        from adda._src.nodes.slots import AwakeSlots
        node._awake_slots = AwakeSlots(max_awake)
    return node


def test_second_same_role_delegation_is_reported_queued(tmp_path):
    worker = _BlockingWorker()
    n = _node(tmp_path, worker, max_awake=2)
    tools = n.adapter.closure_tools

    out1 = tools["Delegate"]("implementer", "task A", "a report")
    assert "Delegation started" in out1
    assert worker.entered.wait(timeout=2), "D001 never reached invoke()"

    # D001 is genuinely inside invoke() (blocked), still "Working" in the
    # registry -- a second dispatch to the SAME role must be truthful.
    out2 = tools["Delegate"]("implementer", "task B", "a report")
    assert "QUEUED: too many nodes working" in out2, out2

    worker.release.set()


def test_queued_delegations_registry_entry_has_no_session_start_yet(tmp_path):
    worker = _BlockingWorker()
    n = _node(tmp_path, worker, max_awake=2)
    tools = n.adapter.closure_tools

    tools["Delegate"]("implementer", "task A", "a report")
    worker.entered.wait(timeout=2)
    tools["Delegate"]("implementer", "task B", "a report")

    entry = n._registry["D002"]
    assert entry["session_started_at"] is None
    assert "too many nodes working" in entry["queue_reason"]
    # started_at (dispatch time) is still recorded -- only session_started_at
    # is withheld until the session actually begins.
    assert entry["started_at"] is not None

    worker.release.set()


def test_wait_block_false_reports_queued_state(tmp_path):
    worker = _BlockingWorker()
    n = _node(tmp_path, worker, max_awake=2)
    tools = n.adapter.closure_tools

    tools["Delegate"]("implementer", "task A", "a report")
    worker.entered.wait(timeout=2)
    tools["Delegate"]("implementer", "task B", "a report")

    status = tools["Wait"]("D002", block=False)
    assert status.startswith("QUEUED: too many nodes working"), status
    assert "has not actually started yet" in status

    worker.release.set()


def test_session_started_at_is_patched_once_the_queued_delegation_actually_runs(tmp_path):
    """Once D001 finishes and D002's own thread reaches worker.invoke(),
    _mark_session_started_if_queued patches session_started_at (registry AND
    delegation_log) even though it was None at dispatch."""
    worker = _BlockingWorker()
    n = _node(tmp_path, worker, max_awake=2)
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


class _SessionWorker:
    """Mirrors ClaudeAdapter's session-id mechanics: ONE shared adapter, a
    real _lock, and last_session_id written at the END of each turn, inside
    the lock, as shared adapter state. Each call gets its own session id
    (S1, S2, ...) and reports it through on_session_end from inside the
    lock, like the real backend."""

    def __init__(self):
        self.last_usage: dict = {}
        self.closure_tools: dict = {}
        self.last_session_id = None
        self._lock = threading.Lock()
        self._n = 0
        self.by_thread: dict[int, str] = {}
        self.usage_by_call: list[dict] = []
        self._first_thread: int | None = None
        self.second_turn_done = threading.Event()

    def invoke(self, messages, on_session_start=None, on_session_end=None):
        with self._lock:
            if on_session_start is not None:
                on_session_start()
            self._n += 1
            sid = f"S{self._n}"
            self.last_session_id = sid
            tid = threading.get_ident()
            self.by_thread[tid] = sid  # the LAST session this thread ran
            if self._first_thread is None:
                self._first_thread = tid
            elif tid != self._first_thread:
                self.second_turn_done.set()
            self.last_usage = {"input_tokens": 100 * self._n,
                               "output_tokens": 10 * self._n,
                               "total_cost_usd": 0.01 * self._n}
            self.usage_by_call.append(dict(self.last_usage))
            if on_session_end is not None:
                on_session_end(self.last_session_id, dict(self.last_usage))
            return ("## Report\n### Actions taken\n"
                    + "nothing further to do here. " * 4
                    + "\n### Conclusions\nok\n### Numbers\nn: 0")


def test_each_review_stores_its_own_session_id_not_the_shared_adapters(
        tmp_path, monkeypatch):
    """A same-role delegation queued behind another can run a WHOLE turn
    between the first one's invoke() returning (lock released) and the
    first one opening its review. Reading worker.last_session_id at that
    point stored the SECOND delegation's session id on the first's review,
    so a revision round would silently resume the wrong worker's session."""
    from adda._src.nodes.tools.routing.delegation import WorkerSession

    worker = _SessionWorker()
    n = _node(tmp_path, worker)
    tools = n.adapter.closure_tools

    real_reconcile = WorkerSession._reconcile_evals

    def _reconcile_after_d002_ran(self):
        # Whichever delegation won the (non-fair) lock and ran first reaches
        # its post-invoke bookkeeping only after the other's whole turn has
        # completed and overwritten the shared last_session_id.
        if worker.by_thread.get(threading.get_ident()) == "S1":
            assert worker.second_turn_done.wait(timeout=5)
        return real_reconcile(self)

    monkeypatch.setattr(WorkerSession, "_reconcile_evals",
                        _reconcile_after_d002_ran)

    worker._lock.acquire()  # hold the adapter so D001 then D002 both queue
    tools["Delegate"]("implementer", "task A", "a report")
    tools["Delegate"]("implementer", "task B", "a report")
    worker._lock.release()
    n._threads["D001"].join(timeout=10)
    n._threads["D002"].join(timeout=10)

    own = {d: worker.by_thread[n._threads[d].ident] for d in ("D001", "D002")}
    assert own["D001"] != own["D002"]
    # the shared state the first runner must NOT have read: it belongs to
    # the second runner's turn
    first = next(d for d in own if own[d] == "S1")
    second = next(d for d in own if d != first)
    assert worker.last_session_id == own[second] != own[first]
    for d in ("D001", "D002"):
        assert n._registry[d]["session_id"] == own[d], (d, own)


def test_recorded_usage_sums_each_invokes_own_usage_not_the_shared_adapters(
        tmp_path, monkeypatch):
    """The run's token/cost TOTAL is what lands in run_ledger. Reading
    worker.last_usage after the lock is released let a queued neighbour's
    turn overwrite it, so one delegation's usage was counted twice and the
    other's lost. Here D001's post-invoke bookkeeping is held (at its first
    step, just before usage is taken) until D002's whole turn has run (the exact interleaving that corrupts the shared
    value); the node's totals must still equal the sum of every invoke's
    own usage."""
    from adda._src.nodes.tools.routing.delegation import WorkerSession

    worker = _SessionWorker()
    n = _node(tmp_path, worker)
    tools = n.adapter.closure_tools

    real_nudges = WorkerSession._record_oracle_nudges

    def _nudges_after_d002_ran(self):
        # the first post-invoke step, right before usage is read
        if self.delegation_id == "D001":
            assert worker.second_turn_done.wait(timeout=5)
        return real_nudges(self)

    monkeypatch.setattr(WorkerSession, "_record_oracle_nudges",
                        _nudges_after_d002_ran)

    worker._lock.acquire()
    tools["Delegate"]("implementer", "task A", "a report")
    tools["Delegate"]("implementer", "task B", "a report")
    worker._lock.release()
    n._threads["D001"].join(timeout=10)
    n._threads["D002"].join(timeout=10)

    calls = worker.usage_by_call
    assert len(calls) == 2, calls
    totals = n._token_totals
    assert totals["input_tokens"] == sum(c["input_tokens"] for c in calls)
    assert totals["output_tokens"] == sum(c["output_tokens"] for c in calls)
    assert totals["total_cost_usd"] == pytest.approx(
        sum(c["total_cost_usd"] for c in calls))
    # and each delegation's own row carries its own invoke's usage
    by_id = {d: n._registry[d]["usage"] for d in ("D001", "D002")}
    assert {u["input_tokens"] for u in by_id.values()} == {100, 200}


def test_usage_sums_across_a_delegations_own_invokes(tmp_path):
    """A delegation that invokes more than once (malformed report -> one
    corrective retry) spent tokens on BOTH; worker.last_usage held only the
    last one."""
    worker = _SessionWorker()
    n = _node(tmp_path, worker)
    tools = n.adapter.closure_tools
    worker_short = "## Report\n### Actions taken\nx\n### Conclusions\nok\n### Numbers\nn: 0"
    real_invoke = worker.invoke
    calls = {"n": 0}

    def _first_short_then_real(messages, **kw):
        calls["n"] += 1
        text = real_invoke(messages, **kw)
        return worker_short if calls["n"] == 1 else text

    worker.invoke = _first_short_then_real
    tools["Delegate"]("implementer", "task A", "a report", wait=True)
    assert len(worker.usage_by_call) == 2
    assert n._token_totals["input_tokens"] == sum(
        c["input_tokens"] for c in worker.usage_by_call)
