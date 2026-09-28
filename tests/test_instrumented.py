"""Tests for InstrumentedDataGenerator and get_evaluator.

TDD: these tests were written BEFORE the implementation.
Run with:
    uv run pytest tests/test_instrumented.py -v --no-cov
"""
from __future__ import annotations

import json
import threading

import pytest
from f3dasm._src.core import DataGenerator, datagenerator
from f3dasm._src.design.domain import Domain
from f3dasm._src.experimentdata import ExperimentData
from f3dasm._src.experimentsample import ExperimentSample, JobStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_domain() -> Domain:
    d = Domain()
    d.add_float("x0", 0.0, 1.0)
    return d


def _make_sample(x0: float = 0.5) -> ExperimentSample:
    return ExperimentSample(
        _input_data={"x0": x0},
        _output_data={},
        job_status=JobStatus.OPEN,
    )


class _SumGenerator(DataGenerator):
    """Trivial DataGenerator: f = sum of inputs."""

    def execute(
        self, experiment_sample: ExperimentSample, **kwargs
    ) -> ExperimentSample:
        val = sum(experiment_sample._input_data.values())
        experiment_sample._output_data["f"] = val
        experiment_sample.job_status = JobStatus.FINISHED
        return experiment_sample


# ---------------------------------------------------------------------------
# 1. Provenance stamping
# ---------------------------------------------------------------------------


def test_execute_stamps_provenance(tmp_path):
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(),
        store_dir=tmp_path,
        delegation_id="D001",
        source="test_source",
        flush_every=1,
    )
    sample = _make_sample(0.3)
    gen.execute(sample)

    data = ExperimentData.from_file(project_dir=tmp_path)
    df_in, df_out = data.to_pandas()

    assert "_delegation_id" in df_out.columns, df_out.columns.tolist()
    assert "_source" in df_out.columns, df_out.columns.tolist()
    assert "_ts" in df_out.columns, df_out.columns.tolist()
    assert "f" in df_out.columns, df_out.columns.tolist()

    row = df_out.iloc[0]
    assert row["_delegation_id"] == "D001"
    assert row["_source"] == "test_source"
    # _ts should be a non-empty string
    assert isinstance(row["_ts"], str) and len(row["_ts"]) > 0


def test_execute_marks_finished_in_canonical_store(tmp_path):
    """B (run 20260629T191754): a completed eval must persist as FINISHED in the
    canonical store. A real oracle does NOT self-mark its sample finished —
    f3dasm's _run_sample marks the agent's *working* copy, not the deepcopy the
    instrumented wrapper buffers. So InstrumentedDataGenerator must stamp it, or
    finished rows persist as IN_PROGRESS (defeating is_all_finished(), the
    FINISHED-regression store guard, and resumption). The stub here deliberately
    leaves job_status untouched, mimicking the real oracle."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    class _NoMarkGenerator(DataGenerator):
        def execute(self, experiment_sample, **kwargs):
            experiment_sample._output_data["f"] = 1.0  # no job_status change
            return experiment_sample

    gen = InstrumentedDataGenerator(
        inner=_NoMarkGenerator(), store_dir=tmp_path,
        delegation_id="D001", source="s", flush_every=1,
    )
    # Mirrors the live dispatch: get_open_job() marks a row IN_PROGRESS before
    # handing it to the evaluator — the exact state that leaked to disk.
    gen.execute(ExperimentSample(
        _input_data={"x0": 0.5}, _output_data={},
        job_status=JobStatus.IN_PROGRESS))

    data = ExperimentData.from_file(project_dir=tmp_path)
    assert data.is_all_finished(), (
        f"completed eval persisted as non-FINISHED: {data.jobs.tolist()}")


def test_execute_stamps_wall_ms(tmp_path):
    """Spec A: each eval carries its own wall-time (_wall_ms), generically."""
    import time as _time

    from adda._src.evaluation.instrumented import _PROVENANCE_COLS, InstrumentedDataGenerator

    class _Slow(DataGenerator):
        def execute(self, experiment_sample, **kwargs):
            _time.sleep(0.03)
            experiment_sample._output_data["f"] = 1.0
            experiment_sample.job_status = JobStatus.FINISHED
            return experiment_sample

    gen = InstrumentedDataGenerator(
        inner=_Slow(), store_dir=tmp_path, delegation_id="D001",
        source="t", flush_every=1)
    out = gen.execute(_make_sample(0.3))

    assert "_wall_ms" in out._output_data
    assert isinstance(out._output_data["_wall_ms"], float)
    assert out._output_data["_wall_ms"] >= 20.0  # slept ~30ms, allow slack
    # provenance convention: counted as metadata, excluded from value stats
    assert "_wall_ms" in _PROVENANCE_COLS


def test_to_numpy_excludes_underscore_provenance(tmp_path):
    """All provenance columns are now underscore-prefixed (_delegation_id,
    _source, _ts), so core to_numpy() drops them and returns a clean numeric
    array instead of an object-dtype array contaminated by the metadata."""
    import numpy as np

    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(),
        store_dir=tmp_path,
        delegation_id="D001",
        source="test_source",
        flush_every=1,
    )
    gen.execute(_make_sample(0.3))

    data = ExperimentData.from_file(project_dir=tmp_path)
    _, out_arr = data.to_numpy()

    # only the real output "f" survives → numeric, not object dtype
    assert out_arr.shape[1] == 1, out_arr
    assert np.issubdtype(out_arr.dtype, np.floating), out_arr.dtype


# ---------------------------------------------------------------------------
# 2. Concurrent appends — no rows lost
# ---------------------------------------------------------------------------


def _worker(store_dir, delegation_id, n_samples, lock_path):
    """Run in a thread; each gets its own InstrumentedDataGenerator."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(),
        store_dir=store_dir,
        delegation_id=delegation_id,
        source="concurrent_test",
        lock_path=lock_path,
        flush_every=1,
    )
    for i in range(n_samples):
        gen.execute(_make_sample(float(i) * 0.05))
    gen.flush()


