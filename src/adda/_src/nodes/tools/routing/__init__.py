"""The tool closures an orchestrating node is handed (Delegate/Wait/Done/FollowUp/
WriteNote/ReadNote/WriteCell/QueryStore/AskForFeedback +
hypothesis tools). Built per-node; the node is passed in so closures reach its
state. Built by an orchestrating node via Node._build_routing_closures.

This package assembles the final tool dict from the family-specific builders
in its sibling modules — delegation.py, notes.py, notebook.py, feedback.py,
store.py — in the exact same order and under the exact same declaration gates
as the original single-file implementation, so tool_catalog.py's rendered
``<tools>`` prompt section is unaffected by the split.
"""
from __future__ import annotations

from .delegation import (
    build_delegation_closures,
    build_recall_history,
    build_report_evals,
    build_sandboxed_write,
    resolve_target,
)
from .feedback import (
    _EXIT_INTERVIEW,
    _FAILED_RETROSPECTIVE,
    build_feedback_closures,
)
from .ledger import build_ledger_closures
from .notebook import (
    _strip_leading_md_header,
    build_notebook_closures,
)
from .notes import build_notes_closures
from .store import _select_best_index, build_declared_shared_closures

__all__ = [
    "build_declared_shared_closures",
    "build_routing_tools",
    # Re-exported for tests and internal callers that reached these directly
    # off the old flat routing.py module (kept so nothing outside this
    # package needs to know which family file now defines them).
    "_EXIT_INTERVIEW",
    "_FAILED_RETROSPECTIVE",
    "resolve_target",
    "_select_best_index",
    "_strip_leading_md_header",
    # One implementation per tool, not one per call site — these are the
    # shared builders both nodes/node.py (every node's own capabilities) and
    # nodes/tools/routing/delegation.py (a dispatched worker's) call.
    "build_recall_history",
    "build_report_evals",
    "build_sandboxed_write",
]


