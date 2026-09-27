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


def _make_node(worker=None) -> Node:
    return Node(
        _StubAdapter(), name="strategizer", outgoing=["implementer"],
        spec=_spec(),
        worker_adapters={"implementer": worker or _FakeWorker()},
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


def test_errored_delegation_does_not_block_a_new_delegate():
    """Edge 1: a worker that raises has no report to review -- it is
    terminal immediately and must NOT count toward the open-review
    block, even with peer_interaction on."""
    node = _make_node(worker=_FakeWorker(raises=True))
    dt = DelegationTools(node)
    set_delegation_id(None)

    out1 = dt.Delegate("implementer", "boom", "a report", wait=True)
    assert out1.startswith("Errored:")
    did = next(iter(node._registry))
    assert node._registry[did]["status"] == "Errored"

    # A second Delegate must be refused for cutoff/target reasons only,
    # never for "you have an open review" -- there is none.
    out2 = dt.Delegate("implementer", "try again", "a report", wait=True)
    assert "open for review" not in out2
    assert len(node._registry) == 2


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
