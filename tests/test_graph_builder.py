"""Tests for build_graph() and Graph primitives."""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.runtime.graph_builder import build_graph
from adda._src.runtime.graph_state import AgenticState
from adda._src.nodes import Node, Node


class StubAdapter:
    """Test adapter that returns scripted responses."""
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.closure_tools: dict = {}

    def invoke(self, messages: list[dict]) -> str:
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
        else:
            resp = self._responses[-1] if self._responses else "No response"
        self._call_count += 1
        return resp


_GRAPH_TEST_STUDY_DIR = None


def _graph_study_dir():
    global _GRAPH_TEST_STUDY_DIR
    if _GRAPH_TEST_STUDY_DIR is None:
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp(prefix="f3dasm_graph_test_"))
        (d / "pipeline.py").write_text("# test pipeline\n")
        _GRAPH_TEST_STUDY_DIR = d
    return _GRAPH_TEST_STUDY_DIR


def make_initial_state(problem="Test problem") -> AgenticState:
    return AgenticState(
        messages=[HumanMessage(content=problem)],
        study_dir=str(_graph_study_dir()),
        done=False,
        last_report=None,
        total_delegations=0,
        budget_seconds=None,
    )


class StrategAgent(Agent):
    role = "strategizer"
    description = "Test strategizer."
    tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote"})


class ImplAgent(Agent):
    role = "implementer"
    description = "Test implementer."


def test_build_graph_creates_compiledgraph():
    """build_graph returns a compiled LangGraph graph."""
    spec = Graph(nodes={"s": ImplAgent()}, edges=(), entry="s")
    graph = build_graph(spec, lambda n, a: StubAdapter("## Done\nAll done."))

    assert hasattr(graph, "invoke")


def test_build_graph_strategizer_role_creates_strategizer_node():
    """Agent with role='strategizer' is built as Node."""
    built_nodes = {}

    class TrackingStrategizerNode(Node):
        pass

    class TrackingImplementerNode(Node):
        pass

    spec = Graph(
        nodes={"s": StrategAgent(), "i": ImplAgent()},
        edges=(Edge("s", "i"),),
        entry="s",
    )

    import adda._src.runtime.graph_builder as gb
    original_strat = gb.Node
    original_impl = gb.Node
    try:
        gb.Node = TrackingStrategizerNode
        gb.Node = TrackingImplementerNode

        adapters = {}

        def make_adapter(name, agent):
            a = StubAdapter("## Done\nAll done.")
            adapters[name] = a
            return a

        build_graph(spec, make_adapter)
    finally:
        gb.Node = original_strat
        gb.Node = original_impl


def test_build_graph_entry_node_receives_initial_message():
    """The entry node gets invoked with the initial message in state."""
    messages_seen = []

    class CapturingAdapter:
        closure_tools: dict = {}
        def invoke(self, messages):
            messages_seen.extend(messages)
            self.closure_tools["Done"](summary="Captured.")
            return "Done."

    spec2 = Graph(
        nodes={"s": StrategAgent(), "i": ImplAgent()},
        edges=(Edge("s", "i"),),
        entry="s",
    )

    s_adapter = CapturingAdapter()
    i_adapter = StubAdapter("## Report\nDone.")

    def make_adapter(name, agent):
        return s_adapter if name == "s" else i_adapter

    graph = build_graph(spec2, make_adapter, MemorySaver())
    config = {"configurable": {"thread_id": "test-1"}}
    state = make_initial_state("My initial problem")
    graph.invoke(state, config=config)

    assert any("My initial problem" in m.get("content", "") for m in messages_seen)


def test_build_graph_routes_delegate_to_implementer():
    """Node delegates to Node when Delegate closure is called."""
    strat_call_count = [0]
    impl_call_count = [0]

    class StratAdapter:
        closure_tools: dict = {}
        def invoke(self, messages):
            strat_call_count[0] += 1
            if strat_call_count[0] == 1:
                self.closure_tools["Delegate"](target="i", intent="Do something", expected_report="Report back")
                return "Delegating."
            else:
                self.closure_tools["Done"](summary="All done")
                return "Done."

    class ImplAdapter:
        closure_tools: dict = {}
        def invoke(self, messages):
            impl_call_count[0] += 1
            return "## Report\nTask complete."
        def copy(self):
            return ImplAdapter()

    spec = Graph(
        nodes={"s": StrategAgent(), "i": ImplAgent()},
        edges=(Edge("s", "i"),),
        entry="s",
    )

    graph = build_graph(
        spec,
        lambda name, a: StratAdapter() if name == "s" else ImplAdapter(),
        MemorySaver(),
    )

    config = {"configurable": {"thread_id": "test-2"}}
    result = graph.invoke(make_initial_state(), config=config)

    assert strat_call_count[0] >= 1
    assert impl_call_count[0] >= 1
    assert result["done"] is True


