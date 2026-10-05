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
    parse_funnel,
    parse_objective,
)
from adda._src.runtime.run_setup import _init_canonical_store
from adda._src.viewer.readers import read_figure_of_merit

_STUDIES = Path(__file__).resolve().parents[1] / "studies"
if str(_STUDIES) not in sys.path:
    sys.path.insert(0, str(_STUDIES))

import run_ledger  # noqa: E402

_DECL = {"column": "score", "direction": "max", "feasible": "feasible"}


def test_parse_accepts_a_full_block_and_none():
    assert parse_objective(None) is None
    assert parse_objective(_DECL, ["score", "feasible"]) == _DECL
    assert parse_objective({"column": "y", "direction": "min"}) == {
        "column": "y", "direction": "min"}


@pytest.mark.parametrize("raw, msg", [
    ({"column": "y"}, "direction"),
    ({"direction": "max"}, "column"),
    ({"column": "y", "direction": "up"}, "direction"),
    ({"column": "y", "direction": "max", "extra": 1}, "unknown key"),
    ({"column": "y", "direction": "max", "feasible": 3}, "feasible"),
    ("score", "mapping"),
])
def test_parse_refuses_malformed_blocks(raw, msg):
    with pytest.raises(ValueError, match=msg):
        parse_objective(raw)


def test_parse_refuses_a_column_the_oracle_does_not_produce():
    with pytest.raises(ValueError, match="scor"):
        parse_objective({"column": "scor", "direction": "max"}, ["score"])
    with pytest.raises(ValueError, match="feasible"):
        parse_objective(_DECL, ["score"])


def test_run_start_refuses_an_unknown_objective_column_and_records_a_good_one(tmp_path):
    ev = {"entrypoint": "w/e.py:f", "output_names": ["score", "feasible"]}
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
    # namespaced-run-shaped: every score is finite, but only the feasible==1 rows count
    return [
        {"score": "9.0", "feasible": "0"},
        {"score": "3.0", "feasible": "1"},
        {"score": "7.0", "feasible": "True"},
        {"score": "nan", "feasible": "1"},
        {"score": "1e9", "feasible": "1"},
        {"score": "5.0", "feasible": ""},
    ]


def test_finite_but_infeasible_rows_do_not_count():
    vals = objective_values(_rows(), _DECL, "score")
    assert vals == [None, 3.0, 7.0, None, None, None]
    assert best_so_far(vals, _DECL) == {"best": [None, 3.0, 7.0, 7.0, 7.0, 7.0]}
    undeclared = objective_values(_rows(), None, "score")
    assert undeclared == [9.0, 3.0, 7.0, None, None, 5.0]
    assert set(best_so_far(undeclared, None)) == {"min", "max"}


def test_label():
    assert label(None) == "undeclared"
    assert label(_DECL) == "score:max:feasible=feasible"


def _run(tmp_path: Path, objective) -> Path:
    run = tmp_path / "studyX" / "runs" / "20260101T000000"
    (run / "debug").mkdir(parents=True)
    (run / "debug" / "run_started_at").write_text("1000.0")
    (run / "debug" / "run_config.json").write_text(json.dumps({"objective": objective}))
    d = run / "experiment_data" / "experiment_data"
    d.mkdir(parents=True)
    lines = [",score,feasible,_ts"]
    for i, r in enumerate(_rows()):
        lines.append(f"{i},{r['score']},{r['feasible']},"
                     f"1970-01-01T00:{20 + i:02d}:00+00:00")
    (d / "output.csv").write_text("\n".join(lines) + "\n")
    (d / "jobs.csv").write_text(",0\n" + "".join(f"{i},FINISHED\n" for i in range(6)))
    return run


def test_ledger_uses_the_declared_objective(tmp_path):
    row = run_ledger.extract(_run(tmp_path, _DECL))
    assert row["objective"] == "score:max:feasible=feasible"
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


def test_a_run_without_a_recorded_objective_falls_back_to_the_study_config_and_says_so(tmp_path):
    run = _run(tmp_path, None)
    study = run.parent.parent
    (study / "config.yaml").write_text("objective:\n  column: score\n  direction: max\n  feasible: feasible\n")
    fom = read_figure_of_merit(run, study)
    assert (fom["declared"], fom["value"], fom["row"]) == (True, 7.0, 2)
    assert fom["from_study_config"] is True
    assert read_figure_of_merit(run) == {"declared": False}      # no study: nothing is inferred


