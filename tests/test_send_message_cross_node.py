"""SendMessage across REAL Node objects (spec 12).

A delegation's registry entry lives in its DELEGATOR's Node; the worker calls
the SendMessage closure bound to its OWN Node. Every test in
test_send_message.py drives one shared Node, which hides the difference: run
20260928T225501 D014 (datagenerator -> strategizer) got "no live delegation
found for 'strategizer'" because the worker's own Node had no entry for D014.
These build the graph the way a run does and call the tool closures the
backend hands the model.
"""
from __future__ import annotations

import threading

import pytest

from adda._src.backends.base import Agent, Edge, Graph, set_delegation_id
from adda._src.nodes.tools.routing.delegation import DelegationTools
from adda._src.runtime import settings
from adda._src.runtime.graph_builder import build_graph


@pytest.fixture(autouse=True)
def _peer_interaction_on():
    settings.configure({"peer_interaction": True})
    yield
    settings.configure(None)
    set_delegation_id(None)


class _Adapter:
    def __init__(self) -> None:
        self.closure_tools: dict = {}


def _live_nodes():
    class Strategizer(Agent):
        role = "strategizer"
        description = "entry"

    class Implementer(Agent):
        role = "implementer"
        description = "worker and delegator"

    class MathExpert(Agent):
        role = "math_expert"
        description = "worker"

    spec = Graph(
        nodes={"strategizer": Strategizer(), "implementer": Implementer(),
               "math_expert": MathExpert()},
        edges=(Edge("strategizer", "implementer"),
               Edge("implementer", "math_expert")),
        entry="strategizer",
    )
    nodes: dict = {}
    build_graph(spec, lambda n, a: _Adapter(), node_registry=nodes)
    return nodes


def _dispatch(node, delegation_id, target, *, as_id=None):
    set_delegation_id(as_id)
    DelegationTools(node)._register_dispatch(
        delegation_id, target, [], False, None, None,
        "2026-01-01T00:00:00+00:00")
    set_delegation_id(None)


def _tool(node, name):
    return node.adapter.closure_tools[name]


def _as(delegation_id, fn, *a, **kw):
    out = {}

    def _run():
        set_delegation_id(delegation_id)
        out["v"] = fn(*a, **kw)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive()
    return out["v"]


def test_a_worker_reaches_its_delegator_through_its_own_tool_closure():
    nodes = _live_nodes()
    _dispatch(nodes["strategizer"], "D001", "implementer")
    send = _tool(nodes["implementer"], "SendMessage")

    out = _as("D001", send, "strategizer", "interim FYI")
    assert out == "Delivered to your delegator."

    set_delegation_id(None)
    waited = _tool(nodes["strategizer"], "Wait")()
    assert "interim FYI" in waited and "D001" in waited


def test_a_worker_that_is_also_a_delegator_messages_in_both_directions():
    nodes = _live_nodes()
    _dispatch(nodes["strategizer"], "D001", "implementer")
    _dispatch(nodes["implementer"], "D002", "math_expert", as_id="D001")
    d1 = _tool(nodes["implementer"], "SendMessage")
    d2 = _tool(nodes["math_expert"], "SendMessage")

    assert _as("D001", d1, "D002", "down") == "Delivered to D002."
    up = _as("D002", d2, "implementer", "up")
    assert up.startswith("Delivered to your delegator.")
    assert "your delegator sent you a message: down" in up
    up2 = _as("D001", d1, "strategizer", "up2")
    assert up2.startswith("Delivered to your delegator.")


def test_a_worker_is_told_of_an_unread_message_from_its_delegator():
    nodes = _live_nodes()
    _dispatch(nodes["strategizer"], "D001", "implementer")
    set_delegation_id(None)
    _tool(nodes["strategizer"], "SendMessage")("D001", "please stop at 40 evals")

    note = nodes["implementer"]._pending_for_you("D001")
    assert "please stop at 40 evals" in note or "message" in note.lower()
    assert note.strip()