def test_concurrent_appends_no_loss(tmp_path):
    lock_path = tmp_path / "experiment_data" / ".lock"
    delegation_ids = ["D001", "D002", "D003"]
    K = 10  # samples per delegation

    threads = [
        threading.Thread(
            target=_worker,
            args=(tmp_path, did, K, lock_path),
        )
        for did in delegation_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = ExperimentData.from_file(project_dir=tmp_path)
    df_in, df_out = data.to_pandas()

    total = len(df_out)
    assert total == len(delegation_ids) * K, (
        f"Expected {len(delegation_ids) * K} rows, got {total}"
    )

    for did in delegation_ids:
        count = (df_out["_delegation_id"] == did).sum()
        assert count == K, (
            f"Expected {K} rows for {did}, got {count}"
        )


# ---------------------------------------------------------------------------
# 4. Provenance survives + reindex
# ---------------------------------------------------------------------------


def test_provenance_survives_plus_reindex(tmp_path):
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    lock_path = tmp_path / "experiment_data" / ".lock"

    gen1 = InstrumentedDataGenerator(
        inner=_SumGenerator(),
        store_dir=tmp_path,
        delegation_id="D001",
        source="s1",
        lock_path=lock_path,
        flush_every=2,
    )
    gen2 = InstrumentedDataGenerator(
        inner=_SumGenerator(),
        store_dir=tmp_path,
        delegation_id="D002",
        source="s2",
        lock_path=lock_path,
        flush_every=2,
    )

    gen1.execute(_make_sample(0.1))
    gen1.execute(_make_sample(0.2))  # triggers flush

    gen2.execute(_make_sample(0.3))
    gen2.execute(_make_sample(0.4))  # triggers flush

    data = ExperimentData.from_file(project_dir=tmp_path)
    _, df_out = data.to_pandas()

    assert len(df_out) == 4
    d1_rows = df_out[df_out["_delegation_id"] == "D001"]
    d2_rows = df_out[df_out["_delegation_id"] == "D002"]
    assert len(d1_rows) == 2
    assert len(d2_rows) == 2


# ---------------------------------------------------------------------------
# 5. get_evaluator binds delegation_id from cwd
# ---------------------------------------------------------------------------


def test_get_evaluator_binds_delegation_id_from_cwd(
    tmp_path, monkeypatch
):
    from adda._src.evaluation.oracle_resolution import get_evaluator

    # Build the workspace: .../runs/ts/debug/delegations/D007
    debug_dir = tmp_path / "runs" / "ts" / "debug"
    delegation_dir = debug_dir / "delegations" / "D007"
    delegation_dir.mkdir(parents=True)

    store_dir = tmp_path / "store"
    store_dir.mkdir()

    # Register a tiny entrypoint so get_evaluator() (no inner) resolves the
    # source and we can still assert delegation_id derivation from cwd.
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "sum_eval.py").write_text(
        "def evaluate_kw(**kwargs):\n"
        "    return float(sum(kwargs.values()))\n"
    )

    run_config = {
        "store_dir": str(store_dir),
        "lock_path": str(store_dir / "experiment_data" / ".lock"),
        "source": "test_eval",
        "evaluator_name": "test_eval",
        "study_dir": str(study_dir),
        "fidelity_column": None,
        "evaluator_entrypoint": "sum_eval.py:evaluate_kw",
        "evaluator_output_names": ["f"],
    }
    run_config_path = debug_dir / "run_config.json"
    run_config_path.write_text(json.dumps(run_config))

    monkeypatch.chdir(delegation_dir)
    monkeypatch.delenv("F3DASM_DELEGATION_ID", raising=False)

    gen = get_evaluator()
    assert gen.delegation_id == "D007"
    assert gen.dedup_scope == "delegation"  # default, unless env overrides


