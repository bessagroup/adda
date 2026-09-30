"""LangGraph StateGraph builder for f3dasm agentic runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

from ..backends.base import Agent, Graph
from ..infra.delegation_log import DelegationLog
from ..nodes import Node
from .graph_state import AgenticState

__all__ = ["build_graph"]


def build_graph(
    spec: Graph,
    make_adapter: Callable[[str, Agent], Any],
    checkpointer: Any = None,
    study_dir: Any = None,
    interactive: bool = False,
    max_ask: int = 1,
    notes_dir: Any = None,
    workspace_dir: Any = None,
    delegation_log: DelegationLog | None = None,
    node_registry: dict[str, Any] | None = None,
) -> Any:
    """Build and compile a LangGraph StateGraph from a Graph spec.

    Parameters
    ----------
    spec : Graph
        Agent graph specification (nodes, edges, entry).
    make_adapter : callable
        ``(name: str, agent: Agent) -> adapter`` — factory that produces a
        ``ClaudeAdapter`` or ``OllamaAdapter`` for the given node.
    checkpointer : any, optional
        LangGraph checkpointer.  Defaults to an in-memory :class:`MemorySaver`.
    delegation_log : DelegationLog, optional
        Graph-wide delegation log for episodic memory (RecallHistory tool).
    node_registry : dict, optional
        If given, populated in place with ``{name: Node instance}`` as each
        node is constructed — an explicit, caller-owned way to reach the
        live Node objects afterward (e.g. spec 12's close-time open-review
        sweep) instead of reaching through the COMPILED graph's own
        internals (``compiled.nodes[name].bound.func``), which is a
        LangGraph implementation detail that could silently break on an
        upgrade.

    Returns
    -------
    CompiledGraph
        A compiled LangGraph graph ready to invoke.
    """
    builder = StateGraph(AgenticState)

    # ONE adapter per named node — shared across all orchestrating nodes.
    node_adapters = {n: make_adapter(n, spec.nodes[n]) for n in spec.nodes}

    live_nodes: dict[str, Any] = (
        node_registry if node_registry is not None else {})

    for name, agent in spec.nodes.items():
        adapter = node_adapters[name]  # shared instance, NOT make_adapter() again
        outgoing = spec.outgoing(name)

        # ONE node class; every node runs the same turn loop
        # (Node._orchestrate). What a node may DO follows from what its
        # Agent declares in `tools` — never from its topology. notes_dir is
        # passed to every node: a delegating node is simply a node that needs
        # help from another node (CLAUDE.md "all nodes are equal"), and
        # telemetry / the science monitor / hypothesis-ledger READ access
        # matter for every role, not only the entry node. WRITE access
        # (HypothesisPropose/Update, Milestone*) stays gated separately, by
        # each Agent's own declared `tools` (see
        # nodes/tools/routing/__init__.py) — passing notes_dir here grants no
        # capability a node has not already declared. A node with no
        # outgoing edges still receives this argument (Node.__init__ has one
        # init path for every node), but never ACQUIRES ledger ownership from
        # it — Node._init_orchestration gates `_owns_epistemics` on having
        # outgoing edges, not merely on notes_dir being set, so this only
        # takes effect for a node that itself has outgoing edges.
        node = Node(
            adapter,
            name=name,
            outgoing=outgoing,
            spec=spec,
            study_dir=study_dir,
            interactive=interactive,
            max_ask=max_ask,
            worker_adapters={n: node_adapters[n] for n in outgoing},
            notes_dir=notes_dir,
            workspace_dir=workspace_dir,
            delegation_log=delegation_log,
            report_sections=getattr(agent, "report_sections", None),
            agent_tools=getattr(agent, "tools", None),
        )
        live_nodes[name] = node

        builder.add_node(name, node)

    # A delegation's registry entry lives in its DELEGATOR's Node, while the
    # worker calls the tool closures bound to its OWN Node -- so every node
    # must be able to find the others (Node._delegation_entry).
    from ..nodes.slots import AwakeSlots
    from . import settings
    awake_slots = AwakeSlots(settings.get_int("max_awake_nodes", 5))
    for node in live_nodes.values():
        node._peers = live_nodes
        node._awake_slots = awake_slots

    builder.set_entry_point(spec.entry)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
