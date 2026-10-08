"""Structured notebook-authoring closures (SetNotebookIntro / AddPipelineCell).

These make the four-pillar structure + the WHY-explainer UNFORGEABLE: the pillar
name and the rationale are required arguments, so the agent cannot author a
structureless notebook. They replace the live Jupyter MCP server (ripped out).
"""
from __future__ import annotations

import nbformat
import pytest

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.nodes import Node


class _Stub:
    def __init__(self):
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return ""


def _node(study_dir):
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "WriteCell", "RunNotebook"})
        description = "strategizer"

    class B(Agent):
        role = "implementer"
        description = "implementer"

    spec = Graph(
        nodes={"strategizer": A(), "implementer": B()},
        edges=(Edge("strategizer", "implementer"),), entry="strategizer",
    )
    return Node(
        _Stub(), name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": _Stub()}, study_dir=str(study_dir),
    )


def _read(study_dir):
    return nbformat.read(str(study_dir / "pipeline.ipynb"), as_version=4)


def _named(nb):
    return {c.metadata.get("name"): c for c in nb.cells if c.metadata.get("name")}


def test_closures_present_only_when_declared(tmp_path):
    n = _node(tmp_path)
    assert "WriteCell" in n.adapter.closure_tools
    for retired in ("AddPipelineMarkdownCell", "AddPipelineCell",
                    "EditPipelineCell", "DeletePipelineCell"):
        assert retired not in n.adapter.closure_tools  # folded into WriteCell
    assert "SetNotebookIntro" not in n.adapter.closure_tools  # retired


def test_set_intro_then_add_pillars_canonical_order(tmp_path):
    n = _node(tmp_path)
    tools = n.adapter.closure_tools
    # Add pillars OUT of order — the notebook must still come out canonical.
    tools["WriteCell"]("analysis", why="derive headline", code="print('REPRODUCED: 1.0')")
    tools["WriteCell"]("doe", why="LHS over the box", code="domain = ...; sampler = ...")
    tools["WriteCell"]("problem", content="minimise f over the 3-box.")
    tools["WriteCell"]("hypotheses", content="H1: ... H2: ...")
    tools["WriteCell"]("data_generation", why="evaluate via get_evaluator", code="data = ...")

    nb = _read(tmp_path)
    names = [c.metadata.get("name") for c in nb.cells if c.metadata.get("name")]
    # canonical: problem, hypotheses, then doe(+why), data_generation(+why),
    # ... analysis(+why) — phases present appear in pillar order regardless of
    # the call order above.
    assert names == [
        "problem", "hypotheses",
        "doe__why", "doe",
        "data_generation__why", "data_generation",
        "analysis__why", "analysis",
    ]
    # code cell carries name + tag metadata (machine-checkable pillar presence)
    by = _named(nb)
    assert by["doe"].cell_type == "code"
    assert by["doe"].metadata.get("tags") == ["doe"]
    assert by["doe__why"].cell_type == "markdown"


def test_verdict_cell_orders_immediately_before_analysis(tmp_path):
    """<deliverable_format> step 7 places '## Verdict & result' immediately
    ahead of the analysis pillar's code cell, mirroring every other pillar's
    WHY-explainer-then-code shape — verdict must land there in canonical
    order regardless of call order, not at the end as an unrecognized custom
    cell."""
    n = _node(tmp_path)
    tools = n.adapter.closure_tools
    tools["WriteCell"]("doe", why="LHS over the box", code="domain = ...; sampler = ...")
    tools["WriteCell"]("analysis", why="derive headline", code="print('REPRODUCED: 1.0')")
    tools["WriteCell"]("verdict", content="H1 SUPPORTED because ...")
    tools["WriteCell"]("problem", content="minimise f over the 3-box.")

    nb = _read(tmp_path)
    names = [c.metadata.get("name") for c in nb.cells if c.metadata.get("name")]
    assert names == [
        "problem",
        "doe__why", "doe",
        "verdict", "analysis__why", "analysis",
    ]


