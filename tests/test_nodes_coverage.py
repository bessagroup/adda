"""Coverage tests for nodes.py — Node, WriteDeliverable, RecallHistory,
hypothesis closures, budget warnings, and other uncovered lines."""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from adda._src.backends.base import Agent, Edge, Graph


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class StubAdapter:
    """Minimal adapter stub."""

    def __init__(self, response: str = "## Report\n### Actions taken\nDone.\n"
                 "### Files touched\n(none)\n### Conclusions\nOK\n### Numbers\nn: 0") -> None:
        self._response = response
        self.closure_tools: dict = {}
        self.last_usage: dict = {}

    def invoke(self, messages: list[dict]) -> str:
        return self._response


def _make_state(study_dir=None, **kwargs):
    from adda._src.runtime.graph_state import AgenticState
    if study_dir is None:
        d = Path(tempfile.mkdtemp(prefix="f3dasm_nodes_cov_"))
        (d / "pipeline.py").write_text("# test\n")
        study_dir = d
    return AgenticState(
        messages=[HumanMessage(content="Test problem")],
        study_dir=str(study_dir),
        done=False,
        last_report=None,
        total_delegations=0,
        budget_seconds=kwargs.pop("budget_seconds", None),
        **kwargs,
    )


def _minimal_spec(name: str = "strategizer", target: str = "implementer") -> Graph:
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote", "WriteDeliverable", "WriteCell", "HypothesisPropose", "HypothesisUpdate", "HypothesisList", "MilestoneList", "MilestoneSet", "QueryStore"})
        description = "Test strategizer."

    class B(Agent):
        description = "Test implementer."

    return Graph(
        nodes={name: A(), target: B()},
        edges=(Edge(name, target),),
        entry=name,
    )


# ---------------------------------------------------------------------------
# Node basic invocation
#
# test_worker_node_returns_command and test_worker_node_retry_on_malformed_
# response used to drive a bare `Node(adapter, name="implementer")` (no
# outgoing edges) through `_respond` — the single-turn "answer once, hand
# back to return_to" behaviour. `_respond` and `return_to` are gone (STEP 3
# of the leaf/orchestration merge): every node now runs the same
# delegate-or-close loop, which does not accept a bare text reply as a
# finished report at all (it requires an accepted Done()). Both tests
# asserted a routing model production never used (return_to is always
# None — agent_runtime.py sets it that way; the tests supplied "strategizer"
# or END by hand), so they are deleted rather than forced onto the unified
# loop. The real per-delegation retry-on-malformed-report path
# (WorkerSession._invoke_with_report_retry, delegation.py) is unchanged by
# this refactor; _classify_response's own behaviour (what "malformed" means)
# is unit-tested directly further down this file.
# ---------------------------------------------------------------------------


def test_worker_node_reports_evals(tmp_path):
    """Node's ReportEvals closure records the count, callable directly.

    Construction wires this closure onto the node's adapter for every node,
    delegation-capable or not (Node._init_capabilities) — a dispatched
    worker's closure_tools IS this same dict (ClaudeAdapter.copy() returns
    self; see nodes/node.py's module docstring), so this closure's behaviour
    matters independently of whichever node happens to invoke it.
    """
    from adda._src.nodes import Node

    node = Node(StubAdapter(), name="implementer")

    result = node.adapter.closure_tools["ReportEvals"](42)

    assert "ERROR" not in result
    assert node._evals_reported.get("count") == 42


# ---------------------------------------------------------------------------
# Node sandboxed Write
# ---------------------------------------------------------------------------


def test_worker_node_sandboxed_write_allows_workspace(tmp_path):
    """Node Write closure allows writes inside workspace_dir, callable directly."""
    from adda._src.nodes import Node

    workspace = tmp_path / "ws"
    workspace.mkdir()

    node = Node(StubAdapter(), name="implementer", workspace_dir=workspace)

    result = node.adapter.closure_tools["Write"]("output.txt", "hello")

    assert "ERROR" not in result
    assert (workspace / "output.txt").exists()


