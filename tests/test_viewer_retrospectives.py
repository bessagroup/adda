"""GET /api/runs/{id}/retrospectives — sections, delegation links, missing."""
from starlette.testclient import TestClient

from adda._src.viewer.app import create_app
from adda._src.viewer.readers import _split_retrospective
from tests.test_viewer_app import _make_run, _make_study, _write_jsonl

RUN = "20260904T120000"


def test_both_bullet_styles_split_into_sections():
    pre, sec = _split_retrospective(
        "intro\n- **CONSISTENCY**: ok\n  fine\n- **DECISION**: used EI\n"
        "**FRICTION**: a: b\n\n**BLOCKED**: none\n- TIME: ran out\n(c) fix it\n#### BLOCKED: again")
    assert pre == "intro"
    assert sec == {"CONSISTENCY": "ok\n  fine", "DECISION": "used EI",
                   "FRICTION": "a: b", "BLOCKED": "none\n\nagain",
                   "TIME": "ran out\n(c) fix it"}


def test_unstructured_text_is_kept_as_preamble():
    pre, sec = _split_retrospective("just prose, FRICTION mid-sentence")
    assert pre == "just prose, FRICTION mid-sentence" and sec == {}


def test_endpoint_links_delegations_and_reports_missing(tmp_path):
    study = _make_study(tmp_path)
    debug = _make_run(study, RUN) / "debug"
    _write_jsonl(debug / "delegation_log.jsonl", [
        {"id": "D001", "status": "DONE", "from_node": "strategizer",
         "to_node": "implementer"}])
    _write_jsonl(debug / "retrospectives.jsonl", [
        {"ts": "t1", "source_id": "D001", "role": "implementer",
         "flagged": True, "text": "- **FRICTION**: slow API"},
        {"ts": "t2", "source_id": "critic-1", "role": "critic",
         "flagged": False, "text": "**BLOCKED**: nothing"},
    ])
    _write_jsonl(debug / "diagnostics.jsonl", [
        {"ts": "t3", "node": "(run)", "tool": "RETROSPECTIVES_MISSING",
         "message": "no retrospective from: D002"},
        {"ts": "t4", "node": "x", "tool": "ERROR_RETURN", "message": "e"}])
    body = TestClient(create_app(study)).get(
        f"/api/runs/{RUN}/retrospectives").json()
    first, second = body["retrospectives"]
    assert first["delegation_id"] == "D001" and first["flagged"] is True
    assert first["sections"] == {"FRICTION": "slow API"}
    assert second["delegation_id"] is None
    assert [m["message"] for m in body["missing"]] == [
        "no retrospective from: D002"]


def test_no_retrospectives_file_is_empty_not_an_error(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, RUN)
    c = TestClient(create_app(study))
    assert c.get(f"/api/runs/{RUN}/retrospectives").json() == {
        "retrospectives": [], "missing": []}
    assert c.get("/api/runs/nope/retrospectives").status_code == 404
