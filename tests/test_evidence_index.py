"""The critic's brief carries a generated index of every delegation."""
from __future__ import annotations

import json
from pathlib import Path

from adda._src.infra.evidence_index import evidence_index_block
from adda._src.infra.workspace_vcs import commit_workspace, init_workspace_repo


def _run(tmp_path: Path, n: int) -> Path:
    debug = tmp_path / "debug"
    ws = debug / "delegations"
    assert init_workspace_repo(ws)
    rows = []
    for i in range(1, n + 1):
        did = f"D{i:03d}"
        (ws / did).mkdir()
        (ws / did / "solid_model.py").write_text(f"x = {i}\n")
        sha = commit_workspace(ws, f"{did} strategizer -> implementer [DONE]")
        rows.append({"id": did, "to_node": "implementer", "status": "DONE",
                     "task": f"Build model {i}\nmore detail",
                     "deliverable": f"report body {i}", "workspace_sha": sha})
    (debug / "delegation_log.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    return debug


def test_index_lists_every_delegation_with_a_readable_report(tmp_path):
    debug = _run(tmp_path, 3)
    block = evidence_index_block(debug)
    for i in (1, 2, 3):
        did = f"D{i:03d}"
        assert did in block
        report = debug / "delegation_reports" / f"{did}.md"
        assert str(report) in block
        assert report.read_text() == f"report body {i}"
        assert str(debug / "delegations" / did / "solid_model.py") in block
    assert "Build model 2" in block and "more detail" not in block


def test_a_patch_row_does_not_replace_the_delegations_final_state(tmp_path):
    debug = _run(tmp_path, 1)
    with (debug / "delegation_log.jsonl").open("a") as f:
        f.write(json.dumps({"id": "D001", "ts": "t",
                            "patch": {"is_falsification_attempt": True}}) + "\n")
    block = evidence_index_block(debug)
    assert "D001 | implementer | DONE" in block
    assert (debug / "delegation_reports" / "D001.md").read_text() == "report body 1"


def test_a_long_index_is_capped_with_a_pointer_to_the_full_one(tmp_path):
    debug = _run(tmp_path, 40)
    block = evidence_index_block(debug)
    assert "index truncated" in block
    full = (debug / "evidence_index.md").read_text()
    assert "D040" in full and "D040" not in block


def test_no_delegations_no_block(tmp_path):
    assert evidence_index_block(tmp_path) == ""
    assert evidence_index_block(None) == ""


def test_the_feedback_brief_embeds_the_index(tmp_path):
    from adda._src.nodes.critic_gate import CriticGateMixin

    debug = _run(tmp_path, 2)
    notes = debug / "strategizer_notes"
    notes.mkdir()

    class _S:
        _current_notes_dir = notes
        _study_dir = tmp_path

    msg = CriticGateMixin._build_feedback_task_msg(_S(), ["H1"])
    assert "<evidence_index>" in msg
    assert str(debug / "delegation_reports" / "D002.md") in msg
