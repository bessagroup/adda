"""End-to-end integration test: AgenticRun.execute() with mock adapters.

Runs under the default (peer_interaction on): ScriptedStrategistAdapter
(tests/fixtures.py) collects each delegation via Wait(block=False) polling,
then Wait(id) + approve_delegation (SendMessage(..., approve=True)) before
using its result -- exactly what a real delegator must do now that a report
opens for review instead of auto-finalizing (spec 12 item 8). This exercises
workspace/VCS wiring, the hypothesis ledger and the delegation log WITH the
review gate in the loop, not around it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from adda._src.runtime.agent_runtime import AgenticRun
from adda._src.backends.base import Agent, Edge, Graph

from .fixtures import MockWorkerAdapter, ScriptedStrategistAdapter

# Every run here ends in the reproduction gate, which starts a real Jupyter kernel;
# kernels racing for ports under -n auto produced "Address already in use" (CI 53f3861).
pytestmark = pytest.mark.xdist_group(name="jupyter_kernel")


PROBLEM_MD = """\
# Test Problem

Maximise sigma_crit for coilable designs.

## Design space
ratio_d: [0.004, 0.073]

## Objective
Find highest sigma_crit with coilable == 1.
"""


def _make_study(tmp_path: Path) -> Path:
    study = tmp_path / "study"
    study.mkdir()
    (study / "PROBLEM_STATEMENT.md").write_text(PROBLEM_MD)
    return study


class _StrategistSpec(Agent):
    role = "strategizer"
    description = "Test strategizer."
    # This scripted driver polls delegations deterministically with
    # Wait(block=False), and writes the notebook with WriteCell.
    tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote", "WriteCell", "Wait", "HypothesisPropose", "HypothesisUpdate", "HypothesisList", "MilestoneList", "MilestoneSet", "QueryStore"})


class _WorkerSpec(Agent):
    description = "Test implementer."


def _graph_spec() -> Graph:
    return Graph(
        nodes={"strategizer": _StrategistSpec(), "implementer": _WorkerSpec()},
        edges=(Edge("strategizer", "implementer"),),
        entry="strategizer",
    )


@pytest.fixture
def pipeline_run(tmp_path):
    """AgenticRun with mock adapters; returns (run, study_dir) after execute()."""
    study = _make_study(tmp_path)
    run = AgenticRun(study_dir=study, graph=_graph_spec())
    strat = ScriptedStrategistAdapter(run=run)
    worker = MockWorkerAdapter()

    def _mock_make_adapter(name, agent):
        return strat if name == "strategizer" else worker

    run._make_adapter = _mock_make_adapter
    run.execute()
    return run, study


# ---------------------------------------------------------------------------
# Output directory structure
# ---------------------------------------------------------------------------


def test_default_arm_reaches_gated_via_delegate_wait_approve(pipeline_run, tmp_path):
    """The headless proof that the default (peer_interaction on) actually
    works end to end: Delegate -> Wait(block=False) polling -> Wait(id) ->
    approve (SendMessage(..., approve=True)) -> Done()'s reproduction gate
    check -> a clean terminal close. Every other test in this file exercises
    this same path (ScriptedStrategistAdapter approves both its
    delegations); this one checks the terminal outcome explicitly rather
    than only the side effects along the way.

    UNGATED (not GATED) is the correct outcome here, unrelated to
    peer_interaction: GATED requires a connected critic whose gate PASSES
    (nodes/tools/routing/feedback.py::_close/_critic_gate) -- this graph
    has no critic node, so Done() closes UNGATED directly. See
    test_default_arm_reaches_gated_via_critic_pass below for the arm that
    DOES have one and reaches GATED -- confirmed unchanged with the
    feature OFF too."""
    _, study = pipeline_run
    run_dir = next((study / "runs").iterdir())
    status = json.loads((run_dir / "debug" / "run_status.json").read_text())
    assert status["termination"] == "done", status
    assert status["status"] in ("GATED", "UNGATED"), status
    assert (study / "pipeline.ipynb").exists()


class _CriticSpec(Agent):
    role = "critic"
    description = "Test critic."


class _CriticPassAdapter:
    """A critic that always PASSes -- the one path that earns GATED
    (nodes/tools/routing/feedback.py::_critic_gate)."""

    def __init__(self) -> None:
        self.closure_tools: dict = {}
        self.last_usage: dict = {}

    def invoke(self, messages):
        return "### Verdict\nPASS\n\nThe conclusion is grounded in the run's evidence."

    def copy(self):
        return self


def _graph_spec_with_critic() -> Graph:
    return Graph(
        nodes={
            "strategizer": _StrategistSpec(), "implementer": _WorkerSpec(),
            "critic": _CriticSpec(),
        },
        edges=(Edge("strategizer", "implementer"), Edge("strategizer", "critic")),
        entry="strategizer",
    )


@pytest.fixture
def pipeline_run_with_critic(tmp_path):
    """Same scripted flow as ``pipeline_run``, plus a critic that always
    PASSes -- the arm that reaches GATED (not just a clean UNGATED close)."""
    study = _make_study(tmp_path)
    run = AgenticRun(study_dir=study, graph=_graph_spec_with_critic())
    strat = ScriptedStrategistAdapter(run=run)
    worker = MockWorkerAdapter()
    critic = _CriticPassAdapter()

    def _mock_make_adapter(name, agent):
        return {"strategizer": strat, "critic": critic}.get(name, worker)

    run._make_adapter = _mock_make_adapter
    run.execute()
    return run, study


def test_default_arm_reaches_gated_via_critic_pass(pipeline_run_with_critic):
    """The headless proof that GATED itself composes with the default
    (peer_interaction on): the same Delegate -> Wait -> approve flow as
    test_default_arm_reaches_gated_via_delegate_wait_approve above, but with
    a critic connected that PASSes -- the one path that earns GATED
    (nodes/tools/routing/feedback.py::_critic_gate). No headless test
    exercised this exact composition before (grep across tests/*.py for
    ``outcome.*GATED`` before this commit finds only direct unit calls into
    _finalize_run/terminal.resolve, never a scripted run reaching it through
    Delegate/Wait/approve for real)."""
    _, study = pipeline_run_with_critic
    run_dir = next((study / "runs").iterdir())
    status = json.loads((run_dir / "debug" / "run_status.json").read_text())
    assert status["status"] == "GATED", status
    assert status["reviewed"] is True, status


def test_run_dir_created(pipeline_run, tmp_path):
    """execute() creates runs/<timestamp>/ directory; solution.md at study root."""
    _, study = pipeline_run
    runs = list((study / "runs").iterdir())
    assert len(runs) == 1
    run_dir = runs[0]
    assert (run_dir / "debug" / "strategizer_notes").is_dir()
    assert (run_dir / "debug" / "run.log").exists()
    # the deliverable notebook lives at the study root (there is no solution.md)
    assert (study / "pipeline.ipynb").exists()
    assert not (study / "solution.md").exists()


def test_workspace_subfolders_created(pipeline_run, tmp_path):
    """Worker delegations create debug/delegations/D001/ and D002/."""
    _, study = pipeline_run
    run_dir = next((study / "runs").iterdir())
    workspace = run_dir / "debug" / "delegations"
    assert workspace.is_dir()
    # Two delegations → two subfolders
    subfolders = [p.name for p in workspace.iterdir() if p.is_dir()]
    assert "D001" in subfolders
    assert "D002" in subfolders


# ---------------------------------------------------------------------------
# Hypothesis ledger
# ---------------------------------------------------------------------------


def test_hypotheses_json_created(pipeline_run):
    """hypotheses.json exists under strategizer_notes/."""
    _, study = pipeline_run
    notes = next((study / "runs").iterdir()) / "debug" / "strategizer_notes"
    assert (notes / "hypotheses.json").exists()


def test_hypotheses_json_has_two_entries(pipeline_run):
    """H1 and H2 are recorded in hypotheses.json."""
    _, study = pipeline_run
    notes = next((study / "runs").iterdir()) / "debug" / "strategizer_notes"
    data = json.loads((notes / "hypotheses.json").read_text())
    assert "H1" in data
    assert "H2" in data


def test_h1_falsified_h2_supported(pipeline_run):
    """H1 ends FALSIFIED and H2 ends SUPPORTED."""
    _, study = pipeline_run
    notes = next((study / "runs").iterdir()) / "debug" / "strategizer_notes"
    data = json.loads((notes / "hypotheses.json").read_text())
    assert data["H1"]["status_log"][-1]["status"] == "FALSIFIED"
    assert data["H2"]["status_log"][-1]["status"] == "SUPPORTED"


def test_triggered_by_links_delegation(pipeline_run):
    """H1's FALSIFIED entry has triggered_by = D001."""
    _, study = pipeline_run
    notes = next((study / "runs").iterdir()) / "debug" / "strategizer_notes"
    data = json.loads((notes / "hypotheses.json").read_text())
    falsified_entry = data["H1"]["status_log"][-1]
    assert falsified_entry["triggered_by"] == "D001"


# ---------------------------------------------------------------------------
# Delegation log
# ---------------------------------------------------------------------------


def test_delegation_log_created(pipeline_run):
    """delegation_log.jsonl exists under debug/ with two logical records.

    (The raw file also carries dispatch-time RUNNING entries; query_all()
    collapses last-wins to one record per delegation — see provenance fix.)"""
    from adda._src.infra.delegation_log import DelegationLog
    _, study = pipeline_run
    debug = next((study / "runs").iterdir()) / "debug"
    records = DelegationLog(debug / "delegation_log.jsonl").query_all()
    assert len(records) == 2


def test_delegation_records_have_token_fields(pipeline_run):
    """Each COMPLETED delegation record has non-zero tokens_in and tokens_out.

    (Dispatch-time RUNNING entries legitimately carry 0 tokens; query_all()
    collapses to the terminal record per delegation.)"""
    from adda._src.infra.delegation_log import DelegationLog
    _, study = pipeline_run
    debug = next((study / "runs").iterdir()) / "debug"
    for rec in DelegationLog(debug / "delegation_log.jsonl").query_all():
        if rec["status"] != "DONE":
            continue
        assert rec["tokens_in"] > 0, f"tokens_in missing in {rec['id']}"
        assert rec["tokens_out"] > 0, f"tokens_out missing in {rec['id']}"
        assert rec["cost_usd"] is not None


def test_delegation_records_link_hypotheses(pipeline_run):
    """D001 links to H1 and D002 links to H2."""
    _, study = pipeline_run
    debug = next((study / "runs").iterdir()) / "debug"
    records = [
        json.loads(l)
        for l in (debug / "delegation_log.jsonl").read_text().strip().splitlines()
    ]
    by_id = {r["id"]: r for r in records}
    assert "H1" in by_id["D001"]["hypothesis_ids"]
    assert "H2" in by_id["D002"]["hypothesis_ids"]


# ---------------------------------------------------------------------------
# solution.md token table
# ---------------------------------------------------------------------------


def _notebook_markdown(study):
    """Concatenated markdown of the deliverable notebook (the writeup)."""
    import nbformat
    nb = nbformat.read(str(study / "pipeline.ipynb"), as_version=4)
    return "\n\n".join(
        c.source for c in nb.cells if c.get("cell_type") == "markdown")


def test_notebook_has_token_table(pipeline_run):
    """The deliverable notebook carries a ## Token usage section (stamped by the
    runtime as a trailing markdown cell) — there is no solution.md."""
    _, study = pipeline_run
    assert not (study / "solution.md").exists()
    text = _notebook_markdown(study)
    assert "## Token usage" in text
    assert "input_tokens" in text
    assert "output_tokens" in text
    assert "estimated_cost" in text


def test_notebook_nonzero_tokens(pipeline_run):
    """Token counts in the notebook writeup are non-zero (mock usage)."""
    _, study = pipeline_run
    text = _notebook_markdown(study)
    m = re.search(r"\| total_tokens \| ([\d,]+) \|", text)
    assert m, "total_tokens row not found in the notebook writeup"
    total = int(m.group(1).replace(",", ""))
    assert total > 0


# ---------------------------------------------------------------------------
# notebook provenance stamp
# ---------------------------------------------------------------------------


def test_notebook_has_provenance_metadata(pipeline_run):
    """The deliverable notebook carries run provenance in its metadata (model +
    run dir + timestamp) — the script provenance header is gone."""
    import nbformat
    _, study = pipeline_run
    nb = nbformat.read(str(study / "pipeline.ipynb"), as_version=4)
    agentic = nb.metadata.get("agentic", {})
    assert agentic.get("model")
    assert agentic.get("run")
    assert agentic.get("timestamp")


def test_notebook_carries_problem_statement_provenance(pipeline_run):
    """A study is meant to be run once per PROBLEM_STATEMENT.md; when it isn't
    (the file drifts and gets re-run against), a reader of a later
    pipeline.ipynb needs a way to check what statement THAT run actually
    answered. debug/PROBLEM_STATEMENT_snapshot.md is the verbatim, recoverable
    copy; the notebook only carries its hash (both the visible Run metadata
    cell and nb.metadata['agentic']) so a reader can confirm which snapshot
    matches without needing to inspect raw notebook JSON.
    """
    import hashlib

    import nbformat
    run, study = pipeline_run
    run_dir = next((study / "runs").iterdir())
    snapshot_path = run_dir / "debug" / "PROBLEM_STATEMENT_snapshot.md"
    assert snapshot_path.exists()
    assert snapshot_path.read_text(encoding="utf-8") == (
        study / "PROBLEM_STATEMENT.md"
    ).read_text(encoding="utf-8")

    expected_hash = hashlib.sha256(
        snapshot_path.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()

    nb = nbformat.read(str(study / "pipeline.ipynb"), as_version=4)
    assert nb.metadata["agentic"]["problem_statement_sha256"] == expected_hash

    stamp_cell = next(
        c for c in nb.cells
        if (getattr(c, "metadata", None) or {}).get("name") == "_run_provenance"
    )
    assert expected_hash in stamp_cell.source
    assert "PROBLEM_STATEMENT_snapshot.md" in stamp_cell.source


# ---------------------------------------------------------------------------
# run.log
# ---------------------------------------------------------------------------


def test_run_log_mentions_tokens(pipeline_run):
    """run.log final line includes token counts."""
    _, study = pipeline_run
    run_dir = next((study / "runs").iterdir())
    log_text = (run_dir / "debug" / "run.log").read_text()
    assert "Tokens" in log_text or "tokens" in log_text


# ---------------------------------------------------------------------------
# constraint snapshot on the very first strategizer turn (human -> strategizer
# is a delegation like any other; the budget/time state must be automatically
# in the first message set the agent receives, not something it has to go
# query for — see constraint_snapshot.py)
#
# This asserted messages[0] — the block CONCATENATED onto the problem
# statement. That position was the bug: the problem statement is the standing
# first user turn, re-sent verbatim, so the numbers inside it froze at run
# start (run 20260917T141603 read "5% used" 23 minutes in). The block is now
# injected per turn, so it is in the first invoke's messages but not glued to
# messages[0]. What this test protects is that the strategizer never takes a
# turn without being told its budgets — not where the block sits.
# ---------------------------------------------------------------------------


def test_first_strategizer_message_carries_constraint_snapshot(tmp_path):
    """The FIRST message the strategizer's underlying adapter.invoke() ever
    receives — traced through the real AgenticRun.execute() path, not a
    smaller unit test of one function in isolation — already contains the
    <constraints> block, before the strategizer has taken any action at all.
    """
    study = _make_study(tmp_path)

    captured_first_messages: list = []

    class CapturingStrategist:
        """Records the messages of its FIRST invoke() call, then closes the
        run immediately (no critic in this graph, so Done() closes directly)."""

        def __init__(self):
            self.closure_tools: dict = {}
            self.route_watcher = None
            self.last_usage: dict = {}

        def invoke(self, messages):
            if not captured_first_messages:
                captured_first_messages.append(list(messages))
            tools = self.closure_tools
            if "MilestoneList" in tools:
                import re as _re
                for mid in _re.findall(r"M\d{3}", tools["MilestoneList"]()):
                    tools["MilestoneSet"](mid, "SKIPPED", note="n/a for this test")
            tools["WriteCell"]("analysis", why="headline",
                               code="print('REPRODUCED: 0.0')")
            tools["Done"](summary="closing immediately")
            tools["Done"](summary="closing immediately")
            return "done"

    class NoOpWorker:
        closure_tools: dict = {}
        last_usage: dict = {}

        def invoke(self, messages):
            return "## Report\n### Actions taken\n- n/a\n"

    run = AgenticRun(study_dir=study, graph=_graph_spec())
    strat = CapturingStrategist()
    worker = NoOpWorker()

    def _mock_make_adapter(name, agent):
        return strat if name == "strategizer" else worker

    run._make_adapter = _mock_make_adapter
    run.execute()

    assert captured_first_messages, "Strategizer adapter was never invoked"
    first_call_messages = captured_first_messages[0]
    assert first_call_messages, "First invoke() call had no messages"
    blocks = [
        str(m.get("content", "")) for m in first_call_messages
        if "<constraints>" in str(m.get("content", ""))
    ]
    assert blocks, (
        "Expected a constraint snapshot somewhere in the very first message "
        "set the strategizer receives; got roles/prefixes:\n"
        + repr([(m.get("role"), str(m.get("content", ""))[:80])
                for m in first_call_messages])
    )
    assert "Evaluation budget" in blocks[-1]
    assert "Wall-clock budget" in blocks[-1]

    # ...and exactly one, or the agent is reading two clocks at once.
    assert len(blocks) == 1, (
        f"{len(blocks)} constraint blocks in one turn; a stale one alongside "
        "a fresh one is what the agent cannot tell apart"
    )


def test_run_log_does_not_falsely_claim_the_notebook_was_stamped(tmp_path):
    """Regression: a live wet-test run (an under-capable model that closed
    the run without ever calling WriteDeliverable) showed run.log claiming
    "pipeline.ipynb stamped" even though pipeline.ipynb never existed — the
    log line was unconditional while the actual stamping code was gated on
    nb_path.exists(). If the notebook was never written, run.log must say so
    honestly instead of claiming success.
    """
    study = _make_study(tmp_path)

    class NoDeliverableStrategist:
        """Closes the run without ever calling WriteDeliverable()."""

        def __init__(self):
            self.closure_tools: dict = {}
            self.route_watcher = None
            self.last_usage: dict = {}

        def invoke(self, messages):
            tools = self.closure_tools
            if "MilestoneList" in tools:
                for mid in re.findall(r"M\d{3}", tools["MilestoneList"]()):
                    tools["MilestoneSet"](mid, "SKIPPED", note="n/a for this test")
            tools["Done"](summary="closing without a deliverable")
            tools["Done"](summary="closing without a deliverable")
            return "done"

    class NoOpWorker:
        closure_tools: dict = {}
        last_usage: dict = {}

        def invoke(self, messages):
            return "## Report\n### Actions taken\n- n/a\n"

    run = AgenticRun(study_dir=study, graph=_graph_spec())
    strat = NoDeliverableStrategist()
    worker = NoOpWorker()

    def _mock_make_adapter(name, agent):
        return strat if name == "strategizer" else worker

    run._make_adapter = _mock_make_adapter
    run.execute()

    assert not (study / "pipeline.ipynb").exists(), (
        "test setup invariant broken: the deliverable should not exist"
    )
    run_dir = next((study / "runs").iterdir())
    log_text = (run_dir / "debug" / "run.log").read_text()
    assert "pipeline.ipynb stamped" not in log_text, (
        f"run.log falsely claims the notebook was stamped; log:\n{log_text}"
    )
    assert "NEVER WRITTEN" in log_text, (
        f"Expected run.log to honestly report the missing deliverable; "
        f"log:\n{log_text}"
    )


# ---------------------------------------------------------------------------
# Spec 11 — the workspace repo, end to end through a real run
# ---------------------------------------------------------------------------


def test_run_initialises_a_workspace_repo(pipeline_run):
    """_prepare_run version-controls debug/delegations/ for the run."""
    import subprocess
    _, study = pipeline_run
    ws = next((study / "runs").iterdir()) / "debug" / "delegations"
    assert (ws / ".git").exists()
    count = subprocess.run(
        ["git", f"--git-dir={ws / '.git'}", f"--work-tree={ws}",
         "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # root commit + one per delegation
    assert int(count) == 1 + 2


def test_every_delegation_record_carries_a_resolvable_workspace_sha(pipeline_run):
    """The KPI of spec 11: 'which files did this delegation change' is answered
    by the record, not by the deliverable's own prose."""
    import subprocess
    from adda._src.infra.delegation_log import DelegationLog

    _, study = pipeline_run
    run_dir = next((study / "runs").iterdir())
    ws = run_dir / "debug" / "delegations"
    records = DelegationLog(run_dir / "debug" / "delegation_log.jsonl").query_all()

    assert records
    for rec in records:
        sha = rec.get("workspace_sha")
        assert sha, f"{rec['id']} carries no workspace_sha"
        done = subprocess.run(
            ["git", f"--git-dir={ws / '.git'}", f"--work-tree={ws}",
             "cat-file", "-t", sha],
            capture_output=True, text=True,
        )
        assert done.returncode == 0 and done.stdout.strip() == "commit", (
            f"{rec['id']}'s workspace_sha {sha!r} does not resolve"
        )
