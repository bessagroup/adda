"""Falsification read-time ritual (friction: late SUPPORTED_WITHOUT_ATTACK spin).

When the strategizer reads a finished delegation's report, a checkpoint forces
it to classify whether that delegation was a falsification ATTEMPT of a
registered hypothesis — and if so, link it and record the verdict THEN, judged
against the hypothesis's pre-registered (immutable) prediction. The ritual only
LINKS; it never writes a verdict (no rubber-stamp) and never authors a criterion
(no §4 goalpost-move). Exploration delegations pass cleanly with no hypothesis.
"""
from __future__ import annotations

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra.delegation_log import DelegationLog
from adda._src.nodes import Node


class _Stub:
    def __init__(self) -> None:
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return ""


def _node(tmp_path, with_critic=False):
    class A(Agent):
        role = "strategizer"
        # The ritual tests drive a delegation to completion by polling it
        # with Wait(block=False).
        tools = frozenset({"Done", "Wait", "HypothesisPropose", "HypothesisUpdate", "HypothesisList", "MilestoneList", "MilestoneSet", "QueryStore"})
        description = "strategizer"

    class B(Agent):
        role = "implementer"
        description = "implementer"

    nodes = {"strategizer": A(), "implementer": B()}
    edges = [Edge("strategizer", "implementer")]
    spec = Graph(nodes=nodes, edges=tuple(edges), entry="strategizer")
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    dlog = DelegationLog(tmp_path / "debug" / "delegation_log.jsonl")
    n = Node(
        _Stub(), name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": _Stub()},
        notes_dir=notes, delegation_log=dlog,
    )
    return n


def _propose(n, statement="thin walls buckle first", pred="best feasible f < 2.0"):
    return n.adapter.closure_tools["HypothesisPropose"](
        statement, "an adequate sweep finds a feasible f >= 2.0", pred, 0.5)


def _done_record(n, did="D001", started_at="2026-01-01T00:00:00+00:00"):
    n._delegation_log.record(
        id=did, from_node="strategizer", to_node="implementer",
        task="t", deliverable="## Report\n### Numbers\nbest f: 1.5\n",
        hypothesis_ids=[], started_at=started_at,
        completed_at="2099-01-01T01:00:00+00:00", status="DONE")


def _after_now():
    """A start time later than any hypothesis proposed in this test."""
    return "2099-01-01T00:00:00+00:00"


def test_hypothesis_link_deferred_while_process_backlog_open(tmp_path):
    """#3: the 'every delegation must link a hypothesis' error is DEFERRED while
    the process backlog is still open (setup phase — no hypotheses exist yet, so
    there's nothing to link). Once the backlog is cleared, the link is required.
    """
    n = _node(tmp_path)
    assert [m["id"] for m in n._milestones.pending()]  # backlog open at start
    deleg = n.adapter.closure_tools["Delegate"]

    # Backlog OPEN: an empty-hypothesis delegation is NOT bounced on the link
    # rule (it defers, then hits the separate milestone gate).
    out = deleg(target="implementer", intent="wrap the oracle",
                expected_report="validation", hypothesis_ids=[])
    assert "must not be empty" not in out

    # Backlog CLEARED: the link is required again.
    for m in n._milestones.list_all():
        n._milestones.skip(m["id"], "n/a")
    out2 = deleg(target="implementer", intent="run experiment",
                 expected_report="results", hypothesis_ids=[])
    assert "must not be empty" in out2


# --------------------------------------------------------------------------
# 1. DelegationLog.mark_attempt — retroactive post-hoc link
# --------------------------------------------------------------------------

def test_mark_attempt_flags_and_stamps_posthoc(tmp_path):
    dlog = DelegationLog(tmp_path / "dlog.jsonl")
    dlog.record(
        id="D001", from_node="s", to_node="i", task="t", deliverable="d",
        hypothesis_ids=[], started_at="t0", completed_at="t1", status="DONE")
    assert dlog.mark_attempt("D001", "H1") is True
    rec = dlog.query_all()[0]
    assert rec["is_falsification_attempt"] is True
    assert "H1" in rec["hypothesis_ids"]
    assert rec["attempt_linked_post_hoc"] is True


def test_mark_attempt_unknown_id_returns_false(tmp_path):
    dlog = DelegationLog(tmp_path / "dlog.jsonl")
    assert dlog.mark_attempt("D999", "H1") is False


