"""config.yaml `objective:` block: validated at run start, recorded in
run_config.json, and the one definition of "counts" for the run ledger and the
viewer's figure of merit. Finite-but-infeasible rows must never count."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from adda._src.evaluation.objective import (
    best_so_far,
    label,
    objective_values,
    parse_objective,
)
from adda._src.runtime.run_setup import _init_canonical_store
from adda._src.viewer.readers import read_figure_of_merit

_STUDIES = Path(__file__).resolve().parents[1] / "studies"
if str(_STUDIES) not in sys.path:
    sys.path.insert(0, str(_STUDIES))

import run_ledger  # noqa: E402

_DECL = {"column": "sigma_peak", "direction": "max", "feasible": "feasible"}


def test_parse_accepts_a_full_block_and_none():
    assert parse_objective(None) is None
    assert parse_objective(_DECL, ["sigma_peak", "feasible"]) == _DECL
    assert parse_objective({"column": "y", "direction": "min"}) == {
        "column": "y", "direction": "min"}


@pytest.mark.parametrize("raw, msg", [
    ({"column": "y"}, "direction"),
    ({"direction": "max"}, "column"),
    ({"column": "y", "direction": "up"}, "direction"),
    ({"column": "y", "direction": "max", "extra": 1}, "unknown key"),
    ({"column": "y", "direction": "max", "feasible": 3}, "feasible"),
    ("sigma", "mapping"),
])
def test_parse_refuses_malformed_blocks(raw, msg):
    with pytest.raises(ValueError, match=msg):
        parse_objective(raw)


def test_parse_refuses_a_column_the_oracle_does_not_produce():
    with pytest.raises(ValueError, match="sigma_pek"):
        parse_objective({"column": "sigma_pek", "direction": "max"}, ["sigma_peak"])
    with pytest.raises(ValueError, match="feasible"):
        parse_objective(_DECL, ["sigma_peak"])


def test_run_start_refuses_an_unknown_objective_column_and_records_a_good_one(tmp_path):
    ev = {"entrypoint": "w/e.py:f", "output_names": ["sigma_peak", "feasible"]}
    for r in ("r1", "r2", "r3"):
        (tmp_path / r / "debug").mkdir(parents=True)
    with pytest.raises(ValueError, match="oracle produces"):
        _init_canonical_store(
            tmp_path / "r1", tmp_path, evaluator_config=ev,
            objective_config={"column": "nope", "direction": "max"})
    cfg = _init_canonical_store(
        tmp_path / "r2", tmp_path, evaluator_config=ev, objective_config=_DECL)
    assert cfg["objective"] == _DECL
    on_disk = json.loads((tmp_path / "r2" / "debug" / "run_config.json").read_text())
    assert on_disk["objective"] == _DECL
    assert _init_canonical_store(tmp_path / "r3", tmp_path)["objective"] is None


def _rows():
    # s55r2-shaped: every sigma is finite, but only the feasible==1 rows count
    return [
        {"sigma_peak": "9.0", "feasible": "0"},
        {"sigma_peak": "3.0", "feasible": "1"},
        {"sigma_peak": "7.0", "feasible": "True"},
        {"sigma_peak": "nan", "feasible": "1"},
        {"sigma_peak": "1e9", "feasible": "1"},
        {"sigma_peak": "5.0", "feasible": ""},
    ]


def test_finite_but_infeasible_rows_do_not_count():
    vals = objective_values(_rows(), _DECL, "sigma_peak")
    assert vals == [None, 3.0, 7.0, None, None, None]
    assert best_so_far(vals, _DECL) == {"best": [None, 3.0, 7.0, 7.0, 7.0, 7.0]}
    undeclared = objective_values(_rows(), None, "sigma_peak")
    assert undeclared == [9.0, 3.0, 7.0, None, None, 5.0]
    assert set(best_so_far(undeclared, None)) == {"min", "max"}


def test_label():
    assert label(None) == "undeclared"
    assert label(_DECL) == "sigma_peak:max:feasible=feasible"


def _run(tmp_path: Path, objective) -> Path:
    run = tmp_path / "studyX" / "runs" / "20260101T000000"
    (run / "debug").mkdir(parents=True)
    (run / "debug" / "run_started_at").write_text("1000.0")
    (run / "debug" / "run_config.json").write_text(json.dumps({"objective": objective}))
    d = run / "experiment_data" / "experiment_data"
    d.mkdir(parents=True)
    lines = [",sigma_peak,feasible,_ts"]
    for i, r in enumerate(_rows()):
        lines.append(f"{i},{r['sigma_peak']},{r['feasible']},"
                     f"1970-01-01T00:{20 + i:02d}:00+00:00")
    (d / "output.csv").write_text("\n".join(lines) + "\n")
    (d / "jobs.csv").write_text(",0\n" + "".join(f"{i},FINISHED\n" for i in range(6)))
    return run


def test_ledger_uses_the_declared_objective(tmp_path):
    row = run_ledger.extract(_run(tmp_path, _DECL))
    assert row["objective"] == "sigma_peak:max:feasible=feasible"
    assert row["first_feasible_eval"] == 2          # row 0 is finite but infeasible
    assert row["first_feasible_s"] == 200.0 + 60
    tr = json.loads(row["best_trace"])
    assert tr["best"][-1] == 7.0 and "min" not in tr


def test_ledger_marks_an_undeclared_objective(tmp_path):
    row = run_ledger.extract(_run(tmp_path, None))
    assert row["objective"] == "undeclared"
    assert row["first_feasible_eval"] == 1
    assert {"min", "max"} <= set(json.loads(row["best_trace"]))


def test_figure_of_merit_is_the_declared_best_counted_row(tmp_path):
    fom = read_figure_of_merit(_run(tmp_path, _DECL))
    assert (fom["declared"], fom["value"], fom["row"]) == (True, 7.0, 2)
    assert (fom["n"], fom["n_counted"]) == (6, 2)
    mn = read_figure_of_merit(_run(tmp_path / "m", {**_DECL, "direction": "min"}))
    assert (mn["value"], mn["row"]) == (3.0, 1)


def test_figure_of_merit_ranks_nothing_when_undeclared(tmp_path):
    assert read_figure_of_merit(_run(tmp_path, None)) == {"declared": False}
