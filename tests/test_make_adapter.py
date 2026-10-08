"""Tests for AgenticRun._make_adapter() — preamble selection and backend routing."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adda._src.runtime.agent_runtime import (
    DEFAULT_MODEL,
    AgenticRun,
    _default_graph,
)
from adda._src.backends.base import Agent, Edge, Graph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(tmp_path: Path, backend: str = "claude") -> AgenticRun:
    """Build a bare AgenticRun via __new__ with minimal attributes set."""
    (tmp_path / "PROBLEM_STATEMENT.md").write_text("test")
    run = AgenticRun.__new__(AgenticRun)
    run.study_dir = tmp_path
    run._model = DEFAULT_MODEL
    run._backend = backend
    run._graph_spec = _default_graph()
    # Create a run_dir so _make_adapter can compute paths
    run_dir = tmp_path / "runs" / "test_run"
    (run_dir / "debug" / "strategizer_notes").mkdir(parents=True, exist_ok=True)
    run._run_dir = run_dir
    return run


def _agent_with_tools(*tools) -> Agent:
    class _A(Agent):
        description = "Test agent."

    _A.tools = frozenset(tools)
    return _A()


# ---------------------------------------------------------------------------
# Entry/orchestrator node → RUN_PATHS preamble + cwd=study_dir
# ---------------------------------------------------------------------------


def test_make_adapter_entry_node_uses_run_paths_preamble(tmp_path):
    """_make_adapter for the graph's ENTRY node uses RUN_PATHS_PREAMBLE and
    cwd=study_dir — this is keyed off being the entry node specifically, NOT
    off merely having outgoing edges (see the non-entry-with-outgoing-edges
    regression test below for why that distinction matters)."""
    run = _make_run(tmp_path)

    agent = _agent_with_tools("Bash", "WriteNote")

    # Patch ClaudeAdapter so we don't need real credentials
    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = ["implementer"]

        result = run._make_adapter("strategizer", agent)

    call_kwargs = MockClaude.call_args[1]
    assert "system_prompt" in call_kwargs
    # Both preambles are tagged <workspace>; the entry's carries the run's
    # paths (strategizer_notes_dir), a worker's the delegation contract.
    assert "strategizer_notes_dir" in call_kwargs["system_prompt"]
    assert "<delegation_contract>" not in call_kwargs["system_prompt"]
    assert call_kwargs["study_dir"] == run.study_dir  # cwd == study_dir


# ---------------------------------------------------------------------------
# Non-entry node (leaf, no outgoing edges) → WORKSPACE preamble
# ---------------------------------------------------------------------------


def test_make_adapter_worker_node_uses_workspace_preamble(tmp_path):
    """_make_adapter for a leaf node (not the entry, no outgoing) uses
    WORKSPACE_PREAMBLE and cwd=run_dir/debug/delegations."""
    run = _make_run(tmp_path)

    agent = _agent_with_tools("Bash")

    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = []

        result = run._make_adapter("implementer", agent)

    call_kwargs = MockClaude.call_args[1]
    assert "system_prompt" in call_kwargs
    assert "<workspace>" in call_kwargs["system_prompt"]
    assert call_kwargs["study_dir"] == run._run_dir / "debug" / "delegations"


def test_make_adapter_non_entry_node_with_outgoing_edges_uses_workspace_preamble(
    tmp_path
):
    """Regression (run 20260718T132852): datagenerator/implementer each have
    their OWN outgoing edge to literature_reviewer (for sub-delegating a
    lookup — see agents/_graphs.py) but are NOT the entry node. Before the
    fix, _make_adapter keyed the RUN_PATHS/cwd=study_dir choice off "has ANY
    outgoing edge", so these two roles wrongly got cwd=study_dir instead of
    the run-scoped workspace their OWN preamble promises — splitting their
    delegation output across two physical trees (study_dir/debug/delegations
    vs run_dir/debug/delegations) with the same D### ids in both. A non-entry
    node must get the WORKSPACE preamble and the run-scoped cwd regardless of
    whether it has its own outgoing edges."""
    run = _make_run(tmp_path)

    agent = _agent_with_tools("Bash")

    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        # datagenerator has its own outgoing edge (to literature_reviewer)
        # but must still be treated as a worker, not the orchestrator.
        run._graph_spec.outgoing.return_value = ["literature_reviewer"]

        result = run._make_adapter("datagenerator", agent)

    call_kwargs = MockClaude.call_args[1]
    assert "<delegation_contract>" in call_kwargs["system_prompt"]
    assert "strategizer_notes_dir" not in call_kwargs["system_prompt"]
    assert call_kwargs["study_dir"] == run._run_dir / "debug" / "delegations"


# ---------------------------------------------------------------------------
# Ollama backend → OllamaAdapter created
# ---------------------------------------------------------------------------


def test_make_adapter_ollama_backend_creates_ollama_adapter(tmp_path):
    """_make_adapter with backend='ollama' returns an OllamaAdapter instance."""
    run = _make_run(tmp_path, backend="ollama")

    agent = _agent_with_tools("Bash")

    # Registry dispatch resolves OllamaAdapter from its source module, so
    # patching it there is all that's needed.
    with patch("adda._src.backends.ollama.OllamaAdapter") as MockOllama:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockOllama.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = []

        result = run._make_adapter("implementer", agent)

    # The result should be the mock OllamaAdapter instance
    assert result is mock_instance


# ---------------------------------------------------------------------------
# _make_adapter when run_dir is None — returns adapter without RUN_PATHS
# ---------------------------------------------------------------------------


def test_make_adapter_no_run_dir_returns_adapter_without_run_paths(tmp_path):
    """When run_dir is None, _make_adapter falls through to WORKSPACE path."""
    run = _make_run(tmp_path)
    run._run_dir = None  # simulate pre-execute state

    agent = _agent_with_tools("Bash")

    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = []  # leaf node (run_dir is None anyway)

        # Should not raise even when run_dir is None
        result = run._make_adapter("implementer", agent)

    assert result is mock_instance


# ---------------------------------------------------------------------------
# lit_reviewer_notes_dir — study-scoped, NOT per-run (a study is typically
# run many times; re-downloading/re-embedding the same papers every run is
# pure waste with no corresponding staleness risk, unlike cross-run findings
# memory). Regression: this used to be run_dir/debug/lit_reviewer_notes,
# wiped every fresh run.
# ---------------------------------------------------------------------------


def _make_lit_reviewer_agent():
    from adda._src.agents.literature import LiteratureReviewAgent
    return LiteratureReviewAgent()


def test_lit_reviewer_corpus_is_study_scoped_not_per_run(tmp_path):
    """The corpus lands under study_dir/runs/lit_reviewer_notes — a SIBLING of
    the timestamped run dir, not inside it — for any real run_dir, so a
    second run of the same study (a fresh run_dir) still sees it."""
    run = _make_run(tmp_path)
    agent = _make_lit_reviewer_agent()

    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = []

        run._make_adapter("literature_reviewer", agent)

    expected = tmp_path / "runs" / "lit_reviewer_notes"
    assert expected.is_dir()
    # NOT created under the per-run debug dir
    assert not (run._run_dir / "debug" / "lit_reviewer_notes").exists()


def test_lit_reviewer_corpus_path_unaffected_by_run_dir(tmp_path):
    """Same study-scoped path whether or not a run_dir/run context exists at
    all — the corpus location no longer depends on self._run_dir."""
    run = _make_run(tmp_path)
    run._run_dir = None  # simulate pre-execute state
    agent = _make_lit_reviewer_agent()

    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = []

        run._make_adapter("literature_reviewer", agent)

    assert (tmp_path / "runs" / "lit_reviewer_notes").is_dir()


# ---------------------------------------------------------------------------
# notebook_deliverable_spec injection gated on pipeline_deliverable (BACKLOG #27)
# ---------------------------------------------------------------------------


def _make_strategizer_agent() -> Agent:
    class _Strategist(Agent):
        role = "strategizer"
        description = "Test strategizer."

    return _Strategist()


def test_notebook_deliverable_spec_injected_by_default(tmp_path):
    """Default (pipeline_deliverable unset -> True): the strategizer's prompt
    still carries the pipeline.ipynb contract, unchanged from before #27."""
    from adda._src.runtime import settings
    settings.configure(None)  # no pipeline_deliverable key -> default True
    run = _make_run(tmp_path)
    agent = _make_strategizer_agent()

    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = ["literature_reviewer"]

        run._make_adapter("strategizer", agent)

    system_prompt = MockClaude.call_args[1]["system_prompt"]
    assert "DELIVERABLE = pipeline.ipynb" in system_prompt


