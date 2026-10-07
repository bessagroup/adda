"""An interrupted run returns promptly even when a node is stuck in a blocking
call (e.g. the strategizer in Wait, joined on a delegation inside an LLM read).

Before the fix, the exception unwound into LangGraph's thread pool, whose
shutdown joined the stuck node thread: the run (and the process) stayed alive
until that thread's blocking call came back on its own.
"""
from __future__ import annotations

import _thread
import json
import threading
import time
from pathlib import Path

import pytest

from adda._src.runtime.agent_runtime import AgenticRun

_BOUND_S = 15.0


def _study(tmp_path: Path) -> Path:
    study = tmp_path / "study"
    study.mkdir()
    (study / "PROBLEM_STATEMENT.md").write_text("# trivial\n")
    return study


def _blocking_graph(release: threading.Event, entered: threading.Event):
    from langgraph.graph import END, START, StateGraph

    def stuck(state):
        entered.set()
        release.wait()
        return {}

    g = StateGraph(dict)
    g.add_node("stuck", stuck)
    g.add_edge(START, "stuck")
    g.add_edge("stuck", END)
    return g.compile()


def test_interrupt_returns_while_a_node_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("adda._src.runtime.agent_runtime._ABANDON_GRACE_S", 1.0)
    release, entered = threading.Event(), threading.Event()
    run = AgenticRun(study_dir=_study(tmp_path), interactive=False)
    run._graph = _blocking_graph(release, entered)

    fired: dict = {}

    def interrupt():
        entered.wait(30)
        fired["t"] = time.monotonic()
        _thread.interrupt_main()

    threading.Thread(target=interrupt, daemon=True).start()
    # Safety net so a regression fails this test instead of hanging the suite.
    threading.Timer(_BOUND_S + 15, release.set).start()
    try:
        with pytest.raises(KeyboardInterrupt):
            run.execute()
        elapsed = time.monotonic() - fired["t"]
    finally:
        release.set()
    assert elapsed < _BOUND_S, (
        f"the interrupted run took {elapsed:.0f}s to return; it waited for "
        "the blocked node")
    diag = next((tmp_path / "study" / "runs").glob("*/debug/diagnostics.jsonl"))
    rows = [json.loads(line) for line in diag.read_text().splitlines()]
    row = next(r for r in rows if r["error_type"] == "RUN_ABANDONED")
    assert row["detail"]["graph_thread_alive"] is True


def _node():
    from adda._src.backends.base import Agent, Edge, Graph
    from adda._src.nodes import Node

    class _Stub:
        def __init__(self) -> None:
            self.closure_tools: dict = {}
            self.last_usage: dict = {}
            self.model = "m"

        def invoke(self, messages):
            return ""

    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "WriteNote", "ReadNote", "Wait"})
        description = "strategizer"

    class B(Agent):
        role = "implementer"
        description = "implementer"

    spec = Graph(nodes={"strategizer": A(), "implementer": B()},
                 edges=(Edge("strategizer", "implementer"),),
                 entry="strategizer")
    return Node(_Stub(), name="strategizer", outgoing=["implementer"],
                spec=spec, worker_adapters={"implementer": _Stub()})


@pytest.mark.parametrize("target", ["D001", None])
def test_wait_ends_when_the_run_abandons_the_node(target, monkeypatch):
    monkeypatch.setattr(
        "adda._src.nodes.tools.routing.delegation._NOTICE_POLL_S", 0.3)
    from adda._src.infra.run_abandon import RunAbandoned

    n = _node()
    hold = threading.Event()
    t = threading.Thread(target=hold.wait, daemon=True, name="D001")
    t.start()
    n._registry["D001"] = {"status": "Working", "result": None, "evals": 0,
                           "parent": "entry", "target": "implementer"}
    n._threads["D001"] = t
    seen: dict = {}

    def call():
        try:
            n.adapter.closure_tools["Wait"](target)
        except RunAbandoned as exc:
            seen["raised"] = exc

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    time.sleep(0.5)
    assert caller.is_alive()
    n._abandon.set()
    caller.join(timeout=_BOUND_S)
    hold.set()
    assert not caller.is_alive(), "Wait kept blocking after the run abandoned it"
    assert "raised" in seen


def test_tool_call_after_abandon_raises():
    from adda._src.infra.run_abandon import RunAbandoned

    n = _node()
    n._abandon.set()
    with pytest.raises(RunAbandoned):
        n.adapter.closure_tools["ReadNote"]("x")
