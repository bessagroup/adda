"""run_ledger's Verdict audit: closing verdicts next to the evaluations they cite."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_STUDIES = Path(__file__).resolve().parents[1] / "studies"
if str(_STUDIES) not in sys.path:
    sys.path.insert(0, str(_STUDIES))

import run_ledger  # noqa: E402


def _entry(status, delegation=None, posterior=0.1, note="validated: no charter concern"):
    return {"status": status, "comment": "c", "posterior": posterior, "triggered_by": delegation,
            "validator_note": note,
            "evidence": {"delegation": delegation, "numbers": {"n": 1}} if delegation else None}


def _run(tmp_path, ledger):
    run_dir = tmp_path / "s" / "runs" / "20260101T000000"
    (run_dir / "debug").mkdir(parents=True)
    store = run_dir / "experiment_data" / "experiment_data"
    store.mkdir(parents=True)
    (store / "output.csv").write_text(
        ",f,_delegation_id,_source\n0,1,D000,precomputed_pool\n1,2,D001,s\n2,3,D001,s\n")
    ns = run_dir / "experiment_data" / "other" / "experiment_data"
    ns.mkdir(parents=True)
    (ns / "output.csv").write_text(",f,_delegation_id,_source\n0,1,D001,s\n")
    if ledger is not None:
        notes = run_dir / "debug" / "strategizer_notes"
        notes.mkdir()
        (notes / "hypotheses.json").write_text(json.dumps(ledger))
    return run_dir


def test_verdict_audit_counts_rows_across_all_stores_and_flags_a_verdict_with_none(tmp_path):
    ledger = {
        "H1": {"id": "H1", "statement": "S one, in full.", "falsification_criterion": "C one, in full.",
               "prior": 0.5, "status_log": [_entry("OPEN", posterior=0.5), _entry("FALSIFIED", "D001", 0.05)]},
        "H2": {"id": "H2", "statement": "S two.", "falsification_criterion": "C two.",
               "prior": 0.4, "status_log": [_entry("OPEN", posterior=0.4),
                                             _entry("FALSIFIED", "D009", 0.02, note="validated: literature only")]},
        "H3": {"id": "H3", "statement": "S three.", "falsification_criterion": "C three.",
               "prior": 0.3, "status_log": [_entry("OPEN", posterior=0.3)]},
    }
    text = "\n".join(run_ledger.verdict_audit(_run(tmp_path, ledger)))
    assert "1 of 2 closing verdicts cite no new evaluations" in text
    h1 = text.split("### H2")[0]
    assert "H1: OPEN -> FALSIFIED  (posterior 0.5 -> 0.05)" in h1
    assert "N_NEW_EVALS: 3" in h1 and "NO_NEW_EVIDENCE" not in h1
    h2 = text.split("### H2")[1]
    assert "N_NEW_EVALS: 0" in h2 and "NO_NEW_EVIDENCE" in text.split("\n- statement: S two")[0]
    assert "validator_note: validated: literature only" in h2
    assert "S one, in full." in h1 and "C one, in full." in h1
    assert "H3" not in text


def test_verdict_audit_does_not_count_precomputed_pool_rows_as_new_evaluations(tmp_path):
    ledger = {"H1": {"id": "H1", "statement": "s", "falsification_criterion": "c", "prior": 0.5,
                     "status_log": [_entry("FALSIFIED", "D000")]}}
    text = "\n".join(run_ledger.verdict_audit(_run(tmp_path, ledger)))
    assert "N_NEW_EVALS: 0 (plus 1 precomputed-pool rows)" in text and "NO_NEW_EVIDENCE" in text


def test_verdict_audit_without_a_ledger_says_so_and_the_brief_includes_the_section(tmp_path):
    run_dir = _run(tmp_path, None)
    assert run_ledger.verdict_audit(run_dir) == ["- no hypothesis ledger"]
    assert "## Verdict audit" in run_ledger.analysis_brief(run_dir)
