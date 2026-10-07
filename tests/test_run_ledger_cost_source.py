"""The ledger's cost and token columns come from telemetry, not the notebook.

Tokens and cost used to be parsed back out of `solution.md` or the stamped
`pipeline.ipynb`. Two consequences, both of which bite the ablation work:

1. A study with `pipeline_deliverable: false` has neither file, so the run
   recorded NO cost data at all (BACKLOG #31).
2. Only two of the four token fields were carried. The two dropped ones are
   the cache fields — precisely what a prompt-section change moves.

`debug/telemetry/summary.json` is written per LLM call regardless of
deliverable shape and carries all four, so it is the source of truth. The
deliverable-derived path stays as a fallback for runs predating telemetry and
must never overwrite a telemetry value.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_STUDIES = Path(__file__).resolve().parents[1] / "studies"
if str(_STUDIES) not in sys.path:
    sys.path.insert(0, str(_STUDIES))

import run_ledger  # noqa: E402

_TOTALS = {
    "calls": 4,
    "cost_calls": 4,
    "input_tokens": 1200,
    "output_tokens": 340,
    "cache_read_input_tokens": 90_000,
    "cache_creation_input_tokens": 5_000,
    "total_cost_usd": 1.2345,
    "total_tokens": 1540,
    "wall_time_s": 500.0,
}


def _run(tmp_path: Path, *, totals=None, status_extra=None) -> Path:
    run_dir = tmp_path / "studyX" / "runs" / "20260101T000000"
    debug = run_dir / "debug"
    debug.mkdir(parents=True)
    status = {"status": "GATED", "wall_s": 1234.5}
    status.update(status_extra or {})
    (debug / "run_status.json").write_text(json.dumps(status))
    if totals is not None:
        (debug / "telemetry").mkdir()
        (debug / "telemetry" / "summary.json").write_text(
            json.dumps({"totals": totals, "by_role": {}}))
    return run_dir


def test_a_run_with_no_notebook_still_records_its_cost(tmp_path):
    """The BACKLOG #31 regression: no deliverable used to mean no cost row."""
    row = run_ledger.extract(_run(tmp_path, totals=_TOTALS))

    assert row["cost_usd"] == 1.2345
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 340


def test_the_cache_token_fields_are_recorded(tmp_path):
    """Dropped by the deliverable-derived path, and the ones a prompt-section
    ablation actually changes."""
    row = run_ledger.extract(_run(tmp_path, totals=_TOTALS))

    assert row["cache_read_tokens"] == 90_000
    assert row["cache_creation_tokens"] == 5_000


def test_an_unpriced_run_leaves_cost_blank_rather_than_zero(tmp_path):
    """Open-weight backends report no price. Blank means unmeasured; 0 would
    claim the run was free, which is a different and false statement."""
    totals = dict(_TOTALS, total_cost_usd=None, cost_calls=0)
    row = run_ledger.extract(_run(tmp_path, totals=totals))

    assert row["cost_usd"] == ""
    # tokens are still fully measured — only the price is unavailable
    assert row["input_tokens"] == 1200


def test_wall_s_comes_from_the_runs_own_close(tmp_path):
    """Preferred over telemetry's first-call-to-last-call span, which excludes
    setup and the final write."""
    row = run_ledger.extract(_run(tmp_path, totals=_TOTALS))

    assert row["wall_s"] == 1234.5


def test_a_run_predating_telemetry_still_reports_cost(tmp_path):
    """The deliverable-derived fallback must keep working for old run dirs."""
    run_dir = _run(tmp_path, totals=None)
    (run_dir.parent.parent / "solution.md").write_text(
        "- input_tokens: 111\n- output_tokens: 222\n"
        "- estimated_cost: $0.5000\n- time_used: 00:10:00\n"
    )

    row = run_ledger.extract(run_dir)

    assert row["input_tokens"] == "111"
    assert row["cost_usd"] == "0.5000"
    assert row["time_used"] == "00:10:00"


def test_the_deliverable_never_overwrites_a_telemetry_value(tmp_path):
    """Telemetry counts every call; the notebook stamp is a summary written
    once. Where they disagree, the instrument wins."""
    run_dir = _run(tmp_path, totals=_TOTALS)
    (run_dir.parent.parent / "solution.md").write_text(
        "- input_tokens: 111\n- output_tokens: 222\n"
        "- estimated_cost: $0.5000\n- time_used: 00:10:00\n"
    )

    row = run_ledger.extract(run_dir)

    assert row["input_tokens"] == 1200
    assert row["cost_usd"] == 1.2345
    # time_used has no telemetry equivalent, so it still comes from here
    assert row["time_used"] == "00:10:00"


def test_ledger_header_migrates_when_a_column_is_added(tmp_path, monkeypatch):
    import csv as _csv
    old = [c for c in run_ledger.COLUMNS if c != "cost_usd_computed"]
    led = tmp_path / "run_ledger.csv"
    with led.open("w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=old)
        w.writeheader()
        w.writerow({c: "x" for c in old})
    monkeypatch.setattr(run_ledger, "LEDGER", led)
    run_ledger._migrate_header()
    rows = list(_csv.DictReader(led.open()))
    assert list(rows[0]) == run_ledger.COLUMNS
    assert rows[0]["cost_usd_computed"] == "" and rows[0]["study"] == "x"


_SCHEMA_TOTALS = dict(
    _TOTALS, normalized_calls=4, legacy_calls=0, fresh_input=1200,
    cache_read=90_000, cache_write=5_000, output=340, tokens_total=96_540)


def test_normalized_token_columns_come_from_the_schema(tmp_path):
    row = run_ledger.extract(_run(tmp_path, totals=_SCHEMA_TOTALS))

    assert row["tokens_schema"] == "normalized"
    assert (row["tokens_fresh_input"], row["tokens_cache_read"],
            row["tokens_cache_write"], row["tokens_output"],
            row["tokens_total"]) == (1200, 90_000, 5_000, 340, 96_540)


def test_a_mixed_run_is_marked_legacy_with_blank_schema_columns(tmp_path):
    """One call without the schema makes the run's total incomparable: the
    columns stay blank rather than hold a partial sum."""
    row = run_ledger.extract(
        _run(tmp_path, totals=dict(_SCHEMA_TOTALS, legacy_calls=1)))

    assert row["tokens_schema"] == "legacy"
    assert row["tokens_total"] == ""


def test_telemetry_without_the_schema_is_legacy(tmp_path):
    row = run_ledger.extract(_run(tmp_path, totals=_TOTALS))

    assert row["tokens_schema"] == "legacy"
    assert row["tokens_total"] == ""
    assert row["input_tokens"] == 1200   # the old column is untouched


def test_old_ledger_rows_keep_their_values_and_get_blank_schema(tmp_path, monkeypatch):
    """Adding the columns migrates the header; no old value changes. Blank
    `tokens_schema` reads as legacy (see COLUMNS)."""
    ledger = tmp_path / "run_ledger.csv"
    ledger.write_text("commit,study,run_id,input_tokens\nabc,s,r1,777\n")
    monkeypatch.setattr(run_ledger, "LEDGER", ledger)
    run_ledger._migrate_header()
    import csv
    rows = list(csv.DictReader(ledger.open()))
    assert rows[0]["input_tokens"] == "777"
    assert rows[0]["tokens_schema"] == ""
    assert "tokens_total" in rows[0]
