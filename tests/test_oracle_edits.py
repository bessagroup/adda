"""An edit of the registered oracle by a delegation that does not own it is reported.

The run-2 pattern (truss-iscso2015-open, 20261007T002015): the oracle's owner
evaluates, a second delegation rewrites the oracle file, an evaluation runs
under the new revision. The evaluating delegation and the entry node are told
once per new revision, the editor is named only when the delegation log
supports it, and nothing blocks.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from f3dasm._src.experimentsample import ExperimentSample, JobStatus

from adda._src.evaluation.oracle_resolution import get_evaluator
from adda._src.infra import pending_notices


def _sample(x0=0.5):
    return ExperimentSample(
        _input_data={"x0": x0}, _output_data={}, job_status=JobStatus.OPEN)


def _iso(delta_s):
    t = datetime.now(tz=timezone.utc) + timedelta(seconds=delta_s)
    return t.isoformat(timespec="seconds")


class _Run:
    def __init__(self, tmp_path, monkeypatch, owner):
        self.debug = tmp_path / "runs" / "ts" / "debug"
        self.store = tmp_path / "runs" / "ts" / "experiment_data"
        self.study = tmp_path / "study"
        self.monkeypatch = monkeypatch
        for d in ("D001", "D002"):
            (self.debug / "delegations" / d).mkdir(parents=True)
        self.store.mkdir()
        self.study.mkdir()
        self.oracle = self.study / "oracle.py"
        self.oracle.write_text("def f(**kw):\n    return 100.0\n")
        (self.debug / "run_config.json").write_text(json.dumps({
            "store_dir": str(self.store),
            "lock_path": str(self.store / "experiment_data" / ".lock"),
            "source": "t", "study_dir": str(self.study),
            "evaluator_entrypoint": "oracle.py:f",
            "evaluator_output_names": ["f"],
            "evaluator_owner": owner,
        }))
        monkeypatch.delenv("F3DASM_DELEGATION_ID", raising=False)
        monkeypatch.delenv("F3DASM_DEDUP_SCOPE", raising=False)

    def log(self, **windows):
        """windows: id -> (started offset s, completed offset s or None)."""
        rows = [{"id": d, "started_at": _iso(s),
                 "completed_at": None if e is None else _iso(e),
                 "status": "RUNNING" if e is None else "DONE"}
                for d, (s, e) in windows.items()]
        (self.debug / "delegation_log.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    def evaluate(self, delegation, value=None):
        if value is not None:
            self.oracle.write_text(f"def f(**kw):\n    return {value}\n")
        self.monkeypatch.chdir(self.debug / "delegations" / delegation)
        get_evaluator().execute(_sample())

    def notices(self, who):
        return [t for t in pending_notices.drain(self.debug, who)
                if t.startswith("[ORACLE EDITED")]

    def diag(self):
        path = self.debug / "diagnostics.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(ln) for ln in path.read_text().splitlines()]
        return [r for r in rows if r["error_type"] == "ORACLE_EDITED"]


@pytest.fixture
def run(tmp_path, monkeypatch):
    return _Run(tmp_path, monkeypatch, owner="D001")


def test_non_owner_edit_is_reported_to_the_evaluator_and_the_entry_node(run):
    run.log(D001=(-3600, -600), D002=(-300, None))
    run.evaluate("D001")
    assert run.notices("D001") == [] and run.notices("entry") == []
    run.evaluate("D001", value=1.0)

    [text] = run.notices("D001")
    assert text.startswith("[ORACLE EDITED — D001] the registered oracle "
                           "(owner D001) changed from revision ")
    assert "stay in the store as a superseded revision" in text
    assert "Edited by D002." in text
    assert run.notices("entry") == [text]
    [row] = run.diag()
    assert row["detail"]["owner"] == "D001"
    assert row["detail"]["editor"] == "D002"
    assert row["detail"]["old_revision"] != row["detail"]["new_revision"]


def test_one_notice_per_new_revision(run):
    run.log(D001=(-3600, -600), D002=(-300, None))
    run.evaluate("D001")
    run.evaluate("D001", value=1.0)
    run.notices("D001")
    run.evaluate("D001")
    run.evaluate("D002")
    assert run.notices("D001") == [] and run.notices("D002") == []
    assert len(run.diag()) == 1
    run.evaluate("D002", value=2.0)
    assert len(run.diag()) == 2


def test_the_editor_is_unknown_when_two_delegations_were_running(run):
    run.log(D001=(-3600, None), D002=(-300, None))
    run.evaluate("D001")
    run.evaluate("D001", value=1.0)
    [text] = run.notices("D001")
    assert "Editor unknown" in text and "D001, D002" in text
    assert run.diag()[0]["detail"]["editor"] is None


def test_the_owner_editing_its_own_oracle_is_silent(run):
    run.log(D001=(-300, None), D002=(-3600, -600))
    run.evaluate("D001")
    run.evaluate("D001", value=1.0)
    assert run.notices("D001") == [] and run.notices("entry") == []
    assert run.diag() == []


def test_without_an_owner_a_self_edit_is_silent_and_a_foreign_one_is_not(
        tmp_path, monkeypatch):
    r = _Run(tmp_path, monkeypatch, owner=None)
    r.log(D001=(-300, None))
    r.evaluate("D001")
    r.evaluate("D001", value=1.0)
    assert r.diag() == []
    r.log(D001=(-3600, -600), D002=(-300, None))
    r.evaluate("D001", value=2.0)
    [text] = r.notices("D001")
    assert "no registered owner" in text and "Edited by D002." in text


def test_an_edit_never_blocks_the_evaluation(run):
    run.log(D001=(-300, None), D002=(-300, None))
    run.evaluate("D001")
    run.evaluate("D001", value=1.0)
    from f3dasm import ExperimentData
    _, out = ExperimentData.from_file(project_dir=run.store).to_pandas()
    assert sorted(float(v) for v in out["f"]) == [1.0, 100.0]


def test_the_entry_node_receives_the_notice_at_its_next_tool_call(
        run, tmp_path):
    from adda._src.nodes import Node
    from tests.test_nodes import StubAdapter, _spec_with_write_deliverable

    run.log(D001=(-3600, -600), D002=(-300, None))
    run.evaluate("D001")
    run.evaluate("D001", value=1.0)

    entry = Node(StubAdapter(), name="strategizer", outgoing=["implementer"],
                 spec=_spec_with_write_deliverable())
    entry._current_notes_dir = run.debug / "strategizer_notes"
    out = entry._drain_notifications()
    assert "[ORACLE EDITED — D001]" in out
    assert "[ORACLE EDITED" not in entry._drain_notifications()


def test_a_registration_records_its_delegation_as_the_owner(tmp_path):
    from adda._src.runtime.run_setup import register_evaluator_entrypoint

    cfg = tmp_path / "run_config.json"
    cfg.write_text(json.dumps({"study_dir": str(tmp_path)}))
    register_evaluator_entrypoint(cfg, "oracle.py", "f", ["f"], owner="D007")
    assert json.loads(cfg.read_text())["evaluator_owner"] == "D007"