def build_routing_tools(node) -> dict:
    _dele = build_delegation_closures(node)

    # Topology-derived tools (task B's roster review, Elvis via
    # adda-boss-whopper): offering Delegate() on a node with nowhere to
    # delegate TO was a prompt-vs-tool contradiction -- the tool was always
    # present, calling it on such a node always errored ("ERROR: unknown
    # target ...  Valid targets: []", Delegate's own _resolve_target).
    # Delegate/Wait now BOTH exist only for a node with >=1 outgoing edge --
    # the node that started work is the only one entitled to collect it
    # (spec 12, internal/specs/12-peer-interaction.md, design item 4).
    #
    # Wait was briefly widened to "any edge, either direction" between the
    # two reviews of this same fix (commit f613427), on the theory that a
    # pure worker needed it too to block on "something addressed to me."
    # Spec 12's SendMessage(wait_for_reply=True) now owns all peer-reply
    # waiting for such a worker -- Wait goes back to being exactly what it
    # already was: the delegator's tool for collecting finished
    # delegations, including fan-out. This is a deliberate UN-shipping of
    # that widening, not an oversight -- see spec 12's own Risk note on it.
    #
    # Reply/FollowUp (peer-facing) vs SendMessage: mutually exclusive on the
    # peer_interaction knob, not stacked. FollowUp routes to whoever
    # delegated to THIS node (or a human, for the entry node) regardless of
    # this node's own outgoing edges, and Reply answers a FollowUp this
    # node received as a delegator -- both retired in favour of SendMessage
    # when the feature is on (its default now; see runtime/features.py).
    # Off is the old-contract ablation arm, restoring exactly this surface.
    from ....runtime import features as _features
    closures: dict = {}
    if not _features.enabled("peer_interaction"):
        closures["Reply"] = _dele["Reply"]
        closures["FollowUp"] = _dele["FollowUp"]
    if node._outgoing:
        closures["Delegate"] = _dele["Delegate"]
        closures["Wait"] = _dele["Wait"]

    # SendMessage (spec 12): granted unconditionally when the feature is on
    # (like Reply/FollowUp were, above) -- a node with no edges at all is
    # never dispatched, so the tool being present but practically
    # unreachable there is harmless.
    if _features.enabled("peer_interaction"):
        closures["SendMessage"] = _dele["SendMessage"]

    if node._delegation_log is not None:
        closures["RecallHistory"] = _dele["RecallHistory"]

    # Agent-declared closure tools: inject only what the subclass opted in to.
    # Done / WriteNote / ReadNote are declared in StrategizerAgent.tools;
    # an ImplementerAgent or DebuggerAgent that gains outgoing edges does not
    # declare them and therefore does not receive them.
    _agent_tools: frozenset = frozenset()
    if node._spec is not None:
        _ag = node._spec.nodes.get(node._name)
        if _ag is not None:
            _agent_tools = _ag.tools
    # A disabled feature takes its tools with it. Leaving them registered does
    # not produce an agent without the feature -- it produces an agent that
    # keeps calling a tool which returns "ERROR: ... not available in this
    # run.", and every such return is counted as an ERROR_RETURN diagnostic.
    # This generalises the hand-rolled pipeline_deliverable strip (BACKLOG
    # #30), which was the only feature that did it: the strategizer otherwise
    # saw a toolset saturated with pipeline.ipynb capability regardless of what
    # the study's own PROBLEM_STATEMENT.md said, and used it. Done is never
    # owned by a feature -- a run must always be able to close.
    _agent_tools = _agent_tools - _features.disabled_tool_names()

    _fb = build_feedback_closures(node)
    _nb = build_notebook_closures(node)
    _notes = build_notes_closures(node)

    if "Done" in _agent_tools:
        closures["Done"] = _fb["Done"]
    for _t in ("WriteCell", "ShowNotebook", "RunNotebook", "RunScratch",
               "WriteDeliverable"):
        if _t in _agent_tools:
            closures[_t] = _nb[_t]
    if "WriteNote" in _agent_tools:
        closures["WriteNote"] = _notes["WriteNote"]
    if "ReadNote" in _agent_tools:
        closures["ReadNote"] = _notes["ReadNote"]
    if "Confer" in _agent_tools and not _features.enabled("peer_interaction"):
        closures["Confer"] = _dele["Confer"]
    # CancelDelegation is OPT-IN (plug-and-play), not always-on: its def is
    # intact but it is granted only to an agent that lists it in its `tools`.
    # PRODUCTION agents do not — dropped (drop-but-don't-delete) pending the
    # cooperative-stop decision; restore by adding the name to an agent's
    # `tools` (one line), exactly like the debugger agent is plug-and-play.
    if "CancelDelegation" in _agent_tools:
        closures["CancelDelegation"] = _dele["CancelDelegation"]
    # ConsultHandbook is injected universally at adapter construction
    # (agent_runtime._make_adapter) — no per-node duplication here.

    # Capability closures are DECLARATION-GATED (single source of truth = the
    # Agent's `tools`), exactly like the notebook/Done/notes tools above.
    # Every ledger tool MUTATES — hypothesis (epistemics) or milestone
    # (process policy) — so they go only to agents that declare them; a
    # stateless leaf must never mutate a shared ledger (MilestoneList is the
    # one read, kept beside its write). The read-only HypothesisList comes
    # from the shared builder below instead.
    for _t, _fn in build_ledger_closures(node).items():
        if _t in _agent_tools:
            closures[_t] = _fn
    # Read-only ledger/store tools — declaration-gated and shared verbatim
    # across every node (Node._init_capabilities calls the same builder), so
    # the exposure surface is identical regardless of a node's outgoing edges.
    closures.update(build_declared_shared_closures(node, _agent_tools))

    # AskForFeedback is only injected when a critic node is
    # connected AND this is the entry node (only the entry node
    # gates Done).
    if "AskForFeedback" in _fb:
        closures["AskForFeedback"] = _fb["AskForFeedback"]

    # Wrap every closure so ERROR returns and exceptions are counted.
    _node_name = node._name
    return {k: node._wrap_closure(v, _node_name) for k, v in closures.items()}