# ---------------------------------------------------------------------------
# Graph.to_mermaid and Graph.__repr__
# ---------------------------------------------------------------------------


def _two_node_graph() -> Graph:
    class S(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote",
                           "HypothesisList", "MilestoneList"})
        description = "Test strategizer."

    class I(Agent):
        description = "Test worker."

    return Graph(
        nodes={"orch": S(), "worker": I()},
        edges=(Edge("orch", "worker", preamble="Focus on Python."),),
        entry="orch",
    )


def test_to_mermaid_starts_with_flowchart():
    g = _two_node_graph()
    mermaid = g.to_mermaid()
    assert mermaid.startswith("flowchart TD")


def test_to_mermaid_entry_uses_stadium_shape():
    g = _two_node_graph()
    mermaid = g.to_mermaid()
    assert 'orch(["' in mermaid


def test_to_mermaid_non_entry_uses_rect_shape():
    g = _two_node_graph()
    mermaid = g.to_mermaid()
    assert 'worker["' in mermaid


def test_to_mermaid_edge_with_preamble():
    g = _two_node_graph()
    mermaid = g.to_mermaid()
    assert "Focus on Python." in mermaid
    assert "orch -->|" in mermaid


def test_to_mermaid_edge_without_preamble():
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote"})
        description = "Test strategizer."

    class B(Agent):
        description = "Test worker."

    g = Graph(nodes={"a": A(), "b": B()}, edges=(Edge("a", "b"),), entry="a")
    mermaid = g.to_mermaid()
    assert "a --> b" in mermaid
    assert "--|" not in mermaid


def test_to_mermaid_preamble_truncated_at_35():
    long_preamble = "A" * 50

    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote"})
        description = "Test strategizer."

    class B(Agent):
        description = "Test worker."

    g = Graph(
        nodes={"a": A(), "b": B()},
        edges=(Edge("a", "b", preamble=long_preamble),),
        entry="a",
    )
    mermaid = g.to_mermaid()
    assert "…" in mermaid


def test_repr_contains_entry_label():
    g = _two_node_graph()
    r = repr(g)
    assert "Graph(entry='orch')" in r
    assert "[entry]" in r


def test_repr_leaf_nodes_marked():
    g = _two_node_graph()
    r = repr(g)
    assert "(leaf)" in r


def test_repr_arrow_shows_preamble():
    g = _two_node_graph()
    r = repr(g)
    assert "Focus on Python." in r


# ---------------------------------------------------------------------------
# notes_dir grants every orchestrating node its own ledgers, not just entry.
#
# CLAUDE.md: "a delegating node is simply a node that needs help from another
# node" — there is no worker-vs-non-worker class. graph_builder used to pass
# notes_dir only to `name == spec.entry`; a node's ledger/telemetry access
# was decided by its POSITION in the graph rather than by what its Agent
# declares in `tools`. See the graph_builder.py comment for the fix.
# ---------------------------------------------------------------------------