def test_a_recorded_objective_is_never_labelled_as_a_fallback(tmp_path):
    run = _run(tmp_path, _DECL)
    study = run.parent.parent
    (study / "config.yaml").write_text("objective:\n  column: score\n  direction: min\n")
    fom = read_figure_of_merit(run, study)
    assert fom["from_study_config"] is False and fom["direction"] == "max"


def test_an_invalid_study_objective_is_not_applied(tmp_path):
    run = _run(tmp_path, None)
    study = run.parent.parent
    (study / "config.yaml").write_text("objective:\n  column: score\n")
    assert read_figure_of_merit(run, study) == {"declared": False}


# ---- a study whose oracle is authored mid-run (no workspace/ at start) ------

def test_declared_objective_with_a_not_yet_existing_oracle_starts(tmp_path):
    (tmp_path / "r" / "debug").mkdir(parents=True)
    ev = {"entrypoint": "workspace/data_generator.py:SomeDataGenerator"}
    cfg = _init_canonical_store(
        tmp_path / "r", tmp_path, evaluator_config=ev, objective_config=_DECL)
    assert cfg["objective"] == _DECL
    assert cfg["evaluator_output_names"] is None


def _register(tmp_path, output_names, objective=_DECL):
    from tests.test_registration_handoff import (
        _build,
        _DataGen,
        _delegate_and_approve,
        _drop_manifest,
    )
    node, closures, run_dir, cfg_path = _build(tmp_path, "datagen", _DataGen())
    cfg = json.loads(cfg_path.read_text())
    cfg["objective"] = objective
    cfg_path.write_text(json.dumps(cfg))
    _drop_manifest(run_dir, "D001")
    man = run_dir / "debug/delegations/D001/generators/registration.json"
    m = json.loads(man.read_text())
    if output_names is None:
        m.pop("output_names")
    else:
        m["output_names"] = output_names
    m["namespace"] = "probe"
    man.write_text(json.dumps(m))
    out = _delegate_and_approve(
        closures, target="datagen", intent="build", expected_report="")
    diag = run_dir / "debug" / "diagnostics.jsonl"
    events = [json.loads(line) for line in diag.read_text().splitlines()
              ] if diag.exists() else []
    notes = list(node._notifications)
    return cfg_path, events, notes, out


def test_registering_an_oracle_without_the_feasible_column_is_reported(tmp_path):
    cfg_path, events, notes, out = _register(tmp_path, ["score"])
    missing = [e for e in events if e["error_type"] == "OBJECTIVE_COLUMN_MISSING"]
    assert [(e["namespace"], e["column"], e["key"]) for e in missing] == [
        ("probe", "feasible", "feasible")]
    assert any("OBJECTIVE_COLUMN_MISSING" in n and "'feasible'" in n
               for n in notes + [out])
    assert "probe" in json.loads(cfg_path.read_text())["oracles"]  # not refused


def test_registering_an_oracle_with_every_objective_column_is_silent(tmp_path):
    _, events, notes, _ = _register(tmp_path, ["score", "feasible"])
    assert not [e for e in events if e["error_type"] == "OBJECTIVE_COLUMN_MISSING"]
    assert not [n for n in notes if "OBJECTIVE_COLUMN_MISSING" in n]


def test_a_manifest_without_output_names_is_reported_only_under_an_objective(tmp_path):
    _, events, notes, out = _register(tmp_path / "a", None)
    missing = [e for e in events if e["error_type"] == "OBJECTIVE_COLUMN_MISSING"]
    assert [(e["namespace"], e["key"]) for e in missing] == [("probe", "output_names")]
    assert any("output_names is required" in n for n in [*notes, out])
    _, events, notes, out = _register(tmp_path / "b", None, objective=None)
    assert not [e for e in events if e["error_type"] == "OBJECTIVE_COLUMN_MISSING"]
    assert not any("OBJECTIVE_COLUMN_MISSING" in n for n in [*notes, out])


_LINES = [{"value": 2.0, "label": "reference"}, {"value": 20.0, "label": "goal"}]
_UNIT = {"divide_by": 2.0, "label": "x reference"}


