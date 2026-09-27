"""Regression: a degraded (BM25-only) ConsultLiterature used to leave no
trace anywhere but a log.warning() — invisible to diagnostics.jsonl and to
the agent doing the searching (the benchmarks session on Oscar hit this
inside a bubblewrap sandbox and found it only in orchestrator logs).

LiteratureCorpus now records WHY on `embedder_fallback_reason` and surfaces
it once per corpus instance via `pop_diagnostic_event()`. ConsultLiterature
tags itself with its corpus (`_adda_diagnostic_source`); Node._init_capabilities
re-wraps any closure tool carrying that tag through orchestration.py's
_wrap_closure — the one place with both a per-run diagnostics path and,
via the tag, a handle back to the corpus.
"""
from __future__ import annotations

import json

from adda._src.agents.literature_tools.corpus import build_corpus_read_closures
from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra.delegation_log import DelegationLog
from adda._src.literature.literature_corpus import LiteratureCorpus
from adda._src.nodes import Node


class _StubAdapter:
    def __init__(self, closure_tools=None) -> None:
        self.closure_tools: dict = dict(closure_tools or {})
        self.last_usage: dict = {}
        self.model = "m"
        self.native_tools: list = []

    def invoke(self, messages):
        return ""


def _node_with_tools(tmp_path, closure_tools):
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done"})
        description = "strategizer"

    class B(Agent):
        role = "implementer"
        description = "implementer"

    nodes = {"strategizer": A(), "implementer": B()}
    spec = Graph(
        nodes=nodes, edges=(Edge("strategizer", "implementer"),),
        entry="strategizer",
    )
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    dlog = DelegationLog(tmp_path / "debug" / "delegation_log.jsonl")
    return Node(
        _StubAdapter(closure_tools), name="strategizer",
        outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": _StubAdapter({})},
        notes_dir=notes, delegation_log=dlog,
    )


def _diagnostics_events(tmp_path):
    path = tmp_path / "debug" / "diagnostics.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_pop_diagnostic_event_is_one_shot():
    corpus = LiteratureCorpus.__new__(LiteratureCorpus)
    corpus._degradation_reported = False
    corpus.embedder_fallback_reason = None
    assert corpus.pop_diagnostic_event() is None  # nothing wrong yet

    corpus.embedder_fallback_reason = "fastembed unavailable in this sandbox"
    ev = corpus.pop_diagnostic_event()
    assert ev is not None
    kind, msg = ev
    assert kind == "RETRIEVAL_DEGRADED"
    assert "fastembed unavailable in this sandbox" in msg

    # Same corpus instance, still degraded: reported only once.
    assert corpus.pop_diagnostic_event() is None


def test_consult_literature_reports_degradation_once(tmp_path):
    corpus = LiteratureCorpus(tmp_path / "corpus")
    corpus.embedder_fallback_reason = "fastembed unavailable in this sandbox"
    tools = build_corpus_read_closures(corpus)
    n = _node_with_tools(tmp_path, tools)

    out1 = n.adapter.closure_tools["ConsultLiterature"]()
    assert "dense retrieval is unavailable" in out1
    assert "fastembed unavailable in this sandbox" in out1

    events = _diagnostics_events(tmp_path)
    assert len(events) == 1
    assert events[0]["error_type"] == "RETRIEVAL_DEGRADED"
    assert events[0]["fault"] == "nudge"  # environment fact, not an agent error

    # Second call: still degraded, but already reported — no repeat notice,
    # no repeat diagnostics event (boss: "checks that once per run").
    out2 = n.adapter.closure_tools["ConsultLiterature"]()
    assert "dense retrieval is unavailable" not in out2
    assert len(_diagnostics_events(tmp_path)) == 1


def test_consult_literature_silent_when_embedder_healthy(tmp_path):
    corpus = LiteratureCorpus(tmp_path / "corpus")
    assert corpus.embedder_fallback_reason is None
    tools = build_corpus_read_closures(corpus)
    n = _node_with_tools(tmp_path, tools)

    out = n.adapter.closure_tools["ConsultLiterature"]()
    assert "dense retrieval is unavailable" not in out
    assert _diagnostics_events(tmp_path) == []
