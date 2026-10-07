"""Re-executing a run's deliverable from the viewer (spec 14 Phase 5.8).

The notebooks are real and run in a real kernel against a real f3dasm store,
because what must hold is a property of that execution: the live ledger and
the notebook file are never written, and the verdict is the gate's own.
"""
from __future__ import annotations

import json

import nbformat
import pytest
from f3dasm._src.experimentdata import ExperimentData
from starlette.testclient import TestClient

from adda._src.viewer import notebook_replay
from adda._src.viewer.app import create_app
from tests.test_reproduction_gate import _seed_store
from tests.test_viewer_app import _make_run, _make_study, _writer

RUN = "20260904T120000"

# Every test here replays in a worker subprocess that starts a real kernel, which
# an in-process probe cannot see; serialise them with the other kernel tests.
pytestmark = pytest.mark.xdist_group(name="jupyter_kernel")

_LAZY = (
    "import os\n"
    "from f3dasm._src.experimentdata import ExperimentData\n"
    "_, out = ExperimentData.from_file(\n"
    "    project_dir=os.environ['F3DASM_CANONICAL_STORE']).to_pandas()\n"
    "print(f\"REPRODUCED: {float(out['f'].max())}\")\n"
)
_NON_LAZY = (
    "import os\n"
    "from adda._src.evaluation.instrumented import InstrumentedDataGenerator\n"
    "from f3dasm._src.core import DataGenerator\n"
    "from f3dasm._src.experimentsample import ExperimentSample, JobStatus\n"
    "class G(DataGenerator):\n"
    "    def execute(self, experiment_sample, **k):\n"
    "        s = experiment_sample\n"
    "        s._output_data['f'] = 1.0\n"
    "        s.job_status = JobStatus.FINISHED\n"
    "        return s\n"
    "g = InstrumentedDataGenerator(inner=G(),\n"
    "    store_dir=os.environ['F3DASM_CANONICAL_STORE'],\n"
    "    delegation_id='D777', flush_every=1)\n"
    "g.execute(ExperimentSample(_input_data={'x0': 9.0}, _output_data={},\n"
    "                           job_status=JobStatus.OPEN))\n"
    "g.flush()\n"
)


def _notebook(study, code: str) -> bytes:
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(code)])
    path = study / f"pipeline_{RUN}.ipynb"
    nbformat.write(nb, str(path))
    return path.read_bytes()


@pytest.fixture
def study(tmp_path):
    s = _make_study(tmp_path)
    run = _make_run(s, RUN)
    _seed_store(run / "experiment_data", n=3)
    (run / "debug" / "run_config.json").write_text("{}")
    return s


def _rows(study) -> int:
    return len(ExperimentData.from_file(
        project_dir=study / "runs" / RUN / "experiment_data").to_pandas()[1])


def _audit_rows(study):
    return [json.loads(line) for line in
            (study / "viewer_actions.jsonl").read_text().splitlines()]


def test_a_lazy_notebook_passes_and_nothing_on_disk_changes(study):
    before = _notebook(study, _LAZY)
    resp = _writer(study).post(f"/api/runs/{RUN}/notebook/reexecute", json={})
    body = resp.json()
    assert resp.status_code == 200 and body["passed"], body
    assert body["reproduced"] == "2.0"
    assert body["rows_before"] == body["rows_after"] == 3
    assert "REPRODUCED: 2.0" in body["stdout_tail"]
    assert (study / f"pipeline_{RUN}.ipynb").read_bytes() == before
    assert _rows(study) == 3


def test_a_non_lazy_notebook_fails_and_the_live_ledger_is_untouched(study):
    _notebook(study, _NON_LAZY)
    body = _writer(study).post(
        f"/api/runs/{RUN}/notebook/reexecute", json={}).json()
    assert body["passed"] is False and body["lazy"] is False
    assert (body["rows_before"], body["rows_after"]) == (3, 4)
    assert _rows(study) == 3


def test_a_notebook_that_raises_fails_with_its_error(study):
    _notebook(study, "raise ValueError('boom')\n")
    body = _writer(study).post(
        f"/api/runs/{RUN}/notebook/reexecute", json={}).json()
    assert body["passed"] is False and body["returncode"] != 0
    assert "boom" in body["stderr_tail"]


def test_the_exact_command_is_audited_before_and_after(study):
    _notebook(study, _LAZY)
    _writer(study).post(f"/api/runs/{RUN}/notebook/reexecute", json={})
    rows = [r for r in _audit_rows(study) if r["action"] == "reexecute"]
    assert [r["phase"] for r in rows] == ["run", "done"]
    assert "notebook_replay" in rows[0]["command"] and RUN in rows[0]["command"]
    assert rows[1]["passed"] is True


