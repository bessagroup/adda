"""A node owns the ledger, the milestones and the monitor only if it holds their tools."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml

from adda._src.runtime import settings
from adda._src.runtime.agent_runtime import AgenticRun
from adda._src.runtime.graph_builder import build_graph


def _nodes(tmp_path, nodes_block=None):
    src = Path(__file__).resolve().parent.parent / "studies" / "example_study"
    study = tmp_path / "study"
    study.mkdir(parents=True)
    shutil.copy(src / "PROBLEM_STATEMENT.md", study)
    cfg = yaml.safe_load((src / "config.yaml").read_text()) or {}
    if nodes_block:
        cfg["nodes"] = nodes_block
    (study / "config.yaml").write_text(yaml.safe_dump(cfg))
    logging.disable(logging.CRITICAL)
    try:
        run = AgenticRun(study_dir=study, review_statement=False, interactive=False)
        settings.configure(run._study_runtime, run._runtime_override)
        ctx = run._prepare_run()
        live: dict = {}
        build_graph(run._graph_spec, run._make_adapter, study_dir=run.study_dir,
                    interactive=False, notes_dir=ctx.notes_dir,
                    workspace_dir=ctx.workspace_dir,
                    delegation_log=ctx.delegation_log, node_registry=live)
        return live, study
    finally:
        logging.disable(logging.NOTSET)
        settings.configure(None)


def _owned(node):
    return {"ledger": node._ledger is not None,
            "milestones": node._milestones is not None,
            "monitor": node._science_monitor is not None,
            "telemetry": node._telemetry is not None}


def test_the_default_graph_keeps_its_owners(tmp_path):
    live, _ = _nodes(tmp_path)
    full = dict(ledger=True, milestones=True, monitor=True, telemetry=True)
    # datagenerator and implementer hold HypothesisList, no Milestone tool: they
    # keep the ledger and the monitor and no longer carry a milestone ledger
    # that nothing on them can write or read.
    worker = dict(ledger=True, milestones=False, monitor=True, telemetry=True)
    none = dict(ledger=False, milestones=False, monitor=False, telemetry=False)
    assert {n: _owned(x) for n, x in live.items()} == {
        "strategizer": full, "datagenerator": worker, "implementer": worker,
        "literature_reviewer": none, "critic": none}


def test_a_node_without_the_epistemic_tools_owns_nothing(tmp_path):
    live, _ = _nodes(tmp_path, {"strategizer": {"tools": ["Delegate", "Done"]}})
    s = _owned(live["strategizer"])
    assert (s["ledger"], s["milestones"], s["monitor"]) == (False, False, False)
    assert s["telemetry"] is True


def test_holding_the_milestone_tools_alone_gives_milestones_and_the_monitor(tmp_path):
    live, _ = _nodes(tmp_path, {"strategizer": {
        "tools": ["Delegate", "Done", "MilestoneList", "MilestoneSet"]}})
    s = _owned(live["strategizer"])
    assert (s["ledger"], s["milestones"], s["monitor"]) == (False, True, True)


def test_the_notebook_is_required_only_of_a_node_that_can_author_it(tmp_path):
    state = {"study_dir": "x", "required_deliverables": []}
    live, _ = _nodes(tmp_path / "a")
    assert live["strategizer"]._missing_deliverables(state) == ["pipeline.ipynb"]
    live, _ = _nodes(tmp_path / "b", {"strategizer": {"tools": ["Delegate", "Done"]}})
    assert live["strategizer"]._missing_deliverables(state) == []
