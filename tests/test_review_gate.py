"""Report review gate (spec 12 item 3, peer_interaction feature): a
worker's non-error report moves to OPEN-FOR-REVIEW instead of finalizing,
automatically, whenever the feature is on -- no opt-in, no implicit close.
Only ``SendMessage(id, ..., approve=True)`` runs ``_finish_ok``. Also
covers the two ratified edges: an ERRORED delegation never counts as an
open review (edge 1), and a run closing with a review still open is
recorded honestly, never as approved (edge 2, tested separately in
tests/test_agent_runtime_extra.py against the run-ending sweep).
"""
from __future__ import annotations

import pytest

from adda._src.backends.base import Agent, Edge, Graph, set_delegation_id
from adda._src.nodes import Node
from adda._src.nodes.tools.routing.delegation import DelegationTools
from adda._src.runtime import settings


@pytest.fixture(autouse=True)
def _peer_interaction_on():
    settings.configure({"peer_interaction": True})
    yield
    settings.configure(None)
    set_delegation_id(None)


class _FakeWorker:
    """Just enough of an adapter for a real Delegate()/WorkerSession round
    trip: closure_tools for install_worker_tools, .copy() (ClaudeAdapter's
    "share one instance" contract), .invoke() returning canned report
    text, and the two attributes _open_for_review reads."""

    def __init__(self, text="A fine report.\n\n" * 5, session_id="sess-1",
                 raises=False):
        self.closure_tools: dict = {}
        self._text = text
        self._raises = raises
        self.last_usage: dict = {}
        self.last_session_id = session_id

    def copy(self):
        return self

    def invoke(self, messages):
        if self._raises:
            raise RuntimeError("worker exploded")
        return self._text


def _spec() -> Graph:
    class Strategizer(Agent):
        role = "strategizer"
        description = "entry"

    class Implementer(Agent):
        role = "implementer"
        description = "worker"

    return Graph(
        nodes={"strategizer": Strategizer(), "implementer": Implementer()},
        edges=(Edge("strategizer", "implementer"),),
        entry="strategizer",
    )


class _StubAdapter:
    def __init__(self) -> None:
        self.closure_tools: dict = {}


def _make_node(worker=None, delegation_log=None) -> Node:
    return Node(
        _StubAdapter(), name="strategizer", outgoing=["implementer"],
        spec=_spec(),
        worker_adapters={"implementer": worker or _FakeWorker()},
        delegation_log=delegation_log,
    )


def test_a_successful_delegation_opens_for_review_not_done():
    """The whole trigger: with the feature on, a non-error report moves
    to OPEN-FOR-REVIEW automatically -- no opt-in flag, nothing the
    delegator had to ask for."""
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)

    out = dt.Delegate("implementer", "do the thing", "a report", wait=True)

    assert "OPEN FOR REVIEW" in out
    assert "A fine report." in out
    did = next(iter(node._registry))
    assert node._registry[did]["status"] == "OpenForReview"
    assert node._registry[did]["session_id"] == "sess-1"
    assert did in node._worker_sessions  # kept live for later approval


def test_delegate_refused_while_a_review_is_open():
    """spec 12 item 6 / design item 1: no NEW Delegate while ANY report
    is open for review, named in the refusal."""
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "first task", "a report", wait=True)
    did = next(iter(node._registry))

    out = dt.Delegate("implementer", "second task", "a report")

    assert out.startswith("ERROR:")
    assert did in out
    assert len(node._registry) == 1  # the refused call never dispatched