def test_add_pillar_is_create_only_on_recall(tmp_path):
    # Re-writing an existing cell in full is an edit, and an edit that does
    # not name the rev it saw errors instead of blindly overwriting it.
    n = _node(tmp_path)
    tools = n.adapter.closure_tools
    tools["WriteCell"]("doe", why="first", code="v = 1")
    out = tools["WriteCell"]("doe", why="second", code="v = 2")
    assert out.startswith("ERROR:") and "expected_rev" in out
    nb = _read(tmp_path)
    does = [c for c in nb.cells if c.metadata.get("name") == "doe"]
    assert len(does) == 1 and "v = 1" in does[0].source  # unchanged


def test_unknown_phase_proceeds_with_tip_as_custom_cell(tmp_path):
    """A non-pillar phase PROCEEDS immediately (adding a cell is reversible) with
    a tip — no refusal, no two-shot. It is appended after the standard pillars.
    The deliverable's shape must not constrain what science can be expressed."""
    n = _node(tmp_path)
    tools = n.adapter.closure_tools
    tools["WriteCell"]("doe", why="LHS", code="domain = ...")
    # Custom phase: added on the FIRST call (not refused, not two-shot), with a tip.
    out = tools["WriteCell"]("ellipse_sweep", why="explore ellipse phase", code="data = ...")
    assert not out.startswith("ERROR:") and not out.startswith("[CONFIRM]")
    assert "custom" in out.lower()
    nb = _read(tmp_path)
    names = [c.metadata.get("name") for c in nb.cells if c.metadata.get("name")]
    # standard pillar first, custom section appended after (and it survived).
    assert "doe" in names and "ellipse_sweep" in names
    assert names.index("doe") < names.index("ellipse_sweep")


def test_add_pillar_requires_why_and_code(tmp_path):
    n = _node(tmp_path)
    tools = n.adapter.closure_tools
    assert tools["WriteCell"]("doe", why="  ", code="code").startswith("ERROR:")
    assert tools["WriteCell"]("doe", why="why", code="").startswith("ERROR:")


def test_markdown_cells_are_per_cell_and_create_only(tmp_path):
    # WriteCell authors problem/hypotheses INDEPENDENTLY (no bundling), and a
    # blind re-write errors — an edit must name the rev it saw.
    n = _node(tmp_path)
    tools = n.adapter.closure_tools
    assert "Added problem" in tools["WriteCell"]("problem", content="p1")
    assert "Added hypotheses" in tools["WriteCell"]("hypotheses", content="h1")
    out = tools["WriteCell"]("problem", content="p2")  # blind re-write
    assert out.startswith("ERROR:") and "expected_rev" in out
    nb = _read(tmp_path)
    by = _named(nb)
    assert "p1" in by["problem"].source  # unchanged by the failed re-add
    assert "h1" in by["hypotheses"].source
    # a non-reserved name is a custom narrative cell, not an error — the
    # deliverable's structure must not block what an agent needs to say
    # (mirrors AddPipelineCell's own custom-phase philosophy)
    assert "Added custom" in tools["WriteCell"]("intro", content="x")
    # only a pillar name / <pillar>__why collides and is rejected
    assert tools["WriteCell"]("doe", content="x").startswith("ERROR:")
    assert tools["WriteCell"]("doe__why", content="x").startswith("ERROR:")


