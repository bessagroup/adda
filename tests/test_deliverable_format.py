"""A required deliverable may declare its top-level JSON keys; Done() refuses
a file that does not hold exactly those keys (plain names keep the existence
check)."""
from __future__ import annotations

import json
import sys

import pytest

from adda._src.backends.registry import available_backends
from adda._src.backends.base import Agent, Edge, Graph
from adda._src.nodes import Node
from adda._src.runtime import study_config as sc

SPEC = {"path": "design.json", "json_keys": ["lattice", "joints", "beams"]}


def _node(backend, study_dir, entries):
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_backend_parity import _instantiate

    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "WriteDeliverable"})
        description = "s"

    class B(Agent):
        description = "i"

    spec = Graph(nodes={"strategizer": A(), "implementer": B()},
                 edges=(Edge("strategizer", "implementer"),),
                 entry="strategizer")
    node = Node(_instantiate(backend), name="strategizer",
                outgoing=["implementer"], spec=spec, study_dir=study_dir)
    node._required_deliverables = entries
    return node


@pytest.mark.parametrize("backend", available_backends())
def test_done_refuses_wrong_keys_and_accepts_exact(backend, tmp_path):
    node = _node(backend, tmp_path, [SPEC])
    done = node.adapter.closure_tools["Done"]
    (tmp_path / "design.json").write_text(json.dumps(
        {"cell": {}, "lattice": 1, "joints": 2, "beams": 3, "status": "ok"}))
    out = done("s")
    assert out.lstrip().startswith("ERROR")
    assert "extra ['cell', 'status']" in out and "missing []" in out
    (tmp_path / "design.json").write_text(json.dumps({"lattice": 1}))
    out = done("s")
    assert out.lstrip().startswith("ERROR")
    assert "missing ['joints', 'beams']" in out
    (tmp_path / "design.json").write_text(json.dumps(
        {"lattice": 1, "joints": 2, "beams": 3}))
    assert "required deliverable is not in the declared" not in done("s")


@pytest.mark.parametrize("backend", available_backends())
def test_write_deliverable_accepts_mapping_entry(backend, tmp_path):
    node = _node(backend, tmp_path, [SPEC])
    wd = node.adapter.closure_tools["WriteDeliverable"]
    assert "Written" in wd("design.json", "{}")


def test_plain_string_entries_are_not_format_checked(tmp_path):
    (tmp_path / "a.json").write_text("not json")
    assert sc.deliverable_format_errors(tmp_path, ["a.json"]) == []


def test_format_errors_cover_missing_invalid_and_non_object(tmp_path):
    assert "does not exist" in sc.deliverable_format_errors(
        tmp_path, [SPEC])[0]
    (tmp_path / "design.json").write_text("{nope")
    assert "not valid JSON" in sc.deliverable_format_errors(
        tmp_path, [SPEC])[0]
    (tmp_path / "design.json").write_text("[1]")
    assert "must be a JSON object" in sc.deliverable_format_errors(
        tmp_path, [SPEC])[0]


def test_config_validation():
    ok = {"required_deliverables": ["x.json", SPEC]}
    assert sc.validate_top_level(ok) == []
    for bad in ([{"path": "d.json", "json_keys": []}],
                [{"json_keys": ["a"]}], [{"path": "d.json", "keys": ["a"]}],
                [3], "d.json"):
        assert sc.validate_top_level({"required_deliverables": bad})


def test_gate_does_not_refuse_in_the_wind_down(tmp_path, monkeypatch):
    node = _node(available_backends()[0], tmp_path, [SPEC])
    (tmp_path / "design.json").write_text(json.dumps({"lattice": 1}))
    from adda._src.nodes.tools.routing.feedback import FeedbackTools
    monkeypatch.setattr(type(node), "_wind_down_active", lambda self: True)
    assert FeedbackTools(node)._deliverable_format("s", "") is None
    monkeypatch.setattr(type(node), "_wind_down_active", lambda self: False)
    assert FeedbackTools(node)._deliverable_format("s", "")


def test_close_records_malformed_and_blocks_gated(tmp_path):
    import logging
    from unittest.mock import MagicMock

    from adda._src.runtime.agent_runtime import AgenticRun, _RunContext

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "design.json").write_text(json.dumps({"lattice": 1}))
    run = AgenticRun(tmp_path)
    run._required_deliverables = [SPEC]
    run_dir = tmp_path / "runs" / "T"
    debug = run_dir / "debug"
    debug.mkdir(parents=True)
    ctx = _RunContext(
        ts="T", run_dir=run_dir, debug_dir=debug,
        notes_dir=debug / "strategizer_notes",
        workspace_dir=run_dir / "workspace", problem="x",
        problem_sha256="a", live_problem_sha256="a", resume_from=None,
        start_time=0.0, thread_id="th", log=logging.getLogger("t"),
        log_handler=logging.NullHandler(), delegation_log=MagicMock(),
        canonical_cfg={}, study_cfg={}, initial_state={}, graph_config={})
    run._finalize_run(ctx, {"last_report": "done", "outcome": "GATED",
                            "termination": "done", "reviewed": True,
                            "token_totals": {}})
    status = json.loads((debug / "run_status.json").read_text())
    assert status["status"] == "UNGATED"
    assert "missing ['joints', 'beams']" in status["deliverables_malformed"][0]
    rows = [json.loads(x) for x in
            (debug / "diagnostics.jsonl").read_text().splitlines()]
    assert rows[0]["error_type"] == "DELIVERABLE_MALFORMED"