def test_worker_node_sandboxed_write_rejects_escape(tmp_path):
    """Node Write closure rejects paths outside workspace_dir."""
    from adda._src.nodes import Node

    workspace = tmp_path / "ws"
    workspace.mkdir()

    node = Node(StubAdapter(), name="implementer", workspace_dir=workspace)

    result = node.adapter.closure_tools["Write"]("../../etc/passwd", "hack")

    assert "ERROR" in result
    assert "outside" in result.lower() or "rejected" in result.lower()


# ---------------------------------------------------------------------------
# build_sandboxed_write: the study workspace/ allowance (Elvis, option A —
# report 1, run 20260926T214835: a worker had no sanctioned way to satisfy a
# problem statement naming a deliverable path under study workspace/, and
# reached for Bash instead, which this guard cannot see at all).
# ---------------------------------------------------------------------------

def _build_write(tmp_path, *, with_study_workspace=True):
    from adda._src.nodes.tools.routing.delegation import build_sandboxed_write

    study_dir = tmp_path / "study"
    delegation_dir = study_dir / "runs" / "T1" / "debug" / "delegations" / "D001"
    delegation_dir.mkdir(parents=True)
    study_ws = study_dir / "workspace"
    return build_sandboxed_write(
        delegation_dir,
        strip_prefix="D001",
        scope_label="D001/",
        study_workspace=study_ws if with_study_workspace else None,
    ), study_dir, delegation_dir, study_ws


def test_write_reaches_the_study_workspace_with_a_relative_path(tmp_path):
    Write, study_dir, _delegation_dir, study_ws = _build_write(tmp_path)
    result = Write("workspace/km_baseline.m", "function y = f(x)\nend\n")
    assert "ERROR" not in result
    assert (study_ws / "km_baseline.m").exists()


def test_write_reaches_the_study_workspace_with_an_absolute_path(tmp_path):
    Write, study_dir, _delegation_dir, study_ws = _build_write(tmp_path)
    abs_path = str(study_ws / "lcp_model.m")
    result = Write(abs_path, "function y = g(x)\nend\n")
    assert "ERROR" not in result
    assert (study_ws / "lcp_model.m").exists()


def test_write_still_reaches_its_own_delegation_directory(tmp_path):
    Write, _study_dir, delegation_dir, _study_ws = _build_write(tmp_path)
    result = Write("D001/result.csv", "col,val\n1,2\n")
    assert "ERROR" not in result
    assert (delegation_dir / "result.csv").exists()


def test_write_rejects_a_sibling_runs_directory(tmp_path):
    Write, study_dir, _delegation_dir, _study_ws = _build_write(tmp_path)
    other_run = study_dir / "runs" / "T2" / "debug" / "delegations" / "D001"
    other_run.mkdir(parents=True)
    result = Write(str(other_run / "leak.txt"), "leaked")
    assert result.startswith("ERROR")
    assert not (other_run / "leak.txt").exists()


def test_write_rejects_the_study_root_itself(tmp_path):
    Write, study_dir, _delegation_dir, _study_ws = _build_write(tmp_path)
    result = Write(str(study_dir / "PROBLEM_STATEMENT.md"), "tampered")
    assert result.startswith("ERROR")
    assert not (study_dir / "PROBLEM_STATEMENT.md").exists()


def test_write_rejects_dotdot_escape_through_workspace_prefix(tmp_path):
    """'workspace/../../secret' must not escape the study root via the new
    workspace/ allowance — '..' is collapsed by resolve() before the
    relative_to check, same as the pre-existing delegation-dir escape guard."""
    Write, study_dir, _delegation_dir, _study_ws = _build_write(tmp_path)
    outside = study_dir.parent / "secret.txt"
    result = Write("workspace/../../secret.txt", "escaped")
    assert result.startswith("ERROR")
    assert not outside.exists()


