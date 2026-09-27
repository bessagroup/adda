"""SendMessage (spec 12, peer_interaction feature) — real-thread concurrency
tests, not mocks of the waits.

Deliberately uses actual `threading.Thread`s and the real per-delegation
Condition/queue mechanism (`DelegationTools.SendMessage`, `_wait_for_any`),
not a simulation of blocking — the whole point of this mechanism is
concurrency correctness (no lost wakeups, no cross-talk between sibling
delegations, no deadlock when two peers block on each other at once), which
mocked waits cannot exercise.
"""
from __future__ import annotations

import threading
import time

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


class _StubAdapter:
    def __init__(self) -> None:
        self.closure_tools: dict = {}


def _spec() -> Graph:
    class Strategizer(Agent):
        role = "strategizer"
        description = "entry"

    class Implementer(Agent):
        role = "implementer"
        description = "worker"

    class MathExpert(Agent):
        role = "math_expert"
        description = "worker"

    return Graph(
        nodes={"strategizer": Strategizer(), "implementer": Implementer(),
               "math_expert": MathExpert()},
        edges=(Edge("strategizer", "implementer"),
               Edge("implementer", "math_expert")),
        entry="strategizer",
    )


def _make_node(name="strategizer", outgoing=("implementer",)) -> Node:
    return Node(_StubAdapter(), name=name, outgoing=list(outgoing), spec=_spec())


def _register(node, delegation_id, target, *, as_delegation_id=None):
    """Create a live registry entry, stamping `parent` from the CALLING
    thread's own thread-local identity (as production code does) —
    `as_delegation_id=None` means "as the entry node"."""
    set_delegation_id(as_delegation_id)
    dt = DelegationTools(node)
    dt._register_dispatch(
        delegation_id, target, [], False, None, None, "2026-01-01T00:00:00+00:00")
    set_delegation_id(None)


def test_two_worker_fan_out_questions_are_correctly_attributed():
    """Two 'implementer' delegations (D001, D002) both ask a question at
    once: the delegator's Wait() sees each, and each answer reaches only
    the worker that asked it — none lost, none cross-delivered."""
    node = _make_node()
    _register(node, "D001", "implementer")
    _register(node, "D002", "implementer")
    dt = DelegationTools(node)

    results: dict[str, str] = {}

    def _worker(did, question):
        set_delegation_id(did)
        results[did] = dt.SendMessage(
            "strategizer", question, wait_for_reply=True)

    t1 = threading.Thread(
        target=_worker, args=("D001", "Q from D001"), daemon=True)
    t2 = threading.Thread(
        target=_worker, args=("D002", "Q from D002"), daemon=True)
    t1.start(); t2.start()

    set_delegation_id(None)  # this thread is "entry"
    seen = {}
    for _ in range(2):
        out = dt.Wait()
        assert "message from" in out
        did = "D001" if "D001" in out else "D002"
        seen[did] = out
        answer = f"answer for {did}"
        dt.SendMessage(did, answer, approve=False)

    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()
    assert "D001" in seen and "D002" in seen
    assert results["D001"] == "message from strategizer: answer for D001"
    assert results["D002"] == "message from strategizer: answer for D002"


def test_deadlock_guard_both_sides_return_with_the_others_message():
    """Delegator and worker SendMessage(wait_for_reply=True) each other at
    the same moment — neither hangs; each returns with what the OTHER
    sent, not a timeout."""
    node = _make_node()
    _register(node, "D001", "implementer")
    dt = DelegationTools(node)

    delegator_result = {}
    worker_result = {}

    def _delegator():
        set_delegation_id(None)
        delegator_result["out"] = dt.SendMessage(
            "D001", "question from entry", wait_for_reply=True)

    def _worker():
        set_delegation_id("D001")
        worker_result["out"] = dt.SendMessage(
            "strategizer", "question from worker", wait_for_reply=True)

    t1 = threading.Thread(target=_delegator, daemon=True)
    t2 = threading.Thread(target=_worker, daemon=True)
    t1.start()
    time.sleep(0.05)  # let t1 enqueue+start waiting first, not required for
                      # correctness (the guard must work either order) but
                      # makes the interleaving deterministic for this test
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert "question from worker" in delegator_result["out"]
    assert "question from entry" in worker_result["out"]


