"""Editing a study's problem statement / config through git (spec 14 §3.1/3.2).

A real throwaway repository per test: the point of the feature is what ends up
committed, so git is not mocked.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from adda._src.viewer import study_edit
from adda._src.viewer.app import create_app
from tests.test_viewer_app import _writer

ENV = {"GIT_AUTHOR_NAME": "Op", "GIT_AUTHOR_EMAIL": "op@x", "GIT_COMMITTER_NAME": "Op",
       "GIT_COMMITTER_EMAIL": "op@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def _g(repo, *a):
    import os
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                          text=True, env={**os.environ, **ENV}).stdout.strip()


@pytest.fixture
def study(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "repo"
    s = repo / "studies" / "s1"
    s.mkdir(parents=True)
    (s / "PROBLEM_STATEMENT.md").write_text("# minimise y\nline2\n")
    (s / "config.yaml").write_text("budget: '00:10:00'\nruntime:\n  max_awake_nodes: 2\n")
    (repo / "other.txt").write_text("x")
    _g(repo, "init", "-q", "-b", "main")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "init")
    (s / "runs").mkdir()
    return s


def _base(client, name):
    return client.get(f"/api/study/file/{name}").json()["base"]


def test_read_returns_committed_text_and_a_base(study):
    c = TestClient(create_app(study))
    r = c.get("/api/study/file/problem_statement").json()
    assert r["committed"] == "# minimise y\nline2\n" and r["dirty"] is False and r["base"]
    assert c.get("/api/study/file/nope").status_code == 404


def test_diff_is_side_by_side_and_needs_no_write_token(study):
    c = TestClient(create_app(study))
    r = c.post("/api/study/file/problem_statement/diff", json={"text": "# minimise y\nline2 changed\nline3\n"}).json()
    ops = [x["op"] for x in r["rows"]]
    assert r["changed"] and "same" in ops and ("change" in ops or "add" in ops)


def test_commit_lands_exactly_that_file_with_the_message_and_is_audited(study):
    c = _writer(study)
    out = c.post("/api/study/file/problem_statement/commit",
                 json={"text": "# minimise y\nline2\nnew criterion\n", "message": "add criterion",
                       "base": _base(c, "problem_statement")})
    assert out.status_code == 200, out.text
    repo = study.parent.parent
    assert _g(repo, "log", "-1", "--format=%s") == "add criterion"
    assert _g(repo, "show", "--name-only", "--format=", "HEAD") == "studies/s1/PROBLEM_STATEMENT.md"
    assert _g(repo, "status", "--porcelain", "--", "studies/s1/PROBLEM_STATEMENT.md") == ""
    rows = [json.loads(x) for x in (study / "viewer_actions.jsonl").read_text().splitlines()]
    assert rows[-1]["action"] == "commit_study_file" and rows[-1]["message"] == "add criterion"


def test_commit_refuses_without_token_message_change_or_current_base(study):
    ro = TestClient(create_app(study, token="t"))
    assert ro.post("/api/study/file/config/commit", json={"text": "a: 1", "message": "m", "base": "x"}).status_code == 403
    c = _writer(study)
    b = _base(c, "problem_statement")
    url = "/api/study/file/problem_statement/commit"
    assert c.post(url, json={"text": "new\n", "message": "  ", "base": b}).status_code == 400
    assert c.post(url, json={"text": "# minimise y\nline2\n", "message": "m", "base": b}).status_code == 409
    assert c.post(url, json={"text": "new\n", "message": "m", "base": "deadbeef"}).status_code == 409
    assert _g(study.parent.parent, "log", "--format=%s") == "init"


def test_commit_refuses_when_other_study_files_are_dirty_but_ignores_runs_and_audit(study):
    (study / "runs" / "x.txt").write_text("x")
    (study / "viewer_actions.jsonl").write_text("")
    (study.parent.parent / "other.txt").write_text("changed elsewhere")
    c = _writer(study)
    b = _base(c, "problem_statement")
    url = "/api/study/file/problem_statement/commit"
    ok = c.post(url, json={"text": "v2\n", "message": "m", "base": b})
    assert ok.status_code == 200, ok.text
    (study / "evaluator.py").write_text("def f(): pass\n")
    r = c.post(url, json={"text": "v3\n", "message": "m", "base": _base(c, "problem_statement")})
    assert r.status_code == 409 and "evaluator.py" in r.json()["error"]
    assert (study / "PROBLEM_STATEMENT.md").read_text() == "v2\n"


def test_config_is_validated_before_commit(study):
    c = _writer(study)
    b = _base(c, "config")
    url = "/api/study/file/config/commit"
    bad = c.post(url, json={"text": "runtime:\n  max_awake_nodez: 2\n", "message": "m", "base": b})
    assert bad.status_code == 422 and "max_awake_nodes" in bad.json()["error"]
    assert c.post(url, json={"text": "budget: soon\n", "message": "m", "base": b}).status_code == 422
    assert c.post(url, json={"text": "a: [\n", "message": "m", "base": b}).status_code == 422
    good = c.post(url, json={"text": "budget: '00:20:00'\nruntime:\n  max_awake_nodes: 3\n", "message": "m", "base": b})
    assert good.status_code == 200, good.text


def test_a_failed_commit_leaves_the_file_as_it_was(study, monkeypatch):
    real = study_edit._git

    def boom(root, *argv):
        if argv and argv[0] == "commit":
            raise study_edit.StudyEditError("hook refused", 502)
        return real(root, *argv)

    monkeypatch.setattr(study_edit, "_git", boom)
    c = _writer(study)
    r = c.post("/api/study/file/problem_statement/commit",
               json={"text": "v2\n", "message": "m", "base": _base(c, "problem_statement")})
    assert r.status_code == 502
    assert (study / "PROBLEM_STATEMENT.md").read_text() == "# minimise y\nline2\n"
    assert _g(study.parent.parent, "status", "--porcelain") == ""


def test_per_commit_patch_of_one_file(study):
    c = _writer(study)
    c.post("/api/study/file/problem_statement/commit",
           json={"text": "# minimise y\nline2\nmore\n", "message": "m", "base": _base(c, "problem_statement")})
    sha = _g(study.parent.parent, "rev-parse", "HEAD")
    r = c.get(f"/api/study/commit?sha={sha}&file=problem_statement").json()
    assert "+more" in r["patch"]
    assert c.get("/api/study/commit?sha=--output=x&file=config").status_code == 502


def test_preflight_reports_the_committed_model_and_budget_and_uncommitted_edits(study):
    (study / "config.yaml").write_text("model: m1\nbudget: '00:10:00'\n")
    _g(study.parent.parent, "commit", "-qam", "model")
    (study / "PROBLEM_STATEMENT.md").write_text("edited, not committed\n")
    c = TestClient(create_app(study))
    conf = c.get("/api/study/preflight").json()["configured"]
    assert conf == {"model": "m1", "budget_s": 600.0, "uncommitted": ["PROBLEM_STATEMENT.md"]}