def test_write_rejects_symlink_escape_from_study_workspace(tmp_path):
    Write, study_dir, _delegation_dir, study_ws = _build_write(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    study_ws.mkdir(parents=True)
    link = study_ws / "escape"
    link.symlink_to(outside)
    result = Write("workspace/escape/pwned.txt", "hacked")
    assert result.startswith("ERROR")
    assert not (outside / "pwned.txt").exists()


def test_write_rejection_message_is_honest_about_allowed_targets(tmp_path):
    """No implied sandbox the toolset lacks, and no mention of Bash as a
    workaround — just the two directories Write actually permits."""
    Write, study_dir, _delegation_dir, study_ws = _build_write(tmp_path)
    result = Write(str(study_dir.parent / "elsewhere.txt"), "x")
    assert "D001/" in result
    assert str(study_ws) in result
    assert "bash" not in result.lower()


def test_write_without_study_workspace_configured_is_unchanged(tmp_path):
    """A leaf/scope built with no study_workspace= at all (the pre-fix
    call shape) behaves exactly as before — no accidental broadening."""
    Write, _study_dir, _delegation_dir, study_ws = _build_write(
        tmp_path, with_study_workspace=False)
    result = Write(f"{study_ws}/km_baseline.m", "x")
    assert result.startswith("ERROR")
    assert not study_ws.exists()


# ---------------------------------------------------------------------------
# Node RecallHistory
# ---------------------------------------------------------------------------


def test_worker_node_recall_history_with_delegation_log(tmp_path):
    """Node.RecallHistory returns prior delegations from the log, callable directly."""
    from adda._src.nodes import Node
    from adda._src.infra.delegation_log import DelegationLog

    log_path = tmp_path / "delegation_log.jsonl"
    log = DelegationLog(log_path)
    log.record(
        id="D001",
        from_node="strategizer",
        to_node="implementer",
        task="First task",
        deliverable="First deliverable.",
        hypothesis_ids=[],
        started_at="2024-01-01T12:00:00+00:00",
        completed_at="2024-01-01T12:05:00+00:00",
        status="DONE",
    )

    node = Node(StubAdapter(), name="implementer", delegation_log=log)

    result = node.adapter.closure_tools["RecallHistory"](5)

    assert "First task" in result


def test_worker_node_recall_history_empty(tmp_path):
    """Node.RecallHistory returns no-records message when log is empty."""
    from adda._src.nodes import Node
    from adda._src.infra.delegation_log import DelegationLog

    log_path = tmp_path / "delegation_log.jsonl"
    log = DelegationLog(log_path)

    node = Node(StubAdapter(), name="implementer", delegation_log=log)

    result = node.adapter.closure_tools["RecallHistory"](5)

    assert "No prior" in result


# ---------------------------------------------------------------------------
# Node: WriteDeliverable
# ---------------------------------------------------------------------------


def test_write_deliverable_creates_declared_file(tmp_path):
    """WriteDeliverable writes a declared extra deliverable to study_dir."""
    from adda._src.nodes import Node

    write_results = []

    class DeliverableAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["WriteDeliverable"](
                "replicate.py", "x = 42\n"
            )
            write_results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = DeliverableAdapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        study_dir=str(tmp_path),
    )
    state = _make_state(study_dir=tmp_path)
    state["required_deliverables"] = ["replicate.py"]
    node(state)

    assert write_results
    assert "ERROR" not in write_results[0]
    assert "42" in (tmp_path / "replicate.py").read_text()


def test_cell_tools_repair_a_code_cell_missing_outputs(tmp_path):
    """A hand-authored notebook missing `outputs` on a code cell (nbformat.
    reads() accepts this with no validation error — confirmed empirically)
    must not crash a LATER nbformat.write with AttributeError: outputs
    (BACKLOG #28). The notebook can no longer be written raw through a tool,
    but one can still arrive on disk that way, so the cell tools repair it in
    place on load before writing it back."""
    import json

    import nbformat

    from adda._src.nodes import Node

    (tmp_path / "pipeline.ipynb").write_text(json.dumps({
        "cells": [
            {"cell_type": "code", "metadata": {"name": "doe"}, "source": "x = 1"},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }))
    node = Node(
        StubAdapter(), name="strategizer", outgoing=["implementer"],
        spec=_minimal_spec(), study_dir=str(tmp_path),
    )
    out = node.adapter.closure_tools["WriteCell"]("problem", content="p")
    assert "ERROR" not in out

    nb_on_disk = nbformat.read(str(tmp_path / "pipeline.ipynb"), as_version=4)
    code = [c for c in nb_on_disk.cells if c.cell_type == "code"][0]
    assert "outputs" in code
    # The repaired notebook must itself be writable again without crashing —
    # this is the exact call that raised AttributeError: outputs before the fix.
    nbformat.write(nb_on_disk, str(tmp_path / "pipeline.ipynb"))


def test_write_deliverable_rejects_bad_extension(tmp_path):
    """WriteDeliverable rejects any file the study did not declare."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.ipynb").write_text("# r\n")

    results = []

    class DeliverableAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["WriteDeliverable"](
                "output.csv", "col1,col2\n1,2\n"
            )
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = DeliverableAdapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        study_dir=str(tmp_path),
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    assert results
    assert "ERROR" in results[0]


def test_write_deliverable_rejects_path_separators(tmp_path):
    """WriteDeliverable rejects filenames with path separators."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.py").write_text("# r\n")

    results = []

    class DeliverableAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["WriteDeliverable"](
                "subdir/output.py", "x = 1\n"
            )
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = DeliverableAdapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        study_dir=str(tmp_path),
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    assert results
    assert "ERROR" in results[0]


