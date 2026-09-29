"""A bare Wait() collects whichever delegation finishes first.

Fan-out was already cheap to DISPATCH — Delegate(wait=False) is used on 85% of
real Delegate calls — but expensive to COLLECT: Wait() bound to a single
delegation id, so the only way to harvest several in flight was GetStatus
polling, which the poll-count nudges actively discourage. Measured consequence
across 39 cluster runs: reliance on Wait predicted SERIAL execution
(wait-share-of-harvest vs mean concurrent delegations r=-0.54 controlling for
delegation duration), and mean concurrency sat at 1.21 against a median 15
delegations created per run.

These cover the bare form: it harvests each finished delegation exactly once,
blocks only while nothing is ready, and refuses rather than hanging when
waiting cannot make progress.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

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


def _node(run_dir: Path) -> Node:
    class S(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "Wait"})
        description = "s"

    class W(Agent):
        description = "w"

    spec = Graph(
        nodes={"strategizer": S(), "worker": W()},
        edges=(Edge("strategizer", "worker"),),
        entry="strategizer",
    )
    dlog = DelegationLog(run_dir / "debug" / "delegation_log.jsonl")
    notes_dir = run_dir / "debug" / "strategizer_notes"
    notes_dir.mkdir(parents=True)
    return Node(
        _Stub(), name="strategizer", outgoing=["worker"], spec=spec,
        worker_adapters={"worker": _Stub()}, notes_dir=notes_dir,
        delegation_log=dlog,
    )


def _wait(tmp_path, registry):
    node = _node(tmp_path / "runs" / "T1")
    with node._registry_lock:
        # Real entries always carry their delegator ("entry" = the strategizer).
        node._registry.update(
            {k: {"parent": "entry", **v} for k, v in registry.items()})
    return node, node._build_routing_closures()["Wait"]


def test_bare_wait_returns_a_finished_delegation_labelled_with_its_id(tmp_path):
    _, Wait = _wait(tmp_path, {
        "D001": {"status": "Working"},
        "D002": {"status": "Done", "result": "the report"},
    })
    out = Wait()
    # The agent did not name a target, so it must be told which one this is.
    assert "D002" in out, out
    assert "the report" in out, out


def test_bare_wait_harvests_each_delegation_exactly_once(tmp_path):
    """N in flight are drained by N bare Wait() calls — the whole point."""
    _, Wait = _wait(tmp_path, {
        "D001": {"status": "Done", "result": "first"},
        "D002": {"status": "Errored", "result": "second"},
    })
    first, second = Wait(), Wait()
    assert {"first" in first, "second" in first} == {True, False}
    assert {"first" in second, "second" in second} == {True, False}
    # Not the same report twice.
    assert ("first" in first) != ("first" in second), (first, second)
    # Both drained: a third call has nothing left and must refuse.
    assert "ERROR" in Wait()


def test_bare_wait_blocks_until_a_worker_finishes(tmp_path):
    node, Wait = _wait(tmp_path, {"D001": {"status": "Working"}})

    def finish():
        time.sleep(1.5)  # longer than one poll tick
        with node._registry_lock:
            node._registry["D001"].update(
                {"status": "Done", "result": "late report"})

    t = threading.Thread(target=finish, daemon=True)
    t.start()
    out = Wait()
    t.join(timeout=5)
    assert "late report" in out, out
    assert "D001" in out, out


def test_bare_wait_refuses_when_nothing_is_in_flight(tmp_path):
    _, Wait = _wait(tmp_path, {})
    out = Wait()
    assert "ERROR" in out and "nothing to wait for" in out, out


def test_bare_wait_refuses_instead_of_hanging_on_followup_only(tmp_path):
    """Every open worker parked on a FollowUp: waiting cannot make progress,
    so refuse and name the unblock path rather than blocking forever."""
    _, Wait = _wait(tmp_path, {"D001": {"status": "FollowUp"}})
    started = time.monotonic()
    out = Wait()
    assert time.monotonic() - started < 5, "bare Wait() hung on a FollowUp"
    assert "ERROR" in out and "FollowUp" in out, out
    assert "Reply(" in out, out
    assert "D001" in out, out


class _DeadThread:
    """A worker thread that has exited without recording a terminal status."""

    def is_alive(self) -> bool:
        return False


def test_bare_wait_refuses_instead_of_hanging_on_a_crashed_worker(tmp_path):
    """A blocking tool call ends no turn, so the run's time backstop cannot
    fire while we are inside Wait(). Waiting on a delegation whose thread is
    already gone would therefore hang the whole run, not just the call."""
    node, Wait = _wait(tmp_path, {"D001": {"status": "Working"}})
    node._threads["D001"] = _DeadThread()
    started = time.monotonic()
    out = Wait()
    assert time.monotonic() - started < 5, "bare Wait() hung on a dead worker"
    assert "ERROR" in out and "D001" in out, out
    assert "never reported" in out, out


def test_bare_wait_keeps_waiting_while_any_worker_is_still_live(tmp_path):
    """One crashed worker must not cancel the wait for a healthy sibling."""
    node, Wait = _wait(tmp_path, {
        "D001": {"status": "Working"},   # crashed
        "D002": {"status": "Working"},   # healthy, no thread registered
    })
    node._threads["D001"] = _DeadThread()

    def finish():
        time.sleep(1.5)
        with node._registry_lock:
            node._registry["D002"].update(
                {"status": "Done", "result": "sibling report"})

    t = threading.Thread(target=finish, daemon=True)
    t.start()
    out = Wait()
    t.join(timeout=5)
    assert "sibling report" in out, out


def test_bare_wait_never_harvests_a_cancelled_delegation(tmp_path):
    """Cancelled is terminal but its result is explicitly excluded from the
    run, so it is not a harvestable completion."""
    _, Wait = _wait(tmp_path, {
        "D001": {"status": "Cancelled", "result": "discarded"},
    })
    out = Wait()
    assert "ERROR" in out, out
    assert "discarded" not in out, out


def test_named_wait_still_returns_its_report_unlabelled(tmp_path):
    """The single-id form is unchanged: the agent named the target, so the
    report needs no ID prefix."""
    _, Wait = _wait(tmp_path, {"D001": {"status": "Done", "result": "r1"}})
    out = Wait("D001")
    assert "r1" in out, out
    assert not out.startswith("[D001]"), out


def test_named_wait_marks_the_report_read_for_a_later_bare_wait(tmp_path):
    """Otherwise a bare Wait() would hand back a report already collected."""
    _, Wait = _wait(tmp_path, {"D001": {"status": "Done", "result": "r1"}})
    assert "r1" in Wait("D001")
    assert "ERROR" in Wait()


def test_named_wait_still_reports_an_unknown_delegation(tmp_path):
    _, Wait = _wait(tmp_path, {})
    assert "unknown delegation" in Wait("D404")


# ── a blocked Wait must not leave the agent unaware that someone needs it ──

def _fast_poll(monkeypatch):
    from adda._src.nodes.tools.routing import delegation as d
    monkeypatch.setattr(d, "_NOTICE_POLL_TICKS", 1)
    monkeypatch.setattr(d, "_NOTICE_POLL_S", 0.2)


def test_bare_wait_wakes_on_an_operator_note(tmp_path, monkeypatch):
    from adda._src.infra import operator_channel as oc
    _fast_poll(monkeypatch)
    run_dir = tmp_path / "runs" / "T1"
    node, Wait = _wait(tmp_path, {"D001": {"status": "Working"}})
    oc.queue_note(run_dir, "use the coarse mesh")
    started = time.monotonic()
    out = Wait()
    assert time.monotonic() - started < 5, "Wait ignored the operator note"
    assert "use the coarse mesh" in out, out
    assert "OPERATOR NOTE" in out, out
    assert "still in flight: D001" in out, out
    assert node._registry["D001"]["status"] == "Working"  # nothing harvested


def test_named_wait_wakes_on_an_operator_note(tmp_path, monkeypatch):
    from adda._src.infra import operator_channel as oc
    _fast_poll(monkeypatch)
    node, Wait = _wait(tmp_path, {"D001": {"status": "Working"}})
    gate = threading.Event()
    t = threading.Thread(target=gate.wait, args=(10,), daemon=True)
    t.start()
    node._threads["D001"] = t
    oc.queue_note(tmp_path / "runs" / "T1", "reconsider the floor")
    try:
        started = time.monotonic()
        out = Wait("D001")
        assert time.monotonic() - started < 5
        assert "reconsider the floor" in out, out
        assert "D001 is still" in out, out
    finally:
        gate.set()


def test_bare_wait_wakes_on_a_science_monitor_message(tmp_path, monkeypatch):
    _fast_poll(monkeypatch)
    node, Wait = _wait(tmp_path, {"D001": {"status": "Working"}})

    class _Monitor:
        def __init__(self):
            self.sent = False

        def escalation_due(self):
            return []

        def drain(self):
            # Nothing at Wait's entry; the nudge lands mid-wait.
            first, self.sent = not self.sent, True
            return "" if first else "DUPLICATE_EVALUATION: you re-ran x=(1,1)"

    node._science_monitor = _Monitor()
    started = time.monotonic()
    out = Wait()
    assert time.monotonic() - started < 5
    assert "DUPLICATE_EVALUATION" in out, out
    assert "still in flight: D001" in out, out


def test_a_routine_notification_does_not_wake_the_wait():
    """A report-ready style notice is drained but does not end the Wait."""
    from adda._src.nodes.tools.routing.delegation import DelegationTools

    class _N:
        _notifications = ["[Delegation D001 report ready for review]"]
        _notifications_lock = threading.Lock()
        _science_monitor = None

        def _drain_operator_notes(self):
            return ""

    text, wake = DelegationTools._drain_while_waiting(
        type("T", (), {"node": _N()})())
    assert "report ready" in text
    assert wake is False


def test_note_arriving_mid_wait_wakes_a_named_wait(tmp_path, monkeypatch):
    from adda._src.infra import operator_channel as oc
    _fast_poll(monkeypatch)
    node, Wait = _wait(tmp_path, {"D001": {"status": "Working"}})
    gate = threading.Event()
    t = threading.Thread(target=gate.wait, args=(10,), daemon=True)
    t.start()
    node._threads["D001"] = t
    threading.Timer(0.5, oc.queue_note, (tmp_path / "runs" / "T1",
                                         "mid-wait correction")).start()
    try:
        started = time.monotonic()
        out = Wait("D001")
        assert time.monotonic() - started < 5
        assert "mid-wait correction" in out and "still Working" in out, out
    finally:
        gate.set()