# --------------------------------------------------------------------------
# 2. HypothesisUpdate(falsification_attempt=True) — the post-hoc link
# --------------------------------------------------------------------------

def _verdict(n, hid, did="D001", **kw):
    return n.adapter.closure_tools["HypothesisUpdate"](
        hid, "FALSIFIED", "the sweep found a feasible f of 1.5", 0.1,
        evidence={"delegation": did, "numbers": {"best f": 1.5}},
        falsification_attempt=True, **kw)


def test_link_falsification_links_registry_and_log(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)
    n._registry["D001"] = {
        "status": "Done", "result": "r", "is_falsification_attempt": False,
        "hypothesis_ids": [], "reconciled": False}
    _done_record(n, started_at=_after_now())
    out = _verdict(n, hid)
    assert not out.startswith("ERROR"), out
    assert n._registry["D001"]["is_falsification_attempt"] is True
    assert hid in n._registry["D001"]["hypothesis_ids"]
    assert n._registry["D001"]["reconciled"] is True
    rec = n._delegation_log.query_all()[0]
    assert rec["is_falsification_attempt"] is True
    assert rec["attempt_linked_post_hoc"] is True


def test_link_refuses_a_delegation_that_began_before_the_hypothesis(tmp_path):
    """The anti-retrofit rule, enforced in code: a delegation that started
    before the hypothesis was registered cannot have been designed to test
    it, so it cannot be linked as its falsification attempt."""
    n = _node(tmp_path)
    hid = _propose(n)
    n._registry["D001"] = {
        "status": "Done", "result": "r", "is_falsification_attempt": False,
        "hypothesis_ids": [], "reconciled": False}
    _done_record(n, started_at="2000-01-01T00:00:00+00:00")
    out = _verdict(n, hid)
    assert out.startswith("ERROR") and "before" in out
    assert n._registry["D001"]["is_falsification_attempt"] is False


def test_a_refused_link_records_no_verdict(tmp_path):
    """No rubber-stamp: when the link is refused, the verdict is not
    recorded either — the hypothesis stays OPEN."""
    n = _node(tmp_path)
    hid = _propose(n)
    out = _verdict(n, hid, did="D999")
    assert "unknown delegation" in out.lower()
    statuses = [h["current_status"] for h in n._ledger.list_all()
                if h["id"] == hid]
    assert statuses == ["OPEN"]


def test_link_errors_on_unknown_delegation_or_hypothesis(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)
    assert "unknown delegation" in _verdict(n, hid, did="D999").lower()
    n._registry["D001"] = {"status": "Done", "hypothesis_ids": [],
                           "is_falsification_attempt": False}
    assert "not found" in _verdict(n, "H99").lower()


def test_link_needs_the_attempt_as_evidence(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)
    out = n.adapter.closure_tools["HypothesisUpdate"](
        hid, "FALSIFIED", "no evidence named", 0.1, evidence=None,
        falsification_attempt=True)
    assert out.startswith("ERROR") and "evidence" in out


def test_link_attempt_on_d000_names_it_as_the_pool_not_unknown(tmp_path):
    """'D000' is never a real delegation (it's the precomputed-pool marker),
    so falsification_attempt=True citing it is still refused — but the
    message must say WHY (it's the pool, not that it's an unrecognized id),
    unlike the generic 'unknown delegation ... Known: []' that used to fire
    here and reads as if D000 were simply a typo."""
    n = _node(tmp_path)
    hid = _propose(n)
    out = _verdict(n, hid, did="D000")
    assert out.startswith("ERROR")
    assert "precomputed pool" in out
    assert "Known:" not in out


# --------------------------------------------------------------------------
# 4. _check_cited_delegation D000 integrity (run 20260926T124841: an agent in
# a study with NO precomputed pool cited "D000" for its own inline results,
# and the guard used to accept it unconditionally)
# --------------------------------------------------------------------------

