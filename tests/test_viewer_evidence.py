"""Evidence view + the read-only safe_git module behind it."""
import subprocess

import pytest
from starlette.testclient import TestClient

from adda._src.viewer import safe_git
from adda._src.viewer.app import create_app
from tests.test_viewer_app import _make_run, _make_study, _write_jsonl

RUN = "20260904T120000"


def _git(ws, *args):
    return subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True).stdout.strip()


def _workspace(run_dir):
    ws = run_dir / "debug" / "delegations"
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init", "-q")
    shas = []
    for name, body in (("D001/a.py", "x = 1\n"), ("D002/b.py", "y = 2\n")):
        (ws / name).parent.mkdir(exist_ok=True)
        (ws / name).write_text(body)
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", f"commit {name}")
        shas.append(_git(ws, "rev-parse", "HEAD"))
    return ws, shas


def _setup(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, RUN)
    ws, shas = _workspace(run_dir)
    _write_jsonl(run_dir / "debug" / "delegation_log.jsonl", [
        {"id": "D001", "status": "DONE", "to_node": "implementer",
         "workspace_sha": shas[0]},
        {"id": "D002", "status": "DONE", "to_node": "implementer",
         "workspace_sha": shas[1]},
        {"id": "D003", "status": "DONE", "to_node": "implementer",
         "workspace_sha": None}])
    (run_dir / "debug" / "evidence_index.md").write_text("INDEX")
    return TestClient(create_app(study)), run_dir, shas


def test_evidence_lists_files_and_predecessors(tmp_path):
    c, _, shas = _setup(tmp_path)
    body = c.get(f"/api/runs/{RUN}/evidence").json()
    assert body["index"] == "INDEX" and body["repo"] is True
    d1, d2, d3 = body["delegations"]
    assert d2["predecessor_sha"] == shas[0] == d1["workspace_sha"]
    assert d1["predecessor_sha"] is None
    assert [f["path"] for f in d2["files"]] == ["D002/b.py"]
    assert d2["files"][0]["insertions"] == 1
    assert d3["files"] == [] and d3["predecessor_sha"] is None


def test_evidence_stat_runs_show_and_diff_stat(tmp_path):
    c, _, shas = _setup(tmp_path)
    body = c.get(f"/api/runs/{RUN}/evidence/D002").json()
    assert "D002/b.py" in body["show_stat"]
    assert "D002/b.py" in body["diff_stat"] and "D001" not in body["diff_stat"]
    assert c.get(f"/api/runs/{RUN}/evidence/D001").json()["diff_stat"] is None
    assert c.get(f"/api/runs/{RUN}/evidence/D003").status_code == 404
    assert c.get(f"/api/runs/{RUN}/evidence/nope").status_code == 404


def test_run_without_a_workspace_repo_is_empty_not_an_error(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, RUN)
    _write_jsonl(run_dir / "debug" / "delegation_log.jsonl", [
        {"id": "D001", "status": "DONE", "workspace_sha": "abc1234"}])
    body = TestClient(create_app(study)).get(
        f"/api/runs/{RUN}/evidence").json()
    assert body["repo"] is False and body["index"] is None
    assert body["delegations"][0]["files"] == []


@pytest.mark.parametrize("bad", ["--output=/tmp/x", "HEAD", "abc", "g" * 8,
                                 "abcdef1; rm -rf /", ""])
def test_only_hex_commit_ids_reach_git(tmp_path, bad):
    _, run_dir, _ = _setup(tmp_path)
    ws = run_dir / "debug" / "delegations"
    with pytest.raises(safe_git.GitViewError):
        safe_git.show_stat(ws, bad)
    with pytest.raises(safe_git.GitViewError):
        safe_git.diff_stat(ws, bad, bad)


def test_git_never_discovers_a_parent_repository(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init", "-q")
    nested = parent / "nested"
    nested.mkdir()
    with pytest.raises(safe_git.GitViewError, match="not a git repository"):
        safe_git.log(nested)


def test_log_reports_commits_newest_first(tmp_path):
    _, run_dir, shas = _setup(tmp_path)
    rows = safe_git.log(run_dir / "debug" / "delegations")
    assert [r["sha"] for r in rows] == shas[::-1]
    assert rows[0]["subject"] == "commit D002/b.py"


def test_a_relative_repo_path_works(tmp_path, monkeypatch):
    _, run_dir, shas = _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    rel = (run_dir / "debug" / "delegations").relative_to(tmp_path)
    assert [r["sha"] for r in safe_git.log(rel)] == shas[::-1]
