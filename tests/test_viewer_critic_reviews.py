"""GET /api/runs/{id}/critic_reviews — verdict, numbers, findings, pairing."""
from starlette.testclient import TestClient

from adda._src.viewer.app import create_app
from tests.test_viewer_app import _make_run, _make_study, _write_jsonl

RUN = "20260904T120000"
REVISE = ("## Report\n\n### Findings\n\n**[CRITICAL]** refits on re-run\n\n"
          "### Numbers\nfindings_critical: 1\nfindings_major: 0\n"
          "verdict: REVISE\n")
PASS = "### Findings\nnone\n\n### Verdict\n\n**PASS**\n"


def _setup(tmp_path, n_critic_rows):
    study = _make_study(tmp_path)
    debug = _make_run(study, RUN) / "debug"
    (debug / "critic_reviews").mkdir()
    (debug / "critic_reviews" / "call_001.md").write_text(REVISE)
    (debug / "critic_reviews" / "call_002.md").write_text(PASS)
    (debug / "critic_reviews" / "notes.md").write_text("ignored")
    _write_jsonl(debug / "delegation_log.jsonl", [
        {"id": f"GATE{i}", "status": "DONE", "from_node": "strategizer",
         "to_node": "critic"} for i in range(n_critic_rows)])
    return TestClient(create_app(study))


def test_reviews_are_parsed_as_the_gate_parses_them(tmp_path):
    body = _setup(tmp_path, 2).get(f"/api/runs/{RUN}/critic_reviews").json()
    first, second = body["reviews"]
    assert (first["call"], first["verdict"]) == (1, "REVISE")
    assert first["numbers"] == {"findings_critical": 1, "findings_major": 0}
    assert "refits on re-run" in first["findings"]
    assert (second["call"], second["verdict"]) == (2, "PASS")
    assert first["source_id"] == "critic-1" and first["text"] == REVISE


def test_delegation_pairing_is_set_only_when_counts_match(tmp_path):
    ok = _setup(tmp_path, 2).get(f"/api/runs/{RUN}/critic_reviews").json()
    assert [r["delegation_id"] for r in ok["reviews"]] == ["GATE0", "GATE1"]
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    bad = _setup(tmp2, 3).get(f"/api/runs/{RUN}/critic_reviews").json()
    assert [r["delegation_id"] for r in bad["reviews"]] == [None, None]


def test_no_reviews_dir_and_unknown_run(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, RUN)
    c = TestClient(create_app(study))
    assert c.get(f"/api/runs/{RUN}/critic_reviews").json() == {"reviews": []}
    assert c.get("/api/runs/nope/critic_reviews").status_code == 404