def _write_d000_pool(run_dir):
    from f3dasm._src.design.domain import Domain
    from f3dasm._src.experimentdata import ExperimentData
    from f3dasm._src.experimentsample import ExperimentSample, JobStatus

    domain = Domain()
    domain.add_float("x0", 0.0, 1.0)
    for k in ("f", "_delegation_id", "_source", "_ts"):
        domain.add_output(k, exist_ok=True)
    sample = ExperimentSample(
        _input_data={"x0": 0.5},
        _output_data={
            "f": 1.0, "_delegation_id": "D000",
            "_source": "precomputed_pool",
            "_ts": "2026-01-01T00:00:00+00:00",
        },
        job_status=JobStatus.FINISHED,
    )
    data = ExperimentData.from_data(data={0: sample}, domain=domain)
    data.store(project_dir=run_dir)


def test_d000_citation_rejected_when_study_has_no_pool(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)
    out = n.adapter.closure_tools["HypothesisUpdate"](
        hid, "FALSIFIED", "the inline sweep found f=1.5", 0.1,
        evidence={"delegation": "D000", "numbers": {"best f": 1.5}})
    assert out.startswith("ERROR")
    assert "no precomputed pool" in out


def test_d000_citation_accepted_when_pool_was_ingested(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)
    _write_d000_pool(n._resolve_run_dir())
    out = n.adapter.closure_tools["HypothesisUpdate"](
        hid, "FALSIFIED", "the ground-truth pool shows f=1.0", 0.1,
        evidence={"delegation": "D000", "numbers": {"best f": 1.0}})
    assert not out.startswith("ERROR"), out


# --------------------------------------------------------------------------
# 4b. Bug 4 policy (Elvis: go) — D000 AS a falsification attempt. Popper
# accepts a refutation from existing data, so it is allowed — same
# pool-existence contract as 4.1's evidence-citation check, no separate
# accommodation tag (Elvis: over-engineering for this).
# --------------------------------------------------------------------------

def test_d000_falsification_attempt_accepted_with_a_pool(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)
    _write_d000_pool(n._resolve_run_dir())
    out = n.adapter.closure_tools["HypothesisUpdate"](
        hid, "FALSIFIED", "the ground-truth pool refutes it", 0.1,
        evidence={"delegation": "D000", "numbers": {"best f": 1.0}},
        falsification_attempt=True,
    )
    assert not out.startswith("ERROR"), out
    entry = n._ledger.get(hid)
    last = entry["status_log"][-1]
    assert last["evidence"]["delegation"] == "D000"


def test_d000_falsification_attempt_still_rejected_without_a_pool(tmp_path):
    """No pool → still refused, with the SAME 4.3 message _check_cited_delegation
    already uses (one consistent error regardless of which check path fired),
    not the old link-specific 'unknown delegation' wording."""
    n = _node(tmp_path)
    hid = _propose(n)
    out = n.adapter.closure_tools["HypothesisUpdate"](
        hid, "FALSIFIED", "no pool here", 0.1,
        evidence={"delegation": "D000", "numbers": {"best f": 1.0}},
        falsification_attempt=True,
    )
    assert out.startswith("ERROR")
    assert "no precomputed pool" in out
    assert "Known:" not in out


# --------------------------------------------------------------------------
# 3. Read-time ritual on the Done report
# --------------------------------------------------------------------------

def test_ritual_fires_once_then_reconciled(tmp_path):
    n = _node(tmp_path)
    _propose(n)
    n._registry["D001"] = {
        "status": "Done", "result": "the report", "start_time": 0.0,
        "is_falsification_attempt": False, "hypothesis_ids": [],
        "reconciled": False, "getstatus_count": 0}
    out1 = n.adapter.closure_tools["Wait"]("D001", block=False)
    assert out1.lstrip().startswith("Done")
    assert "FALSIFICATION CHECKPOINT" in out1
    assert n._registry["D001"]["reconciled"] is True
    # second read: no checkpoint (fire-once)
    out2 = n.adapter.closure_tools["Wait"]("D001", block=False)
    assert "FALSIFICATION CHECKPOINT" not in out2


def test_ritual_surfaces_pre_registered_prediction(tmp_path):
    n = _node(tmp_path)
    _propose(n, pred="resonance >= 8000 Hz")
    n._registry["D001"] = {
        "status": "Done", "result": "r", "start_time": 0.0,
        "is_falsification_attempt": False, "hypothesis_ids": [],
        "reconciled": False, "getstatus_count": 0}
    out = n.adapter.closure_tools["Wait"]("D001", block=False)
    assert "resonance >= 8000 Hz" in out  # the immutable, pre-registered text
    # the ritual must point at the post-hoc link, never invite a new
    # criterion after seeing the result
    assert "falsification_attempt=True" in out
    assert "do not retrofit" in out.lower()


