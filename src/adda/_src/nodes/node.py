"""``Node`` — the one kind of thing the agent graph is made of.

There is no hierarchy of node types here, and deliberately so. Every node
runs the same loop:

    def __call__(self, state):
        return self._orchestrate(state)

A node with outgoing edges has somewhere to delegate; a node with none does
not (``Delegate()`` simply has no valid target) — that is the only thing
``self._outgoing`` decides. There used to be a second behaviour
(``_respond``, in a now-deleted ``leaf.py``) for a node with no outgoing
edges, reachable only for a single-agent study (every other node is dispatched
via ``Delegate()`` on a worker thread, never invoked as a graph node at all —
see ``runtime/graph_builder.py``). Single-agent studies are the exception, not
the rule, so that second behaviour is gone: a lone entry node now runs the
same delegate-or-close loop everyone else does, just with nobody to delegate
to.

Everything else a node can do comes from what its ``Agent`` DECLARES in
``tools`` (see ``nodes/tools/routing``), never from what it is. The
strategizer, the implementer and the critic are all this class; they differ by
the name they are registered under, the prompt they carry, and the tools they
declare — which is exactly how a reader should think about them.

The turn behaviour lives in ``orchestration``; the remaining mixins are
per-concern, not per-type: ``recording`` (notes, retrospectives),
``critic_gate`` (the adversarial review), ``lifecycle`` (budget/backstop
halts), ``reproduction_gate`` (the notebook's lazy-reproduction
check). The ledger tools are a tool family like any other and live in
``nodes/tools/routing/ledger.py``.

Construction installs the same capability closures on every node's adapter
regardless of ``outgoing`` — sandboxed ``Write``, ``ReportEvals``,
``RecallHistory``, and the declaration-gated read-only store/ledger tools
(:meth:`Node._init_capabilities`). This matters even for a node that never
takes a turn of its own: ``runtime/graph_builder.py`` hands every named node's
adapter to whichever OTHER node may ``Delegate()`` to it
(``worker_adapters``), and each dispatch takes ``adapter.copy()``, whose
``closure_tools`` starts as a copy of the dict this construction-time setup
populated. Losing it here would be a silent regression for every
delegated specialist, not just the rare standalone leaf.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..epistemics.hypothesis_ledger import HypothesisLedger
    from ..runtime.graph_state import AgenticState

from ..infra.delegation_log import DelegationLog
from ..infra.run_abandon import RunAbandoned, raise_if_stopped
from .critic_gate import CriticGateMixin
from .lifecycle import LifecycleMixin
from .orchestration import OrchestrationMixin
from .recording import RecordingMixin
from .reproduction_gate import ReproductionGateMixin
from .stop import StopMixin
from .time_rules import TimeRulesMixin
from .wind_down import WindDownMixin


class Node(
    RecordingMixin,
    CriticGateMixin,
    LifecycleMixin,
    ReproductionGateMixin,
    OrchestrationMixin,
    StopMixin,
    TimeRulesMixin,
    WindDownMixin,
):
    """One node in the agent graph.

    ``inspect.getsource(Node._orchestrate)`` reads the routing logic every
    node runs.

    Parameters
    ----------
    adapter
        The LLM adapter this node speaks through.
    name
        The node's name in the graph. This is what distinguishes one agent
        from another — there is no per-agent node class.
    outgoing
        Names of the nodes this one may delegate to. Empty means ``Delegate()``
        has nowhere to send work — the only thing that differs structurally.
    spec
        The whole :class:`~..backends.base.Graph`, so a node can read its
        peers' roles and descriptions.
    report_sections, agent_tools
        This agent's own declared report contract and toolset. ``agent_tools``
        gates which capability closures :meth:`_init_capabilities` grants;
        ``report_sections`` is retained for callers that validate a worker's
        report against its own contract (``parsing._classify_response``).
    """

    _awake_slots = None  # run-wide AwakeSlots, set by graph_builder

    def __init__(
        self,
        adapter: Any,
        *,
        name: str = "node",
        outgoing: list[str] | tuple[str, ...] = (),
        spec: Any = None,
        study_dir: Any = None,
        interactive: bool = False,
        max_ask: int = 1,
        worker_adapters: dict | None = None,
        notes_dir: Any = None,
        workspace_dir: Any = None,
        delegation_log: DelegationLog | None = None,
        report_sections: tuple[str, ...] | None = None,
        agent_tools: frozenset[str] | None = None,
    ) -> None:
        self.adapter = adapter
        self.adapter.on_retry = self._record_llm_retry
        self._name = name
        self._outgoing = list(outgoing)
        self._spec = spec
        self._init_recording()
        # Universal capability setup — every node's adapter, regardless of
        # whether it has outgoing edges (see the module docstring for why
        # this matters even for a node that never takes a turn of its own).
        self._init_capabilities(
            study_dir=study_dir, workspace_dir=workspace_dir,
            delegation_log=delegation_log,
            report_sections=report_sections, agent_tools=agent_tools,
        )
        # Delegation registry, ledgers and routing closures. Harmless for a
        # node with no outgoing edges (Delegate() simply has no valid
        # target); epistemic OWNERSHIP still requires outgoing edges (see
        # _init_orchestration / _install_epistemics) — a leaf never acquires
        # the ledgers just because notes_dir was passed to every node.
        self._init_orchestration(
            name=name, outgoing=self._outgoing, spec=spec,
            study_dir=study_dir, interactive=interactive, max_ask=max_ask,
            worker_adapters=worker_adapters, notes_dir=notes_dir,
            workspace_dir=workspace_dir, delegation_log=delegation_log,
        )
        # Plain Claude Code (Default tools + Default prompt) is handed no
        # adda closure at all, whatever the topology would otherwise grant.
        from ..runtime.node_tools import is_plain_default
        #: A plain Claude Code node is run by its own session: adda sends it
        #: the statement and nothing after (no notice, no re-prompt, no
        #: wind-down). The backend ends it at a budget (``token_clock``).
        self._silent = is_plain_default(
            spec.nodes.get(name) if spec is not None else None)
        if self._silent:
            self.adapter.closure_tools.clear()

    def __call__(self, state: AgenticState) -> Any:
        """Take one turn. Every node runs the same delegate-or-close loop."""
        return self._orchestrate(state)

    def _raise_if_abandoned(self) -> None:
        """End the calling thread once the run has stopped waiting for it."""
        if self._abandon.is_set():
            raise RunAbandoned(self._name)
        raise_if_stopped(self._name)

    def _init_recording(self) -> None:
        """Establish the state ``RecordingMixin`` writes to, on EVERY node.

        Recording is a property of a node — any node's tools can return an
        ERROR, and any node's adapter reports token usage — so its substrate
        belongs here rather than in one behaviour's setup. It used to be
        created only by the orchestration setup, which was invisible while
        leaves were a separate class that did not carry RecordingMixin at all:
        the moment a leaf reached any recording call it raised AttributeError
        on ``_registry_lock``. The orchestration setup still assigns these
        itself, to the same values, so an orchestrating node is unchanged.
        """
        self._registry_lock = threading.Lock()
        self._abandon = threading.Event()
        self._notifications: list[str] = []
        self._notifications_lock = threading.Lock()
        self._error_counts: dict[str, int] = {}
        self._token_totals: dict = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            # The backend-independent schema (infra/telemetry.py): `legacy_calls`
            # counts calls whose backend reported none of it.
            "fresh_input": 0,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "normalized_calls": 0,
            "legacy_calls": 0,
            "total_cost_usd": 0.0,
        }
        self._cost_observed: bool = False
        self._current_notes_dir: Path | None = None
        self._telemetry: Any = None

    def _init_capabilities(
        self,
        *,
        study_dir: Any = None,
        workspace_dir: Any = None,
        delegation_log: DelegationLog | None = None,
        report_sections: tuple[str, ...] | None = None,
        agent_tools: frozenset[str] | None = None,
    ) -> None:
        """Wire this node's sandboxed Write and its declared read-only tools.

        Runs for EVERY node, not only one reachable as a standalone entry
        point — see the module docstring: whichever node ends up as someone
        else's ``Delegate()`` target is dispatched through a copy of THIS adapter
        (``adapter.copy()`` carries the ``closure_tools`` set here), so this is where a
        dispatched specialist's baseline capabilities actually come from,
        before ``WorkerSession.install_worker_tools``/``_sandbox_worker_writes``
        layer the per-delegation overrides (ReportEvals's own record callback,
        a delegation-scoped Write) on top.
        """
        self._study_dir = study_dir
        self._workspace_dir = Path(workspace_dir) if workspace_dir else None
        # The agent's declared tools — the single source of truth for which
        # capability closures this node is granted (read-only ledger/store
        # tools). Kept as a frozenset for membership checks.
        self._agent_tools: frozenset[str] = frozenset(agent_tools or ())
        # A node built without a declared toolset has nothing to narrow by.
        self._tools_declared = agent_tools is not None
        # This agent's declared report sections (e.g. the critic's
        # Findings/Verdict, not the implementer's Conclusions/Files touched).
        # Used to validate a worker's report against ITS OWN contract instead
        # of the implementer-shaped default — otherwise a correct critic or
        # literature report is wrongly flagged malformed (audit BF-10/O40).
        self._report_sections = report_sections
        self._delegation_log = delegation_log
        self._evals_reported: dict = {}
        self._setup_sandboxed_write()
        self.adapter.closure_tools.update(self._build_eval_closures())
        if delegation_log is not None:
            from .tools.routing import build_recall_history
            self.adapter.closure_tools["RecallHistory"] = build_recall_history(self)
        # Declaration-gated read-only ledger/store tools — the SAME builder
        # every node uses, so a specialist (e.g. the critic) gets an
        # identical, working QueryStore/HypothesisList
        # surface whenever it declares them. Resolves the run via the shared
        # Node._resolve_run_dir (delegation-log path).
        from ..runtime import features as _features
        from .tools.routing import build_declared_shared_closures
        self.adapter.closure_tools.update(
            build_declared_shared_closures(
                self, self._agent_tools - _features.disabled_tool_names()))
        # Closures from Agent.build_closure_tools() (ConsultLiterature,
        # CorpusAdd, ...) are installed on the adapter BEFORE this node
        # exists (agent_runtime.py), so they never pass through
        # orchestration.py's _wrap_closure the way routing tools do. A
        # closure tagged _adda_diagnostic_source (a fact about the run
        # environment a tool discovers, not about one call's return value —
        # see LiteratureCorpus.pop_diagnostic_event) needs that wrapper
        # regardless, so re-wrap it here, once, by the tag rather than by
        # name — any future tool needing the same reporting just carries the
        # same tag.
        for _cname, _cfn in list(self.adapter.closure_tools.items()):
            if getattr(_cfn, "_adda_diagnostic_source", None) is not None:
                self.adapter.closure_tools[_cname] = self._wrap_closure(
                    _cfn, self._name)
        # A config.yaml `nodes.<name>.tools` list is the whole tool set: the
        # always-on closures it does not name are withheld.
        from ..runtime.node_tools import withheld_closures
        _agent = self._spec.nodes.get(self._name) if self._spec is not None else None
        for _cname in withheld_closures(_agent):
            self.adapter.closure_tools.pop(_cname, None)

    def _setup_sandboxed_write(self) -> None:
        """Replace native Write with a workspace-sandboxed closure.

        Removes 'Write' from native_tools so the SDK doesn't expose it,
        then installs the same builder a dispatched worker gets
        (:func:`build_sandboxed_write`), scoped to this node's whole
        workspace instead of one delegation's subfolder — a node reached via
        real graph routing (the module docstring) has no delegation_id of
        its own to scope tighter than that.

        Bash is kept native but cwd is already set to study_dir by the adapter;
        the prompt further constrains it to the workspace.
        """
        if self._workspace_dir is None:
            return  # no sandboxing if study_dir unknown (e.g. tests)
        from ..runtime.node_tools import withheld_closures
        if "Write" in withheld_closures(
                self._spec.nodes.get(self._name) if self._spec is not None else None):
            return  # a Default node keeps the backend's native Write

        # Remove native Write so the SDK doesn't expose an unrestricted version
        if hasattr(self.adapter, "native_tools") and "Write" in self.adapter.native_tools:
            self.adapter.native_tools = [
                t for t in self.adapter.native_tools if t != "Write"
            ]

        from .tools.routing import build_sandboxed_write

        # Bound to a plain name (not inlined into the assignment) so
        # internal/tools/promptmap.py's injected_tool_docs() scanner — which
        # resolves a closure_tools["Write"] = <name> rebind to the function
        # <name> refers to — can still find this tool's docstring after the
        # move into the shared builder.
        _study_ws = (
            Path(self._study_dir) / "workspace"
            if self._study_dir is not None else None
        )
        Write = build_sandboxed_write(
            self._workspace_dir, study_workspace=_study_ws,
            deliverable_dir=(
                Path(self._study_dir) if self._study_dir is not None else None),
            deliverable_names=lambda: getattr(
                self, "_required_deliverables", None) or [])
        self.adapter.closure_tools["Write"] = Write

    def _build_eval_closures(self) -> dict:
        from .tools.routing import build_report_evals

        evals = self._evals_reported
        return {
            "ReportEvals": build_report_evals(
                record=lambda n: evals.__setitem__("count", n)
            )
        }

    # ── Run-context resolution (shared by every node) ────────────────────────
    # The read tools (QueryStore/HypothesisList) may be granted
    # to any node — the entry node, a mid-tier delegating node, or a leaf such
    # as the critic. They need the run's store/ledger paths, which are resolved
    # here so the tools work identically wherever they are granted.
    def _commit_workspace(self, message: str) -> str | None:
        """Commit the run workspace and return the sha (spec 11).

        On Node rather than on WorkerSession because a delegation is recorded
        from four places, not one: a worker finishing (ok or error), the
        critic gate, an AskForFeedback audit, and the close-time
        reconciliation of a delegation still running when the run ended. Every
        one of them appends a row to the delegation log, so every one of them
        owes that row a sha — otherwise ``workspace_sha: null`` means both
        "this delegation changed nothing" and "nobody looked", which is the
        ambiguity --allow-empty exists to prevent.

        The interrupted case is the one with teeth: a delegation killed
        mid-flight never reaches _finish_ok/_finish_error, so its partial file
        writes stay uncommitted and are swept into whichever delegation
        commits NEXT — attributing one delegation's work to another. Silence
        would be better than that; a commit is better still.

        Never raises — see infra/workspace_vcs.
        """
        run_dir = self._resolve_run_dir()
        if run_dir is None:
            return None
        from ..infra.workspace_vcs import commit_workspace
        return commit_workspace(run_dir / "debug" / "delegations", message)

    def _resolve_run_dir(self) -> Path | None:
        """Best-effort run_dir, valid on any node.

        Every node's ``_current_notes_dir`` is set at construction (from
        ``notes_dir``, passed to every node — see ``graph_builder.py``) and
        re-pointed each turn by ``_absorb_state`` if the run's real notes dir
        differs. It can still be ``None`` (e.g. a bare ``Node()`` built
        without one, as in unit tests) — every node DOES hold the shared
        delegation log at run_dir/debug/delegation_log.jsonl, so derive
        run_dir from that when the notes dir is unavailable.
        """
        notes = getattr(self, "_current_notes_dir", None)
        if notes is not None:
            return Path(notes).parent.parent
        dlog = getattr(self, "_delegation_log", None)
        p = getattr(dlog, "_path", None)
        return Path(p).parent.parent if p is not None else None

    def _read_ledger(self) -> HypothesisLedger | None:
        """The hypothesis ledger for READ access, resolved on any node.

        Returns the node's own bound ledger when it has one (the entry node);
        otherwise resolves a read-only view from the run's strategizer_notes
        when hypotheses.json exists. HypothesisLedger.__init__ performs no I/O,
        and only READ callers use this, so there is no write race with the
        entry node that owns the file.
        """
        own = getattr(self, "_ledger", None)
        if own is not None:
            return own
        rd = self._resolve_run_dir()
        if rd is None:
            return None
        notes = rd / "debug" / "strategizer_notes"
        if (notes / "hypotheses.json").exists():
            from ..epistemics.hypothesis_ledger import HypothesisLedger
            return HypothesisLedger(notes)
        return None