def test_get_evaluator_reads_dedup_scope_from_env(tmp_path, monkeypatch):
    """F3DASM_DEDUP_SCOPE=all (set by notebook_exec.sandbox_env() for
    reproduction-gate/deliverable execution) must reach the returned
    InstrumentedDataGenerator — this is the wiring half of the
    dedup_scope="all" fix; test_dedup_scope_all_matches_regardless_of_
    delegation_id above tests the dedup LOGIC itself in isolation."""
    from adda._src.evaluation.oracle_resolution import get_evaluator

    debug_dir = tmp_path / "runs" / "ts" / "debug"
    delegation_dir = debug_dir / "delegations" / "D007"
    delegation_dir.mkdir(parents=True)

    store_dir = tmp_path / "store"
    store_dir.mkdir()

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "sum_eval.py").write_text(
        "def evaluate_kw(**kwargs):\n"
        "    return float(sum(kwargs.values()))\n"
    )

    run_config = {
        "store_dir": str(store_dir),
        "lock_path": str(store_dir / "experiment_data" / ".lock"),
        "source": "test_eval",
        "evaluator_name": "test_eval",
        "study_dir": str(study_dir),
        "fidelity_column": None,
        "evaluator_entrypoint": "sum_eval.py:evaluate_kw",
        "evaluator_output_names": ["f"],
    }
    run_config_path = debug_dir / "run_config.json"
    run_config_path.write_text(json.dumps(run_config))

    monkeypatch.chdir(delegation_dir)
    monkeypatch.delenv("F3DASM_DELEGATION_ID", raising=False)
    monkeypatch.setenv("F3DASM_DEDUP_SCOPE", "all")

    gen = get_evaluator()
    assert gen.dedup_scope == "all"


# ---------------------------------------------------------------------------
# 6. get_evaluator raises outside delegation workspace
# ---------------------------------------------------------------------------


def test_get_evaluator_raises_outside_delegation(
    tmp_path, monkeypatch
):
    from adda._src.evaluation.oracle_resolution import get_evaluator

    # cwd is not a D### directory
    bad_dir = tmp_path / "not_a_delegation"
    bad_dir.mkdir()
    monkeypatch.chdir(bad_dir)
    monkeypatch.delenv("F3DASM_DELEGATION_ID", raising=False)

    with pytest.raises(ValueError, match="get_evaluator"):
        get_evaluator()


# ---------------------------------------------------------------------------
# 7. flush_every batching
# ---------------------------------------------------------------------------


def test_flush_every_batches(tmp_path):
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(),
        store_dir=tmp_path,
        delegation_id="D001",
        source="batch_test",
        flush_every=5,
    )

    for i in range(4):
        gen.execute(_make_sample(float(i) * 0.1))

    # Store should not exist yet (or be empty if dir exists)
    exp_data_dir = tmp_path / "experiment_data"
    if exp_data_dir.exists():
        # If store was written it would have CSV files
        output_csv = exp_data_dir / "output_data.csv"
        if output_csv.exists():
            import pandas as pd
            df = pd.read_csv(output_csv, index_col=0)
            assert len(df) == 0, (
                f"Expected 0 rows before flush_every=5 triggered, "
                f"got {len(df)}"
            )

    # 5th execute triggers flush
    gen.execute(_make_sample(0.4))

    data = ExperimentData.from_file(project_dir=tmp_path)
    _, df_out = data.to_pandas()
    assert len(df_out) == 5, f"Expected 5 rows after flush, got {len(df_out)}"