def test_notebook_deliverable_spec_suppressed_when_pipeline_deliverable_false(tmp_path):
    """pipeline_deliverable: false must ALSO suppress this injection, not just
    the CRAFT_PIPELINE milestone nudge (BACKLOG #27) — its text is an
    unconditional imperative ("this SUPERSEDES every ... instruction above")
    that previously overrode a PROBLEM_STATEMENT.md saying there is no
    pipeline deliverable at all."""
    from adda._src.runtime import settings
    settings.configure({"pipeline_deliverable": False})
    try:
        run = _make_run(tmp_path)
        agent = _make_strategizer_agent()

        with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
            mock_instance = MagicMock()
            mock_instance.closure_tools = {}
            MockClaude.return_value = mock_instance

            run._graph_spec = MagicMock()
            run._graph_spec.entry = "strategizer"
            run._graph_spec.outgoing.return_value = ["literature_reviewer"]

            run._make_adapter("strategizer", agent)

        from adda._src.runtime import features
        system_prompt = features.resolve_gates(
            MockClaude.call_args[1]["system_prompt"])
        assert "DELIVERABLE = pipeline.ipynb" not in system_prompt
    finally:
        settings.configure(None)  # don't leak into other tests


# ---------------------------------------------------------------------------
# reproduction_gate_contract injection gated on reproduction_gate — separate
# knob from pipeline_deliverable above (Elvis, via adda-boss-whopper: the
# gate's mechanical preconditions must reach the agent generated from the
# gate's own code, not paraphrased, and must be independently ablatable)
# ---------------------------------------------------------------------------


