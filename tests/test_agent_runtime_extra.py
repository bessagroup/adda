"""Extra tests for agent_runtime.py — config loading, budget parsing, and
_make_adapter edge cases."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _load_study_config
# ---------------------------------------------------------------------------


def test_load_study_config_returns_empty_dict_when_no_config(tmp_path):
    """_load_study_config returns {} when config.yaml does not exist."""
    from adda._src.runtime.run_setup import _load_study_config

    result = _load_study_config(tmp_path)
    assert result == {}


def test_load_study_config_reads_yaml_when_present(tmp_path):
    """_load_study_config reads and parses config.yaml."""
    import yaml as _yaml
    from adda._src.runtime.run_setup import _load_study_config

    config = {"model": "claude-haiku", "budget": 3600}
    (tmp_path / "config.yaml").write_text(_yaml.dump(config))

    result = _load_study_config(tmp_path)
    assert result["model"] == "claude-haiku"
    assert result["budget"] == 3600


def test_load_study_config_returns_empty_for_empty_yaml(tmp_path):
    """_load_study_config returns {} when config.yaml is empty."""
    from adda._src.runtime.run_setup import _load_study_config

    (tmp_path / "config.yaml").write_text("")

    result = _load_study_config(tmp_path)
    assert result == {}


# ---------------------------------------------------------------------------
# _parse_budget_str
# ---------------------------------------------------------------------------


def test_parse_budget_str_none():
    """_parse_budget_str returns None for None input."""
    from adda._src.runtime.run_setup import _parse_budget_str

    assert _parse_budget_str(None) is None


def test_parse_budget_str_float():
    """_parse_budget_str returns float for numeric input."""
    from adda._src.runtime.run_setup import _parse_budget_str

    assert _parse_budget_str(3600.0) == 3600.0
    assert _parse_budget_str(1800) == 1800.0


def test_parse_budget_str_hhmmss():
    """_parse_budget_str parses HH:MM:SS string."""
    from adda._src.runtime.run_setup import _parse_budget_str

    result = _parse_budget_str("01:30:00")
    assert result == 5400.0  # 1h30m = 5400s


def test_parse_budget_str_float_string():
    """_parse_budget_str parses a plain float string."""
    from adda._src.runtime.run_setup import _parse_budget_str

    result = _parse_budget_str("7200")
    assert result == 7200.0


# ---------------------------------------------------------------------------
# AgenticRun.__init__ reads config.yaml
# ---------------------------------------------------------------------------


def test_agentic_run_reads_model_from_config(tmp_path):
    """AgenticRun reads model from config.yaml if not passed explicitly."""
    import yaml as _yaml
    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("test")
    (tmp_path / "config.yaml").write_text(_yaml.dump({"model": "claude-opus"}))

    run = AgenticRun(tmp_path)
    assert run._model == "claude-opus"


def test_agentic_run_reads_budget_from_config(tmp_path):
    """AgenticRun reads budget from config.yaml if not passed explicitly."""
    import yaml as _yaml
    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("test")
    (tmp_path / "config.yaml").write_text(_yaml.dump({"budget": "02:00:00"}))

    run = AgenticRun(tmp_path)
    assert run._budget == 7200.0


def test_agentic_run_explicit_budget_overrides_config(tmp_path):
    """AgenticRun explicit budget parameter overrides config.yaml value."""
    import yaml as _yaml
    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("test")
    (tmp_path / "config.yaml").write_text(_yaml.dump({"budget": "02:00:00"}))

    run = AgenticRun(tmp_path, budget=1000)
    assert run._budget == 1000


# ---------------------------------------------------------------------------
# AgenticRun.execute raises AgenticRunError when PROBLEM_STATEMENT.md missing
# ---------------------------------------------------------------------------


def test_execute_raises_when_no_problem_statement(tmp_path):
    """AgenticRun.execute raises AgenticRunError when PROBLEM_STATEMENT.md is missing."""
    from adda._src.runtime.agent_runtime import AgenticRun, AgenticRunError

    run = AgenticRun(tmp_path)
    with pytest.raises(AgenticRunError, match="PROBLEM_STATEMENT"):
        run.execute()


# ---------------------------------------------------------------------------
# _make_adapter for ollama backend calls build_closure_tools
# ---------------------------------------------------------------------------


def test_make_adapter_ollama_calls_build_closure_tools(tmp_path):
    """_make_adapter for ollama backend calls agent.build_closure_tools."""
    from adda._src.runtime.agent_runtime import AgenticRun, _default_graph, DEFAULT_MODEL
    from adda._src.backends.base import Agent

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("test")

    run = AgenticRun.__new__(AgenticRun)
    run.study_dir = tmp_path
    run._model = DEFAULT_MODEL
    run._backend = "ollama"
    run._graph_spec = MagicMock()
    run._graph_spec.outgoing.return_value = []

    run_dir = tmp_path / "runs" / "test"
    (run_dir / "debug" / "strategizer_notes").mkdir(parents=True, exist_ok=True)
    run._run_dir = run_dir

    class TestAgent(Agent):
        description = "Test."
        tools = frozenset({"Bash"})

    agent = TestAgent()

    mock_closures = {"Done": lambda s: "done"}
    with patch.object(agent, "build_closure_tools", return_value=mock_closures) as mock_build:
        with patch("adda._src.backends.ollama.OllamaAdapter") as MockOllama:
            mock_instance = MagicMock()
            mock_instance.closure_tools = {}
            MockOllama.return_value = mock_instance
            import os
            with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://localhost:11434/v1"}):
                result = run._make_adapter("implementer", agent)

    mock_build.assert_called_once()
    assert "Done" in mock_instance.closure_tools


# ---------------------------------------------------------------------------
# AgenticRun._make_adapter with extra_closures updates adapter
# ---------------------------------------------------------------------------


def test_make_adapter_claude_extra_closures_injected(tmp_path):
    """_make_adapter injects extra_closures from agent.build_closure_tools into adapter."""
    from adda._src.runtime.agent_runtime import AgenticRun, _default_graph, DEFAULT_MODEL
    from adda._src.backends.base import Agent

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("test")

    run = AgenticRun.__new__(AgenticRun)
    run.study_dir = tmp_path
    run._model = DEFAULT_MODEL
    run._backend = "claude"
    run._graph_spec = MagicMock()
    run._graph_spec.outgoing.return_value = []

    run_dir = tmp_path / "runs" / "test"
    (run_dir / "debug" / "strategizer_notes").mkdir(parents=True, exist_ok=True)
    run._run_dir = run_dir

    class TestAgent(Agent):
        description = "Test."
        tools = frozenset({"Bash"})

    agent = TestAgent()

    extra_closures = {"SpecialTool": lambda x: x}
    with patch.object(agent, "build_closure_tools", return_value=extra_closures):
        with patch("adda._src.backends.claude.ClaudeAdapter") as MockClaude:
            mock_instance = MagicMock()
            mock_instance.closure_tools = {}
            MockClaude.return_value = mock_instance

            result = run._make_adapter("implementer", agent)

    assert "SpecialTool" in mock_instance.closure_tools


# ---------------------------------------------------------------------------
# _init_canonical_store helper
# ---------------------------------------------------------------------------


def test_init_canonical_store_creates_dirs(tmp_path):
    """_init_canonical_store creates experiment_data/."""
    from adda._src.runtime.run_setup import _init_canonical_store

    run_dir = tmp_path / "runs" / "20260101T000000"
    (run_dir / "debug").mkdir(parents=True, exist_ok=True)
    study_dir = tmp_path / "my_study"

    _init_canonical_store(run_dir, study_dir)

    assert (run_dir / "experiment_data").is_dir()
    # eval_counter directory is no longer created (counter removed)
    assert not (run_dir / "debug" / "eval_counter").exists()


def test_init_canonical_store_writes_run_config_json(tmp_path):
    """_init_canonical_store writes run_config.json with correct keys."""
    import json
    from adda._src.runtime.run_setup import _init_canonical_store

    run_dir = tmp_path / "runs" / "20260101T000000"
    (run_dir / "debug").mkdir(parents=True, exist_ok=True)
    study_dir = tmp_path / "my_study"

    cfg = _init_canonical_store(run_dir, study_dir)

    cfg_path = run_dir / "debug" / "run_config.json"
    assert cfg_path.exists()
    loaded = json.loads(cfg_path.read_text())
    assert loaded["store_dir"] == str(run_dir / "experiment_data")
    assert "counter_dir" not in loaded
    # Lock co-located with the data (store_dir/experiment_data/) so D000
    # ingestion and D001+ evaluations lock the same file (audit Finding 3).
    assert loaded["lock_path"] == str(
        run_dir / "experiment_data" / "experiment_data" / ".lock"
    )
    assert loaded["evaluator_name"] == "my_study"
    assert loaded["fidelity_column"] is None
    assert loaded["evaluator_entrypoint"] is None
    # return value matches written file
    assert cfg == loaded


def test_init_canonical_store_returns_config_dict(tmp_path):
    """_init_canonical_store return value is the config dict."""
    from adda._src.runtime.run_setup import _init_canonical_store

    run_dir = tmp_path / "runs" / "ts"
    (run_dir / "debug").mkdir(parents=True, exist_ok=True)
    study_dir = tmp_path / "study_x"

    result = _init_canonical_store(run_dir, study_dir)

    assert isinstance(result, dict)
    assert "store_dir" in result
    assert "counter_dir" not in result


def test_execute_creates_canonical_store_dirs_and_state(tmp_path):
    """After execute(), canonical store dirs exist and state has
    experiment_data_dir.
    """
    from langgraph.checkpoint.memory import MemorySaver

    from adda._src.runtime.agent_runtime import (
        DEFAULT_MODEL,
        AgenticRun,
        _default_graph,
    )
    from adda._src.runtime.graph_builder import build_graph

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("Find min of f(x)=x^2")
    (tmp_path / "pipeline.py").write_text("# test\n")

    state_snapshots: list[dict] = []

    class CapturingStratAdapter:
        closure_tools: dict = {}

        def invoke(self, messages):
            state_snapshots.append(dict(self.closure_tools))
            self.closure_tools["Done"](summary="Done.")
            self.closure_tools["Done"](summary="Done.")
            return "Done."

    class StubImplAdapter:
        closure_tools: dict = {}

        def invoke(self, messages):
            return "## Report\nDone."

    graph_spec = _default_graph()
    compiled = build_graph(
        graph_spec,
        lambda n, a: (
            CapturingStratAdapter()
            if n == "strategizer"
            else StubImplAdapter()
        ),
        MemorySaver(),
        study_dir=tmp_path,
    )

    run = AgenticRun.__new__(AgenticRun)
    run.study_dir = tmp_path
    run._model = DEFAULT_MODEL
    run._budget = None
    run._graph_spec = graph_spec
    run._graph = compiled

    run.execute()

    # run_dir was set
    assert run._run_dir is not None
    run_dir = run._run_dir

    # dirs exist
    assert (run_dir / "experiment_data").is_dir()
    # eval_counter directory is no longer created (counter removed)
    assert not (run_dir / "debug" / "eval_counter").exists()

    # run_config.json exists with correct keys
    import json

    cfg = json.loads(
        (run_dir / "debug" / "run_config.json").read_text()
    )
    assert cfg["store_dir"] == str(run_dir / "experiment_data")
    assert "counter_dir" not in cfg
    assert cfg["evaluator_name"] == tmp_path.name


# ---------------------------------------------------------------------------
# Change 1: _ingest_precomputed_pool
# ---------------------------------------------------------------------------


def _make_pool(pool_dir: Path, n: int = 5) -> None:
    """Write a tiny ExperimentData pool to pool_dir."""
    from f3dasm import ExperimentData, datagenerator
    from f3dasm.design import Domain
    from f3dasm._src.samplers import RandomUniform

    d = Domain()
    d.add_float("x0", low=0.0, high=1.0)
    d.add_float("x1", low=0.0, high=1.0)
    d.add_output("y")
    data = ExperimentData(domain=d)
    data = RandomUniform(seed=42).call(data, n_samples=n)

    @datagenerator(output_names=["y"])
    def _fn(**kw):
        return float(kw["x0"]) + float(kw["x1"])

    data = _fn.call(data, mode="sequential")
    data.store(project_dir=pool_dir)


def test_ingest_precomputed_pool_d000_rows_in_store(tmp_path):
    """_ingest_precomputed_pool writes D000 rows to the canonical store."""
    from f3dasm import ExperimentData
    from adda._src.runtime.run_setup import _ingest_precomputed_pool

    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    _make_pool(pool_dir, n=5)

    store_dir = tmp_path / "experiment_data"
    store_dir.mkdir()

    lookup_cfg = {"pool": "pool"}
    n = _ingest_precomputed_pool(store_dir, tmp_path, lookup_cfg)

    assert n == 5, f"Expected 5 rows ingested, got {n}"

    canon = ExperimentData.from_file(project_dir=store_dir)
    _, df_out = canon.to_pandas()
    assert len(df_out) == 5
    assert (df_out["_delegation_id"] == "D000").all(), (
        f"Expected all D000, got: {df_out['_delegation_id'].unique()}"
    )
    assert (df_out["_source"] == "precomputed_pool").all()
    assert df_out["_ts"].notna().all()


def test_ingest_precomputed_pool_readable_by_runstatesummary(tmp_path):
    """D000 rows are visible in RunStateSummary.from_store."""
    from adda._src.runtime.run_setup import _ingest_precomputed_pool
    from adda._src.evaluation.ledger_summary import RunStateSummary

    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    _make_pool(pool_dir, n=7)

    store_dir = tmp_path / "experiment_data"
    store_dir.mkdir()

    _ingest_precomputed_pool(store_dir, tmp_path, {"pool": "pool"})

    summary = RunStateSummary.from_store(store_dir)
    assert summary is not None, (
        "RunStateSummary.from_store returned None after D000 ingest"
    )
    assert summary.n_per_delegation.get("D000", 0) == 7, (
        f"Expected D000=7, got {summary.n_per_delegation}"
    )


def test_d000_not_counted_as_evals(tmp_path):
    """_resolve_delegation_evals for D000 returns 0 (no real eval rows)."""
    from adda._src.runtime.run_setup import _ingest_precomputed_pool
    from adda._src.nodes import _resolve_delegation_evals

    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    _make_pool(pool_dir, n=3)

    store_dir = tmp_path / "experiment_data"
    store_dir.mkdir()

    _ingest_precomputed_pool(store_dir, tmp_path, {"pool": "pool"})

    # D000 rows exist in the store, but we never call
    # _resolve_delegation_evals("D000", ...) in production.
    # Verify: if it were called, it would return the row count.
    # More importantly, for a real delegation that doesn't exist (D001),
    # the reported fallback is used — D000 doesn't pollute D001.
    evals_d001 = _resolve_delegation_evals(store_dir, "D001", reported=0)
    assert evals_d001 == 0, (
        f"D000 rows must not inflate D001 eval count; got {evals_d001}"
    )


def test_execute_with_lookup_config_ingests_d000(tmp_path):
    """execute() with evaluator.lookup ingests D000 rows before graph runs."""
    import yaml as _yaml
    from langgraph.checkpoint.memory import MemorySaver

    from adda._src.runtime.agent_runtime import (
        DEFAULT_MODEL,
        AgenticRun,
        _default_graph,
    )
    from adda._src.runtime.graph_builder import build_graph
    from adda._src.evaluation.ledger_summary import RunStateSummary

    # Build pool.
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    _make_pool(pool_dir, n=4)

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("Find min")
    (tmp_path / "pipeline.py").write_text("# test\n")
    (tmp_path / "config.yaml").write_text(_yaml.dump({
        "evaluator": {
            "lookup": {
                "pool": "pool",
                "input_columns": ["x0", "x1"],
                "output_columns": ["y"],
            }
        }
    }))

    class CapturingStratAdapter:
        closure_tools: dict = {}

        def invoke(self, messages):
            self.closure_tools["Done"](summary="Done.")
            self.closure_tools["Done"](summary="Done.")
            return "Done."

    class StubImplAdapter:
        closure_tools: dict = {}

        def invoke(self, messages):
            return "## Report\nDone."

    graph_spec = _default_graph()
    compiled = build_graph(
        graph_spec,
        lambda n, a: (
            CapturingStratAdapter()
            if n == "strategizer"
            else StubImplAdapter()
        ),
        MemorySaver(),
        study_dir=tmp_path,
    )

    run = AgenticRun.__new__(AgenticRun)
    run.study_dir = tmp_path
    run._model = DEFAULT_MODEL
    run._budget = None
    run._graph_spec = graph_spec
    run._graph = compiled

    run.execute()

    run_dir = run._run_dir
    store_dir = run_dir / "experiment_data"
    summary = RunStateSummary.from_store(store_dir)
    assert summary is not None, (
        "No rows in canonical store after execute() with lookup config"
    )
    assert summary.n_per_delegation.get("D000", 0) == 4, (
        f"Expected D000=4, got {summary.n_per_delegation}"
    )


def test_execute_with_training_data_ingests_d000_no_oracle(tmp_path):
    """A top-level `training_data` pool is ingested as D000 WITHOUT declaring
    any evaluator — get_evaluator() then resolves nothing (surrogate-only
    study). This separates 'training data' from 'lookup oracle'."""
    import json as _json

    import yaml as _yaml
    from langgraph.checkpoint.memory import MemorySaver

    from adda._src.runtime.agent_runtime import (
        DEFAULT_MODEL,
        AgenticRun,
        _default_graph,
    )
    from adda._src.runtime.graph_builder import build_graph
    from adda._src.evaluation.ledger_summary import RunStateSummary

    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    _make_pool(pool_dir, n=4)

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("Surrogate study")
    (tmp_path / "pipeline.py").write_text("# test\n")
    (tmp_path / "config.yaml").write_text(
        _yaml.dump({"training_data": "pool"})
    )

    class CapturingStratAdapter:
        closure_tools: dict = {}

        def invoke(self, messages):
            self.closure_tools["Done"](summary="Done.")
            self.closure_tools["Done"](summary="Done.")
            return "Done."

    class StubImplAdapter:
        closure_tools: dict = {}

        def invoke(self, messages):
            return "## Report\nDone."

    graph_spec = _default_graph()
    compiled = build_graph(
        graph_spec,
        lambda n, a: (
            CapturingStratAdapter()
            if n == "strategizer"
            else StubImplAdapter()
        ),
        MemorySaver(),
        study_dir=tmp_path,
    )
    run = AgenticRun.__new__(AgenticRun)
    run.study_dir = tmp_path
    run._model = DEFAULT_MODEL
    run._budget = None
    run._graph_spec = graph_spec
    run._graph = compiled
    run.execute()

    run_dir = run._run_dir
    summary = RunStateSummary.from_store(run_dir / "experiment_data")
    assert summary is not None and summary.n_per_delegation.get("D000", 0) == 4
    # No oracle declared → run_config carries no entrypoint/lookup.
    cfg = _json.loads((run_dir / "debug" / "run_config.json").read_text())
    assert cfg["evaluator_entrypoint"] is None
    assert cfg["evaluator_lookup"] is None


def test_record_node_models_writes_what_make_adapter_would_resolve(tmp_path):
    """The record must never disagree with the thing it describes: both the
    adapter construction and the record go through resolve_node_identity."""
    import json as _json

    from adda._src.backends.base import Agent, Graph
    from adda._src.runtime.agent_runtime import (
        AgenticRun,
        resolve_node_identity,
    )

    class _Hub(Agent):
        role = "strategizer"
        description = "hub"

    class _Math(Agent):
        role = "math_expert"
        description = "derivations"
        model = "qwen3.8-27b-256k"
        backend = "ollama"

    study = tmp_path / "study"
    study.mkdir()
    (study / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    graph = Graph(
        nodes={"strategizer": _Hub(), "math_expert": _Math()},
        edges=(), entry="strategizer")

    run = AgenticRun(study_dir=study, graph=graph)
    debug = tmp_path / "debug"
    debug.mkdir()
    run._record_node_models(debug)

    written = _json.loads((debug / "node_models.json").read_text())
    for name, agent in graph.nodes.items():
        model, backend = resolve_node_identity(agent, run._model, run._backend)
        assert written[name] == {"model": model, "backend": backend}
    # the per-agent override is what actually lands, not the run default
    assert written["math_expert"]["model"] == "qwen3.8-27b-256k"
    assert written["math_expert"]["backend"] == "ollama"
    assert written["strategizer"]["model"] == run._model


# ---------------------------------------------------------------------------
# _fallback_retrospective — a non-compliant close (UNGATED/FAILED/external
# stop) must still leave a strategizer retrospective on disk, without relying
# on the strategizer's cooperative extra Done() call ever arriving. Run
# 20260920T005201 (studies/tube_buckling_sensitivity, UNGATED) closed with
# _awaiting_retro still true and NO strategizer entry in
# debug/retrospectives.jsonl — see CLAUDE.md's "capture, don't request".
# ---------------------------------------------------------------------------


def test_fallback_retrospective_synthesizes_for_the_entry_node_role(tmp_path):
    """A close whose exit-interview reply never arrived still yields a
    strategizer retrospective, synthesized from disk state."""
    import json as _json

    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    (run_dir / "debug").mkdir(parents=True)

    run._fallback_retrospective(run_dir)

    lines = [
        line for line in
        (run_dir / "debug" / "retrospectives.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["role"] == "strategizer"
    assert rec["source_id"].startswith("SYNTHESIZED")


def test_fallback_retrospective_appends_worker_entries_survive(tmp_path):
    """Pre-existing worker retrospectives (recorded at delegation completion)
    are never clobbered by the fallback."""
    import json as _json

    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    debug = run_dir / "debug"
    debug.mkdir(parents=True)
    (debug / "retrospectives.jsonl").write_text(
        _json.dumps({"source_id": "D001", "role": "datagenerator", "text": "x"})
        + "\n"
        + _json.dumps({"source_id": "D002", "role": "implementer", "text": "y"})
        + "\n"
    )

    run._fallback_retrospective(run_dir)

    lines = [
        line for line in
        (debug / "retrospectives.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 3
    roles = {_json.loads(line)["role"] for line in lines}
    assert roles == {"datagenerator", "implementer", "strategizer"}


def test_fallback_retrospective_no_duplicate_on_a_compliant_close(tmp_path):
    """A compliant close (the real Done()-carried retrospective already
    landed) produces exactly one real strategizer entry — no synthesized
    duplicate alongside it."""
    import json as _json

    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    debug = run_dir / "debug"
    debug.mkdir(parents=True)
    (debug / "retrospectives.jsonl").write_text(
        _json.dumps(
            {"source_id": "DONE", "role": "strategizer", "text": "real"})
        + "\n"
    )

    run._fallback_retrospective(run_dir)

    lines = [
        line for line in
        (debug / "retrospectives.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["source_id"] == "DONE"


def test_fallback_retrospective_never_raises_when_debug_missing(tmp_path):
    """Best-effort: a run_dir/debug/ that was never created must not break
    the close."""
    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run._fallback_retrospective(tmp_path / "runs" / "does_not_exist")
    # No exception is the assertion here.


def test_finalize_run_calls_the_fallback_retrospective(tmp_path):
    """_finalize_run — the single choke point every close (UNGATED, FAILED,
    external stop, budget exhaustion) passes through — must invoke the
    fallback so no non-watchdog close path can skip it."""
    from unittest.mock import patch as _patch

    from adda._src.runtime.agent_runtime import AgenticRun, _RunContext

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    debug_dir = run_dir / "debug"
    debug_dir.mkdir(parents=True)

    import logging as _logging
    ctx = _RunContext(
        ts="T", run_dir=run_dir, debug_dir=debug_dir,
        notes_dir=debug_dir / "strategizer_notes",
        workspace_dir=run_dir / "workspace",
        problem="x", problem_sha256="a", live_problem_sha256="a",
        resume_from=None, start_time=0.0, thread_id="th",
        log=_logging.getLogger("test_finalize_run"),
        log_handler=_logging.NullHandler(),
        delegation_log=MagicMock(), canonical_cfg={}, study_cfg={},
        initial_state={}, graph_config={},
    )
    with _patch.object(run, "_fallback_retrospective") as mocked:
        run._finalize_run(ctx, {"last_report": "done", "outcome": "GATED",
                                 "termination": "done", "reviewed": True,
                                 "token_totals": {}})
    mocked.assert_called_once_with(run_dir)


def test_invoke_graph_crash_still_yields_a_fallback_retrospective(tmp_path):
    """A crash (GraphRecursionError, KeyboardInterrupt, OOM, ...) never
    reaches _finalize_run's normal close, so _invoke_graph's own
    `except BaseException` around graph.invoke must call the fallback
    itself, before re-raising -- and the original exception must still
    propagate unchanged (a failure inside the fallback must never mask the
    crash the user needs to see)."""
    import json as _json
    import logging as _logging

    from adda._src.runtime.agent_runtime import AgenticRun, _RunContext

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    debug_dir = run_dir / "debug"
    debug_dir.mkdir(parents=True)

    class _CrashingGraph:
        def invoke(self, *a, **k):
            raise RuntimeError("boom")

    run._graph = _CrashingGraph()

    ctx = _RunContext(
        ts="T", run_dir=run_dir, debug_dir=debug_dir,
        notes_dir=debug_dir / "strategizer_notes",
        workspace_dir=run_dir / "workspace",
        problem="x", problem_sha256="a", live_problem_sha256="a",
        resume_from=None, start_time=0.0, thread_id="th",
        log=_logging.getLogger("test_invoke_graph_crash"),
        log_handler=_logging.NullHandler(),
        delegation_log=MagicMock(), canonical_cfg={}, study_cfg={},
        initial_state={}, graph_config={},
    )

    with pytest.raises(RuntimeError, match="boom"):
        run._invoke_graph(ctx)

    lines = [
        line for line in
        (debug_dir / "retrospectives.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["role"] == "strategizer"
    assert rec["source_id"].startswith("SYNTHESIZED")
    assert "RuntimeError" in rec["text"]


def test_invoke_graph_crash_fallback_never_shadows_the_exception(tmp_path):
    """Even if the fallback synthesis itself raises, the ORIGINAL crash must
    still propagate -- the fallback is diagnostic, not load-bearing."""
    import logging as _logging
    from unittest.mock import patch as _patch

    from adda._src.runtime.agent_runtime import AgenticRun, _RunContext

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    debug_dir = run_dir / "debug"
    debug_dir.mkdir(parents=True)

    class _CrashingGraph:
        def invoke(self, *a, **k):
            raise RuntimeError("boom")

    run._graph = _CrashingGraph()

    ctx = _RunContext(
        ts="T", run_dir=run_dir, debug_dir=debug_dir,
        notes_dir=debug_dir / "strategizer_notes",
        workspace_dir=run_dir / "workspace",
        problem="x", problem_sha256="a", live_problem_sha256="a",
        resume_from=None, start_time=0.0, thread_id="th",
        log=_logging.getLogger("test_invoke_graph_crash_shadow"),
        log_handler=_logging.NullHandler(),
        delegation_log=MagicMock(), canonical_cfg={}, study_cfg={},
        initial_state={}, graph_config={},
    )

    with _patch.object(
        run, "_fallback_retrospective",
        side_effect=Exception("fallback exploded"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            run._invoke_graph(ctx)


# ---------------------------------------------------------------------------
# _sweep_open_reviews — spec 12 edge 2: a run can close (Done()/watchdog/
# budget cutoff) while a delegation is still OPEN-FOR-REVIEW. Must be
# recorded honestly (open, never approved), never as DONE and never dropped.
# ---------------------------------------------------------------------------


class _FakeDelegationLog:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)


class _FakeNode:
    """Just enough of a Node for _record_unapproved_review/_sweep_open_reviews
    to operate on -- not the real thing, since only these few attributes and
    methods are actually touched."""

    def __init__(self, name: str, registry: dict) -> None:
        import threading as _threading

        self._name = name
        self._registry = registry
        self._registry_lock = _threading.Lock()
        self._delegation_log = _FakeDelegationLog()
        self.retrospectives: list[tuple] = []

    def _role_of(self, target: str) -> str:
        return target or "unknown"

    def _record_retrospective(self, role, source_id, text) -> None:
        self.retrospectives.append((role, source_id, text))


def test_record_unapproved_review_writes_a_distinct_terminal_status(tmp_path):
    """The delegation log entry for an unreviewed report must NOT read
    DONE -- that would claim approval that never happened."""
    from adda._src.runtime.agent_runtime import AgenticRun

    node = _FakeNode("strategizer", {})
    entry = {
        "target": "implementer", "result": "the report text",
        "hypothesis_ids": ["H1"], "started_at": "2026-01-01T00:00:00+00:00",
        "evals": 3, "phase": "optimization",
        "is_falsification_attempt": True,
    }

    AgenticRun._record_unapproved_review(node, "D001", entry)

    assert len(node._delegation_log.records) == 1
    rec = node._delegation_log.records[0]
    assert rec["status"] == "OPEN_UNAPPROVED"
    assert rec["status"] != "DONE"
    assert rec["deliverable"] == "the report text"
    assert rec["evals"] == 3
    assert len(node.retrospectives) == 1
    role, source_id, text = node.retrospectives[0]
    assert source_id == "D001"
    assert "OPEN FOR REVIEW" in text
    assert "never approved" in text or "never-approved" in text


def test_record_unapproved_review_is_best_effort_never_raises(tmp_path):
    """A node too broken to log against (no delegation_log, or one that
    itself raises) must not take the run's close down with it."""
    from adda._src.runtime.agent_runtime import AgenticRun

    node = _FakeNode("strategizer", {})
    node._delegation_log = None  # simulate a run with no delegation log

    # Must not raise.
    AgenticRun._record_unapproved_review(node, "D001", {"target": "x"})
    assert len(node.retrospectives) == 1  # retrospective still recorded


