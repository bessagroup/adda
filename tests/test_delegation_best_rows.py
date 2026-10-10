"""A delegation's report carries the evaluator's FULL output row for its best
design, not only the target metric (telephone audit: 'nu without E_min').
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from adda._src.evaluation.ledger_summary import delegation_best_rows
from adda._src.nodes.tools.routing.delegation import WorkerSession
from f3dasm._src.design.domain import Domain
from f3dasm._src.experimentdata import ExperimentData
from f3dasm._src.experimentsample import ExperimentSample, JobStatus

OUTS = ("E_min", "nu_max", "feasible")


def _store(store_dir: Path, rows) -> None:
    domain = Domain()
    domain.add_float("x0", 0.0, 10.0)
    for k in (*OUTS, "_delegation_id", "_source", "_ts"):
        domain.add_output(k, exist_ok=True)
    samples = {}
    for i, (e, nu, feas, did) in enumerate(rows):
        samples[i] = ExperimentSample(
            _input_data={"x0": float(i)},
            _output_data={"E_min": e, "nu_max": nu, "feasible": feas,
                          "_delegation_id": did, "_source": "oracle",
                          "_ts": "2026-01-01T00:00:00+00:00"},
            job_status=JobStatus.FINISHED,
        )
    ExperimentData.from_data(data=samples, domain=domain).store(
        project_dir=store_dir)


ROWS = [
    (0.02, -0.40, 1.0, "D001"),
    (0.0001, -0.55, 1.0, "D001"),     # best nu, but E_min tiny: must show together
    (1000.0, 1000.0, 0.0, "D001"),    # infeasible sentinel row
    (0.5, -0.9, 1.0, "D002"),         # another delegation's row
]
OBJ = {"column": "nu_max", "direction": "min", "feasible": "feasible"}


def test_declared_objective_reports_every_output_of_the_best_row(tmp_path):
    _store(tmp_path, ROWS)
    out = delegation_best_rows(tmp_path, "D001", OBJ, OUTS)
    assert "E_min=0.0001" in out and "nu_max=-0.55" in out and "feasible=1.0" in out
    assert "-0.9" not in out          # D002's row is not this delegation's


def test_undeclared_objective_reports_both_extremes_and_says_so(tmp_path):
    _store(tmp_path, ROWS)
    out = delegation_best_rows(tmp_path, "D001", None, OUTS)
    assert "direction not guessed" in out
    assert "min: E_min=0.0001" in out and "max: E_min=1000.0" in out
    assert "nu_max=-0.55" in out


def test_output_names_default_to_store_columns_without_provenance(tmp_path):
    _store(tmp_path, ROWS)
    out = delegation_best_rows(tmp_path, "D001", OBJ, None)
    assert "E_min=" in out and "_source" not in out and "_ts" not in out


def test_no_counting_row_or_unknown_delegation_gives_none(tmp_path):
    _store(tmp_path, ROWS)
    assert delegation_best_rows(tmp_path, "D009", OBJ, OUTS) is None
    _store(tmp_path / "b", [(1000.0, 1000.0, 0.0, "D001")])
    assert delegation_best_rows(tmp_path / "b", "D001", OBJ, OUTS) is None
    assert delegation_best_rows(tmp_path / "missing", "D001", OBJ, OUTS) is None


def _session(run: Path, did: str):
    s = SimpleNamespace(
        node=SimpleNamespace(_current_notes_dir=run / "notes" / "x"),
        delegation_id=did)
    s._run_experiment_root = lambda: run / "experiment_data"
    return s


def test_report_block_reads_names_and_objective_from_run_config_per_namespace(tmp_path):
    run = tmp_path
    (run / "notes" / "x").mkdir(parents=True)
    (run / "debug").mkdir()
    _store(run / "experiment_data", ROWS)
    _store(run / "experiment_data" / "ring", [(0.3, -0.2, 1.0, "D003")])
    (run / "debug" / "run_config.json").write_text(json.dumps({
        "objective": OBJ, "evaluator_output_names": list(OUTS),
        "oracles": {"ring": {"evaluator_output_names": ["nu_max", "E_min"]}}}))
    a = WorkerSession._best_rows_block(_session(run, "D001"))
    assert "BEST ROW(S) (D001" in a and "E_min=0.0001" in a
    b = WorkerSession._best_rows_block(_session(run, "D003"))
    assert "[ring]" in b and "nu_max=-0.2, E_min=0.3" in b


def test_report_block_never_raises(tmp_path):
    s = SimpleNamespace(node=SimpleNamespace(_current_notes_dir=None),
                        delegation_id="D001")
    s._run_experiment_root = lambda: None
    assert WorkerSession._best_rows_block(s) == ""