def test_delegator_blocked_in_wait_wakes_on_a_worker_question_mid_fan_out():
    """A delegator blocked in Wait() (collecting a fan-out) wakes on a
    worker's question almost immediately — it does not wait out the poll
    tick, and it does not wait for the OTHER still-running delegation."""
    node = _make_node()
    _register(node, "D001", "implementer")
    _register(node, "D002", "implementer")
    dt = DelegationTools(node)

    wait_result = {}

    def _wait():
        set_delegation_id(None)
        wait_result["out"] = dt.Wait()
        wait_result["at"] = time.monotonic()

    start = time.monotonic()
    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    time.sleep(0.2)  # Wait() is now blocked (well past its first tick)

    def _ask():
        set_delegation_id("D002")
        dt.SendMessage("strategizer", "D002 needs input", wait_for_reply=False)

    threading.Thread(target=_ask, daemon=True).start()
    t.join(timeout=5)

    assert not t.is_alive()
    elapsed = wait_result["at"] - start
    assert elapsed < 0.8, f"Wait() took {elapsed:.2f}s — did not wake early"
    assert "D002 needs input" in wait_result["out"]
    # D001 is still "Working" — Wait() did not wait for it.
    assert node._registry["D001"]["status"] == "Working"


def test_nested_worker_delegator_only_sees_its_own_childrens_messages():
    """implementer/D001 delegates to math_expert/D010; implementer/D002
    (a SIBLING delegation of the same role, sharing this Node object)
    delegates to math_expert/D020. D001's own Wait() must see D010's
    message and must NOT see D020's, which belongs to D002."""
    node = _make_node()
    _register(node, "D001", "implementer")  # parent="entry"
    _register(node, "D002", "implementer")  # parent="entry"
    _register(node, "D010", "math_expert", as_delegation_id="D001")  # parent=D001
    _register(node, "D020", "math_expert", as_delegation_id="D002")  # parent=D002
    dt = DelegationTools(node)

    d001_wait_result = {}

    def _d001_waits():
        set_delegation_id("D001")
        d001_wait_result["out"] = dt.Wait()

    # D001's own Wait() runs in the background the whole time — nothing
    # of D001's is ready yet, so it must still be blocked after D020's
    # message (which belongs to D002, not D001).
    t_wait = threading.Thread(target=_d001_waits, daemon=True)
    t_wait.start()
    time.sleep(0.2)  # let D001's Wait() actually start blocking

    def _d020_asks():
        set_delegation_id("D020")
        dt.SendMessage("implementer", "from D020", wait_for_reply=False)

    threading.Thread(target=_d020_asks, daemon=True).start()
    time.sleep(0.3)  # give it a full tick+ to (wrongly) wake D001 if it would
    assert t_wait.is_alive(), (
        "D001's Wait() woke on D020's message, which belongs to D002")

    # D010 (D001's OWN child) messages D001 -- this one D001 must see,
    # and promptly (the whole point of the Condition-based wake).
    def _d010_asks():
        set_delegation_id("D010")
        dt.SendMessage("implementer", "from D010", wait_for_reply=False)

    threading.Thread(target=_d010_asks, daemon=True).start()
    t_wait.join(timeout=5)
    assert not t_wait.is_alive()
    assert "from D010" in d001_wait_result["out"]
    assert "from D020" not in d001_wait_result["out"]


class _InterleavingCondition:
    """Wraps a real per-identity Condition so the FIRST time a ``with``
    block exits after a successful ``wait_for`` is paused mid-release:
    this is exactly the moment a "pop after the lock" bug would leave a
    message still sitting in the queue, undisturbed, for a second
    consumer to steal first. Forces that exact interleaving deterministically
    instead of hoping the GIL scheduler happens to find it (a probabilistic
    stress loop, kept below, can pass on buggy code by luck; this one
    cannot).

    On the CURRENT (fixed) code the pop already happened INSIDE the lock,
    before this pause ever fires, so a competing consumer here correctly
    finds nothing left to steal and nobody crashes or double-consumes. If
    the pop-outside-the-lock pattern were ever reintroduced, the message
    would still be sitting there when this fires, the competing consumer
    would take it, and the original call's own (now unlocked, in the old
    shape) pop would raise IndexError on the empty queue.
    """

    def __init__(self, real: threading.Condition) -> None:
        self._real = real
        self._fired = False
        self.released = threading.Event()
        self.resume = threading.Event()

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        result = self._real.__exit__(*exc)
        if not self._fired:
            self._fired = True
            self.released.set()
            self.resume.wait(timeout=5)
        return result

    def wait_for(self, predicate, timeout=None):
        return self._real.wait_for(predicate, timeout=timeout)

    def notify_all(self):
        return self._real.notify_all()