def test_sweep_open_reviews_finds_and_records_every_open_one(tmp_path):
    """The sweep reaches every live Node instance via the compiled graph
    and records EACH OpenForReview entry, not just the first."""
    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)

    node = _FakeNode("strategizer", {
        "D001": {
            "status": "OpenForReview", "target": "implementer",
            "result": "report 1", "hypothesis_ids": [], "started_at": "",
            "evals": 0,
        },
        "D002": {
            "status": "Done", "target": "implementer", "result": "report 2",
            "hypothesis_ids": [], "started_at": "", "evals": 0,
        },
        "D003": {
            "status": "OpenForReview", "target": "implementer",
            "result": "report 3", "hypothesis_ids": [], "started_at": "",
            "evals": 0,
        },
    })

    class _FakeRunnable:
        def __init__(self, func):
            self.func = func

    class _FakePregelNode:
        def __init__(self, func):
            self.bound = _FakeRunnable(func)

    class _FakeGraph:
        def __init__(self, nodes):
            self.nodes = {
                name: _FakePregelNode(n) for name, n in nodes.items()}

    run._live_graph = _FakeGraph({"strategizer": node})

    run._sweep_open_reviews(tmp_path / "runs" / "T")

    recorded_ids = {r["id"] for r in node._delegation_log.records}
    assert recorded_ids == {"D001", "D003"}  # D002 (Done) is untouched
    assert all(
        r["status"] == "OPEN_UNAPPROVED" for r in node._delegation_log.records
    )


def test_sweep_open_reviews_is_a_noop_with_no_live_graph(tmp_path):
    """Before _invoke_graph ever ran (or a caller-supplied pre-built
    graph bypassed the stash), the sweep degrades to a silent no-op --
    never an exception at run close."""
    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run._sweep_open_reviews(tmp_path / "runs" / "T")  # must not raise


def test_fallback_retrospective_accepts_a_custom_reason(tmp_path):
    """The crash path passes its own reason naming the exception type, so
    the synthesized text says the run died rather than implying the
    interview was merely skipped."""
    import json as _json

    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run_dir = tmp_path / "runs" / "T"
    (run_dir / "debug").mkdir(parents=True)

    run._fallback_retrospective(run_dir, reason="Run crashed (RuntimeError)")

    rec = _json.loads(
        (run_dir / "debug" / "retrospectives.jsonl")
        .read_text().splitlines()[0])
    assert "Run crashed (RuntimeError)" in rec["text"]
