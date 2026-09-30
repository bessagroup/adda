"""runtime.max_awake_nodes: a run-wide cap on awake worker sessions.

Same-role delegations get their own adapter copy and run concurrently; what
bounds concurrency is the cap. Over it a delegation is QUEUED (FIFO) with the
reason, and a node blocked in Wait on its own queued child gives its slot back
so the child can run.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra.delegation_log import DelegationLog
from adda._src.nodes import Node
from adda._src.nodes.slots import AwakeSlots

REPORT = ("## Report\n### Actions taken\n" + "nothing further to do here. " * 4
          + "\n### Conclusions\nok\n### Numbers\nn: 0")


@pytest.fixture(autouse=True)
def _no_milestone_gate():
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
        return ""


class _Worker:
    """Each invoke blocks on its own gate (by call order) unless open."""

    def __init__(self, body=None):
        self.last_usage: dict = {}
        self.closure_tools: dict = {}
        self.started: list[str] = []
        self.gates: dict[int, threading.Event] = {}
        self._n = 0
        self._lock = threading.Lock()
        self.body = body

    def gate(self, i: int) -> threading.Event:
        with self._lock:
            return self.gates.setdefault(i, threading.Event())

    def invoke(self, messages, on_session_start=None, on_session_end=None):
        with self._lock:
            self._n += 1
            i = self._n
        from adda._src.backends.base import get_delegation_id
        self.started.append(get_delegation_id())
        if self.body is not None:
            self.body(i)
        else:
            self.gate(i).wait(timeout=10)
        return REPORT


def _node(tmp_path, worker, max_awake):
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "Wait"})
        description = "s"

    class B(Agent):
        role = "implementer"
        description = "i"

    spec = Graph(nodes={"strategizer": A(), "implementer": B()},
                 edges=(Edge("strategizer", "implementer"),),
                 entry="strategizer")
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n = Node(_Stub(), name="strategizer", outgoing=["implementer"], spec=spec,
             worker_adapters={"implementer": worker}, notes_dir=notes,
             delegation_log=DelegationLog(tmp_path / "dlog.jsonl"))
    n._awake_slots = AwakeSlots(max_awake)
    n._peers = {"strategizer": n}
    return n


def _until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_over_the_cap_a_delegation_is_queued_with_the_reason_and_starts_fifo(
        tmp_path):
    w = _Worker()
    n = _node(tmp_path, w, max_awake=2)  # strategizer + ONE worker
    tools = n.adapter.closure_tools

    assert "Delegation started" in tools["Delegate"]("implementer", "A", "r")
    assert _until(lambda: w.started == ["D001"])
    out2 = tools["Delegate"]("implementer", "B", "r")
    out3 = tools["Delegate"]("implementer", "C", "r")
    assert "QUEUED: too many nodes working (2/2" in out2, out2
    assert "QUEUED" in out3
    assert n._registry["D002"]["session_started_at"] is None
    assert w.started == ["D001"]

    w.gate(1).set()
    assert _until(lambda: w.started == ["D001", "D002"])  # FIFO: B before C
    assert n._registry["D003"]["session_started_at"] is None
    w.gate(2).set()
    assert _until(lambda: w.started == ["D001", "D002", "D003"])
    assert n._registry["D002"]["session_started_at"] is not None
    w.gate(3).set()
    for d in ("D001", "D002", "D003"):
        n._threads[d].join(timeout=5)


def test_a_parent_waiting_on_its_queued_child_gives_its_slot_back(tmp_path):
    """cap=3 -> 2 worker slots. A holds one, C the other; A's child B queues.
    A's Wait must release A's slot so B runs, then A resumes."""
    finished = {}

    def body(i):
        if i == 2:  # A
            try:
                tools = node.adapter.closure_tools
                finished["spawn"] = tools["Delegate"]("implementer", "child", "r")
                finished["A_wait"] = tools["Wait"]("D003")
            except BaseException as e:  # noqa
                finished["err"] = repr(e)
        elif i == 1:  # C stays busy until the end
            w.gate(1).wait(timeout=10)
        # i == 3: B returns at once

    w = _Worker(body)
    node = _node(tmp_path, w, max_awake=3)
    tools = node.adapter.closure_tools
    tools["Delegate"]("implementer", "C", "r")
    assert _until(lambda: w.started[:1] == ["D001"])
    tools["Delegate"]("implementer", "A", "r")

    assert _until(lambda: "A_wait" in finished, timeout=8), (
        f"deadlock: A held its slot while waiting on its queued child {finished}")
    assert "err" not in finished, finished
    assert "QUEUED" in finished["spawn"], finished
    assert _until(lambda: node._registry["D003"]["status"] != "QUEUED")
    assert node._registry["D003"]["session_started_at"] is not None
    w.gate(1).set()
    for d in ("D001", "D002"):
        node._threads[d].join(timeout=5)
    assert node._awake_slots.capacity == 2


def test_the_strategizer_wakes_on_an_operator_note_while_worker_slots_are_full(
        tmp_path):
    w = _Worker()
    n = _node(tmp_path, w, max_awake=2)
    tools = n.adapter.closure_tools
    tools["Delegate"]("implementer", "A", "r")
    assert _until(lambda: w.started == ["D001"])
    tools["Delegate"]("implementer", "B", "r")  # queued: every slot is taken
    n._drain_operator_notes = lambda: "operator: stop"
    t0 = time.time()
    out = tools["Wait"]()
    assert time.time() - t0 < 3
    assert "operator: stop" in out and "woken early" in out
    w.gate(1).set()
    w.gate(2).set()
    for d in ("D001", "D002"):
        n._threads[d].join(timeout=5)


def test_slots_are_fifo_and_priority_reentry_beats_new_spawns():
    s = AwakeSlots(2)  # one worker slot
    assert s.reserve("D1") is None
    assert "too many nodes working" in s.reserve("D2")
    assert s.reserve("D3") is not None
    s.release("D1")
    assert s.holds("D2") and not s.holds("D3")
    assert s.reserve("D4", priority=True) is not None
    s.release("D2")
    assert s.holds("D4") and not s.holds("D3")


def test_each_delegation_gets_its_own_adapter_and_tools(tmp_path):
    from adda._src.backends.claude import ClaudeAdapter
    a = ClaudeAdapter(model="m", system_prompt="s", closure_tools={"k": 1})
    b, c = a.copy(), a.copy()
    b.closure_tools["ReportEvals"] = "bound-to-D001"
    c.closure_tools["ReportEvals"] = "bound-to-D002"
    assert b.closure_tools["ReportEvals"] != c.closure_tools["ReportEvals"]
    assert "ReportEvals" not in a.closure_tools
    assert b._lock is not c._lock


def test_two_implementers_appending_at_once_lose_nothing_and_dedup(tmp_path):
    """Both write the canonical store concurrently (dedup_scope=all, so the
    overlap is dropped, not doubled), with a RunScratch-style integrity guard
    open over the whole thing and no delegation Working: the guard must not
    revert either campaign's append (59cea31)."""
    from f3dasm import ExperimentData
    from f3dasm._src.core import DataGenerator
    from f3dasm._src.experimentsample import ExperimentSample, JobStatus

    from adda._src.evaluation.instrumented import InstrumentedDataGenerator
    from adda._src.nodes.tools.routing.notebook import NotebookTools

    class _Sum(DataGenerator):
        def execute(self, experiment_sample: ExperimentSample, **kw):
            experiment_sample._output_data["f"] = sum(
                experiment_sample._input_data.values())
            experiment_sample.job_status = JobStatus.FINISHED
            return experiment_sample

    def campaign(did, xs):
        gen = InstrumentedDataGenerator(
            inner=_Sum(), store_dir=tmp_path, delegation_id=did,
            flush_every=1, dedup_scope="all")
        for x in xs:
            gen.execute(ExperimentSample(
                _input_data={"x0": x}, _output_data={},
                job_status=JobStatus.OPEN))
        gen.flush()

    node = SimpleNamespace(
        _resolve_run_dir=lambda: Path(tmp_path), _study_dir=None,
        _registry_lock=threading.Lock(),
        _registry={"D001": {"status": "Working"}})
    tools = NotebookTools(node)
    a = [round(0.01 * i, 2) for i in range(0, 8)]
    b = [round(0.01 * i, 2) for i in range(4, 12)]  # 4 designs overlap
    campaign("D000", [5.0])  # the store exists before the guard fingerprints it
    with tools._canonical_integrity_guard() as changed:
        ts = [threading.Thread(target=campaign, args=("D001", a)),
              threading.Thread(target=campaign, args=("D002", b))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    df_in, _ = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    xs = sorted(round(float(v), 2) for v in df_in["x0"])
    assert xs == sorted([5.0] + [round(0.01 * i, 2) for i in range(12)]), xs
    assert changed[0] is None or changed[0]["reverted"] == []
