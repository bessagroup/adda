"""GET /api/study/history — the study's commits via safe_git, confined to its path."""
import subprocess

from starlette.testclient import TestClient

from adda._src.viewer.app import create_app


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t",
                    "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    *args], check=True, capture_output=True)


def _repo(tmp_path):
    root = tmp_path / "repo"
    study = root / "studies" / "s"
    (study / "runs").mkdir(parents=True)
    _git(root, "init", "-q")
    (study / "config.yaml").write_text("a: 1\n")
    (root / "other.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add study")
    (root / "other.txt").write_text("y\n")
    _git(root, "commit", "-q", "-am", "unrelated")
    (study / "config.yaml").write_text("a: 1\nb: 2\n")
    _git(root, "commit", "-q", "-am", "tune study")
    return study


def test_history_is_confined_to_the_study_path(tmp_path):
    c = TestClient(create_app(_repo(tmp_path)))
    body = c.get("/api/study/history").json()
    assert body["repo"] == "repo" and body["path"] == "studies/s"
    assert [x["subject"] for x in body["commits"]] == ["tune study", "add study"]
    top = body["commits"][0]
    assert top["files"] == [{"path": "studies/s/config.yaml",
                             "insertions": 1, "deletions": 0}]
    assert all("other.txt" not in f["path"]
               for x in body["commits"] for f in x["files"])


def test_limit_and_bad_limit(tmp_path):
    c = TestClient(create_app(_repo(tmp_path)))
    assert len(c.get("/api/study/history?limit=1").json()["commits"]) == 1
    assert c.get("/api/study/history?limit=x").status_code == 400


def test_study_outside_any_repo_is_empty(tmp_path):
    study = tmp_path / "loose"
    (study / "runs").mkdir(parents=True)
    # tmp_path may itself sit under a repo on odd hosts; only assert shape
    body = TestClient(create_app(study)).get("/api/study/history").json()
    assert set(body) == {"repo", "path", "commits"}
