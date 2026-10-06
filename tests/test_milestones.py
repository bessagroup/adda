"""Milestone backlog: a one-time reminder on the first Delegate to any target."""
from __future__ import annotations

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra.delegation_log import DelegationLog
from adda._src.epistemics.milestones import (
    MilestoneLedger,
    open_milestones,
    render_backlog,
)
from adda._src.nodes import Node


class _Stub:
    def __init__(self):
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return ""


# --------------------------------------------------------------------------
# Ledger unit behaviour
# --------------------------------------------------------------------------

def test_seed_defaults_without_pipeline(tmp_path):
    led = MilestoneLedger(tmp_path)
    led.seed_defaults(include_pipeline=False, include_reproduction_gate=False)
    keys = {m["key"] for m in led.list_all()}
    assert keys == {"assess_literature_need"}
    assert all(m["status"] == "PENDING" for m in led.list_all())


def test_seed_defaults_with_pipeline_puts_it_first(tmp_path):
    led = MilestoneLedger(tmp_path)
    led.seed_defaults(include_pipeline=True, include_reproduction_gate=True)
    items = led.list_all()
    assert items[0]["key"] == "craft_pipeline"  # M001, read first
    assert {m["key"] for m in items} == {
        "craft_pipeline", "assess_literature_need", "oracle_gold_state"}


def test_seed_is_idempotent(tmp_path):
    led = MilestoneLedger(tmp_path)
    led.seed_defaults(include_pipeline=True, include_reproduction_gate=True)
    led.seed_defaults(include_pipeline=True, include_reproduction_gate=True)
    assert len(led.list_all()) == 3


def test_oracle_gold_state_seeds_only_with_reproduction_gate(tmp_path):
    """Its whole reason to exist is the reproduction gate's store-row
    precondition — decoupled from pipeline_deliverable, which decides only
    whether a notebook is required at all, not whether it must reproduce."""
    led = MilestoneLedger(tmp_path)
    led.seed_defaults(include_pipeline=True, include_reproduction_gate=False)
    keys = {m["key"] for m in led.list_all()}
    assert "oracle_gold_state" not in keys
    assert keys == {"craft_pipeline", "assess_literature_need"}


def test_assess_literature_is_manual_no_predicate(tmp_path):
    led = MilestoneLedger(tmp_path)
    led.seed_defaults(include_pipeline=True, include_reproduction_gate=True)
    assess = [m for m in led.list_all() if m["key"] == "assess_literature_need"][0]
    assert assess["manual"] is True
    craft = [m for m in led.list_all() if m["key"] == "craft_pipeline"][0]
    assert craft["manual"] is False  # auto via pipeline.py


def test_propose_complete_skip(tmp_path):
    led = MilestoneLedger(tmp_path)
    mid = led.propose("my own step")
    assert led.get(mid)["source"] == "agent"
    led.complete(mid, "did it")
    assert led.get(mid)["status"] == "DONE"
    mid2 = led.propose("optional")
    led.skip(mid2, "n/a")
    assert led.get(mid2)["status"] == "SKIPPED"


# --------------------------------------------------------------------------
# Auto-satisfy + implementer block (against a real node)
# --------------------------------------------------------------------------

def _node(tmp_path):
    class A(Agent):
        role = "strategizer"
        # Declaration-driven exposure: declare the capability tools this stub
        # exercises (hypothesis + milestone closures are no longer injected).
        tools = frozenset({"Done", "HypothesisPropose", "HypothesisUpdate", "HypothesisList", "MilestoneList", "MilestoneSet", "QueryStore"})
        description = "strategizer"

    class Lit(Agent):
        role = "literature_reviewer"
        description = "lit"

    class B(Agent):
        role = "implementer"
        description = "impl"

    spec = Graph(
        nodes={"strategizer": A(), "literature_reviewer": Lit(),
               "implementer": B()},
        edges=(Edge("strategizer", "literature_reviewer"),
               Edge("strategizer", "implementer")), entry="strategizer")
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    return Node(
        _Stub(), name="strategizer",
        outgoing=["literature_reviewer", "implementer"], spec=spec,
        worker_adapters={"literature_reviewer": _Stub(), "implementer": _Stub()},
        study_dir=tmp_path, notes_dir=notes,
        delegation_log=DelegationLog(tmp_path / "debug" / "dlog.jsonl"))


def test_open_milestones_lists_all_pending(tmp_path):
    n = _node(tmp_path)
    pend = open_milestones(n._milestones, n)
    assert {m["key"] for m in pend} == {
        "craft_pipeline", "assess_literature_need", "oracle_gold_state"}


def test_craft_pipeline_auto_satisfies_when_pipeline_exists(tmp_path):
    n = _node(tmp_path)
    (tmp_path / "pipeline.ipynb").write_text("# candidate\n")
    pend_keys = {m["key"] for m in open_milestones(n._milestones, n)}
    assert "craft_pipeline" not in pend_keys      # auto-satisfied
    assert "assess_literature_need" in pend_keys   # manual, still pending


def test_milestone_propose_tool_does_not_crash(tmp_path):
    """Regression (run 20260629T191754): the MilestonePropose tool WRAPPER
    crashed on every call — it forwarded (description, phase, gate) to
    MilestoneLedger.propose(), which takes only `description`, raising
    TypeError: propose() takes 2 positional arguments but 4 were given. The
    feature was dead. Exercise the wrapper end-to-end (not the ledger method)
    and assert it returns a milestone id, and that the proposed milestone is
    pending (so it genuinely joins the implementer-gating backlog)."""
    n = _node(tmp_path)
    mid = n.adapter.closure_tools["MilestoneSet"](description="verify both stage gates")
    assert mid.startswith("M"), mid
    pending_ids = {m["id"] for m in n._milestones.pending()}
    assert mid in pending_ids