# ---------------------------------------------------------------------------
# 8. Public API: importable from adda
# ---------------------------------------------------------------------------


def test_public_api_importable():
    # get_evaluator() is the ONE agent-facing door.
    import adda as _agentic
    from adda import get_evaluator  # noqa: F401

    # InstrumentedDataGenerator is deliberately NOT public — it stays internal
    # so agents cannot construct a store-redirected evaluator (§1 seal).
    assert "InstrumentedDataGenerator" not in _agentic.__all__
    assert not hasattr(_agentic, "InstrumentedDataGenerator")
    # ...but it remains importable internally for the runtime and tests.
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator  # noqa: F401


def test_store_rows_accumulate_across_generator_instances(tmp_path):
    """Two generators in the SAME delegation accumulate rows in store.

    Observed live: a worker built one generator per phase; the store
    accumulated rows (600) correctly while a counter undercounted (300).
    The store is now the single source of truth for eval counts.
    """
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator
    from adda._src.evaluation.ledger_summary import RunStateSummary

    store_dir = tmp_path / "store"
    store_dir.mkdir()

    def make_gen():
        @datagenerator(output_names=["f"])
        def inner(**kw):
            return float(sum(kw.values()))

        return InstrumentedDataGenerator(
            inner, store_dir, "D001",
            source="s", flush_every=1,
        )

    g1 = make_gen()
    g1.execute(_make_sample(0.1))
    g1.execute(_make_sample(0.2))

    g2 = make_gen()  # new instance, same delegation
    g2.execute(_make_sample(0.3))

    summary = RunStateSummary.from_store(store_dir)
    assert summary is not None
    # Store accumulates across both generator instances
    assert summary.n_per_delegation.get("D001", 0) == 3


def test_wall_per_delegation_and_footer(tmp_path):
    """from_store groups _wall_ms by delegation; delegation_footer renders it.

    Auto-appended to each delegation report so the strategizer plans its budget
    on measured sim cost (the 36.5x cost-prior miss in run 20260625T014520).
    """
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator
    from adda._src.evaluation.ledger_summary import RunStateSummary

    store_dir = tmp_path / "store"
    store_dir.mkdir()

    def make_gen(did):
        @datagenerator(output_names=["f"])
        def inner(**kw):
            return float(sum(kw.values()))

        return InstrumentedDataGenerator(
            inner, store_dir, did, source="s", flush_every=1,
        )

    make_gen("D001").execute(_make_sample(0.1))
    make_gen("D001").execute(_make_sample(0.2))
    make_gen("D002").execute(_make_sample(0.3))

    summary = RunStateSummary.from_store(store_dir)
    assert summary is not None

    wpd = summary.wall_per_delegation
    assert wpd["D001"]["n"] == 2
    assert wpd["D002"]["n"] == 1
    # measured wall-times are real positive floats; max ≥ median; total ≈ sum
    d1 = wpd["D001"]
    assert d1["max_ms"] >= d1["median_ms"] > 0
    assert d1["total_ms"] >= d1["max_ms"]

    footer = summary.delegation_footer("D001")
    assert footer is not None
    assert "D001" in footer
    assert "per-eval wall-time" in footer
    assert "ledger total so far: 3" in footer  # 2 + 1 rows
    # No wall budget passed → no budget line.
    assert "wall budget remaining" not in footer

    # With a wall budget, the remaining line appears (telemetry, not a stop).
    footer_b = summary.delegation_footer(
        "D001", wall_remaining_s=1800.0, wall_budget_s=3600.0)
    assert "wall budget remaining" in footer_b

    # Peak-RAM telemetry: rendered when given, against the hard cap; absent otherwise.
    footer_mem = summary.delegation_footer(
        "D001", peak_rss_bytes=2 * 1024**3, ram_cap_bytes=4 * 1024**3)
    assert "peak RAM (this delegation): 2.00 GB of 4.0 GB hard cap" in footer_mem
    assert "peak RAM" not in summary.delegation_footer("D001")  # none → no line

    # A delegation that wrote no rows gets no footer (not a fabricated zero).
    assert summary.delegation_footer("D999") is None


