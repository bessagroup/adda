"""GET /api/runs/{id}/diagnostics — line-cursor paging, kind filter, vocabulary."""
from starlette.testclient import TestClient

from adda._src.viewer.app import create_app
from tests.test_viewer_app import _make_run, _make_study

RUN = "20260904T120000"


def _client(tmp_path, lines):
    study = _make_study(tmp_path)
    debug = _make_run(study, RUN) / "debug"
    (debug / "diagnostics.jsonl").write_text("\n".join(lines) + "\n")
    return TestClient(create_app(study))


def _rows():
    return ['{"error_type": "ERROR_RETURN", "message": "a"}',
            "not json",
            '{"tool": "RETROSPECTIVES_MISSING", "message": "b"}',
            '{"error_type": "ERROR_RETURN", "message": "c"}']


def test_pages_by_line_cursor_and_counts_the_whole_file(tmp_path):
    c = _client(tmp_path, _rows())
    p1 = c.get(f"/api/runs/{RUN}/diagnostics?limit=1").json()
    assert [r["message"] for r in p1["rows"]] == ["a"]
    assert p1["next_cursor"] == 1 and p1["total"] == 4
    p2 = c.get(f"/api/runs/{RUN}/diagnostics?after=1&limit=1").json()
    assert [r["message"] for r in p2["rows"]] == ["b"]  # bad line skipped
    assert p2["next_cursor"] == 3
    assert p2["counts"] == {"ERROR_RETURN": 2, "RETROSPECTIVES_MISSING": 1}
    p3 = c.get(f"/api/runs/{RUN}/diagnostics?after=3").json()
    assert [r["message"] for r in p3["rows"]] == ["c"]
    assert p3["next_cursor"] == p3["total"] == 4


def test_kind_filter_and_errors(tmp_path):
    c = _client(tmp_path, _rows())
    body = c.get(f"/api/runs/{RUN}/diagnostics?kind=ERROR_RETURN").json()
    assert [r["message"] for r in body["rows"]] == ["a", "c"]
    assert body["counts"]["RETROSPECTIVES_MISSING"] == 1
    assert c.get(f"/api/runs/{RUN}/diagnostics?after=x").status_code == 400
    assert c.get("/api/runs/nope/diagnostics").status_code == 404


def test_no_file_is_empty(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, RUN)
    body = TestClient(create_app(study)).get(
        f"/api/runs/{RUN}/diagnostics").json()
    assert body == {"rows": [], "next_cursor": 0, "total": 0, "counts": {}}