# ---------------------------------------------------------------------------
# Node: RecallHistory
# ---------------------------------------------------------------------------


def test_strategizer_recall_history_with_log(tmp_path):
    """Node.RecallHistory returns entries received by a node that
    is NOT the graph's entry (the entry node is always the from_node, never
    the to_node — see test_recall_history_entry_node_gets_orchestrator_message
    in test_nodes.py for that dedicated, structurally-always-empty case).
    Uses a custom spec with entry="hub" so the "strategizer"-named node under
    test keeps its usual Done/FollowUp tool set while genuinely being able to
    receive delegations."""
    from adda._src.nodes import Node
    from adda._src.infra.delegation_log import DelegationLog

    (tmp_path / "pipeline.py").write_text("# r\n")
    log_path = tmp_path / "delegation_log.jsonl"
    log = DelegationLog(log_path)
    log.record(
        id="D001",
        from_node="hub",
        to_node="strategizer",  # records received BY this node
        task="Run experiment",
        deliverable="Result: 42",
        hypothesis_ids=[],
        started_at="2024-01-01T12:00:00+00:00",
        completed_at="2024-01-01T12:05:00+00:00",
        status="DONE",
    )

    recall_results = []

    class RecallAdapter(StubAdapter):
        def invoke(self, messages):
            if "RecallHistory" in self.closure_tools:
                result = self.closure_tools["RecallHistory"](5)
                recall_results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    class _Hub(Agent):
        description = "hub"

    class _Strategizer(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote",
                            "WriteDeliverable"})
        description = "Test strategizer, not the graph entry."

    class _Leaf(Agent):
        role = "implementer"
        description = "something for the mid-tier node to delegate to"

    # A MID-TIER node: it receives delegations from the hub AND has an
    # outgoing edge of its own, which is what earns it the orchestrating
    # toolset (Done/FollowUp/...). A node with no outgoing edge is a leaf and
    # has no Done to call — see nodes/node.py.
    spec = Graph(
        nodes={"hub": _Hub(), "strategizer": _Strategizer(), "leaf": _Leaf()},
        edges=(Edge("hub", "strategizer"), Edge("strategizer", "leaf")),
        entry="hub")

    adapter = RecallAdapter()
    node = Node(
        adapter, name="strategizer", outgoing=["leaf"], spec=spec,
        study_dir=str(tmp_path),
        worker_adapters={"leaf": StubAdapter()},
        delegation_log=log,
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    assert recall_results
    assert "Run experiment" in recall_results[0]


# ---------------------------------------------------------------------------
# Node: HypothesisPropose/Update/List/Get closures
# ---------------------------------------------------------------------------


def test_hypothesis_propose_without_ledger():
    """HypothesisPropose returns ERROR when no ledger is set."""
    from adda._src.nodes import Node

    results = []

    class HypAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["HypothesisPropose"](
                statement="Test hypothesis",
                falsification_criterion="any counter-example",
                prediction="none found",
                prior=0.5,
            )
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = HypAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)
    node(_make_state())

    assert results
    assert "ERROR" in results[0]