def test_forced_interleaving_never_loses_or_duplicates_a_message():
    """Deterministic version of the race regression below (adda-boss-
    whopper's follow-up: a test that merely PASSES on buggy code doesn't
    guard the bug -- force the exact interleaving instead of hoping the
    scheduler finds it). See ``_InterleavingCondition`` for the mechanism.
    """
    node = _make_node()
    _register(node, "D001", "implementer")
    dt = DelegationTools(node)

    # Queue exactly one message upward BEFORE wrapping the cond -- this
    # append's own enter/exit must not be the one intercepted.
    set_delegation_id("D001")
    dt.SendMessage("strategizer", "the only message", wait_for_reply=False)
    set_delegation_id(None)

    real_cond = node._get_delegator_cond("entry")
    wrapped = _InterleavingCondition(real_cond)
    node._delegator_conds["entry"] = wrapped

    victim: dict = {}

    def _victim():
        set_delegation_id(None)
        try:
            victim["out"] = dt.SendMessage(
                "D001", "q", wait_for_reply=True)
        except Exception as exc:  # noqa: BLE001
            victim["exc"] = exc

    t = threading.Thread(target=_victim, daemon=True)
    t.start()

    assert wrapped.released.wait(timeout=5), "victim never reached release"
    # The competing consumer: exactly what a second real consumer does --
    # check, then pop, under the SAME lock the fix requires.
    entry = node._registry["D001"]
    stolen = None
    with real_cond:
        if entry.get("to_delegator"):
            stolen = entry["to_delegator"].popleft()
    wrapped.resume.set()
    t.join(timeout=5)

    assert not t.is_alive()
    assert "exc" not in victim, (
        f"victim raised: {victim.get('exc')!r} -- the pop-outside-the-lock "
        "race is back")
    assert stolen is None, (
        "competing consumer stole the message the victim should already "
        "have consumed inside its own lock -- pop is happening outside "
        "the lock again")
    assert "message from" in victim["out"]
    assert "the only message" in victim["out"]


def test_two_consumers_racing_on_one_queue_never_lose_or_duplicate_a_message():
    """Regression for the check-then-pop race (adda-boss-whopper review of
    6f4f980, item 1): predicate check and pop must share ONE lock
    acquisition. Pre-queues N messages on D001's to_delegator, then races
    TWO different consumers of that same queue -- a bare Wait() and a
    SendMessage(wait_for_reply=True) -- to drain it. Every message must be
    seen exactly once: no loss, no duplicate, no IndexError."""
    node = _make_node()
    _register(node, "D001", "implementer")
    dt = DelegationTools(node)

    n = 100
    produced = [f"m{i}" for i in range(n)]
    set_delegation_id("D001")
    for m in produced:
        dt.SendMessage("strategizer", m, wait_for_reply=False)
    set_delegation_id(None)

    seen: list[str] = []
    seen_lock = threading.Lock()
    tickets = list(range(n))  # one ticket per message actually queued
    tickets_lock = threading.Lock()

    def _take_ticket() -> bool:
        with tickets_lock:
            if tickets:
                tickets.pop()
                return True
        return False

    def _consumer_wait():
        set_delegation_id(None)
        while _take_ticket():
            out = dt.Wait()
            assert "message from" in out, out
            with seen_lock:
                seen.append(out.rsplit(": ", 1)[-1])

    def _consumer_send_message():
        set_delegation_id(None)
        while _take_ticket():
            out = dt.SendMessage("D001", "ack", wait_for_reply=True)
            assert "message from" in out, out
            with seen_lock:
                seen.append(out.rsplit(": ", 1)[-1])

    t1 = threading.Thread(target=_consumer_wait, daemon=True)
    t2 = threading.Thread(target=_consumer_send_message, daemon=True)
    t1.start(); t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(seen) == sorted(produced), (
        "lost or duplicated messages -- the check-then-pop race is back")


