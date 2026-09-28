"""Reusable mock adapters for agentic pipeline integration tests."""

from __future__ import annotations

import re
import time


def approve_delegation(tools: dict, delegation_id: str,
                        message: str = "Approved.") -> str | None:
    """Approve an OPEN-FOR-REVIEW delegation the way a real delegator would,
    via ``SendMessage(id, ..., approve=True)`` -- finalizes it (runs
    ``_finish_ok``, commits the workspace, logs DONE), the same close a
    collected report needs under the spec-12 review gate (default on since
    the migration sweep). A no-op (returns ``None``) when ``SendMessage``
    isn't in this node's closures -- the peer_interaction-off ablation arm,
    where a collected delegation is already terminal on its own -- so
    callers can use this unconditionally in a test meant to run under
    either arm."""
    if "SendMessage" not in tools:
        return None
    return tools["SendMessage"](delegation_id, message, approve=True)


class MockWorkerAdapter:
    """Stub adapter that returns a valid canned Report and exposes last_usage."""

    REPORT = """\
## Report

### Actions taken
- Loaded dataset and ran analysis

### Files touched
- workspace/D001/results.csv

### Conclusions
Thin longerons show lower sigma_crit across all test cases.

### Numbers
evals: 10
"""

    def __init__(self, report=None):
        self._report = report or self.REPORT
        self.closure_tools: dict = {}
        self.route_watcher = None
        # Fake token usage returned after each invoke
        self.last_usage: dict = {
            "input_tokens": 120,
            "output_tokens": 48,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_cost_usd": 0.0025,
        }

    def invoke(self, messages):
        # Simulate the worker writing a result file to its sandbox, which
        # causes the workspace/D### directory to be created on disk.
        write = self.closure_tools.get("Write")
        if write is not None:
            write("results.csv", "ratio_d,sigma_crit,coilable\n0.01,120.5,1\n")
        return self._report

    def copy(self):
        fresh = MockWorkerAdapter(self._report)
        fresh.closure_tools = dict(self.closure_tools)
        return fresh


class ScriptedStrategistAdapter:
    """Runs a deterministic hypothesis→delegate→update→done sequence."""

    def __init__(self, run=None):
        self.closure_tools: dict = {}
        self.route_watcher = None
        self._run = run  # optional AgenticRun ref for canonical store seeding
        self.last_usage: dict = {
            "input_tokens": 200,
            "output_tokens": 80,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_cost_usd": 0.005,
        }

    def invoke(self, messages):
        tools = self.closure_tools

        # Step 1: propose two competing hypotheses
        h1 = tools["HypothesisPropose"](
            statement="Thin longerons buckle at lower stress",
            falsification_criterion="sigma_crit above threshold",
            prediction="sigma_crit drops below threshold in sweep",
            prior=0.6,
        )
        h2 = tools["HypothesisPropose"](
            statement="Taper ratio drives coilability",
            falsification_criterion="coilability independent of taper",
            prediction="taper ratio correlates with coilability",
            prior=0.6,
        )

        # Resolve the process backlog before any implementer delegation
        # (the gate blocks the implementer until it's cleared).
        if "MilestoneList" in tools:
            for mid in re.findall(r"M\d{3}", tools["MilestoneList"]()):
                tools["MilestoneSet"](mid, "SKIPPED", note="not applicable to this scripted test")

        # Step 2: delegate to implementer to test H1
        result = tools["Delegate"](
            target="implementer",
            intent="Analyse sigma_crit vs ratio_d in workspace/D001/",
            expected_report="Report sigma_crit values and coilability classification.",
            hypothesis_ids=[h1],
        )
        d1_id = re.search(r"D\d{3}", result).group()

        # Step 3: poll until done
        for _ in range(200):
            status = tools["Wait"](d1_id, block=False)
            if not status.strip().startswith("Working"):
                break
            time.sleep(0.05)
        # Collect and approve the report like a real delegator would --
        # under peer_interaction (on by default) it opens for review
        # rather than auto-finalizing, and HypothesisUpdate's provenance
        # link (DelegationLog.last_completed_id()) needs the terminal DONE
        # row, which only exists once approved (spec 12 item 8).
        tools["Wait"](d1_id)
        approve_delegation(tools, d1_id)

        # Step 4: falsify H1 based on report
        tools["HypothesisUpdate"](
            hypothesis_id=h1,
            status="FALSIFIED",
            comment="Data shows thin longerons coil; H1 contradicted.",
            evidence={"delegation": d1_id},
            posterior=0.05,
        )

        # Step 5: delegate to confirm H2 (must be a falsification attempt)
        result2 = tools["Delegate"](
            target="implementer",
            intent="Verify taper ratio effect on coilability in workspace/D002/",
            expected_report="Confirm or deny taper ratio hypothesis.",
            hypothesis_ids=[h2],
            is_falsification_attempt=True,
        )
        d2_id = re.search(r"D\d{3}", result2).group()

        for _ in range(200):
            status = tools["Wait"](d2_id, block=False)
            if not status.strip().startswith("Working"):
                break
            time.sleep(0.05)
        tools["Wait"](d2_id)
        approve_delegation(tools, d2_id)

        tools["HypothesisUpdate"](
            hypothesis_id=h2,
            status="SUPPORTED",
            comment="Taper ratio strongly predicts coilability.",
            evidence={"delegation": d2_id},
            posterior=0.9,
        )

        # Author the required deliverable (pipeline.ipynb) before Done(). Must
        # satisfy the controlled reproduction gate: a code cell printing a
        # verifiable 'REPRODUCED: <value>' sentinel.
        if "WriteCell" in tools:
            tools["WriteCell"]("problem", content="minimise f.")
            tools["WriteCell"]("analysis", why="Report the headline.",
                               code="print('REPRODUCED: 0.0')")
            # Seed the canonical store so the reproduction gate can verify
            # grounding (the mock worker never calls get_evaluator()).
            _run = self._run
            _run_dir = getattr(_run, "_run_dir", None) if _run else None
            if _run_dir is not None:
                from pathlib import Path as _Path
                from adda._src.evaluation.instrumented import InstrumentedDataGenerator
                from f3dasm._src.core import DataGenerator
                from f3dasm._src.experimentsample import ExperimentSample, JobStatus
                class _Stub(DataGenerator):
                    def execute(self, s, **k):
                        s._output_data["f"] = 0.0
                        s.job_status = JobStatus.FINISHED
                        return s
                _store = _Path(_run_dir) / "experiment_data"
                _gen = InstrumentedDataGenerator(
                    inner=_Stub(), store_dir=_store,
                    delegation_id="D001", flush_every=1)
                _gen.execute(ExperimentSample(
                    _input_data={"x0": 0.0}, _output_data={},
                    job_status=JobStatus.OPEN))
                _gen.flush()

        # Two-shot without a critic (warn, then accept); a THIRD call is
        # needed when a critic is connected and PASSes -- its own exit
        # interview round (feedback.py::_capture_retrospective) is what
        # actually sets node._route["kind"]="done". Looping rather than
        # hardcoding a call count keeps this script correct for either
        # graph shape.
        summary = "H1 falsified. H2 supported. Optimal design confirmed."
        for _ in range(4):
            out = tools["Done"](summary=summary)
            if "Run complete." in out:
                break
        return "Done."