def test_hypothesis_list_without_ledger():
    """HypothesisList returns ERROR when no ledger is set."""
    from adda._src.nodes import Node

    results = []

    class HypAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["HypothesisList"]()
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = HypAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)
    node(_make_state())

    assert results
    assert "ERROR" in results[0]


def test_hypothesis_propose_with_ledger(tmp_path):
    """HypothesisPropose returns H-id when ledger is active."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.py").write_text("# r\n")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    results = []

    class HypAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["HypothesisPropose"](
                statement="The sky is blue.",
                falsification_criterion="any night-time observation",
                prediction="daytime sky appears blue",
                prior=0.9,
            )
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = HypAdapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        notes_dir=notes_dir,
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    assert results
    # Should return an H-id like H1
    assert results[0].startswith("H") or "ERROR" not in results[0]


def test_hypothesis_list_with_entries(tmp_path):
    """HypothesisList returns hypothesis entries when ledger has items."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.py").write_text("# r\n")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    list_results = []
    call_phase = [0]

    class HypListAdapter(StubAdapter):
        def invoke(self, messages):
            phase = call_phase[0]
            call_phase[0] += 1
            if phase == 0:
                self.closure_tools["HypothesisPropose"](
                    statement="Hypothesis A is correct.",
                    falsification_criterion="counter-example exists",
                    prediction="no counter-example found",
                    prior=0.55,
                )
                result = self.closure_tools["HypothesisList"]()
                list_results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = HypListAdapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        notes_dir=notes_dir,
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    assert list_results
    # Should contain hypothesis A statement or an H-id
    combined = list_results[0]
    assert (
        "Hypothesis A" in combined
        or "H1" in combined
        or "ERROR" not in combined
    )
    # New format must show belief
    assert "(belief 0.55)" in combined


def test_hypothesis_update_coerces_json_string_evidence(tmp_path):
    """Backends may pass evidence as a JSON string; closure coerces."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.py").write_text("# r\n")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    results = []

    class Adapter(StubAdapter):
        def invoke(self, messages):
            self.closure_tools["HypothesisPropose"](
                statement="Claim below 1.0",
                falsification_criterion="any point below 0.5",
                prediction="sweep finds nothing below 0.5",
                prior=0.5,
            )
            results.append(self.closure_tools["HypothesisUpdate"](
                hypothesis_id="H1",
                status="SUPPORTED",
                comment="c",
                evidence='{"delegation": "D001"}',
                posterior=0.8,
            ))
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "Done."

    adapter = Adapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        notes_dir=notes_dir,
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    assert results
    assert "Updated H1" in results[0]


def test_hypothesis_get_not_found(tmp_path):
    """HypothesisGet returns ERROR for unknown hypothesis id."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.py").write_text("# r\n")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    results = []

    class HypGetAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["HypothesisList"](["H999"])
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = HypGetAdapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        notes_dir=notes_dir,
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    assert results
    assert "ERROR" in results[0]


# ---------------------------------------------------------------------------
# Node: budget warnings in __call__
# ---------------------------------------------------------------------------


def test_budget_95_percent_warning_in_context():
    """At 95% budget, a budget warning is injected into the context."""
    from adda._src.nodes import Node

    received_messages = []

    class BudgetAdapter(StubAdapter):
        def invoke(self, messages):
            received_messages.extend(messages)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = BudgetAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)

    state = _make_state()
    # 95% elapsed of a 100s budget
    state["budget_seconds"] = 100.0
    state["start_time"] = time.time() - 96.0
    node(state)

    # At least one message should contain budget warning
    budget_msgs = [m for m in received_messages if "budget" in str(m.get("content", "")).lower()]
    assert budget_msgs, f"Expected budget warning in messages, got: {received_messages}"


def test_eval_budget_exceeded_warning():
    """When eval_budget is exceeded, a warning is injected into context."""
    from adda._src.nodes import Node

    received_messages = []

    class EvalBudgetAdapter(StubAdapter):
        def invoke(self, messages):
            received_messages.extend(messages)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = EvalBudgetAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)

    state = _make_state()
    state["eval_budget"] = 10
    state["evals_used"] = 15
    node(state)

    eval_msgs = [
        m for m in received_messages
        if "eval" in str(m.get("content", "")).lower()
    ]
    assert eval_msgs, f"Expected eval budget warning in messages, got: {received_messages}"