def test_flush_merges_into_typed_canonical_domain(tmp_path):
    """Regression (run 20260624T021359): flushing a batch into a canonical store
    whose domain has a TYPED (add_int) parameter must not raise. The batch domain
    declares inputs as untyped base Parameter(); the merge previously failed with
    'Cannot add non-continuous parameter to continuous!'."""
    import pandas as pd

    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    # Pre-seed a canonical store with a TYPED domain (int + float).
    domain = Domain()
    domain.add_int("n", 1, 3)
    domain.add_float("x0", 0.0, 1.0)
    seed = ExperimentData(
        domain=domain, input_data=pd.DataFrame([{"n": 2, "x0": 0.5}]))
    seed.store(project_dir=tmp_path)

    # Flush a new eval through the instrumented generator (untyped batch domain).
    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path,
        delegation_id="D002", flush_every=1)
    gen.execute(ExperimentSample(
        _input_data={"n": 3, "x0": 0.25}, _output_data={},
        job_status=JobStatus.OPEN))  # must NOT raise the typed/untyped ValueError

    _, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 2


def test_dedup_on_write_skips_design_already_in_ledger(tmp_path):
    """(B) retry-duplication fix: a re-launched/retried campaign that
    re-evaluates a design already FINISHED in the canonical store must NOT
    append a duplicate row (keep-first); a genuinely new design still lands."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    gen.execute(_make_sample(0.5))          # first eval of x0=0.5 -> lands
    gen.execute(_make_sample(0.5))          # duplicate design -> skipped
    gen.execute(_make_sample(0.7))          # distinct design -> lands

    df_in, df_out = ExperimentData.from_file(
        project_dir=tmp_path).to_pandas()
    assert len(df_out) == 2, f"expected 2 rows after dedup, got {len(df_out)}"
    assert sorted(round(float(v), 3) for v in df_in["x0"]) == [0.5, 0.7]


def test_dedup_within_a_single_flush_batch(tmp_path):
    """Duplicates repeated WITHIN one buffered flush are deduped too (the
    example_study 76%-repeat incident, backlog #24)."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path,
        delegation_id="D001", flush_every=10)   # buffer all, single flush
    for _ in range(3):
        gen.execute(_make_sample(0.5))
    gen.execute(_make_sample(0.9))
    gen.flush()

    _, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 2   # x0=0.5 once + x0=0.9


def test_dedup_is_per_delegation_by_default(tmp_path):
    """A DIFFERENT delegation re-measuring the same design is not deduped
    against by default — a deliberate design choice (a genuinely concurrent
    campaign may legitimately re-measure a design; collapsing across
    delegations would corrupt that), not the reproduction-gate bug this test
    file is distinguishing itself from below."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen1 = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path,
        delegation_id="D005", flush_every=1)
    gen1.execute(_make_sample(0.5))

    gen2 = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path,
        delegation_id="D009", flush_every=1)
    gen2.execute(_make_sample(0.5))  # same design, DIFFERENT delegation

    _, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 2  # both rows land — not deduped across delegations


def test_dedup_scope_all_matches_regardless_of_delegation_id(tmp_path):
    """BACKLOG: the reproduction-gate/deliverable execution path stamps a
    FIXED synthetic delegation id ("D999", notebook_exec.sandbox_env's
    default) that never matches the real delegation(s) that actually
    generated the ledger's rows (D005, D009, ...) — so the per-delegation
    dedup scope silently fails to recognize ANY existing row as already
    seen, and re-executing a notebook's data_generation cell (which must add
    ZERO new oracle rows to satisfy the "LAZY" reproduction invariant —
    notebook_exec.py's own documented contract) instead re-adds every design
    point as if new. dedup_scope="all" is the fix: dedup against the WHOLE
    ledger regardless of which delegation wrote each row — the correct
    semantics for a validation replay of already-generated data, as opposed
    to a live campaign delegation genuinely exploring in parallel."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen1 = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path,
        delegation_id="D005", flush_every=1)
    gen1.execute(_make_sample(0.5))

    # Simulates reproduction-gate execution: a different (synthetic) id,
    # but dedup_scope="all" so it still recognizes the design as already
    # evaluated.
    gen2 = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path,
        delegation_id="D999", flush_every=1, dedup_scope="all")
    gen2.execute(_make_sample(0.5))   # same design -> must be skipped
    gen2.execute(_make_sample(0.9))  # distinct design -> must still land

    _, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 2, (
        f"expected 2 rows (D005's 0.5 kept, D999's duplicate 0.5 skipped, "
        f"D999's 0.9 landed), got {len(df_out)}")
    df_in, _ = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert sorted(round(float(v), 3) for v in df_in["x0"]) == [0.5, 0.9]