def test_delegator_wakes_promptly_when_the_target_ends_without_replying():
    """Regression for item 2: a SendMessage(wait_for_reply=True) to D001
    must not sit out the whole timeout when D001 reaches a terminal status
    (Done/Errored/Cancelled) without ever replying -- it wakes as soon as
    that happens (the same notify _finish_ok/_finish_error now perform),
    with a message naming what happened rather than a generic timeout."""
    node = _make_node()
    _register(node, "D001", "implementer")
    dt = DelegationTools(node)

    result = {}

    def _delegator():
        set_delegation_id(None)
        result["out"] = dt.SendMessage(
            "D001", "question", wait_for_reply=True)
        result["at"] = time.monotonic()

    start = time.monotonic()
    t = threading.Thread(target=_delegator, daemon=True)
    t.start()
    time.sleep(0.2)  # SendMessage is now blocked waiting for a reply

    # Exactly what WorkerSession._finish_ok/_finish_error do on completion:
    # flip the status, then notify the parent's Condition.
    with node._registry_lock:
        node._registry["D001"]["status"] = "Done"
        cond = node._get_delegator_cond(
            node._registry["D001"].get("parent", "entry"))
        with cond:
            cond.notify_all()

    t.join(timeout=5)
    assert not t.is_alive()
    elapsed = result["at"] - start
    assert elapsed < 0.8, f"took {elapsed:.2f}s -- did not wake on peer death"
    assert "D001 ended (Done)" in result["out"]
    assert "without replying" in result["out"]


def test_wait_for_reply_labels_an_already_queued_message_honestly():
    """Regression for item 4: if the target's queue already held an
    older, unrelated message when wait_for_reply=True is called, the
    deadlock guard returns it immediately -- but labelled as a message
    FROM its sender, never implied to be the answer to what was just
    sent."""
    node = _make_node()
    _register(node, "D001", "implementer")
    dt = DelegationTools(node)

    set_delegation_id("D001")
    dt.SendMessage(
        "strategizer", "an older, unrelated question", wait_for_reply=False)
    set_delegation_id(None)

    out = dt.SendMessage("D001", "a brand new question", wait_for_reply=True)
    assert "message from strategizer (D001)" in out
    assert "an older, unrelated question" in out


def test_send_message_empty_message_is_a_hard_error():
    node = _make_node()
    dt = DelegationTools(node)
    set_delegation_id(None)
    assert dt.SendMessage("implementer", "").startswith("ERROR:")
    assert dt.SendMessage("implementer", "   ").startswith("ERROR:")


def test_send_message_to_human_is_entry_node_only():
    """Only the entry node may address to="human" — every other node
    (even one with its own outgoing edges, like implementer here) must be
    refused and told to route through its delegator instead."""
    node = _make_node()  # name="strategizer", the spec's own entry node
    _register(node, "D001", "implementer")
    dt = DelegationTools(node)

    # A worker (D001) addressing "human" directly is refused.
    set_delegation_id("D001")
    out = dt.SendMessage("human", "need a decision")
    assert out.startswith("ERROR:")
    assert "entry node" in out

    # The entry node itself is not refused on that same ground — it falls
    # through to FollowUp's own (separately tested) human-channel logic,
    # which degrades to an unattended answer with no run/viewer context.
    set_delegation_id(None)
    out = dt.SendMessage("human", "need a decision")
    assert not out.startswith(
        "ERROR: only the entry node may SendMessage a human")


def test_send_message_to_human_refused_even_for_a_delegator_with_edges():
    """A non-entry node that itself delegates further (e.g. implementer,
    which has its own outgoing edge to math_expert) is still refused --
    "entry" is about topology (the graph's actual entry point), never
    "has outgoing edges of its own"."""
    node = _make_node(name="implementer", outgoing=["math_expert"])
    dt = DelegationTools(node)
    set_delegation_id(None)  # implementer's OWN top-level turn, not a worker
    out = dt.SendMessage("human", "need a decision")
    assert out.startswith("ERROR:") and "entry node" in out