# ---------------------------------------------------------------------------
# Node: _record_tool_error / _wrap_closure (lines 981-1059)
# ---------------------------------------------------------------------------


def test_wrap_closure_counts_error_returns(tmp_path):
    """_wrap_closure increments error_counts when closure returns ERROR:."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.py").write_text("# r\n")

    class ErrorClosureAdapter(StubAdapter):
        def invoke(self, messages):
            # WriteNote with no notes_dir set returns ERROR
            result = self.closure_tools["WriteNote"]("test.md", "body")
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = ErrorClosureAdapter()
    spec = _minimal_spec()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
    )
    state = _make_state(study_dir=tmp_path)
    node(state)

    # error_counts should have been incremented for strategizer
    assert node._error_counts.get("strategizer", 0) >= 1


# ---------------------------------------------------------------------------
# Node: Delegate to unknown target returns ERROR
# ---------------------------------------------------------------------------


def test_delegate_unknown_target_returns_error():
    """Delegate to a non-existent target returns ERROR."""
    from adda._src.nodes import Node

    results = []

    class BadDelegateAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["Delegate"](
                target="nonexistent_agent",
                intent="Do something",
                expected_report="",
            )
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = BadDelegateAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)
    node(_make_state())

    assert results
    assert "ERROR" in results[0]


# ---------------------------------------------------------------------------
# Node: Delegate with no worker adapter returns ERROR
# ---------------------------------------------------------------------------


def test_delegate_no_worker_adapter_returns_error():
    """Delegate to a valid target with no worker_adapters returns ERROR."""
    from adda._src.nodes import Node

    results = []

    class NoWorkerAdapter(StubAdapter):
        def invoke(self, messages):
            result = self.closure_tools["Delegate"](
                target="implementer",
                intent="Do something",
                expected_report="",
            )
            results.append(result)
            self.closure_tools["Done"](summary="done")
            self.closure_tools["Done"](summary="done")
            return "done"

    adapter = NoWorkerAdapter()
    spec = _minimal_spec()
    # No worker_adapters provided — "implementer" has no adapter
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={},
    )
    node(_make_state())

    assert results
    assert "ERROR" in results[0]


# ---------------------------------------------------------------------------
# Node: RecallHistory with None delegation_log
# ---------------------------------------------------------------------------


def test_worker_node_recall_history_none_log(tmp_path):
    """Node RecallHistory when delegation_log is None is not added."""
    from adda._src.nodes import Node

    adapter = StubAdapter()
    node = Node(adapter, name="implementer", delegation_log=None)

    # RecallHistory should NOT be injected when delegation_log is None
    assert "RecallHistory" not in adapter.closure_tools


# ---------------------------------------------------------------------------
# One implementation per tool, not one per call site
#
# Write / ReportEvals / RecallHistory used to have TWO implementations each
# — one closed over in the now-deleted nodes/leaf.py, one in
# nodes/tools/routing/delegation.py — which produced the same bug four
# times (a capability added to one path silently missing from the other).
# leaf.py is gone (STEP 3 of the leaf/orchestration merge: Node has one init
# path, used by every node); node.py's own capability setup
# (Node._setup_sandboxed_write / _build_eval_closures / _init_capabilities)
# must still call the SAME shared builders rather than re-forking a nested
# `def Write` / `def ReportEvals` of its own.
# ---------------------------------------------------------------------------


def test_write_report_evals_recall_history_have_one_implementation():
    """No nested ``def Write`` / ``def ReportEvals`` in node.py, and exactly
    one such def lives in delegation.py (inside the shared builder).
    RecallHistory has no closure body of its own anywhere in node.py — it is
    a call to the shared ``build_recall_history``."""
    import ast
    import inspect

    from adda._src.nodes import node as node_module
    from adda._src.nodes.tools.routing import delegation

    def count_nested_defs(module, name: str) -> int:
        tree = ast.parse(inspect.getsource(module))
        return sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        )

    for tool_name in ("Write", "ReportEvals"):
        assert count_nested_defs(delegation, tool_name) == 1, (
            f"{tool_name} must have exactly one implementation, in "
            "delegation.py's shared builder"
        )
        assert count_nested_defs(node_module, tool_name) == 0, (
            f"{tool_name} was re-implemented in node.py — collapse back "
            "to the shared builder in delegation.py"
        )

    # RecallHistory's real body lives once, on DelegationTools; node.py must
    # not define a closure for it.
    assert count_nested_defs(delegation, "RecallHistory") == 1
    assert count_nested_defs(node_module, "RecallHistory") == 0

    # node.py routes through the same three builders — verified by name, not
    # just by absence of a re-fork, so an import that quietly swaps in a
    # look-alike local helper also fails this test.
    node_src = inspect.getsource(node_module)
    assert "build_sandboxed_write" in node_src
    assert "build_report_evals" in node_src
    assert "build_recall_history" in node_src


# ---------------------------------------------------------------------------
# Node: Done() with pending delegations returns ERROR
# ---------------------------------------------------------------------------


def test_done_with_pending_delegations_returns_error(tmp_path):
    """Done() is refused when delegations are still Working."""
    from adda._src.nodes import Node

    (tmp_path / "pipeline.py").write_text("# r\n")

    done_results = []
    delegation_started = threading.Event()

    class SlowWorker(StubAdapter):
        def invoke(self, messages):
            delegation_started.set()
            time.sleep(3)
            return (
                "## Report\n### Actions taken\nDone.\n"
                "### Files touched\nnone\n### Conclusions\nOK\n### Numbers\nn: 0"
            )

    class EagerDoneAdapter(StubAdapter):
        def invoke(self, messages):
            # Delegate then immediately call Done
            self.closure_tools["Delegate"](
                target="implementer",
                intent="slow task",
                expected_report="",
            )
            delegation_started.wait(timeout=2)
            result = self.closure_tools["Done"](summary="trying to close")
            done_results.append(result)
            # Now wait and close properly
            time.sleep(0.1)
            self.closure_tools["Done"](summary="wait to close")
            self.closure_tools["Done"](summary="close for real")
            return "done"

    adapter = EagerDoneAdapter()
    spec = _minimal_spec()
    worker = SlowWorker()
    node = Node(
        adapter, name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": worker},
    )
    node(_make_state(study_dir=tmp_path))

    assert done_results
    # Soft 2-option nudge (not a hard error): still refuses to close, but
    # offers keep-working / wait (GetStatus). Cancel was dropped from production.
    assert "still running" in done_results[0].lower()
    assert "Wait" in done_results[0]
    assert "CancelDelegation" not in done_results[0]
    assert not done_results[0].lstrip().startswith("ERROR:")


# ---------------------------------------------------------------------------
# Node: _accumulate_usage
# ---------------------------------------------------------------------------


def test_accumulate_usage_sums_token_counts():
    """_accumulate_usage correctly sums token counts across multiple calls."""
    from adda._src.nodes import Node

    adapter = StubAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)

    node._accumulate_usage({"input_tokens": 10, "output_tokens": 5})
    node._accumulate_usage({"input_tokens": 20, "output_tokens": 15})

    assert node._token_totals["input_tokens"] == 30
    assert node._token_totals["output_tokens"] == 20


def test_accumulate_usage_handles_none_values():
    """_accumulate_usage treats None values as 0."""
    from adda._src.nodes import Node

    adapter = StubAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)

    node._accumulate_usage({"input_tokens": None, "output_tokens": None})

    assert node._token_totals["input_tokens"] == 0
    assert node._token_totals["output_tokens"] == 0


def test_accumulate_usage_adds_cost():
    """_accumulate_usage sums total_cost_usd."""
    from adda._src.nodes import Node

    adapter = StubAdapter()
    spec = _minimal_spec()
    node = Node(adapter, name="strategizer", outgoing=["implementer"], spec=spec)

    node._accumulate_usage({"total_cost_usd": 0.01})
    node._accumulate_usage({"total_cost_usd": 0.02})

    assert abs(node._token_totals["total_cost_usd"] - 0.03) < 1e-9


# ---------------------------------------------------------------------------
# _classify_response function
# ---------------------------------------------------------------------------


def test_classify_response_short_text():
    """_classify_response returns REFLECT for text under 100 chars."""
    from adda._src.nodes import _classify_response

    result = _classify_response("too short")
    assert result is not None


def test_classify_response_capability_phrase():
    """_classify_response returns REFLECT when capability-limit phrase present."""
    from adda._src.nodes import _classify_response

    long_text = "A" * 200 + " I cannot do this task because it requires internet access."
    result = _classify_response(long_text)
    assert result is not None


def test_classify_response_no_report_heading():
    """_classify_response returns REFLECT when ## Report heading is missing."""
    from adda._src.nodes import _classify_response

    text = "A" * 200 + "\nSome content without the required heading."
    result = _classify_response(text)
    assert result is not None