# ---------------------------------------------------------------------------
# get_evaluator footguns (#2): self-explaining, no mkdir/cd, visible env source
# ---------------------------------------------------------------------------


def test_resolve_delegation_id_prefers_env_when_cwd_not_ddir(tmp_path, monkeypatch):
    from adda._src.evaluation.oracle_resolution import _resolve_delegation_id
    monkeypatch.chdir(tmp_path)                       # cwd not named D###
    monkeypatch.setenv("F3DASM_DELEGATION_ID", "D007")
    assert _resolve_delegation_id() == "D007"         # no mkdir/cd needed


def test_resolve_delegation_id_error_is_actionable(tmp_path, monkeypatch):
    from adda._src.evaluation.oracle_resolution import _resolve_delegation_id
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("F3DASM_DELEGATION_ID", raising=False)
    with pytest.raises(ValueError) as ei:
        _resolve_delegation_id()
    msg = str(ei.value)
    assert "F3DASM_DELEGATION_ID" in msg
    assert "no directory needs to exist" in msg       # the de-footgun hint


def test_get_evaluator_names_env_namespace_as_the_cause(tmp_path, monkeypatch):
    """A get_evaluator() that silently inherits an unregistered F3DASM_NAMESPACE
    must say SO in the error, so the fix (unset it) is obvious (D007 footgun)."""
    import adda._src.evaluation.oracle_resolution as _inst
    monkeypatch.setattr(_inst, "_resolve_delegation_id", lambda: "D001")
    monkeypatch.setattr(_inst, "_load_run_config", lambda: {"oracles": {}})
    monkeypatch.setenv("F3DASM_NAMESPACE", "graded")   # not registered
    with pytest.raises(ValueError) as ei:
        _inst.get_evaluator()                          # namespace=None -> env
    msg = str(ei.value)
    assert "F3DASM_NAMESPACE" in msg and "graded" in msg


# ---------------------------------------------------------------------------
# supersede() (#2/option C): replace a stale FINISHED row, net-count-preserving
# ---------------------------------------------------------------------------


class _ConstGenerator(DataGenerator):
    """f = a fixed value (simulates a corrected re-eval differing from a stale
    prior read). Set `.val` after construction to avoid a custom __init__."""
    val = 0.0

    def execute(self, experiment_sample, **kwargs):
        experiment_sample._output_data["f"] = self.val
        experiment_sample.job_status = JobStatus.FINISHED
        return experiment_sample


def _const_gen(val):
    g = _ConstGenerator()
    g.val = val
    return g


