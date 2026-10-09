"""Tests for the new-delegation time cutoff (``delegate_cutoff_multiple``).

Past ``delegate_cutoff_multiple`` x the (soft) time budget, ``Delegate()``
must refuse to start anything new, while ``Wait``/``GetStatus``/``Done`` and
every other close-out tool stay fully functional — the run must always have
a path to close, never be stranded (CLAUDE.md task requirement).
"""
from __future__ import annotations

import json
import time

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.nodes import Node
from adda._src.runtime import settings

from .fixtures import approve_delegation

_WORKER_REPORT = (
    "### Actions taken\nran a thing\n"
    "### Conclusions\nit worked\n"
    "### Numbers\nn/a\n"
)


class _Stub:
    def __init__(self, response: str = "") -> None:
        self._response = response
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return self._response

    def copy(self):
        s = self.__class__.__new__(self.__class__)
        _Stub.__init__(s, self._response)
        s.closure_tools = dict(self.closure_tools)
        return s


def _node(worker_response: str = _WORKER_REPORT) -> Node:
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "Delegate", "Wait"})
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
        worker_adapters={"implementer": _Stub(worker_response)},
    )


def teardown_function(_fn) -> None:
    settings.configure(None)  # never leak a knob between tests


def _delegate(n: Node, **kw):
    kw.setdefault("target", "implementer")
    kw.setdefault("intent", "t")
    kw.setdefault("expected_report", "r")
    kw.setdefault("hypothesis_ids", ["H1"])
    kw.setdefault("wait", False)
    return n.adapter.closure_tools["Delegate"](**kw)


def _extract_id(started_text: str) -> str:
    return started_text.split("ID: ")[1].split(".")[0].strip("'\" ")


# ---------------------------------------------------------------------------


def test_below_cutoff_delegate_works():
    n = _node()
    n._budget_seconds = 100.0
    n._run_start = time.time() - 50.0  # 0.5x budget, below default 1.5x cutoff
    out = _delegate(n)
    assert "Delegation started" in out, out


def test_past_cutoff_refuses_and_fires_no_delegation():
    n = _node()
    n._budget_seconds = 100.0
    n._run_start = time.time() - 160.0  # 1.6x budget: past the 1.5x cutoff, before the 2x backstop
    before = dict(n._registry)
    out = _delegate(n)
    assert out.startswith("ERROR"), out
    assert "1.5" in out
    assert "160s" in out  # elapsed/budget figures present
    assert n._registry == before, "no new delegation should be registered"


def test_past_cutoff_wait_and_done_still_work():
    """The run must still be able to close: Wait() on an in-flight
    delegation, and approving/Done(), stay reachable past the cutoff.

    Runs under the default (peer_interaction on): the in-flight delegation
    opens for review rather than auto-finalizing, and approving it -- not a
    NEW Delegate -- must not itself be refused by the cutoff."""
    n = _node()
    n._budget_seconds = 100.0
    n._run_start = time.time() - 10.0  # start well below the cutoff
    out = _delegate(n, wait=False)
    assert "Delegation started" in out, out
    did = _extract_id(out)

    # Now cross the cutoff.
    n._run_start = time.time() - 160.0

    # A NEW delegation is refused...
    refused = _delegate(n)
    assert refused.startswith("ERROR"), refused

    # ...but Wait() on the one already in flight is untouched and returns
    # its real report, open for review — the run can still close.
    wait_out = n.adapter.closure_tools["Wait"](delegation_id=did)
    assert "refused" not in wait_out
    assert "it worked" in wait_out
    assert "OPEN FOR REVIEW" in wait_out

    # Approving it (not a new Delegate) must work past the cutoff too.
    approve_out = approve_delegation(n.adapter.closure_tools, did)
    assert "refused" not in approve_out.lower()
    with n._registry_lock:
        assert n._registry[did]["status"] == "Done"


def test_in_flight_delegation_untouched_by_cutoff():
    """Runs under the default: an in-flight delegation crossing the cutoff
    still opens for review normally, and approving it past the cutoff
    still reaches Done."""
    n = _node()
    n._budget_seconds = 100.0
    n._run_start = time.time() - 10.0
    out = _delegate(n, wait=False)
    did = _extract_id(out)

    # Cross the cutoff mid-flight.
    n._run_start = time.time() - 160.0

    n._threads[did].join(timeout=5)
    with n._registry_lock:
        entry = n._registry[did]
        assert entry["status"] == "OpenForReview"
        assert "it worked" in entry["result"]

    # The report must be READ (Wait/Delegate(wait=True)) before it can be
    # approved -- the "read" enforcement (spec 12 item 4) -- not bypassable
    # by reaching into the registry directly, as this test does above.
    n.adapter.closure_tools["Wait"](delegation_id=did)
    approve_delegation(n.adapter.closure_tools, did)
    with n._registry_lock:
        assert n._registry[did]["status"] == "Done"


def test_cutoff_disabled_restores_today_behaviour():
    settings.configure({"delegate_cutoff_multiple": 0})
    n = _node()
    n._budget_seconds = 100.0
    n._run_start = time.time() - 1000.0  # way past any sane multiple
    out = _delegate(n)
    assert "Delegation started" in out, out


def test_no_budget_configured_no_cutoff_no_crash():
    n = _node()
    n._budget_seconds = None
    n._run_start = None
    out = _delegate(n)
    assert "Delegation started" in out, out


def test_cutoff_refusal_emits_diagnostic(tmp_path):
    n = _node()
    debug_dir = tmp_path / "runs" / "T" / "debug"
    debug_dir.mkdir(parents=True)
    n._current_notes_dir = debug_dir / "notes"
    n._budget_seconds = 100.0
    n._run_start = time.time() - 160.0

    _delegate(n)

    diag = debug_dir / "diagnostics.jsonl"
    assert diag.exists()
    records = [json.loads(line) for line in diag.read_text().splitlines()]
    assert any(r.get("error_type") == "DELEGATE_CUTOFF" for r in records)