def test_reproduction_gate_contract_injected_by_default(tmp_path):
    """Default (reproduction_gate unset -> True): the strategizer's prompt
    carries the gate's exact preconditions, generated from
    reproduction_gate.py::gate_contract()."""
    from adda._src.runtime import settings
    settings.configure(None)
    run = _make_run(tmp_path)
    agent = _make_strategizer_agent()

    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        mock_instance = MagicMock()
        mock_instance.closure_tools = {}
        MockClaude.return_value = mock_instance

        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = ["literature_reviewer"]

        run._make_adapter("strategizer", agent)

    system_prompt = MockClaude.call_args[1]["system_prompt"]
    assert "<reproduction_gate_contract>" in system_prompt
    assert "canonical store must already hold at least one oracle row" in (
        system_prompt)


def test_reproduction_gate_contract_suppressed_when_reproduction_gate_false(
        tmp_path):
    """reproduction_gate: false suppresses the injection — independent of
    pipeline_deliverable, which can stay True (a notebook is still
    required) while this decides whether it must additionally reproduce."""
    from adda._src.runtime import settings
    settings.configure({"reproduction_gate": False})
    try:
        run = _make_run(tmp_path)
        agent = _make_strategizer_agent()

        with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
            mock_instance = MagicMock()
            mock_instance.closure_tools = {}
            MockClaude.return_value = mock_instance

            run._graph_spec = MagicMock()
            run._graph_spec.entry = "strategizer"
            run._graph_spec.outgoing.return_value = ["literature_reviewer"]

            run._make_adapter("strategizer", agent)

        from adda._src.runtime import features
        system_prompt = features.resolve_gates(
            MockClaude.call_args[1]["system_prompt"])
        assert "<reproduction_gate_contract>" not in system_prompt
        assert "DELIVERABLE = pipeline.ipynb" in system_prompt  # untouched
    finally:
        settings.configure(None)


# ---------------------------------------------------------------------------
# A preamble line that names a tool appears only if the node holds the tool
# ---------------------------------------------------------------------------


