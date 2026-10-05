"""GET /api/runs/{id}/literature — corpus split by run window, errors, degraded."""
import csv
import time

from starlette.testclient import TestClient

from adda._src.viewer.app import create_app
from tests.test_viewer_app import _make_run, _make_study, _write_jsonl

RUN = "20260904T120000"
START = 1_800_000_000.0  # epoch; the run window opens here


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _setup(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, RUN)
    debug = run_dir / "debug"
    (debug / "run_started_at").write_text(str(START))
    (debug / "run_status.json").write_text("{}")
    import os
    os.utime(debug / "run_status.json", (START + 100, START + 100))
    corpus = study / "runs" / "lit_reviewer_notes"
    corpus.mkdir(parents=True)
    with (corpus / "corpus.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["paper_id", "title", "added_at"])
        w.writeheader()
        w.writerow({"paper_id": "old", "title": "Earlier run",
                    "added_at": _iso(START - 500)})
        w.writerow({"paper_id": "new", "title": "This run",
                    "added_at": _iso(START + 50)})
        w.writerow({"paper_id": "later", "title": "After close",
                    "added_at": _iso(START + 500)})
        w.writerow({"paper_id": "undated", "title": "No stamp",
                    "added_at": ""})
    _write_jsonl(debug / "diagnostics.jsonl", [
        {"tool": "SearchPapers", "error_type": "ERROR_RETURN",
         "message": "source cooling down"},
        {"tool": "WriteCell", "error_type": "ERROR_RETURN", "message": "x"},
        {"tool": "RETRIEVAL_DEGRADED", "message": "bm25 only"}])
    return TestClient(create_app(study))


def test_corpus_is_split_by_the_run_window(tmp_path):
    body = _setup(tmp_path).get(f"/api/runs/{RUN}/literature").json()
    assert {p["paper_id"]: p["in_run"] for p in body["papers"]} == {
        "old": False, "new": True, "later": False, "undated": None}
    assert body["total"] == 4 and body["added_in_run"] == 1
    assert body["run_window"]["started"] == START


def test_only_literature_tool_errors_and_degradation_are_reported(tmp_path):
    body = _setup(tmp_path).get(f"/api/runs/{RUN}/literature").json()
    assert [e["tool"] for e in body["errors"]] == ["SearchPapers"]
    assert [d["message"] for d in body["degraded"]] == ["bm25 only"]


def test_no_corpus_is_empty_and_unknown_run_404(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, RUN)
    c = TestClient(create_app(study))
    body = c.get(f"/api/runs/{RUN}/literature").json()
    assert body["papers"] == [] and body["errors"] == []
    assert c.get("/api/runs/nope/literature").status_code == 404