def test_delegate_milestone_reminder_is_two_shot_for_any_target(tmp_path):
    """A node holding the milestone tools is reminded once, per namespace, on its
    first Delegate to ANY target (not a hard block); a re-delegate to the same
    target proceeds."""
    import re

    from .fixtures import approve_delegation

    n = _node(tmp_path)
    hid = n.adapter.closure_tools["HypothesisPropose"](
        "stmt", "crit", "pred", 0.5)
    for target in ("literature_reviewer", "implementer"):
        n._milestone_ack = set()
        before = len(n._registry)
        out = n.adapter.closure_tools["Delegate"](
            target, "work", "report", hypothesis_ids=[hid], wait=True)
        assert out.startswith("[CONFIRM]") and "backlog" in out
        assert len(n._registry) == before  # nothing fired yet
        out2 = n.adapter.closure_tools["Delegate"](
            target, "work", "report", hypothesis_ids=[hid], wait=True)
        assert not out2.startswith("[CONFIRM]")
        assert len(n._registry) > before  # it fired
        approve_delegation(n.adapter.closure_tools,
                           re.search(r"\[(D\d+)\]", out2).group(1))


def test_no_reminder_without_the_milestone_tools(tmp_path):
    n = _node(tmp_path)
    n._milestones = None
    hid = n.adapter.closure_tools["HypothesisPropose"](
        "stmt", "crit", "pred", 0.5)
    out = n.adapter.closure_tools["Delegate"](
        "literature_reviewer", "survey", "report", hypothesis_ids=[hid],
        wait=True)
    assert not out.startswith("[CONFIRM]")


def test_milestone_nudge_recurs_per_namespace(tmp_path):
    """The nudge fires once PER namespace — opening a new design re-prompts the
    setup milestones for that design rather than silently inheriting the ack.

    Runs under the default (peer_interaction on): each collected delegation
    is approved (SendMessage(..., approve=True), see approve_delegation) so
    it finalizes and does not block the next Delegate() as an open review
    (spec 12 item 1/6)."""
    import re

    from .fixtures import approve_delegation

    n = _node(tmp_path)
    hid = n.adapter.closure_tools["HypothesisPropose"](
        "stmt", "crit", "pred", 0.5)
    # First call: process-backlog nudge, no delegation created yet.
    n.adapter.closure_tools["Delegate"](
        "implementer", "run", "report", hypothesis_ids=[hid], wait=True)
    # Second call (same target): confirms it -- a real delegation opens for
    # review now, and must be approved so it doesn't block the next Delegate.
    out = n.adapter.closure_tools["Delegate"](
        "implementer", "run", "report", hypothesis_ids=[hid], wait=True)
    did = re.search(r"\[(D\d+)\]", out).group(1)
    approve_delegation(n.adapter.closure_tools, did)
    # A NEW namespace nudges again (its own setup) -- no delegation created.
    out_ns = n.adapter.closure_tools["Delegate"](
        "implementer", "run", "report", hypothesis_ids=[hid],
        namespace="elliptical_rings", wait=True)
    assert out_ns.startswith("[CONFIRM]")


def test_milestone_complete_requires_a_brief(tmp_path):
    n = _node(tmp_path)
    mid = n._milestones.propose("my step")
    assert "ERROR" in n.adapter.closure_tools["MilestoneSet"](mid, "DONE", note="")
    ok = n.adapter.closure_tools["MilestoneSet"](mid, "DONE", note="done via D2")
    assert "ERROR" not in ok and n._milestones.get(mid)["status"] == "DONE"


def test_done_blocked_until_backlog_resolved(tmp_path):
    n = _node(tmp_path)
    out = n.adapter.closure_tools["Done"](summary="done")
    assert "Cannot close yet" in out
    for m in n._milestones.list_all():
        n.adapter.closure_tools["MilestoneSet"](m["id"], "SKIPPED", note="n/a")
    out2 = n.adapter.closure_tools["Done"](summary="done")
    assert "Cannot close yet" not in out2


def test_render_backlog_announcement(tmp_path):
    n = _node(tmp_path)
    bl = render_backlog(n._milestones)
    assert "<process_backlog>" in bl
    assert "one-time reminder" in bl
    assert "MilestoneSet" in bl and "SKIPPED" in bl
    # the three milestones are listed
    for kw in ("Pipeline", "literature", "oracle"):
        assert kw.lower() in bl.lower()


def test_milestone_closures_registered(tmp_path):
    n = _node(tmp_path)
    for tool in ("MilestoneList", "MilestoneSet"):
        assert tool in n.adapter.closure_tools


def test_milestone_set_adds_completes_and_skips(tmp_path):
    """Three outcomes of one write: add (no id), close DONE, close SKIPPED —
    and a close with no note is refused, so closing stays auditable."""
    n = _node(tmp_path)
    ms = n.adapter.closure_tools["MilestoneSet"]
    mid = ms(description="validate the oracle on one sample")
    assert mid.startswith("M")
    assert ms(mid, "DONE").startswith("ERROR")          # no note
    assert "DONE" in ms(mid, "DONE", note="D002 validated it")
    other = ms(description="read the prior art")
    assert "SKIPPED" in ms(other, "skipped", note="no prior art exists")
    assert ms(mid, "OPEN", note="x").startswith("ERROR")  # not a close
    assert ms(description="x", status="DONE").startswith("ERROR")