def test_supersede_replaces_stale_row_net_count_preserving(tmp_path):
    """Option C: supersede() re-evaluates a design and REPLACES its stale row
    (the mcs=0.57-style drift), leaving row count and other rows intact — and
    passing the PROTECTED-store shrink/FINISHED guards."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    # Seed: design A(x0=0.5) with a STALE value 99.0; design B(x0=0.9) good 7.0.
    a_bad = InstrumentedDataGenerator(
        inner=_const_gen(99.0), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    a_bad.execute(_make_sample(0.5))
    b = InstrumentedDataGenerator(
        inner=_const_gen(7.0), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    b.execute(_make_sample(0.9))

    # Supersede A with a corrected oracle (1.0).
    fixer = InstrumentedDataGenerator(
        inner=_const_gen(1.0), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    fixer.supersede(_make_sample(0.5))

    df_in, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 2, f"net count not preserved: {len(df_out)}"
    fvals = sorted(float(v) for v in df_out["f"])
    assert fvals == [1.0, 7.0], f"stale row not replaced: {fvals}"  # 99 gone
    # the corrected value sits on design A (x0=0.5), B untouched
    a_f = [float(v) for v, x in zip(df_out["f"], df_in["x0"])
           if round(float(x), 6) == 0.5]
    assert a_f == [1.0]


# ---------------------------------------------------------------------------
# Submitted-key fix: an evaluator-stamped kwarg must never split or hide a
# design's identity (real bug, run 20260927T012131 -- D011's bo/datagen.py
# stamped an override kwarg into experiment_sample._input_data as a side
# effect of execute(); the old coord key, computed on the buffered/stored
# sample AFTER that stamp, differed between a failed run and its corrected
# re-evaluation, so supersede() silently left the stale row in the store
# alongside the corrected one, and a plain retry with a different stamped
# value was never recognised as a duplicate either).
# ---------------------------------------------------------------------------


class _StampingGenerator(DataGenerator):
    """Mirrors D011's pattern: an override kwarg gets written INTO the
    sample's _input_data as a side effect of execute(), never part of what
    the caller originally submitted."""

    def execute(self, experiment_sample, **kwargs):
        override = kwargs.get("override")
        if override is not None:
            experiment_sample._input_data["override"] = override
        experiment_sample._output_data["f"] = (
            -999.0 if override == "K1" else 42.0)
        experiment_sample.job_status = JobStatus.FINISHED
        return experiment_sample


def test_supersede_replaces_stale_row_despite_a_stamped_override_kwarg(
    tmp_path,
):
    """The (a) repro: after supersede with a DIFFERENT stamped override,
    exactly 1 row survives, carrying the corrected value -- the stale row
    (stamped with the original, failed override) must not remain."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    bad = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    bad.execute(_make_sample(0.5), override="K1")   # bad config -> f=-999.0

    fixer = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=tmp_path,
        delegation_id="D002", flush_every=1)
    fixer.supersede(_make_sample(0.5), override="K2")  # corrected -> f=42.0

    df_in, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 1, (
        f"stale row survived alongside the corrected one: "
        f"{df_out.to_dict('records')}")
    assert float(df_out["f"].iloc[0]) == 42.0


def test_dedup_on_write_recognises_a_retry_with_a_different_stamped_kwarg(
    tmp_path,
):
    """A plain retry (not supersede) of the same submitted design, whose
    evaluator stamps a DIFFERENT override the second time, must still be
    recognised as a duplicate -- the retry-duplication class ("~30h/2-
    designs") that a naive per-row-including-stamped-columns key
    reintroduces the moment any evaluator stamps a kwarg."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    gen.execute(_make_sample(0.5), override="K1")
    gen.execute(_make_sample(0.5), override="K2")  # retry, different stamp

    df_in, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 1, (
        f"retry was not recognised as a duplicate: "
        f"{df_out.to_dict('records')}")


def test_a_genuinely_submitted_extra_input_still_distinguishes_two_designs(
    tmp_path,
):
    """The fix must not erase real design dimensions -- an extra column the
    CALLER submits as part of _input_data from the start (never stamped by
    the evaluator) still makes two designs genuinely different."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    class _Plain(DataGenerator):
        def execute(self, experiment_sample, **kwargs):
            experiment_sample._output_data["f"] = (
                experiment_sample._input_data.get("num_eigen", 0) * 1.0)
            experiment_sample.job_status = JobStatus.FINISHED
            return experiment_sample

    def _sample_with_eigen(x0, num_eigen):
        return ExperimentSample(
            _input_data={"x0": x0, "num_eigen": num_eigen},
            _output_data={}, job_status=JobStatus.OPEN)

    gen = InstrumentedDataGenerator(
        inner=_Plain(), store_dir=tmp_path, delegation_id="D001",
        flush_every=1)
    gen.execute(_sample_with_eigen(0.5, 5))
    gen.execute(_sample_with_eigen(0.5, 10))

    df_in, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 2, (
        "a genuinely submitted extra input was collapsed as a duplicate: "
        f"{df_in.to_dict('records')}")
    assert sorted(df_in["num_eigen"]) == [5.0, 10.0]