def test_classify_response_missing_subsections():
    """_classify_response returns REFLECT when required subsections are missing."""
    from adda._src.nodes import _classify_response

    text = (
        "A" * 200 +
        "\n## Report\n### Actions taken\nDid stuff.\n"
        # Missing: Files touched, Conclusions, Numbers
    )
    result = _classify_response(text)
    assert result is not None


def test_classify_response_honors_per_agent_sections():
    """Audit Finding 4: validation uses the passed report_sections (DRY single
    source), so e.g. a missing ### Retrospective is caught when required."""
    from adda._src.nodes import _classify_response

    body = (
        "A" * 200 +
        "\n## Report\n### Actions taken\nx\n### Conclusions\ny\n"
        # has Actions + Conclusions, but NO Retrospective
    )
    secs = ["### Actions taken", "### Conclusions", "### Retrospective"]
    assert _classify_response(body, secs) is not None  # Retrospective missing
    # Same body passes when Retrospective isn't in the required set.
    assert _classify_response(body, ["### Actions taken", "### Conclusions"]) is None


def test_classify_response_valid_report():
    """_classify_response returns None for a well-formed report."""
    from adda._src.nodes import _classify_response

    text = (
        "A" * 200 +
        "\n## Report\n### Actions taken\nDid stuff.\n"
        "### Files touched\nfile.py\n"
        "### Conclusions\nSuccess.\n"
        "### Numbers\nn: 42\n"
    )
    result = _classify_response(text)
    assert result is None


