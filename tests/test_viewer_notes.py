"""GET /api/runs/{id}/notes — the strategizer's written notes, with mtimes."""
import os

from starlette.testclient import TestClient

from adda._src.viewer.app import create_app
from tests.test_viewer_app import _make_run, _make_study

RUN = "20260904T120000"


def test_serves_markdown_notes_oldest_first_and_ignores_json(tmp_path):
    study = _make_study(tmp_path)
    notes = _make_run(study, RUN) / "debug" / "strategizer_notes"
    notes.mkdir()
    (notes / "strategy_02_b.md").write_text("second")
    (notes / "strategy_01_a.md").write_text("first")
    (notes / "hypotheses.json").write_text("{}")
    os.utime(notes / "strategy_01_a.md", (100, 100))
    os.utime(notes / "strategy_02_b.md", (200, 200))
    body = TestClient(create_app(study)).get(f"/api/runs/{RUN}/notes").json()
    assert [(n["name"], n["text"], n["mtime"]) for n in body["notes"]] == [
        ("strategy_01_a.md", "first", 100.0),
        ("strategy_02_b.md", "second", 200.0)]


def test_no_notes_dir_is_empty_and_unknown_run_404(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, RUN)
    c = TestClient(create_app(study))
    assert c.get(f"/api/runs/{RUN}/notes").json() == {"notes": []}
    assert c.get("/api/runs/nope/notes").status_code == 404