def test_submitted_key_fix_never_crosses_namespaces(tmp_path):
    """Each design namespace is its own store_dir (get_evaluator(namespace=)
    resolves a distinct nested store) -- the subset-match fix must never
    reach into a DIFFERENT namespace's rows just because a generator is
    reused across them. Two separate InstrumentedDataGenerators, two
    separate store_dirs, sharing the same stamping evaluator: a supersede
    in one must not touch the other's stale row."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    ns_a = tmp_path / "ns_a"
    ns_b = tmp_path / "ns_b"

    bad_a = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=ns_a,
        delegation_id="D001", flush_every=1)
    bad_a.execute(_make_sample(0.5), override="K1")

    bad_b = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=ns_b,
        delegation_id="D001", flush_every=1)
    bad_b.execute(_make_sample(0.5), override="K1")

    fixer_a = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=ns_a,
        delegation_id="D002", flush_every=1)
    fixer_a.supersede(_make_sample(0.5), override="K2")

    _, df_out_a = ExperimentData.from_file(project_dir=ns_a).to_pandas()
    _, df_out_b = ExperimentData.from_file(project_dir=ns_b).to_pandas()
    assert len(df_out_a) == 1 and float(df_out_a["f"].iloc[0]) == 42.0
    # ns_b's row is untouched by ns_a's supersede -- still the stale K1 value.
    assert len(df_out_b) == 1 and float(df_out_b["f"].iloc[0]) == -999.0


# ---------------------------------------------------------------------------
# mode="parallel" host-safety hard cap
# ---------------------------------------------------------------------------


def _make_call_data(*x0s) -> ExperimentData:
    domain = _make_domain()
    samples = {i: _make_sample(x0) for i, x0 in enumerate(x0s)}
    return ExperimentData.from_data(data=samples, domain=domain)


def test_call_mode_parallel_is_refused_before_it_spawns_anything(tmp_path):
    """gen.call(mode='parallel') must never reach f3dasm's local
    multiprocessing.Pool — it would spawn N Abaqus-style solves as
    subprocesses on the run's own shared, resource-constrained orchestration
    node (CPU oversubscription + OOM that kills the whole run). A
    documentation warning is not a control; refusing the call itself is."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path, delegation_id="D001")
    data = _make_call_data(0.1, 0.2)
    with pytest.raises(ValueError, match="mode='parallel'"):
        gen.call(data, mode="parallel", nodes=4)


def test_call_mode_sequential_still_delegates_normally(tmp_path):
    """The hard cap targets mode='parallel' specifically — sequential must be
    completely unaffected, still evaluating every sample via execute()."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_SumGenerator(), store_dir=tmp_path, delegation_id="D001")
    data = _make_call_data(0.1, 0.2)
    result = gen.call(data, mode="sequential")
    _, df_out = result.to_pandas()
    assert sorted(float(v) for v in df_out["f"]) == [0.1, 0.2]


# ---------------------------------------------------------------------------
# A dedup-on-write skip must never pass silently (real bug, run
# 20260927T012131: the only trace of a discarded, already-computed
# evaluation was a DEDUP_SKIPPED diagnostics.jsonl line the calling agent
# never reads; the agent's own script only sees its stdout).
# ---------------------------------------------------------------------------


def test_dedup_skip_of_a_differing_retry_notifies_and_flags_the_difference(
    tmp_path, capsys,
):
    """A retry with a DIFFERENT stamped override (so its computed output
    genuinely differs from the stored row's) must: emit an in-band stdout
    notice naming the design, NOT store a second row, and explicitly show
    both the new and stored output values."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    gen.execute(_make_sample(0.5), override="K1")  # f=-999.0, stored
    capsys.readouterr()  # discard the first flush's output
    gen.execute(_make_sample(0.5), override="K2")  # f=42.0, would differ

    out = capsys.readouterr().out
    assert "EVAL NOT STORED" in out
    assert "D001" in out
    assert "x0=0.5" in out
    assert "supersede" in out
    assert "differs from the STORED row" in out
    assert "-999.0" in out and "42.0" in out

    df_in, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 1
    assert float(df_out["f"].iloc[0]) == -999.0


def test_dedup_skip_of_an_identical_retry_notifies_without_differs_flag(
    tmp_path, capsys,
):
    """An exact retry (same stamped override, same computed output) must
    still notify -- compute was spent either way -- but must NOT claim the
    outputs differ, since they don't."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    gen = InstrumentedDataGenerator(
        inner=_StampingGenerator(), store_dir=tmp_path,
        delegation_id="D001", flush_every=1)
    gen.execute(_make_sample(0.5), override="K1")  # f=-999.0, stored
    capsys.readouterr()
    gen.execute(_make_sample(0.5), override="K1")  # identical retry

    out = capsys.readouterr().out
    assert "EVAL NOT STORED" in out
    assert "x0=0.5" in out
    assert "differs from the STORED row" not in out

    df_in, df_out = ExperimentData.from_file(project_dir=tmp_path).to_pandas()
    assert len(df_out) == 1