def _prompt_for(tmp_path, name, tools, *, entry, outgoing):
    run = _make_run(tmp_path)
    with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
        MockClaude.return_value = MagicMock(closure_tools={})
        MockClaude.select_native_tools = lambda t: [x for x in t if x != "Delegate"]
        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer" if entry else "other"
        run._graph_spec.outgoing.return_value = outgoing
        run._make_adapter(name, _agent_with_tools(*tools))
    return MockClaude.call_args[1]["system_prompt"]


def test_entry_preamble_names_only_tools_the_node_holds(tmp_path):
    held = _prompt_for(tmp_path, "strategizer", ("Read", "Bash"),
                       entry=True, outgoing=["implementer"])
    assert "Read() reads FILES" in held
    assert "calling Read()" in held
    bare = _prompt_for(tmp_path, "strategizer", ("Bash",),
                       entry=True, outgoing=[])
    assert "Read()" not in bare
    assert "Workers write exclusively" not in bare


def test_worker_preamble_names_only_tools_the_node_holds(tmp_path):
    held = _prompt_for(tmp_path, "implementer", ("Read", "Bash"),
                       entry=False, outgoing=[])
    assert "get_evaluator" in held
    assert "may Read() files" in held
    bare = _prompt_for(tmp_path, "critic", ("Grep",), entry=False, outgoing=[])
    assert "get_evaluator" not in bare
    assert "Read()" not in bare


def test_tool_gate_without_a_held_set_is_an_error():
    from adda._src.runtime import features

    with pytest.raises(ValueError):
        features.resolve_gates("[[if tool:Read]]x[[/if]]")
    assert features.resolve_gates(
        "[[if tool:Read]]x[[else]]y[[/if]]", holds={"Read"}) == "x"
    assert features.resolve_gates(
        "[[if tool:Read]]x[[else]]y[[/if]]", holds=set()) == "y"


# ---------------------------------------------------------------------------
# base_prompt: Default keeps the backend's own system prompt
# ---------------------------------------------------------------------------


def test_base_prompt_default_validation_and_round_trip():
    from adda._src.runtime.node_tools import apply_node_config, validate_nodes_block

    assert validate_nodes_block({"a": {"base_prompt": "Default"}}) == []
    assert validate_nodes_block({"a": {"base_prompt": "other"}})
    g = Graph(nodes={"a": _agent_with_tools("Bash")}, edges=[], entry="a")
    apply_node_config(g, {"a": {"base_prompt": "Default"}})
    assert g.nodes["a"].base_prompt == "Default"
    apply_node_config(g, {})
    assert g.nodes["a"].base_prompt is None


def test_claude_passes_a_preset_append_only_under_base_prompt(tmp_path):
    from adda._src.backends.claude import ClaudeAdapter

    a = ClaudeAdapter(model="m", system_prompt="own text", study_dir=tmp_path,
                      native_tools=["Default"])
    assert a._system_prompt_option() == a._render_system_prompt()
    a.base_prompt = "Default"
    opt = a._system_prompt_option()
    assert opt["type"] == "preset" and opt["preset"] == "claude_code"
    assert opt["append"] == a._render_system_prompt()
    assert "own text" in opt["append"]


def test_base_prompt_drops_the_preamble_and_openai_refuses(tmp_path):
    def build(backend, bp):
        run = _make_run(tmp_path, backend=backend)
        agent = _agent_with_tools("Bash")
        agent.base_prompt = bp
        run._graph_spec = MagicMock()
        run._graph_spec.entry = "strategizer"
        run._graph_spec.outgoing.return_value = []
        with patch("adda._src.backends.claude.ClaudeAdapter") as M:
            M.return_value = MagicMock(closure_tools={})
            M.HAS_BASE_PROMPT = True
            run._make_adapter("strategizer", agent)
            return M.call_args[1]["system_prompt"]

    assert "<workspace>" in build("claude", None)
    assert "<workspace>" not in build("claude", "Default")
    with pytest.raises(ValueError, match="base_prompt"):
        build("ollama", "Default")
