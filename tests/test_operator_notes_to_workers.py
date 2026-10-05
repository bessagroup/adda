"""Operator notes addressed at a running delegation, and what the
``peer_interaction`` knob grants.

The human's nudge rides the same per-delegation queue as the budget warnings.
The orchestrator's note drain is the ONLY place that may claim the queue (it
is destructive), so routing has to happen there or an addressed note is
swallowed by the orchestrator's own delivery.
"""
from __future__ import annotations

import pytest

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.nodes import Node
from adda._src.runtime import settings


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    settings.configure(None)


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


def _node(tmp_path=None):
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done"})
        description = "strategizer"

    class B(Agent):
        role = "implementer"
        description = "implementer"

    spec = Graph(
        nodes={"strategizer": A(), "implementer": B()},
        edges=(Edge("strategizer", "implementer"),),
        entry="strategizer",
    )
    kwargs = {}
    if tmp_path is not None:
        notes = tmp_path / "debug" / "strategizer_notes"
        notes.mkdir(parents=True)
        from adda._src.infra.delegation_log import DelegationLog
        dlog = DelegationLog(tmp_path / "debug" / "delegation_log.jsonl")
        kwargs = {"notes_dir": notes, "delegation_log": dlog}
    return Node(
        _Stub(), name="strategizer", outgoing=["implementer"], spec=spec,
        worker_adapters={"implementer": _Stub()},
        **kwargs,
    )


def _node_with_run(tmp_path):
    """A node wired to a run dir on disk.

    _current_notes_dir is assigned per-invocation from graph state, not by
    the constructor, so a unit test has to set it — it is what
    _current_run_dir (=notes.parent.parent) resolves the operator channel
    against.
    """
    n = _node(tmp_path)
    n._current_notes_dir = tmp_path / "debug" / "strategizer_notes"
    return n


def test_a_note_aimed_at_a_running_delegation_reaches_that_worker(tmp_path):
    from adda._src.infra import operator_channel as oc

    n = _node_with_run(tmp_path)
    n._registry["D004"] = {
        "status": "Working", "result": None, "target": "implementer"}
    oc.queue_note(tmp_path, "use the coarse mesh", to_node="D004")

    text = n._drain_notifications()

    with n._pending_worker_msgs_lock:
        queued = n._pending_worker_msgs.get("D004", [])
    assert any("use the coarse mesh" in m for m in queued)
    assert any("OPERATOR NOTE" in m for m in queued)
    # ...and it did NOT also land in the orchestrator's own text.
    assert "use the coarse mesh" not in text


def test_an_unaddressed_note_still_goes_to_the_orchestrator(tmp_path):
    from adda._src.infra import operator_channel as oc

    n = _node_with_run(tmp_path)
    oc.queue_note(tmp_path, "reconsider the floor")

    text = n._drain_notifications()
    assert "reconsider the floor" in text
    assert "OPERATOR NOTE" in text


def test_a_note_for_a_finished_delegation_is_not_dropped(tmp_path):
    """The human still said it. Silently discarding it would be the worst
    outcome — worse than delivering it late to the wrong reader — so it goes
    to the orchestrator with the intended recipient named."""
    from adda._src.infra import operator_channel as oc

    n = _node_with_run(tmp_path)
    n._registry["D004"] = {
        "status": "Done", "result": "r", "target": "implementer"}
    oc.queue_note(tmp_path, "too late now", to_node="D004")

    text = n._drain_notifications()
    assert "too late now" in text
    assert "D004" in text
    assert "not" in text.lower()
    with n._pending_worker_msgs_lock:
        assert not n._pending_worker_msgs.get("D004")


def test_peer_interaction_on_grants_sendmessage_and_no_legacy_surface():
    settings.configure({"peer_interaction": True})
    tools = _node().adapter.closure_tools
    assert "SendMessage" in tools
    for retired in ("Confer", "Reply", "FollowUp"):
        assert retired not in tools


def test_peer_interaction_off_is_no_peer_messaging_with_a_human_channel():
    """Off withholds SendMessage; the entry node keeps FollowUp (its only way
    to the human) and nothing peer-facing exists."""
    settings.configure({"peer_interaction": False})
    tools = _node().adapter.closure_tools
    assert "SendMessage" not in tools
    assert "FollowUp" in tools
    for retired in ("Confer", "Reply"):
        assert retired not in tools