def test_open_for_review_writes_a_durable_log_row_immediately(tmp_path):
    """Regression for adda-boss-whopper's review of f39eb4c: the earlier
    version of this mechanism only recorded an open review at CLOSE time
    (a sweep), which a crash or a watchdog kill never reaches -- the
    review would vanish, silently, with the delegation log's last row
    still falsely reading RUNNING. The fix is to persist at TRANSITION
    time: this row must exist on disk the instant _open_for_review runs,
    with no simulated crash or close needed to produce it -- a real
    process death after this point already leaves an honest trace."""
    import json as _json

    from adda._src.infra.delegation_log import DelegationLog

    log = DelegationLog(tmp_path / "delegation_log.jsonl")
    node = _make_node(delegation_log=log)
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "do the thing", "a report", wait=True)

    lines = [
        _json.loads(ln) for ln in
        (tmp_path / "delegation_log.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    statuses = [rec["status"] for rec in lines]
    assert "OPEN_FOR_REVIEW" in statuses
    open_row = next(r for r in lines if r["status"] == "OPEN_FOR_REVIEW")
    assert "A fine report." in open_row["deliverable"]
    # No DONE row exists yet -- nobody approved it.
    assert "DONE" not in statuses


def test_open_for_review_row_is_the_honest_last_word_if_the_process_dies(
    tmp_path,
):
    """The crash/watchdog-kill case, made concrete: if nothing EVER
    approves this delegation (the process simply stops existing right
    after _open_for_review), the delegation log's own last-wins collapse
    means OPEN_FOR_REVIEW is what a reader sees -- never RUNNING (a lie:
    it did finish) and never DONE (a lie: nobody approved it)."""
    import json as _json

    from adda._src.infra.delegation_log import DelegationLog

    log = DelegationLog(tmp_path / "delegation_log.jsonl")
    node = _make_node(delegation_log=log)
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "do the thing", "a report", wait=True)
    # Simulate the process ending here -- nothing else ever writes to
    # this log again (no sweep, no approval, no crash handler).

    last_by_id: dict[str, dict] = {}
    for ln in (tmp_path / "delegation_log.jsonl").read_text().splitlines():
        if ln.strip():
            rec = _json.loads(ln)
            last_by_id[rec["id"]] = rec  # last-wins, same as _load_all
    assert len(last_by_id) == 1
    (only_record,) = last_by_id.values()
    assert only_record["status"] == "OPEN_FOR_REVIEW"


def test_delegate_blocked_by_review_not_by_an_unrelated_errored_sibling():
    """Edge 1, made discriminating (adda-boss-whopper's review of
    f39eb4c: the original version of this test was trivially true even
    on code with NO blocking logic at all, so it guarded nothing). The
    registry holds ONE Errored entry and ONE OpenForReview entry at the
    same time: Delegate is refused because of the review, never the
    error; approving the review unblocks Delegate even though the
    Errored entry is still sitting right there, untouched, in the
    registry. A buggy implementation that counted ERRORED as "open"
    would still refuse Delegate after approval -- the final assertion
    catches that."""
    worker = _FakeWorker(raises=True)
    node = _make_node(worker=worker)
    dt = DelegationTools(node)
    set_delegation_id(None)

    out1 = dt.Delegate("implementer", "boom", "a report", wait=True)
    assert out1.startswith("Errored:")
    errored_id = next(iter(node._registry))
    assert node._registry[errored_id]["status"] == "Errored"

    worker._raises = False
    out2 = dt.Delegate("implementer", "do the thing", "a report", wait=True)
    assert "OPEN FOR REVIEW" in out2
    open_id = next(d for d in node._registry if d != errored_id)
    assert node._registry[open_id]["status"] == "OpenForReview"

    # Refused now -- naming the OPEN review, not the unrelated error.
    out3 = dt.Delegate("implementer", "third task", "a report")
    assert out3.startswith("ERROR:")
    assert open_id in out3
    assert errored_id not in out3

    # Approving the open one unblocks Delegate -- even though the
    # Errored entry is untouched and still in the registry.
    dt.SendMessage(open_id, "looks good", approve=True)
    assert node._registry[errored_id]["status"] == "Errored"
    out4 = dt.Delegate("implementer", "fourth task", "a report", wait=True)
    assert not out4.startswith("ERROR:")


def test_approve_finalizes_the_open_review():
    """SendMessage(id, ..., approve=True) is the ONLY thing that runs
    _finish_ok -- after it, the delegation is Done, the worker session
    is no longer held, and a new Delegate is unblocked."""
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "do the thing", "a report", wait=True)
    did = next(iter(node._registry))
    assert node._registry[did]["status"] == "OpenForReview"

    out = dt.SendMessage(did, "looks good", approve=True)

    assert "Approved" in out
    assert did in out
    assert node._registry[did]["status"] == "Done"
    assert did not in node._worker_sessions

    # The block is lifted -- a new Delegate now succeeds.
    out2 = dt.Delegate("implementer", "next task", "a report", wait=True)
    assert not out2.startswith("ERROR:")


def test_a_non_approve_message_is_recorded_but_not_yet_delivered():
    """Anything other than approve=True on an open review does not
    finalize it -- and is honest that session-resumption itself (the
    part that would actually deliver this to the worker) isn't built
    yet, rather than silently pretending to deliver it."""
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "do the thing", "a report", wait=True)
    did = next(iter(node._registry))

    out = dt.SendMessage(did, "what does this number mean?")

    assert node._registry[did]["status"] == "OpenForReview"  # unchanged
    assert "not built yet" in out
    assert did in node._worker_sessions  # still held for a later approval


def test_bare_wait_names_an_open_review_instead_of_nothing_to_wait_for():
    """A delegator with ONLY an open review (nothing still Working) must
    be told to resolve it -- not "nothing to wait for", which would read
    as if there were no outstanding obligation at all."""
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "do the thing", "a report", wait=True)
    did = next(iter(node._registry))

    out = dt.Wait()

    assert "ERROR: waiting cannot make progress" in out
    assert "open for review" in out
    assert did in out


def test_wait_by_id_still_returns_the_open_reports_text():
    """Wait(id) is the "read" event design item 3 refers to -- it must
    keep returning an open review's report text, not treat it as if
    nothing had happened yet."""
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "do the thing", "a report", wait=True)
    did = next(iter(node._registry))

    out = dt.Wait(did)

    assert "OpenForReview" in out
    assert "A fine report." in out