def test_parse_keeps_lines_and_unit_label_and_the_viewer_serves_them(tmp_path):
    raw = {**_DECL, "lines": _LINES, "unit_label": _UNIT}
    assert parse_objective(raw) == raw
    assert "lines" not in parse_objective(_DECL)
    run = tmp_path / "runs" / "20260917T120000"
    (run / "debug").mkdir(parents=True)
    (run / "debug" / "run_config.json").write_text(json.dumps({"objective": raw}))
    served = read_figure_of_merit(run)
    assert served["lines"] == _LINES and served["unit_label"] == _UNIT


@pytest.mark.parametrize("extra, msg", [
    ({"lines": {"value": 1, "label": "a"}}, "list"),
    ({"lines": [{"value": 1}]}, "exactly"),
    ({"lines": [{"value": "1", "label": "a"}]}, "finite number"),
    ({"lines": [{"value": True, "label": "a"}]}, "finite number"),
    ({"lines": [{"value": float("inf"), "label": "a"}]}, "finite number"),
    ({"lines": [{"value": 1, "label": "  "}]}, "non-empty"),
    ({"unit_label": {"divide_by": 0, "label": "x"}}, "positive"),
    ({"unit_label": {"divide_by": 2}}, "exactly"),
    ({"unit_label": {"divide_by": 2, "label": ""}}, "non-empty"),
])
def test_parse_refuses_malformed_lines_and_unit_label(extra, msg):
    with pytest.raises(ValueError, match=msg):
        parse_objective({**_DECL, **extra})


def _namespaced_run(tmp_path: Path) -> Path:
    """namespaced-run-shaped: canonical best is 7.0; the 'alpha' namespace holds a
    better counted row (20.0, row 1); 'beta' never recorded score."""
    run = _run(tmp_path, _DECL)
    root = run / "experiment_data"
    ff = root / "alpha" / "experiment_data"
    ff.mkdir(parents=True)
    (ff / "output.csv").write_text(
        ",score,feasible,_ts\n"
        "0,30.0,0,1970-01-01T00:40:00+00:00\n"
        "1,20.0,1,1970-01-01T00:41:00+00:00\n"
        "2,12.0,1,1970-01-01T00:42:00+00:00\n")
    lg = root / "beta" / "experiment_data"
    lg.mkdir(parents=True)
    (lg / "output.csv").write_text(",energy,_ts\n0,1.0,1970-01-01T00:50:00+00:00\n")
    return run


def test_figure_of_merit_scores_every_store_and_names_the_best_rows_namespace(tmp_path):
    fom = read_figure_of_merit(_namespaced_run(tmp_path))
    assert (fom["value"], fom["row"], fom["namespace"]) == (20.0, 1, "alpha")
    assert (fom["n"], fom["n_counted"]) == (9, 4)
    assert [(s["namespace"], s["n_counted"]) for s in fom["scored"]] == [
        (None, 2), ("alpha", 2)]
    assert fom["not_scored"] == [{"namespace": "beta", "n": 1, "missing": ["score", "feasible"]}]


def test_figure_of_merit_canonical_best_has_no_namespace(tmp_path):
    fom = read_figure_of_merit(_run(tmp_path, _DECL))
    assert fom["namespace"] is None and fom["not_scored"] == []


def test_ledger_scores_every_store_and_names_what_it_cannot_score(tmp_path):
    row = run_ledger.extract(_namespaced_run(tmp_path))
    tr = json.loads(row["best_trace"])
    assert tr["best"][-1] == 20.0
    assert tr["stores"] == {"scored": [None, "alpha"],
                            "not_scored": {"beta": ["score", "feasible"]}}
    assert row["first_feasible_eval"] == 2


def test_funnel_declaration_is_validated_like_the_objective_and_recorded(tmp_path):
    assert parse_funnel(None) is None
    assert parse_funnel(["a", "b"], ["a", "b", "c"]) == ["a", "b"]
    for bad in ("a", [], [1], ["a", "a"]):
        with pytest.raises(ValueError, match="funnel"):
            parse_funnel(bad)
    with pytest.raises(ValueError, match="oracle produces"):
        parse_funnel(["a", "zz"], ["a", "b"])
    ev = {"entrypoint": "w/e.py:f", "output_names": ["score", "feasible"]}
    (tmp_path / "r1" / "debug").mkdir(parents=True)
    cfg = _init_canonical_store(tmp_path / "r1", tmp_path, evaluator_config=ev,
                                funnel_config=["feasible"])
    assert cfg["funnel"] == ["feasible"]
    assert json.loads((tmp_path / "r1" / "debug" / "run_config.json").read_text())["funnel"] == ["feasible"]
