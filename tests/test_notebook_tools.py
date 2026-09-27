"""Headless tests for the gold-stated notebook tool surface: name-addressed
CRUD with optimistic concurrency (rev guard), surgical find/replace, a read
tool (ShowNotebook), and scratch execution (RunScratch). Plus regression that
the dead live-Jupyter-MCP path is gone.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import nbformat

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.nodes import Node


class _Stub:
    def __init__(self) -> None:
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return ""

    def copy(self):
        s = self.__class__.__new__(self.__class__)
        _Stub.__init__(s)
        s.closure_tools = dict(self.closure_tools)
        return s


_NB_TOOLS = frozenset({"WriteCell", "ShowNotebook", "RunScratch"})


def _node(tmp_path):
    class A(Agent):
        role = "strategizer"
        tools = _NB_TOOLS
        description = "strategizer"

    class B(Agent):
        role = "implementer"
        description = "implementer"

    spec = Graph(
        nodes={"strategizer": A(), "implementer": B()},
        edges=(Edge("strategizer", "implementer"),),
        entry="strategizer",
    )
    return Node(
        _Stub(), name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": _Stub()}, study_dir=str(tmp_path),
    )


def _tool(n, name):
    return n.adapter.closure_tools[name]


def _read_nb(tmp_path):
    return nbformat.read(str(tmp_path / "pipeline.ipynb"), as_version=4)


def _cell(nb, name):
    for c in nb.cells:
        if (c.get("metadata", {}) or {}).get("name") == name:
            return c
    return None


def _rev(tmp_path, phase):
    src = _cell(_read_nb(tmp_path), phase)["source"]
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:8]


# ── WriteCell — create ───────────────────────────────────────────────────────

def test_add_creates_and_reports_rev(tmp_path):
    n = _node(tmp_path)
    out = _tool(n, "WriteCell")("doe", why="w", code="x = 1")
    assert "Added doe" in out and "rev" in out
    assert _cell(_read_nb(tmp_path), "doe")["source"] == "x = 1"


def test_rewriting_an_existing_cell_needs_its_rev(tmp_path):
    """Create-only survived the merge as a rev guard: a second full write to
    a cell that exists is an EDIT, and an edit that does not name the rev it
    saw is refused — so a cell is never overwritten blind."""
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="x = 1")
    out = _tool(n, "WriteCell")("doe", why="w2", code="x = 2")
    assert out.startswith("ERROR:") and "expected_rev" in out
    # unchanged
    assert _cell(_read_nb(tmp_path), "doe")["source"] == "x = 1"


# ── WriteCell — markdown cells ───────────────────────────────────────────────

def test_add_markdown_cell_creates_problem_and_hypotheses(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("problem", content="minimise f over the box")
    _tool(n, "WriteCell")("hypotheses", content="H1: …")
    nb = _read_nb(tmp_path)
    assert "minimise f" in _cell(nb, "problem")["source"]
    assert "# Problem & objective" in _cell(nb, "problem")["source"]  # heading added
    assert "## Hypotheses" in _cell(nb, "hypotheses")["source"]


def test_add_markdown_cell_allows_free_form_custom_name(tmp_path):
    """A non-reserved name is a custom narrative cell, not a rejection — the
    deliverable's structure must not block an agent from adding a section
    <deliverable_format> doesn't already name (mirrors the code cells'
    "non-standard phase just proceeds" philosophy)."""
    n = _node(tmp_path)
    out = _tool(n, "WriteCell")("caveats", content="x is subject to y")
    assert "Added custom" in out
    nb = _read_nb(tmp_path)
    # verbatim content, no forced heading, unlike problem/hypotheses/verdict
    assert _cell(nb, "caveats")["source"] == "x is subject to y"


def test_add_markdown_cell_rejects_pillar_name_collision(tmp_path):
    n = _node(tmp_path)
    out = _tool(n, "WriteCell")("doe", content="x")
    assert out.startswith("ERROR:") and "`code` + `why`" in out
    out2 = _tool(n, "WriteCell")("doe__why", content="x")
    assert out2.startswith("ERROR:") and "`code` + `why`" in out2


def test_rewriting_a_markdown_cell_needs_its_rev(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("problem", content="p1")
    out = _tool(n, "WriteCell")("problem", content="p2")
    assert out.startswith("ERROR:") and "expected_rev" in out
    assert "p1" in _cell(_read_nb(tmp_path), "problem")["source"]


def test_add_markdown_cell_creates_verdict(tmp_path):
    """Regression: <deliverable_format> step 7 ('## Verdict & result',
    immediately ahead of the analysis pillar) instructs a standalone markdown
    heading in exactly the same literal-text shape as problem/hypotheses, but
    AddPipelineMarkdownCell's name whitelist previously only had those two —
    real run 20260820T003819, two independent critic reviews both flagged the
    strategizer hitting "name must be one of ['hypotheses', 'problem']" while
    trying to author this cell (it guessed 'verdict_heading').
    """
    n = _node(tmp_path)
    out = _tool(n, "WriteCell")("verdict", content="H1 SUPPORTED because ...")
    assert "Added verdict" in out
    nb = _read_nb(tmp_path)
    assert "H1 SUPPORTED" in _cell(nb, "verdict")["source"]
    assert "## Verdict & result" in _cell(nb, "verdict")["source"]


def test_edit_custom_markdown_cell_does_not_force_a_heading(tmp_path):
    """EditPipelineCell's markdown branch previously assumed any name not in
    _NARRATIVE ended in '__why' (stripping that suffix for its heading) —
    that assumption breaks for a genuinely free-form custom cell name. A
    custom cell's content must round-trip verbatim, with no heading forced
    on either create or edit.
    """
    n = _node(tmp_path)
    _tool(n, "WriteCell")("caveats", content="first draft")
    out = _tool(n, "WriteCell")("caveats", content="revised caveat text", expected_rev=_rev(tmp_path, "caveats"))
    assert "Edited caveats" in out
    nb = _read_nb(tmp_path)
    assert _cell(nb, "caveats")["source"] == "revised caveat text"


def test_edit_narrative_cell_with_content_and_rev(tmp_path):
    # The clunk fix: update hypotheses ALONE (no resupplying problem), rev-guarded.
    n = _node(tmp_path)
    _tool(n, "WriteCell")("problem", content="the problem")
    _tool(n, "WriteCell")("hypotheses", content="old hyp")
    out = _tool(n, "WriteCell")("hypotheses", content="new hyp", expected_rev=_rev(tmp_path, "hypotheses"))
    assert "Edited hypotheses" in out
    nb = _read_nb(tmp_path)
    assert "new hyp" in _cell(nb, "hypotheses")["source"]
    assert "the problem" in _cell(nb, "problem")["source"]  # untouched


def test_edit_narrative_cell_stale_rev_rejected(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("hypotheses", content="h")
    out = _tool(n, "WriteCell")("hypotheses", content="x", expected_rev="deadbeef")
    assert "changed since" in out


def test_edit_narrative_cell_rejects_code_param(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("problem", content="p")
    out = _tool(n, "WriteCell")("problem", code="x=1", expected_rev=_rev(tmp_path, "problem"))
    assert "markdown cell" in out and "content" in out


def test_delete_narrative_cell(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("problem", content="p")
    _tool(n, "WriteCell")("hypotheses", content="h")
    out = _tool(n, "WriteCell")("hypotheses", delete=True, expected_rev=_rev(tmp_path, "hypotheses"))
    assert "Deleted hypotheses" in out
    nb = _read_nb(tmp_path)
    assert _cell(nb, "hypotheses") is None and _cell(nb, "problem") is not None


def test_delete_custom_markdown_cell(tmp_path):
    """DeletePipelineCell previously gated on a static _NB_ORDER whitelist
    before even checking the notebook's real contents — rejecting a valid
    custom cell as 'unknown' rather than deleting it."""
    n = _node(tmp_path)
    _tool(n, "WriteCell")("caveats", content="x")
    out = _tool(n, "WriteCell")("caveats", delete=True, expected_rev=_rev(tmp_path, "caveats"))
    assert "Deleted caveats" in out
    assert _cell(_read_nb(tmp_path), "caveats") is None


# ── ShowNotebook — read + list by name ───────────────────────────────────────

def test_show_brief_lists_all_cells_by_name(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="rationale", code="x = 1")
    _tool(n, "WriteCell")("ml", why="fit it", code="fit()")
    out = _tool(n, "ShowNotebook")()
    for name in ("doe", "doe__why", "ml", "ml__why"):
        assert name in out
    assert "rev" in out
    assert "Missing:" in out  # not all pillars present


def test_show_detailed_returns_full_source_and_rev(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="x = 1\ny = 2")
    out = _tool(n, "ShowNotebook")("doe")
    assert "x = 1\ny = 2" in out
    assert _rev(tmp_path, "doe") in out


def test_show_missing_phase_lists_present(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="x=1")
    out = _tool(n, "ShowNotebook")("analysis")
    assert "no cell named" in out and "doe" in out


def test_show_absent_file_reports_without_creating(tmp_path):
    n = _node(tmp_path)
    out = _tool(n, "ShowNotebook")()
    assert "does not exist" in out
    assert not (tmp_path / "pipeline.ipynb").exists()


# ── WriteCell — surgical edit (self-guarding) ──────────────────────────────

def test_surgical_edit_replaces_single_occurrence(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="a = 1\nb = 2")
    out = _tool(n, "WriteCell")("doe", old="a = 1", new="a = 99")
    assert "Edited doe" in out
    assert _cell(_read_nb(tmp_path), "doe")["source"] == "a = 99\nb = 2"
    # why untouched, name preserved
    assert "w" in _cell(_read_nb(tmp_path), "doe__why")["source"]
    assert _cell(_read_nb(tmp_path), "doe")["metadata"]["name"] == "doe"


def test_surgical_old_not_found_errors(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="a = 1")
    out = _tool(n, "WriteCell")("doe", old="zzz", new="q")
    assert "not found" in out


def test_surgical_ambiguous_old_names_count(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="p = 1\np = 1")
    out = _tool(n, "WriteCell")("doe", old="p = 1", new="p = 2")
    assert "2" in out and ("occurs" in out or "×" in out)


def test_surgical_and_fullfield_mutually_exclusive(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="a = 1")
    out = _tool(n, "WriteCell")("doe", old="a", new="b", code="z = 1")
    assert "EITHER" in out or "not both" in out


# ── WriteCell — full-field edit, rev-guarded ───────────────────────────────

def test_fullfield_edit_requires_expected_rev(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="x = 1")
    out = _tool(n, "WriteCell")("doe", code="x = 2")
    assert "expected_rev" in out


def test_fullfield_edit_with_current_rev_succeeds(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="x = 1")
    rev = _rev(tmp_path, "doe")
    out = _tool(n, "WriteCell")("doe", code="x = 2", expected_rev=rev)
    assert "Edited doe" in out
    assert _cell(_read_nb(tmp_path), "doe")["source"] == "x = 2"


def test_fullfield_edit_with_stale_rev_rejected(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="x = 1")
    out = _tool(n, "WriteCell")("doe", code="x = 2", expected_rev="deadbeef")
    assert "changed since" in out
    assert _cell(_read_nb(tmp_path), "doe")["source"] == "x = 1"  # unchanged


def test_edit_missing_phase_errors(tmp_path):
    n = _node(tmp_path)
    out = _tool(n, "WriteCell")("analysis", code="y = 1", expected_rev="x")
    assert "not in pipeline.ipynb" in out


# ── WriteCell(delete=True) — rev-guarded ─────────────────────────────────────────

def test_delete_requires_expected_rev(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="c")
    out = _tool(n, "WriteCell")("doe", delete=True)
    assert "expected_rev" in out
    assert _cell(_read_nb(tmp_path), "doe") is not None  # not deleted


def test_delete_with_current_rev_removes_cell_and_why(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="c")
    _tool(n, "WriteCell")("ml", why="w2", code="c2")
    out = _tool(n, "WriteCell")("ml", delete=True, expected_rev=_rev(tmp_path, "ml"))
    assert "Deleted ml" in out
    nb = _read_nb(tmp_path)
    assert _cell(nb, "ml") is None and _cell(nb, "ml__why") is None
    assert _cell(nb, "doe") is not None


def test_delete_stale_rev_rejected(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="c")
    out = _tool(n, "WriteCell")("doe", delete=True, expected_rev="deadbeef")
    assert "changed since" in out
    assert _cell(_read_nb(tmp_path), "doe") is not None


def test_delete_absent_phase_is_noop(tmp_path):
    n = _node(tmp_path)
    _tool(n, "WriteCell")("doe", why="w", code="c")
    out = _tool(n, "WriteCell")("analysis", delete=True, expected_rev="x")
    assert "Nothing to delete" in out


# ── RunScratch ───────────────────────────────────────────────────────────────

def test_run_scratch_executes_and_returns_stdout(tmp_path):
    n = _node(tmp_path)
    notes = tmp_path / "runs" / "T1" / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n._current_notes_dir = notes
    out = _tool(n, "RunScratch")("print(6 * 7)")
    assert "exit 0" in out and "42" in out


def test_run_scratch_surfaces_errors(tmp_path):
    n = _node(tmp_path)
    notes = tmp_path / "runs" / "T1" / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n._current_notes_dir = notes
    out = _tool(n, "RunScratch")("raise ValueError('boom')")
    assert "exit 1" in out and "boom" in out


def test_run_scratch_empty_code_errors(tmp_path):
    n = _node(tmp_path)
    out = _tool(n, "RunScratch")("   ")
    assert "ERROR" in out


# ── Dead jupyter-MCP path removed ────────────────────────────────────────────

def test_strategizer_has_no_jupyter_config():
    from adda._src.agents.strategizer import StrategizerAgent
    a = StrategizerAgent()
    assert not getattr(a, "needs_jupyter_server", False)
    assert getattr(a, "mcp_servers", {}) == {}
    assert not any("jupyter" in t for t in getattr(a, "extra_allowed_tools", []))


def test_agent_runtime_imports_without_notebook_server():
    import importlib
    import os
    importlib.import_module("adda._src.runtime.agent_runtime")
    import adda._src as ag
    assert not os.path.exists(ag.__path__[0] + "/notebook_server.py")


# ── Canonical-store integrity backstop (report 2: RunScratch/RunNotebook ────
# run a real OS subprocess with full filesystem access — cwd=sandbox and
# F3DASM_CANONICAL_STORE redirect the STORE-LOADING PATH, not the filesystem
# — so an absolute-path write still reaches the real store) ────────────────

def test_run_scratch_reverts_a_direct_write_to_the_real_store(tmp_path):
    n = _node(tmp_path)
    run_dir = tmp_path / "runs" / "T1"
    notes = run_dir / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n._current_notes_dir = notes
    store = run_dir / "experiment_data"
    store.mkdir(parents=True)
    real_file = store / "output.csv"
    real_file.write_text("original,content\n")

    code = (
        "import pathlib\n"
        f"p = pathlib.Path({str(real_file)!r})\n"
        "p.write_text('tampered\\n')\n"
        "print('done')\n"
    )
    out = _tool(n, "RunScratch")(code)
    assert out.startswith("ERROR:")
    assert "Reverted" in out
    assert str(real_file) in out
    assert real_file.read_text() == "original,content\n"


def test_run_scratch_leaves_untouched_store_alone(tmp_path):
    n = _node(tmp_path)
    run_dir = tmp_path / "runs" / "T1"
    notes = run_dir / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n._current_notes_dir = notes
    store = run_dir / "experiment_data"
    store.mkdir(parents=True)
    (store / "output.csv").write_text("original,content\n")

    out = _tool(n, "RunScratch")("print(6 * 7)")
    assert not out.startswith("ERROR:")
    assert "42" in out
    assert (store / "output.csv").read_text() == "original,content\n"


def test_run_scratch_reverts_a_direct_delete_of_the_real_store(tmp_path):
    n = _node(tmp_path)
    run_dir = tmp_path / "runs" / "T1"
    notes = run_dir / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n._current_notes_dir = notes
    store = run_dir / "experiment_data"
    store.mkdir(parents=True)
    real_file = store / "output.csv"
    real_file.write_text("original,content\n")

    code = (
        "import pathlib\n"
        f"pathlib.Path({str(real_file)!r}).unlink()\n"
        "print('done')\n"
    )
    out = _tool(n, "RunScratch")(code)
    assert out.startswith("ERROR:")
    assert "Reverted" in out
    assert real_file.exists()
    assert real_file.read_text() == "original,content\n"


def test_run_scratch_does_not_revert_while_a_delegation_is_running(tmp_path):
    """The race boss flagged: a delegation on its own thread can be
    mid-campaign, writing metered evals into the SAME store via
    get_evaluator() while the strategizer calls RunScratch. The guard's
    before/after diff cannot tell that legitimate append apart from snippet
    damage, so while ANY delegation is Working it must report the change but
    leave it in place — never revert a real evaluation."""
    n = _node(tmp_path)
    run_dir = tmp_path / "runs" / "T1"
    notes = run_dir / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n._current_notes_dir = notes
    store = run_dir / "experiment_data"
    store.mkdir(parents=True)
    real_file = store / "output.csv"
    real_file.write_text("original,content\n")

    # A delegation is Working for the whole call, as during a live campaign.
    n._registry["D001"] = {
        "status": "Working", "is_falsification_attempt": False,
        "hypothesis_ids": [],
    }

    # Stand-in for get_evaluator()'s own append (a real metered row).
    code = (
        "import pathlib\n"
        f"p = pathlib.Path({str(real_file)!r})\n"
        "p.write_text(p.read_text() + 'D001,3.14\\n')\n"
        "print('done')\n"
    )
    out = _tool(n, "RunScratch")(code)
    assert out.startswith("ERROR:")
    assert "NOT reverted" in out
    assert "Reverted (" not in out
    # The concurrent write survives — it was never reverted.
    assert real_file.read_text() == "original,content\nD001,3.14\n"


def test_run_scratch_never_reverts_a_pure_append_even_with_no_delegation_running(tmp_path):
    """Boss's narrowing: a background process a worker started
    (Bash(run_in_background=True), or an async solve pool backing a long
    Abaqus-style solve) is reaped only by the run's watchdog at exit, never
    per delegation — so a delegation reading Done/Errored/Cancelled in the
    registry is NOT proof nothing else can still append to the store. A pure
    append must survive regardless of what the registry says; only a
    destructive change (rewrite of existing bytes) is reverted, and only
    then because no delegation is running."""
    n = _node(tmp_path)
    run_dir = tmp_path / "runs" / "T1"
    notes = run_dir / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    n._current_notes_dir = notes
    store = run_dir / "experiment_data"
    store.mkdir(parents=True)
    append_target = store / "output.csv"
    append_target.write_text("original,content\n")
    rewrite_target = store / "jobs.csv"
    rewrite_target.write_text("job,status\n1,FINISHED\n")
    new_file_target = store / "input.csv"
    assert not new_file_target.exists()
    assert n._registry == {}  # no delegation running at all

    code = (
        "import pathlib\n"
        f"a = pathlib.Path({str(append_target)!r})\n"
        "a.write_text(a.read_text() + 'D009,2.71\\n')\n"
        f"r = pathlib.Path({str(rewrite_target)!r})\n"
        "r.write_text('job,status\\n1,CORRUPTED\\n')\n"
        f"nf = pathlib.Path({str(new_file_target)!r})\n"
        "nf.write_text('x0\\n0.5\\n')\n"
        "print('done')\n"
    )
    out = _tool(n, "RunScratch")(code)
    assert out.startswith("ERROR:")
    # The append survives — reported, never touched.
    assert append_target.read_text() == "original,content\nD009,2.71\n"
    assert str(append_target) in out
    # A brand-new file (a late writer's first evaluation, or a new
    # namespace's first store) is ALSO an append — survives, reported.
    assert new_file_target.read_text() == "x0\n0.5\n"
    assert str(new_file_target) in out
    # The rewrite is reverted — no delegation running, not a pure append.
    assert rewrite_target.read_text() == "job,status\n1,FINISHED\n"
    assert "Reverted" in out
    assert str(rewrite_target) in out