@pytest.mark.xdist_group(name="jupyter_kernel")
def test_authored_notebook_passes_the_gate(tmp_path):
    """A notebook authored purely through the closures runs through the
    reproduction gate (a real, executable deliverable)."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator
    from f3dasm._src.core import DataGenerator
    from f3dasm._src.experimentsample import ExperimentSample, JobStatus

    run_dir = tmp_path / "runs" / "A0"
    (run_dir / "debug" / "strategizer_notes").mkdir(parents=True)
    store_dir = run_dir / "experiment_data"
    store_dir.mkdir()

    class _Sum(DataGenerator):
        def execute(self, experiment_sample, **k):
            s = experiment_sample
            s._output_data["f"] = sum(s._input_data.values())
            s.job_status = JobStatus.FINISHED
            return s

    gen = InstrumentedDataGenerator(
        inner=_Sum(), store_dir=store_dir, delegation_id="D001", flush_every=1)
    gen.execute(ExperimentSample(
        _input_data={"x0": 1.0}, _output_data={}, job_status=JobStatus.OPEN))
    gen.flush()

    n = _node(tmp_path)
    n._study_dir = tmp_path
    n._current_notes_dir = run_dir / "debug" / "strategizer_notes"
    tools = n.adapter.closure_tools
    tools["WriteCell"]("problem", content="minimise f")
    tools["WriteCell"]("hypotheses", content="H1")
    tools["WriteCell"]("analysis", why="derive", code="print('REPRODUCED: 1.0')")
    assert n._reproduction_gate({"study_dir": str(tmp_path)}) is None


@pytest.mark.xdist_group(name="jupyter_kernel")
def test_check_deliverable_sees_evals_in_a_design_namespace_only(tmp_path):
    """CheckDeliverable's row-count fail-fast keyed off run_dir/experiment_data/
    experiment_data/output.csv only, so a run whose evals all landed in a
    design-namespace store (run_dir/experiment_data/<namespace>/) got a false
    'canonical store has no evaluations yet' block even though the ledger is
    populated — CheckDeliverable never even reached the reproduction gate."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator
    from f3dasm._src.core import DataGenerator
    from f3dasm._src.experimentsample import ExperimentSample, JobStatus

    run_dir = tmp_path / "runs" / "A1"
    (run_dir / "debug" / "strategizer_notes").mkdir(parents=True)
    # Rows live ONLY in the "polar" namespace store. The default store's
    # output.csv EXISTS (header only, zero data rows) — the exact shape that
    # trips the old single-file row-count fail-fast (a namespace-only run
    # with an empty-but-present default CSV).
    default_csv = run_dir / "experiment_data" / "experiment_data" / "output.csv"
    default_csv.parent.mkdir(parents=True)
    default_csv.write_text("f\n")
    ns_store_dir = run_dir / "experiment_data" / "polar"
    ns_store_dir.mkdir(parents=True)

    class _Sum(DataGenerator):
        def execute(self, experiment_sample, **k):
            s = experiment_sample
            s._output_data["f"] = sum(s._input_data.values())
            s.job_status = JobStatus.FINISHED
            return s

    gen = InstrumentedDataGenerator(
        inner=_Sum(), store_dir=ns_store_dir, delegation_id="D001",
        flush_every=1)
    gen.execute(ExperimentSample(
        _input_data={"x0": 1.0}, _output_data={}, job_status=JobStatus.OPEN))
    gen.flush()

    n = _node(tmp_path)
    n._study_dir = tmp_path
    n._current_notes_dir = run_dir / "debug" / "strategizer_notes"
    tools = n.adapter.closure_tools
    tools["WriteCell"]("problem", content="minimise f")
    tools["WriteCell"]("hypotheses", content="H1")
    tools["WriteCell"]("analysis", why="derive", code="print('REPRODUCED: 1.0')")

    result = tools["RunNotebook"](gate=True)
    assert "no evaluations yet" not in result, (
        f"CheckDeliverable falsely blocked a namespace-only run: {result!r}"
    )


@pytest.mark.parametrize("kwargs", [
    {"content": "text"},
    {"code": "x = 1", "why": "because"},
    {"code": "x = 1"},
])
def test_a_pillar_explainer_is_never_created_on_its_own(tmp_path, kwargs):
    """The explainer is written WITH its pillar, on both CREATE paths: a
    standalone '<pillar>__why' cell would shadow the real one."""
    n = _node(tmp_path)
    out = n.adapter.closure_tools["WriteCell"]("doe__why", **kwargs)
    assert out.startswith("ERROR:") and "WHY-explainer" in out
    assert not (tmp_path / "pipeline.ipynb").exists() or \
        "doe__why" not in _named(_read(tmp_path))