def test_wait_block_false_reports_an_open_review_not_errored():
    """Regression, same bug class as Delegate(wait=True)'s earlier
    misreport: Wait(id, block=False) must not report a successful,
    unapproved report as "Errored:" -- OpenForReview is neither Working
    nor an error."""
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)

    dt.Delegate("implementer", "do the thing", "a report", wait=True)
    did = next(iter(node._registry))

    out = dt.Wait(did, block=False)

    assert not out.startswith("Errored:")
    assert "OPEN FOR REVIEW" in out
    assert "A fine report." in out


# ---------------------------------------------------------------------------
# The pending-for-you notice (spec 12 design item 10(a)) -- lives in
# Node._wrap_closure, so these tests go through the REAL wrapped closures
# (build_routing_tools), not DelegationTools directly.
# ---------------------------------------------------------------------------


def test_pending_notice_names_an_open_review_on_any_tool_result():
    """An open review is named on the VERY NEXT tool result, whatever
    tool that happens to be -- not only Wait()'s own bucket message."""
    from adda._src.nodes.tools.routing import build_routing_tools

    node = _make_node()
    tools = build_routing_tools(node)
    set_delegation_id(None)

    tools["Delegate"]("implementer", "do the thing", "a report", wait=True)
    did = next(iter(node._registry))

    # A DIFFERENT tool call -- block=False status poll, not the
    # review-collection path itself.
    out = tools["Wait"](did, block=False)

    assert "pending items" in out
    assert "open for review" in out
    assert did in out


def test_no_pending_notice_when_nothing_is_owed():
    """The converse of the above: nothing pending stays silent -- no
    manufactured noise on the common case."""
    from adda._src.nodes.tools.routing import build_routing_tools

    node = _make_node()
    tools = build_routing_tools(node)
    set_delegation_id(None)

    out = tools["Wait"]("nonexistent", block=False)

    assert "pending items" not in out


def test_pending_notice_clears_once_the_review_is_approved():
    """Once the last open review is approved, the notice must stop
    appearing -- it is computed fresh every call, not a one-shot drain
    that could go stale."""
    from adda._src.nodes.tools.routing import build_routing_tools

    node = _make_node()
    tools = build_routing_tools(node)
    set_delegation_id(None)

    tools["Delegate"]("implementer", "do the thing", "a report", wait=True)
    did = next(iter(node._registry))
    tools["SendMessage"](did, "looks good", approve=True)
    tools["Wait"](did)  # actually collect it (block=False only peeks)

    out = tools["Wait"]("nonexistent", block=False)

    assert "pending items" not in out


def test_pending_notice_shows_a_childs_unread_question_to_the_delegator():
    """adda-boss-whopper's review of e03f745: the case Elvis named
    first, "respond [to a] delegation" -- a worker's SendMessage
    question, unread by the delegator, must appear on the delegator's
    NEXT tool result, not only get waited out via Wait(). FollowUp's own
    bucket alone can't cover this: FollowUp is being retired."""
    from adda._src.nodes.tools.routing import build_routing_tools
    from adda._src.nodes.tools.routing.delegation import DelegationTools

    node = _make_node()
    dt = DelegationTools(node)
    tools = build_routing_tools(node)

    set_delegation_id(None)
    dt._register_dispatch(
        "D001", "implementer", [], False, None, None,
        "2026-01-01T00:00:00+00:00")

    set_delegation_id("D001")
    dt.SendMessage("strategizer", "what does this bound mean?")
    set_delegation_id(None)

    out = tools["Wait"]("nonexistent", block=False)

    assert "pending items" in out
    assert "D001 (implementer) asked you" in out
    assert "what does this bound mean?" in out


def test_pending_notice_shows_a_delegators_unread_message_to_the_worker():
    """The mirror case: a delegator's SendMessage to a worker, unread by
    that worker, must appear on the WORKER's own next tool result."""
    from adda._src.nodes.tools.routing import build_routing_tools
    from adda._src.nodes.tools.routing.delegation import DelegationTools

    node = _make_node()
    dt = DelegationTools(node)
    tools = build_routing_tools(node)

    set_delegation_id(None)
    dt._register_dispatch(
        "D001", "implementer", [], False, None, None,
        "2026-01-01T00:00:00+00:00")
    dt.SendMessage("D001", "clarify the constraint before you proceed")

    set_delegation_id("D001")
    out = tools["Wait"]("nonexistent", block=False)
    set_delegation_id(None)

    assert "pending items" in out
    assert "your delegator sent you a message" in out
    assert "clarify the constraint" in out


def test_pending_notice_does_not_leak_a_siblings_or_childs_obligations():
    """Scoped strictly to the CALLING identity's own delegations -- a
    sibling delegation's open review (parent == a different identity)
    must never appear in this identity's notice."""
    from adda._src.nodes.tools.routing import build_routing_tools
    from adda._src.nodes.tools.routing.delegation import DelegationTools

    node = _make_node()
    tools = build_routing_tools(node)
    dt = DelegationTools(node)

    # A review opened directly under a DIFFERENT delegator identity
    # ("D999"), simulating a sibling's own sub-delegation.
    set_delegation_id("D999")
    dt.Delegate("implementer", "someone else's task", "a report", wait=True)
    set_delegation_id(None)

    out = tools["Wait"]("nonexistent", block=False)

    assert "pending items" not in out