def test_no_notebook_for_the_run_is_a_409(study):
    resp = _writer(study).post(f"/api/runs/{RUN}/notebook/reexecute", json={})
    assert resp.status_code == 409 and "no pipeline notebook" in resp.json()["error"]


def test_an_unknown_run_is_a_404(study):
    resp = _writer(study).post("/api/runs/nope/notebook/reexecute", json={})
    assert resp.status_code == 404


def test_it_needs_the_write_token(study):
    _notebook(study, _LAZY)
    anon = TestClient(create_app(study))
    resp = anon.post(f"/api/runs/{RUN}/notebook/reexecute", json={})
    assert resp.status_code == 403
    assert not (study / "viewer_actions.jsonl").exists()


def test_a_second_replay_is_refused_while_one_runs(study):
    _notebook(study, _LAZY)
    lock = notebook_replay._locks.setdefault(str(study.resolve()),
                                             notebook_replay.threading.Lock())
    with lock:
        resp = _writer(study).post(
            f"/api/runs/{RUN}/notebook/reexecute", json={})
    assert resp.status_code == 409 and "already running" in resp.json()["error"]


def test_the_stream_reports_progress_cell_by_cell_then_the_verdict(study):
    _notebook(study, _LAZY)
    with _writer(study).stream(
            "POST", f"/api/runs/{RUN}/notebook/reexecute/stream", json={}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in resp.iter_lines() if line]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "result"
    assert "cell" in kinds and kinds.index("cell") < kinds.index("result")
    assert [e["name"] for e in events if e["event"] == "phase"] == [
        "sandbox", "ledger", "execute", "check"]
    assert all(e["line"] for e in events[:-1])
    assert events[-1]["passed"] and events[-1]["reproduced"] == "2.0"


def test_the_stream_refuses_with_a_409_before_streaming_when_busy(study):
    _notebook(study, _LAZY)
    lock = notebook_replay._locks.setdefault(str(study.resolve()),
                                             notebook_replay.threading.Lock())
    with lock:
        resp = _writer(study).post(
            f"/api/runs/{RUN}/notebook/reexecute/stream", json={})
    assert resp.status_code == 409 and "already running" in resp.json()["error"]


def test_the_stream_needs_the_write_token(study):
    _notebook(study, _LAZY)
    resp = TestClient(create_app(study)).post(
        f"/api/runs/{RUN}/notebook/reexecute/stream", json={})
    assert resp.status_code == 403


def test_a_re_execution_is_saved_beside_the_run_and_served_without_touching_the_stored_notebook(study):
    before = _notebook(study, _LAZY)
    body = _writer(study).post(f"/api/runs/{RUN}/notebook/reexecute", json={}).json()
    assert body["passed"] and body["reexec_id"] and body["saved"]
    folder = study / "runs" / RUN / "debug" / "viewer_reexec"
    meta = json.loads((folder / f"reexec_{body['reexec_id']}.json").read_text())
    assert meta["passed"] is True and meta["reproduced"] == "2.0" and meta["notebook"] == body["saved"]
    assert "finished_at" in meta and "adda_commit" in meta
    assert (study / f"pipeline_{RUN}.ipynb").read_bytes() == before

    client = TestClient(create_app(study))
    stored = client.get(f"/api/runs/{RUN}/notebook?reexec=stored").json()
    assert stored["source"] == "stored" and stored["headline"]["reproduced"] is None
    assert [r["id"] for r in stored["reexecutions"]] == [body["reexec_id"]]
    rerun = client.get(f"/api/runs/{RUN}/notebook").json()      # the default
    assert rerun["source"] == body["reexec_id"] and rerun["headline"]["reproduced"] == "2.0"
    # an id that is not a listed re-execution never reaches the filesystem
    odd = client.get(f"/api/runs/{RUN}/notebook?reexec=../../../etc/passwd").json()
    assert odd["source"] == "stored"
    assert any(r.get("saved") for r in _audit_rows(study) if r.get("phase") == "done")


def test_a_failed_re_execution_is_saved_and_marked_failed(study):
    _notebook(study, "raise ValueError('boom')\n")
    body = _writer(study).post(f"/api/runs/{RUN}/notebook/reexecute", json={}).json()
    assert body["passed"] is False and body["reexec_id"]
    nb = TestClient(create_app(study)).get(f"/api/runs/{RUN}/notebook").json()
    assert nb["reexecutions"][0]["passed"] is False
    assert nb["source"] == "stored"                    # a failed one is never the default