# ---------------------------------------------------------------------------
# _to_adapter_messages handles list content
# ---------------------------------------------------------------------------


def test_to_adapter_messages_handles_list_content():
    """_to_adapter_messages concatenates list-typed content items."""
    from adda._src.nodes import _to_adapter_messages
    from langchain_core.messages import HumanMessage

    msg = HumanMessage(content=[{"text": "Hello"}, {"text": "World"}])
    result = _to_adapter_messages([msg])

    assert len(result) == 1
    assert "Hello" in result[0]["content"]
    assert "World" in result[0]["content"]


# build_sandboxed_write: a bare required-deliverable name lands where the gate
# looks for it (BACKLOG #60, solo design.json misplacement 2026-10-08).

def _build_write_with_deliverables(tmp_path, names):
    from adda._src.nodes.tools.routing.delegation import build_sandboxed_write

    study_dir = tmp_path / "study"
    root = study_dir / "runs" / "T1" / "debug" / "delegations"
    root.mkdir(parents=True)
    return build_sandboxed_write(
        root, deliverable_dir=study_dir,
        deliverable_names=lambda: names), study_dir, root


def test_write_routes_a_bare_required_deliverable_to_the_study_dir(tmp_path):
    Write, study_dir, root = _build_write_with_deliverables(
        tmp_path, ["design.json"])
    assert "ERROR" not in Write("design.json", "{}")
    assert (study_dir / "design.json").read_text() == "{}"
    assert not (root / "design.json").exists()


def test_write_routes_by_basename_of_a_configured_path(tmp_path):
    Write, study_dir, _root = _build_write_with_deliverables(
        tmp_path, ["workspace/replicate.py"])
    Write("replicate.py", "x = 1")
    assert (study_dir / "replicate.py").exists()


def test_write_keeps_other_bare_names_in_the_delegation_root(tmp_path):
    Write, study_dir, root = _build_write_with_deliverables(
        tmp_path, ["design.json"])
    Write("notes.txt", "n")
    assert (root / "notes.txt").exists()
    assert not (study_dir / "notes.txt").exists()