def _capture_nodes(monkeypatch):
    """Monkeypatch graph_builder.Node so built instances are inspectable
    (build_graph returns only a compiled LangGraph graph, never the raw
    Node objects it constructed)."""
    import adda._src.runtime.graph_builder as gb

    built: dict = {}
    original = gb.Node

    class TrackingNode(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            built[kw.get("name")] = self

    monkeypatch.setattr(gb, "Node", TrackingNode)
    return built


def _three_tier_graph():
    """entry -> mid -> leaf, where `mid` itself has an outgoing edge (the
    2-tier shape no shipped study graph uses today, but that the fix must
    handle correctly)."""
    class Strat(Agent):
        role = "strategizer"
        description = "entry"
        tools = frozenset({"Done", "HypothesisPropose", "HypothesisList"})

    class Mid(Agent):
        role = "implementer"
        description = "mid-tier node with its own outgoing edge"
        tools = frozenset({"HypothesisList"})

    class Leaf(Agent):
        role = "implementer"
        description = "leaf"

    spec = Graph(
        nodes={"s": Strat(), "mid": Mid(), "leaf": Leaf()},
        edges=(Edge("s", "mid"), Edge("mid", "leaf")),
        entry="s",
    )
    return spec


def _build_three_tier(monkeypatch, tmp_path):
    from adda._src.infra.delegation_log import DelegationLog

    built = _capture_nodes(monkeypatch)
    spec = _three_tier_graph()
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    dlog = DelegationLog(tmp_path / "debug" / "delegation_log.jsonl")

    build_graph(
        spec,
        lambda n, a: StubAdapter("## Done\nAll done."),
        notes_dir=notes,
        delegation_log=dlog,
    )
    return built


def test_build_graph_non_entry_orchestrating_node_gets_ledger(monkeypatch, tmp_path):
    """A non-entry node with its own outgoing edges gets the records whose
    tools it holds (here the hypothesis ledger, so also the monitor) and
    telemetry — not None just because it isn't the entry node. It holds no
    milestone tool, so it has no milestone ledger."""
    built = _build_three_tier(monkeypatch, tmp_path)
    mid = built["mid"]

    assert mid._ledger is not None
    assert mid._milestones is None
    assert mid._science_monitor is not None
    assert mid._telemetry is not None


def test_build_graph_non_entry_node_declared_read_tools_work(monkeypatch, tmp_path):
    """mid's declared HypothesisList must actually see what the entry node
    proposed — these tools were dead (permanently None-gated) before the fix."""
    built = _build_three_tier(monkeypatch, tmp_path)
    strat, mid = built["s"], built["mid"]

    result = strat.adapter.closure_tools["HypothesisPropose"](
        "thin walls buckle first",
        "any sweep produces a feasible f >= 2.0",
        "dense sweep finds nothing below 1.5",
        0.5,
    )
    assert not result.startswith("ERROR")

    listing = mid.adapter.closure_tools["HypothesisList"]()
    assert "ERROR" not in listing
    assert "thin walls buckle first"[:10] in listing or "H1" in listing


def test_build_graph_non_entry_node_has_no_undeclared_write_path(monkeypatch, tmp_path):
    """mid owns a real ledger/milestone-ledger object now, but it never
    declared the WRITE tools — HypothesisPropose/Update/MilestoneSet must stay
    absent from its exposed closures. Write access is gated by the Agent's
    own declared `tools`, never by ledger ownership (CLAUDE.md §4: verdict
    mutation stays the strategizer's)."""
    built = _build_three_tier(monkeypatch, tmp_path)
    mid = built["mid"]

    for write_tool in (
        "HypothesisPropose", "HypothesisUpdate", "MilestoneSet",
    ):
        assert write_tool not in mid.adapter.closure_tools, write_tool


def test_build_graph_single_tier_graph_unaffected(monkeypatch, tmp_path):
    """Regression guard: every shipped graph today is single-tier (entry +
    leaves only). Node has one init path used by every node (STEP 3 of the
    leaf/orchestration merge), so a leaf's constructor DOES read notes_dir
    now — but `_owns_epistemics` stays gated on having outgoing edges
    (Node._init_orchestration), so a leaf never ACQUIRES the ledgers just
    because a path was handed to it: `_ledger` exists (unlike before the
    merge) but is None, and reads still resolve through the delegation-log
    fallback (Node._read_ledger)."""
    from adda._src.infra.delegation_log import DelegationLog

    built = _capture_nodes(monkeypatch)
    spec = _two_node_graph()
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    dlog = DelegationLog(tmp_path / "debug" / "delegation_log.jsonl")

    build_graph(
        spec,
        lambda n, a: StubAdapter("## Done\nAll done."),
        notes_dir=notes,
        delegation_log=dlog,
    )

    entry, worker = built["orch"], built["worker"]
    assert entry._owns_epistemics is True
    assert entry._ledger is not None
    assert worker._owns_epistemics is False
    assert worker._ledger is None


def test_to_mermaid_styles_nodes_and_edges():
    """Styled mermaid: classDef per agent class, class assignments,
    and dotted (consultation) arrows from non-entry sources."""
    from adda._src.agents._graphs import _default_graph
    m = _default_graph().to_mermaid()
    # colour styling present
    assert "classDef StrategizerAgent fill:#" in m
    assert "class strategizer StrategizerAgent" in m
    # entry-sourced edge is a solid delegation arrow
    assert "strategizer --> critic" in m
    # non-entry-sourced edge (specialist consults lit) is dotted
    assert "datagenerator -.-> literature_reviewer" in m
    assert "implementer -.-> literature_reviewer" in m
    # labels carry class + role/description
    assert "<b>strategizer</b><br/>StrategizerAgent" in m
