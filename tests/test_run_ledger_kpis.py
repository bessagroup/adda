"""run_ledger process KPIs: ERROR_RETURN count, time/evals to first feasible
row, best-so-far trace (CLAUDE.md §1 step 5, finding 10)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_STUDIES = Path(__file__).resolve().parents[1] / "studies"
if str(_STUDIES) not in sys.path:
    sys.path.insert(0, str(_STUDIES))

import run_ledger  # noqa: E402


def _run(tmp_path: Path, objective: list, started: float = 1000.0) -> Path:
    run_dir = tmp_path / "studyX" / "runs" / "20260101T000000"
    debug = run_dir / "debug"
    debug.mkdir(parents=True)
    (debug / "run_started_at").write_text(str(started))
    (debug / "diagnostics.jsonl").write_text(
        "".join(json.dumps({"error_type": t}) + "\n"
                for t in ("ERROR_RETURN", "ERROR_RETURN", "BUDGET_WARN")))
    d = run_dir / "experiment_data" / "experiment_data"
    d.mkdir(parents=True)
    lines = [",f,_ts"]
    for i, v in enumerate(objective):
        # 1970-01-01T00:20:00+00:00 == epoch 1200 -> 200 s after start
        lines.append(f"{i},{v},1970-01-01T00:{20 + i:02d}:00+00:00")
    (d / "output.csv").write_text("\n".join(lines) + "\n")
    return run_dir


def test_kpis_skip_sentinels_and_report_first_feasible(tmp_path):
    row = run_ledger.extract(_run(tmp_path, ["nan", -1e9, 5.0, 3.0, 4.0]))
    assert row["error_returns"] == 2
    assert row["first_feasible_eval"] == 3
    assert row["first_feasible_s"] == 200.0 + 2 * 60
    tr = json.loads(row["best_trace"])
    assert tr["obj"] == "f" and tr["n"] == [1, 2, 3, 4, 5]
    assert tr["min"] == [None, None, 5.0, 3.0, 3.0]
    assert tr["max"] == [None, None, 5.0, 5.0, 5.0]


def test_no_feasible_row_leaves_first_feasible_blank(tmp_path):
    row = run_ledger.extract(_run(tmp_path, ["nan", 1e9]))
    assert row["first_feasible_eval"] == ""
    assert row["first_feasible_s"] == ""


def test_no_diagnostics_means_zero_error_returns(tmp_path):
    run_dir = tmp_path / "s" / "runs" / "r"
    (run_dir / "debug").mkdir(parents=True)
    assert run_ledger.extract(run_dir)["error_returns"] == 0