def test_predeclared_attempt_collapses_to_verdict_reminder(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)
    n._registry["D001"] = {
        "status": "Done", "result": "r", "start_time": 0.0,
        "is_falsification_attempt": True, "hypothesis_ids": [hid],
        "reconciled": False, "getstatus_count": 0}
    out = n.adapter.closure_tools["Wait"]("D001", block=False)
    assert "FALSIFICATION CHECKPOINT" in out
    assert "declared a falsification attempt" in out.lower()
    assert hid in out and "HypothesisUpdate" in out


def test_exploration_with_no_hypotheses_passes_clean(tmp_path):
    n = _node(tmp_path)  # no hypotheses proposed
    n._registry["D001"] = {
        "status": "Done", "result": "r", "start_time": 0.0,
        "is_falsification_attempt": False, "hypothesis_ids": [],
        "reconciled": False, "getstatus_count": 0}
    out = n.adapter.closure_tools["Wait"]("D001", block=False)
    assert out.lstrip().startswith("Done")
    assert "CHECKPOINT" not in out


# --------------------------------------------------------------------------
# 4. Done()-gate lists dangling falsification attempts (WARNING, not ERROR)
# --------------------------------------------------------------------------

def test_done_gate_warns_on_dangling_falsification(tmp_path):
    n = _node(tmp_path)
    hid = _propose(n)  # stays OPEN
    n._registry["D001"] = {
        "status": "Done", "result": "r", "is_falsification_attempt": True,
        "hypothesis_ids": [hid], "reconciled": True}
    # clear the (orthogonal) milestone close-gate so we reach the falsification
    # warn this test is about
    if n._milestones is not None:
        for m in n._milestones.list_all():
            n._milestones.skip(m["id"], "n/a")
    out = n.adapter.closure_tools["Done"](summary="done")
    assert not out.lstrip().startswith("ERROR:")
    assert "D001" in out
    assert "OPEN" in out


# --------------------------------------------------------------------------
# 5. science_monitor self-heals once a post-hoc link is made
# --------------------------------------------------------------------------

def test_supported_confirm_without_attack_then_allowed_after_link(tmp_path):
    """SUPPORTED_WITHOUT_ATTACK is a TWO-SHOT CONFIRM at the HypothesisUpdate
    data boundary (§4), not a hard block. Verify:
    1. HypothesisUpdate returns [CONFIRM] for SUPPORTED with no falsification
       attack (the clean path is to run/link an attempt).
    2. After marking D001 as a falsification attempt, the update succeeds
       directly (the confirm is skipped — there IS an attempt now).
    """
    n = _node(tmp_path)
    hid = _propose(n)
    _done_record(n)
    # 1. No falsification attempt yet → two-shot confirm
    out = n.adapter.closure_tools["HypothesisUpdate"](
        hid, "SUPPORTED", "survived", 0.8,
        {"delegation": "D001", "numbers": {"best f": 1.5}})
    assert out.startswith("[CONFIRM]"), (
        f"Expected a two-shot confirm before a falsification attempt, got: {out!r}")
    # 2. Mark D001 as a falsification attempt → now allowed
    n._delegation_log.mark_attempt("D001", hid)
    out2 = n.adapter.closure_tools["HypothesisUpdate"](
        hid, "SUPPORTED", "survived falsification", 0.8,
        {"delegation": "D001", "numbers": {"best f": 1.5}})
    assert not out2.startswith("ERROR:"), (
        f"Expected success after falsification attempt, got: {out2!r}")


def test_hypothesis_list_with_ids_returns_full_entries(tmp_path):
    """HypothesisList absorbed HypothesisGet. Agents already passed it
    hypothesis_ids — the kwarg used to be accepted and ignored so it would not
    crash the turn; now it does what they meant: the full entry of each."""
    n = _node(tmp_path)
    hid = _propose(n)
    hlist = n.adapter.closure_tools["HypothesisList"]
    brief = hlist()
    assert hid in brief and "status_log" not in brief
    full = hlist(hypothesis_ids=[hid])
    assert '"status_log"' in full and hid in full
    assert hlist(hypothesis_ids=hid) == full           # a bare id works too
    assert "not found" in hlist(hypothesis_ids=["H99"])
