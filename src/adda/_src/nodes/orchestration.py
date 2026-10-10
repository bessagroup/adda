"""The one turn behaviour every :class:`~.node.Node` runs.

It reads Reports, decides the next delegation (if it has anywhere to
delegate to), and owns the run's gates (budget, critic, reproduction). A node
with outgoing edges can act on that in ``Delegate()``; a node with none just
never has a valid target — the loop itself does not fork on that.

``inspect.getsource(Node._orchestrate)`` reads the full routing logic.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..runtime.graph_state import AgenticState

from ..epistemics.hypothesis_ledger import HypothesisLedger
from ..epistemics.science_monitor import ScienceMonitor
from ..infra import token_clock
from ..infra import wind_down as _wind_down
from ..infra.delegation_log import DelegationLog
from .notices import insert_notice, wrap_notice
from .parsing import _to_adapter_messages


class OrchestrationMixin:
    """The turn loop and its setup — run by every node, delegation-capable or not."""

    def _init_orchestration(
        self,
        *,
        name: str,
        outgoing: list[str],
        spec: Any,
        study_dir: Any = None,
        interactive: bool = False,
        max_ask: int = 1,
        worker_adapters: dict | None = None,
        notes_dir: Any = None,
        workspace_dir: Any = None,
        delegation_log: DelegationLog | None = None,
    ) -> None:
        """Set up the delegation registry, the ledgers and the routing tools.

        Runs for every node (``Node.__init__`` no longer branches on
        ``outgoing``). The registry/routing state below is inert when
        ``outgoing`` is empty — ``Delegate()`` just has no valid target — so
        there is no harm running it unconditionally. Epistemic OWNERSHIP is
        the one thing that must stay gated on having outgoing edges (see
        ``_owns_epistemics`` below): ``notes_dir`` is passed identically to
        every node by ``graph_builder.py``, and a node with nobody to
        delegate to must never acquire the hypothesis/milestone ledgers just
        because it was handed a path.

        ``study_dir``/``workspace_dir``/``delegation_log`` are already set by
        :meth:`Node._init_capabilities`, which runs first for every node —
        not re-set here.
        """
        self._route: dict = {}
        self._interactive = interactive
        self._max_ask = max_ask
        self._ask_count = 0
        self._current_notes_dir: Path | None = (
            Path(notes_dir) if notes_dir is not None else None
        )
        # Parallel delegation registry: id → {"status", "result", "evals", "hypothesis_ids", "started_at"}
        self._worker_adapters: dict[str, Any] = worker_adapters or {}
        self._registry: dict[str, dict] = {}
        self._registry_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        # SendMessage/review (spec 12, peer_interaction): the live
        # WorkerSession for each OPEN-FOR-REVIEW delegation, so an
        # approving SendMessage can finalize it (call its own _finish_ok)
        # without re-deriving everything _finish_ok needs from the
        # registry alone. Popped once finalized -- not a leak over a long
        # run with many delegations.
        self._worker_sessions: dict[str, Any] = {}
        # SendMessage (spec 12, peer_interaction feature, not yet default-on):
        # one threading.Condition PER DELEGATOR IDENTITY -- keyed by that
        # delegator's OWN delegation_id, or "entry" for the orchestrating
        # node itself -- not per Node. Two concurrent delegations of the
        # SAME role sharing this Node object (e.g. two "implementer"
        # delegations D001/D002, each possibly delegating further) are
        # DIFFERENT delegator identities and must never wake each other's
        # waits or see each other's children's messages. Lazily created
        # (one dict entry per identity that ever waits), guarded by its own
        # lock since Condition creation itself must be race-free.
        self._delegator_conds: dict[str, threading.Condition] = {}
        self._delegator_conds_lock = threading.Lock()
        # Push notifications: background threads append here; tool calls drain it.
        self._notifications: list[str] = []
        self._notifications_lock = threading.Lock()
        # Budget state — set at the start of each __call__ from AgenticState
        self._budget_seconds: float | None = None
        self._run_start: float | None = None
        # Wall-clock rules (nodes/time_rules.py). Only the entry node, which
        # holds the run clock, ever sets ``_time_rules``.
        self._time_rules: Any = None
        self._time_fired: set[str] = set()
        self._eval_step_fired = -1
        self._time_lock = threading.Lock()
        self._time_timers: list[threading.Timer] = []
        self._time_closed = False
        self._time_armed = False
        # Wind-down state (nodes/wind_down.py); set on the entry node only.
        self._wd_started_at: float | None = None
        self._wd_turns = 0
        self._wd_step_turns = {"wait": 0, "done": 0}
        self._wd_save_asked = False
        self._stop: dict | None = None
        # Hard USD cost ceiling (None = inactive). Set each __call__ from state.
        self._budget_usd: float | None = None
        # True once any LLM call reports a real cost (claude). Stays False under
        # ollama (cost is None) → the USD ceiling is treated as inactive.
        self._cost_observed: bool = False
        self._usd_inactive_warned: bool = False
        # Consecutive Errored delegations per target (reset on that target's
        # next success). Drives the repeated-errors resumable halt.
        self._consecutive_errors: dict[str, int] = {}
        # Targets whose latest Errored delegation could not reach the LLM
        # endpoint: the halt then names that cause, not a generic error loop.
        self._unreachable_targets: set[str] = set()
        # Per-delegation pending messages (budget warnings) to prepend to
        # worker tool results.  Keyed by delegation_id; drained on next call.
        self._pending_worker_msgs: dict[str, list[str]] = {}
        self._pending_worker_msgs_lock = threading.Lock()
        # Whether THIS node owns the run's epistemic ledgers. Decided once,
        # here, from what graph_builder passed: it hands the real notes_dir
        # to every orchestrating node (any node with outgoing edges), not
        # just the entry node — a delegating node is a node that needs
        # help from another node, nothing more, and telemetry / the science
        # monitor / hypothesis-ledger READ access matter for every role.
        # WRITE access (HypothesisPropose/Update, Milestone*) is gated
        # separately, by each Agent's own declared `tools`. Ownership is
        # never acquired later — see _install_epistemics, which then narrows it
        # to the records whose tools the node holds. Gated on `outgoing`
        # (not just `notes_dir is not None`) because graph_builder passes
        # the same notes_dir to every node's constructor, including a node
        # with no outgoing edges — that node must not acquire ledger
        # ownership merely because a path was handed to it.
        self._owns_epistemics: bool = bool(outgoing) and notes_dir is not None
        self._install_epistemics()
        # Running total of delegations at the START of the current __call__
        # Used as a seed for the delegation sequence counter.
        self._state_total_delegations: int = 0
        # Snapshot of _delegation_seq at the start of the current turn, so
        # the terminal Command counts only THIS turn's new delegations. Set
        # again by _absorb_state every turn; initialised here so the node's
        # attribute set is complete before any turn has run.
        self._seq_at_turn_start: int = 0
        # Monotonic per-node delegation counter — never reset within a
        # run.  Seeded from _state_total_delegations on first __call__
        # so checkpoint-resumed runs continue from the correct offset.
        # Because it never resets, it avoids the ID collision that
        # occurs when completed delegations are pruned from the registry
        # but _state_total_delegations has not yet accumulated them.
        self._delegation_seq: int = 0
        # Two-shot Done() gate: first call warns, second call closes.
        # Resets to False whenever a new Delegate() fires.
        self._done_warned: bool = False
        # Science monitor fires once per turn; reset at __call__ start.
        self._science_injected_this_turn: bool = False
        # Post-Done exit interview: set after the critic accepts; the next
        # Done() carries only the retrospective. _final_summary holds the real
        # conclusion so the recorded summary is the science, not the interview.
        self._awaiting_retro: bool = False
        self._final_summary: str | None = None
        # Consecutive non-PASS critic verdicts; after 3, the gate closes
        # gracefully UNGATED (bounded escape) instead of looping forever.
        self._revise_count: int = 0
        # Eval budget for this run (stashed each turn from state).
        self._eval_budget: int | None = None
        # Cumulative cap on the "no canonical source registered" nudge (soft).
        self._no_source_nudges: int = 0
        # Bounded re-prompt counter: incremented each time the node loops back
        # due to an unaccepted termination (no Done or refused Done).  NOT reset
        # in the A1/A2 per-turn block — it persists across loopbacks within one
        # run.  After 3 loopbacks the run terminates UNGATED.
        self._finish_attempts: int = 0
        # Accumulated token usage across strategizer + all workers this run.
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
        # Per-node raw tool-call error count: any ERROR: return or raised
        # exception from any injected closure counts as one error for that node.
        self._error_counts: dict[str, int] = {}
        self.adapter.closure_tools.update(self._build_routing_closures())
        self.adapter.route_watcher = lambda: self._route.get("kind") == "done"

    def _install_epistemics(self) -> None:
        """Build (or rebuild at a new path) everything ``notes_dir`` owns.

        THE one construction site for the hypothesis ledger, the milestone
        ledger, the science monitor and telemetry. There used to be two: the
        constructor built the ledger, and ``_absorb_state`` — which runs at the
        start of every turn — rebuilt it with ``if self._ledger is None``,
        knowing nothing about why the constructor had left it None. Three
        consequences, all of them silent:

        * ``graph_builder`` used to hand ``notes_dir`` to the entry node
          alone, so it alone owned the ledgers; any other orchestrating node
          re-acquired one on its first turn, undoing that decision. (It now
          hands the real ``notes_dir`` to every orchestrating node, so this
          particular inconsistency no longer applies — kept here as the
          historical reason ``_install_epistemics`` exists as one site.)
        * Only the ledger was re-pointed when the run's notes dir differed from
          the constructor's; the milestone ledger and telemetry kept writing to
          the stale path.
        * The science monitor, built once in the constructor, was never
          rebuilt at all — so a node whose ledger was resurrected ran with the
          ledger ON and the monitor OFF.

        Ownership is fixed at construction (``_owns_epistemics``); this only
        ever rebuilds at a corrected path, never grants ownership.
        """
        from ..epistemics.milestones import MilestoneLedger
        from ..infra.telemetry import Telemetry
        from ..runtime import features

        notes = self._current_notes_dir
        if not self._owns_epistemics or notes is None:
            self._ledger = None
            self._milestones = None
            self._science_monitor = None
            self._telemetry = None
            return

        # A node owns an epistemic record only if it holds the tools that
        # write to it. A ledger nobody can write is not a ledger, and a
        # milestone gate on a node that cannot set a milestone blocks a close
        # it has no means to unblock. The monitor watches what those tools
        # produce, so it follows either family.
        tools = self._agent_tools
        holds_ledger = not self._tools_declared or bool(
            tools & features.by_key("hypothesis_ledger").tools)
        holds_milestones = not self._tools_declared or bool(
            tools & features.by_key("milestones_enabled").tools)

        # Hypothesis ledger — persists hypotheses.json. Switchable: its tools
        # and its prompt section are withheld by the same knob (runtime.
        # features), so turning it off does not leave the agent commanded to
        # use tools that error.
        self._ledger: HypothesisLedger | None = (
            HypothesisLedger(notes)
            if holds_ledger and features.enabled("hypothesis_ledger") else None
        )

        # Milestone ledger (process policy) — persists milestones.json.
        # Seeded with the config default gates unless disabled. DISTINCT from
        # the hypothesis ledger (epistemics): process vs what's-true.
        self._milestones: MilestoneLedger | None = None
        if holds_milestones and features.enabled("milestones_enabled"):
            self._milestones = MilestoneLedger(notes)
            # C3 switchable: the draft-pipeline gate seeds only when the
            # pipeline-deliverable knob is on (off = byte-identical to today).
            # The oracle-gold-state milestone likewise seeds only when the
            # reproduction gate itself is on — its whole reason to exist is
            # that gate's store-row precondition.
            self._milestones.seed_defaults(
                include_pipeline=features.enabled("pipeline_deliverable"),
                include_reproduction_gate=features.enabled(
                    "reproduction_gate"))

        # Science drift monitor — needs the delegation log and nothing else.
        # It used to be gated on the hypothesis ledger too, via a constructor
        # argument it stored and never read, so disabling the ledger disabled
        # the monitor as well.
        self._science_monitor: ScienceMonitor | None = None
        if (self._delegation_log is not None
                and (holds_ledger or holds_milestones)
                and features.enabled("science_monitor")):
            self._science_monitor = ScienceMonitor(
                self._delegation_log,
                diagnostics_writer=self._record_science_drift,
                role_of=self._role_of,
            )

        # Separable per-call telemetry — additive, off the decision path.
        # Lives under debug/telemetry/ (notes is debug/strategizer_notes).
        self._telemetry: Telemetry | None = Telemetry(notes.parent)

    # ── Authoritative delegation status (audit BF-0) ─────────────────────────
    # The persistent delegation_log owns existence + terminal status: it
    # survives node reconstruction and background threads write their terminal
    # DONE/FAILED record to it. The in-memory _registry is ONLY a cache of live
    # execution state (threads, streamed results) and can lag or be rebuilt
    # empty — so any "does D exist / is it terminal" question reads the log.
    def _log_status(self, delegation_id: str) -> tuple[str | None, str]:
        """Return (status, deliverable) for *delegation_id* from the persistent
        log, or (None, "") if the log has no such delegation."""
        if self._delegation_log is None:
            return None, ""
        for r in self._delegation_log.query_all():
            if r.get("id") == delegation_id:
                return r.get("status"), (r.get("deliverable") or "")
        return None, ""

    def _pending_delegations(self) -> list[str]:
        """In-flight delegations, reconciled against the authoritative log.

        A delegation the log shows terminal (DONE/FAILED) is never reported
        pending, even if the in-memory cache still says "Working". That stale
        state is what made Done()'s liveness gate refuse forever and kill run4
        by watchdog after it had already found the optimum (audit BF-0/BF-2).
        """
        with self._registry_lock:
            pending = [
                d for d, e in self._registry.items()
                if e.get("status") == "Working"
            ]
        if self._delegation_log is not None:
            terminal = {
                r["id"] for r in self._delegation_log.query_all()
                if r.get("status") in ("DONE", "FAILED")
            }
            pending = [d for d in pending if d not in terminal]
        return pending

    def _find_datagenerator_name(self) -> str | None:
        """Name of the first connected datagenerator worker, or None.

        Its presence means a canonical ground-truth source CAN be authored
        and registered for this study.
        """
        spec = self._spec
        if spec is None or not hasattr(spec, "nodes"):
            return None
        for target in self._outgoing:
            agent = spec.nodes.get(target)
            if (
                agent is not None
                and getattr(agent, "role", None) == "datagenerator"
                and target in self._worker_adapters
            ):
                return target
        return None

    def _canonical_source_registered(self) -> bool:
        """True if a canonical ground-truth source is resolvable.

        Reads run_config.json: an evaluator entrypoint OR a lookup pool counts
        as a registered source. Best-effort — on any read failure, assume not
        registered (the nudge is soft, so a false 'no' just costs one notice).
        """
        notes = self._current_notes_dir
        if notes is None:
            return False
        try:
            import json as _json
            cfg = _json.loads(
                (Path(notes).parent / "run_config.json").read_text())
            return bool(
                cfg.get("evaluator_entrypoint")
                or cfg.get("evaluator_lookup")
            )
        except Exception:  # noqa: BLE001
            return False

    @property
    def _current_run_dir(self):
        """This run's root, derived from the notes dir the graph state sets.

        ``run_dir`` arrives on the state, not on the node, and only
        ``_current_notes_dir`` (``<run>/debug/strategizer_notes``) is kept
        from it — so the run root is that path's grandparent. ``None``
        before the first invoke, which callers must tolerate.
        """
        notes = self._current_notes_dir
        return None if notes is None else notes.parent.parent

    def _drain_operator_notes(self) -> str:
        """Operator notes queued by a human in the viewer, marked as coming
        from the operator; "" when none. Notes addressed to a RUNNING
        delegation are routed to that worker instead. Claims the queue
        destructively, so whatever calls this owns delivery."""
        text = ""
        # Operator notes: messages a human queued in the viewer while the run
        # was working. Delivered here, on the same path as the budget warnings, so a note
        # reaches the agent at its next tool call rather than interrupting a
        # turn in progress. Marked as coming from the operator because the
        # agent should weigh it differently from another agent's message —
        # it is the one voice in the run that is not itself an agent.
        run_dir = self._current_run_dir
        if run_dir is not None:
            from ..infra.operator_channel import drain_note_rows
            rows = drain_note_rows(run_dir)
            mine: list[str] = []
            for _row in rows:
                _to = _row.get("to_node") or ""
                _note = (
                    "[OPERATOR NOTE — from the human running this study. "
                    "Weigh it as a briefing correction, not as another "
                    f"agent's opinion: {_row['text']}]"
                )
                # A note addressed to a RUNNING delegation goes to that
                # worker, on the same per-delegation queue the
                # budget warnings use — so a human can correct work already
                # in flight instead of waiting for a wrong result. The queue
                # is claimed destructively, so this is the only place that
                # may drain it: routing here is what keeps an addressed note
                # from being swallowed by the orchestrator's own delivery.
                if _to:
                    with self._registry_lock:
                        _entry = self._registry.get(_to)
                        _live = bool(_entry) and _entry.get("status") == "Working"
                    if _live:
                        with self._pending_worker_msgs_lock:
                            self._pending_worker_msgs.setdefault(
                                _to, []).append(_note)
                        continue
                    # Addressed to something not running: the human still
                    # said it, so it must not vanish — hand it to the
                    # orchestrator with the intended recipient named.
                    _note = (
                        f"[OPERATOR NOTE addressed to {_to}, which is not "
                        f"running — delivered to you instead: "
                        f"{_row['text']}]"
                    )
                mine.append(_note)
            if mine:
                text = "\n\n".join(mine) + "\n\n" + text
        return text

    def _drain_entry_notices(self) -> str:
        """Notices a campaign process queued for the entry node (the oracle
        was edited, say); "" for any other node or when none. The entry
        node holds no delegation id, so the backends' post-tool hook never
        reaches its queue; its next tool call does."""
        run_dir = self._current_run_dir
        entry = getattr(self._spec, "entry", None)
        if run_dir is None or entry is None or entry != self._name:
            return ""
        from ..infra.pending_notices import drain
        queued = drain(run_dir / "debug", "entry")
        return "\n".join(queued) + "\n\n" if queued else ""

    def _drain_notifications(self) -> str:
        """Return and clear any pending push notifications, or empty
        string."""
        with self._notifications_lock:
            if not self._notifications:
                text = ""
            else:
                msgs = list(self._notifications)
                self._notifications.clear()
                text = "\n".join(msgs) + "\n\n"
        text = self._drain_entry_notices() + text
        text = self._drain_operator_notes() + text
        text = self._stop_tick() + text
        if self._science_monitor is not None:
            offenders = self._science_monitor.escalation_due()
            _critic_name = self._find_critic_name()
            if offenders and _critic_name is not None:
                # Escalation fires: perform bookkeeping-only drain (discard
                # text) so the critic findings are the sole corrective
                # payload — regular drift messages would pollute context.
                self._science_monitor.drain()
                task_msg = self._build_feedback_task_msg(offenders)
                findings = self._invoke_critic(task_msg)
                self._science_monitor.note_escalated()
                if self._delegation_log is not None:
                    _fb_id = (
                        "FB"
                        + datetime.now(
                            tz=timezone.utc
                        ).strftime("%H%M%S")
                    )
                    self._delegation_log.record(
                        id=_fb_id,
                        from_node=self._name,
                        to_node=_critic_name,
                        task="ScienceMonitor escalation audit",
                        deliverable=findings,
                        hypothesis_ids=offenders,
                        workspace_sha=self._commit_workspace(
                            f"{_fb_id} {self._name} -> {_critic_name} "
                            "[ESCALATION]"),
                        started_at=datetime.now(
                            tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                        completed_at=datetime.now(
                            tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                        status="FEEDBACK",
                        tokens_in=0,
                        tokens_out=0,
                        cost_usd=None,
                        critic_review=self._last_critic_review,
                    )
                text += (
                    "[SCIENCE MONITOR — ESCALATION] Repeated drift "
                    f"on {', '.join(offenders)}. Critic audit "
                    f"findings:\n{findings}\n"
                )
            else:
                # No escalation: inject at most once per strategizer turn
                # to avoid the same warning appearing on every tool call.
                if not self._science_injected_this_turn:
                    drift = self._science_monitor.drain()
                    if drift:
                        text += drift
                        self._science_injected_this_turn = True
        # Everything accumulated above is adda speaking to the agent, not
        # a tool's output — mark it so both the agent and the viewer can
        # tell the difference (see nodes/notices.py).
        return wrap_notice(text)

    def _delegation_entry(self, delegation_id: str) -> tuple[Any, dict] | None:
        """``(owning node, registry entry)`` of a delegation, or None.

        The entry lives in the registry of the node that DELEGATED (its
        delegator), not of the node running the work; the tools a worker
        calls are bound to the worker's own node, so a worker looking up its
        own delegation must search across the graph's nodes. Delegation ids
        come from one graph-wide log, so at most one node owns an id.
        """
        for n in (self, *getattr(self, "_peers", {}).values()):
            with n._registry_lock:
                entry = n._registry.get(delegation_id)
            if entry is not None:
                return n, entry
        return None

    def _get_delegator_cond(self, identity: str) -> threading.Condition:
        """The one Condition a given delegator identity waits on and is
        woken through (SendMessage, spec 12). ``identity`` is a
        delegation_id or ``"entry"`` — see ``__init__``'s comment on
        ``_delegator_conds``. Lazily created, race-free."""
        with self._delegator_conds_lock:
            cond = self._delegator_conds.get(identity)
            if cond is None:
                cond = threading.Condition()
                self._delegator_conds[identity] = cond
            return cond

    @staticmethod
    def _truncate_for_notice(text: str, n: int = 100) -> str:
        text = text.strip()
        return text if len(text) <= n else text[:n] + "..."

    def _pending_for_you(self, identity: str) -> str:
        """What ``identity`` (a delegator: ``"entry"`` or a delegation id
        of a node that is ITSELF delegating further) currently owes,
        computed FRESH on every call -- never a drained queue, since
        "nothing pending" must read as silence indefinitely, not just
        until the first drain (spec 12 design item 10(a): "at minimum,
        every agent must always KNOW what it currently owes ... or
        nothing (in which case: silence, not a notice for its own
        sake)").

        Four kinds. As a DELEGATOR (``entry.get("parent") == identity``
        -- never a sibling's or a nested child's): a report open for
        review (`OpenForReview`), a finished delegation (`Done`/`Errored`)
        not yet collected via `Wait`, and an unread `SendMessage` question
        sitting in a child's own `to_delegator` queue. As a WORKER (``identity`` is itself a
        registry entry, i.e. this node is mid-delegation): an unread
        `SendMessage` from ITS OWN delegator sitting in that entry's
        `to_worker` queue. Every queue peek (never a pop -- that stays
        Wait's/SendMessage's job) shares ONE lock acquisition with its
        own check, under the SAME Condition/lock the queue's real
        consumer uses (the check-then-pop race rule applies just as much
        to a check-then-PEEK). Returns "" when none apply -- the common
        case, deliberately silent.
        """
        with self._registry_lock:
            mine = [
                (did, dict(e)) for did, e in self._registry.items()
                if e.get("parent") == identity
            ]
        _owned = self._delegation_entry(identity)
        my_own_entry = _owned[1] if _owned else None
        reviews = sorted(
            did for did, e in mine if e.get("status") == "OpenForReview")
        uncollected = sorted(
            did for did, e in mine
            if e.get("status") in ("Done", "Errored") and not e.get("waited")
        )

        cond = self._get_delegator_cond(identity)
        with cond:
            questions = [
                (did, e.get("target", did), e["to_delegator"][0][1])
                for did, e in mine if e.get("to_delegator")
            ]

        worker_message = None
        if my_own_entry is not None:
            with my_own_entry["worker_cond"]:
                if my_own_entry.get("to_worker"):
                    worker_message = my_own_entry["to_worker"][0][1]

        if not (
            reviews or uncollected or questions
            or worker_message
        ):
            return ""
        bits = []
        if reviews:
            bits.append(
                f"open for review: {', '.join(reviews)} -- SendMessage(id, "
                "..., approve=True) to finalize, or ask a question first"
            )
        if uncollected:
            bits.append(
                f"finished, not yet collected: {', '.join(uncollected)} "
                "-- Wait(id)"
            )
        if questions:
            bits.append("; ".join(
                f"{did} ({target}) asked you: "
                f"{self._truncate_for_notice(msg)}"
                for did, target, msg in questions
            ))
        if worker_message:
            bits.append(
                "your delegator sent you a message: "
                f"{self._truncate_for_notice(worker_message)}"
            )
        return "You have pending items -- " + "; ".join(bits) + "."

    def _build_routing_closures(self) -> dict:
        from .tools.routing import build_routing_tools
        return build_routing_tools(self)

    def _wrap_closure(self, fn: Any, node_name: str) -> Any:
        """Return a version of *fn* that records ERROR returns and exceptions.

        Uses functools.wraps so inspect.signature() follows __wrapped__ to the
        original function — _infer_schema_from_callable must see the real
        parameter names, not (*args, **kwargs).

        Also coerces string-typed arguments to int/float/bool when the
        function annotation requests it (handles Ollama passing "5" for
        an int parameter).

        And it is where pending notices reach the agent: whatever
        ``_drain_notifications`` holds is APPENDED to the tool's result — after
        it, so a result's leading word stays what callers dispatch on ("Done",
        "Working", "ERROR:", an id like "H3"). That
        used to be hand-copied into 18 tool bodies and missing from the rest —
        the store tools never delivered a notice — so whether the agent heard
        about a finished delegation depended on which tool it happened to
        call. A tool that places notices itself (the status poll keeps its
        status word first; Wait drains while it blocks; Done composes its own
        reply) still does, and finds the queue already empty here. A
        worker's closures pass through here too, and are never drained into.
        """
        import functools as _functools
        import inspect as _inspect
        import typing as _typing

        node = self
        tool_name = getattr(fn, "__name__", repr(fn))

        # Resolve type hints once; fall back to {} if any forward ref
        # cannot be resolved (e.g. "DelegationLog | None").
        try:
            _hints = _typing.get_type_hints(fn)
        except Exception:  # noqa: BLE001
            _hints = {}
        _COERCIBLE = {int, float, bool}

        def _coerce(name: str, value: Any) -> Any:
            target = _hints.get(name)
            if target not in _COERCIBLE or not isinstance(value, str):
                return value
            if target is bool:
                low = value.strip().lower()
                if low in ("true", "1", "yes"):
                    return True
                if low in ("false", "0", "no"):
                    return False
                return value
            try:
                return target(value)
            except ValueError:
                return value

        @_functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            node._raise_if_abandoned()
            # Coerce string args before calling the real function.
            call_args = dict(kwargs)
            try:
                bound = _inspect.signature(fn).bind_partial(
                    *args, **kwargs
                )
                for pname in list(bound.arguments):
                    bound.arguments[pname] = _coerce(
                        pname, bound.arguments[pname]
                    )
                args, kwargs = bound.args, bound.kwargs
                call_args = dict(bound.arguments)
            except TypeError:
                pass  # signature mismatch: let fn raise its own error

            try:
                from ..backends.base import get_delegation_id as _gdid
                _did = _gdid()
                result = _wind_down.check(
                    _did or "entry", tool_name, can_call_done=not _did)
                if result is None:
                    result = fn(*args, **kwargs)
                if (
                    isinstance(result, str)
                    and result.lstrip().startswith("ERROR:")
                ):
                    node._record_tool_error(
                        node_name,
                        tool_name,
                        "ERROR_RETURN",
                        result[:2000],
                        args=call_args,
                    )
                # A tool can carry a diagnostic source (e.g. ConsultLiterature
                # tags itself with its LiteratureCorpus) for facts that are
                # true of the environment, not of one call's return value —
                # e.g. the dense embedder being unavailable — and so would
                # otherwise never reach diagnostics.jsonl or the agent. This
                # is the one place with both the per-run diagnostics path
                # and, via the tag, a handle back to the source; it fires
                # once (the source pops its own event) rather than on every
                # call.
                _diag_src = getattr(fn, "_adda_diagnostic_source", None)
                if _diag_src is not None:
                    _event = _diag_src.pop_diagnostic_event()
                    if _event is not None:
                        _kind, _msg, *_extra = _event
                        node._record_intervention(
                            _kind, node_name, _msg, **(_extra[0] if _extra else {}))
                        if isinstance(result, str):
                            result = wrap_notice(_msg) + result
                # Only for this node's OWN tools. A dispatched worker's
                # closures are wrapped by the delegating node too (it records
                # their errors), and draining there would hand the
                # orchestrator's notices to the worker.
                if isinstance(result, str) and node_name == node._name:
                    notices = node._drain_notifications()
                    if notices.strip():
                        result = insert_notice(result, notices)
                    # Spec 12 design item 10(a): a pending-for-you notice
                    # on every tool result, scoped to THIS CALL's own
                    # delegator identity (never a sibling's or a nested
                    # child's). Gated behind peer_interaction (on by
                    # default since the migration-sweep commit) -- with the
                    # feature off (the no-peer-messaging arm),
                    # OpenForReview cannot exist and this stays silent.
                    from ..runtime import features as _features
                    if _features.enabled("peer_interaction"):
                        from ..backends.base import get_delegation_id
                        identity = get_delegation_id() or "entry"
                        pending = node._pending_for_you(identity)
                        if pending:
                            result = insert_notice(
                                result, wrap_notice(pending))
                return result
            except Exception as exc:
                node._record_tool_error(
                    node_name,
                    tool_name,
                    type(exc).__name__,
                    str(exc)[:2000],
                    tb=traceback.format_exc(),
                    args=call_args,
                )
                raise

        return _wrapped

    def _orchestrate(self, state: AgenticState) -> Any:
        """One orchestration turn, in the order it happens.

        Absorb the run state onto the node, work out what the agent must be
        TOLD this turn, invoke it once, then route on what it did. Every step
        is a method below, named for its step; a turn ends in exactly one of
        three ways, which :meth:`_route_turn` states.
        """
        self._absorb_state(state)
        budget_warnings = self._budget_warnings(state)
        halt = self._check_unrecoverable(
            state, self._budget_seconds, self._run_start)
        if halt is not None:
            return halt
        pending_notifs = self._reset_for_turn()
        stop_notice = self._stop_tick()
        if stop_notice:
            pending_notifs.append(stop_notice)
        messages = self._compose_messages(state, budget_warnings, pending_notifs)
        try:
            ai_msg = self._invoke_turn(messages)
        except Exception as exc:  # noqa: BLE001
            if self._stop is None:
                raise
            return self._close_after_wind_down_error(state, exc)
        return self._route_turn(state, ai_msg)

    # ── Before the turn ──────────────────────────────────────────────────────

    def _absorb_state(self, state: AgenticState) -> None:
        """Copy the run state the node's TOOLS read onto the node itself.

        The closures reach their context through ``node.…``, not through
        AgenticState, so anything a tool needs has to land here first.
        """
        # Update notes_dir from current state run_dir
        run_dir = state.get("run_dir")
        if run_dir:
            _notes = Path(run_dir) / "debug" / "strategizer_notes"
            if _notes != self._current_notes_dir:
                # The run's real notes dir differs from the one the
                # constructor saw. Re-point EVERYTHING that lives there, not
                # just the hypothesis ledger — the milestone ledger and
                # telemetry used to keep writing to the stale path. Nodes that
                # do not own the ledgers still track the path (the critic gate
                # reads run files through it) but acquire nothing.
                self._current_notes_dir = _notes
                self._install_epistemics()
            # Wire canonical store dir into ScienceMonitor lazily.
            # store_dir is the ExperimentData *project_dir* (run_dir/
            # experiment_data), NOT the folder holding the CSVs. ExperimentData
            # appends its own EXPERIMENTDATA_SUBFOLDER ("experiment_data"), so
            # the rows live one level deeper at
            # run_dir/experiment_data/experiment_data/output.csv — hence the
            # apparent double directory is correct, not a typo.
            if self._science_monitor is not None:
                self._science_monitor.store_dir = (
                    self._current_notes_dir.parent.parent
                    / "experiment_data"
                )

        # Eval budget → available to the Done() critic gate for budget-aware
        # framing (judge the best honest conclusion within evals spent).
        self._eval_budget = state.get("eval_budget")

        # Required aux deliverables (config.yaml) → on the node so WriteDeliverable
        # may write them: the gate REQUIRES them, so the writing tool must accept
        # them (else gate-vs-tool deadlock — audit run 20260624T021359).
        self._required_deliverables = state.get("required_deliverables") or []

        # Store on node so the status poll can compute delegation timeout
        self._budget_seconds = state.get("budget_seconds")
        self._run_start = state.get("start_time")
        self._time_rules_start()
        self._budget_usd = state.get("budget_usd")

        # Capture total_delegations so Delegate() can seed the counter.
        self._state_total_delegations = state.get("total_delegations", 0)
        # Seed the monotonic counter from state on first turn (or after a
        # checkpoint rebuild).  Never decremented — ensures IDs are unique
        # even when the registry is pruned between turns.
        if self._delegation_seq < self._state_total_delegations:
            self._delegation_seq = self._state_total_delegations
        # Snapshot seq at turn start so total_new counts only THIS turn.
        self._seq_at_turn_start: int = self._delegation_seq

    def _ledgered_eval_total(self, floor: int) -> int:
        """The run's eval count, preferring the ledger over an accumulator.

        The canonical ledger is the source of truth: a killed/cancelled
        delegation flushes rows the state accumulator never sees, so the
        accumulator undercounts. Summed across the canonical store AND every
        design namespace — namespace evals were invisible to the run total and
        to the soft budget. ``floor`` keeps the accumulator for lookup-direct
        studies with no instrumented store.
        """
        try:
            from ..evaluation.ledger_summary import total_ledgered_evals
            _nd = getattr(self, "_current_notes_dir", None)
            if _nd is not None:
                return max(
                    floor,
                    int(total_ledgered_evals(
                        _nd.parent.parent / "experiment_data")),
                )
        except Exception:  # noqa: BLE001
            pass
        return floor

    def _budget_warnings(self, state: AgenticState) -> list[dict]:
        """Advisory budget messages for this turn.

        The budget rules (notices at ``budget_warn_from``, the cutoff, the
        wind-down; wall, tokens and evaluations alike) are in
        ``nodes/time_rules.py`` and ``nodes/wind_down.py`` and reach the model as notifications.
        """
        warnings: list[dict] = []
        self._time_rules_tick()
        return warnings

    def _reset_for_turn(self) -> list[str]:
        """A1/A2: clear per-turn state; return the notifications to deliver.

        Working entries are preserved so loopbacks don't orphan live
        delegations whose background threads are still running.
        """
        self._route.clear()
        self._ask_count = 0
        self._done_warned = False
        self._science_injected_this_turn = False
        with self._registry_lock:
            self._registry = {
                d: e for d, e in self._registry.items()
                if e["status"] in ("Working", "Done")
            }
            self._threads = {
                d: t for d, t in self._threads.items()
                if d in self._registry
            }
        with self._notifications_lock:
            pending = list(self._notifications)
            self._notifications.clear()
        return pending

    # ── What the agent is told this turn ─────────────────────────────────────

    def _compose_messages(
        self,
        state: AgenticState,
        budget_warnings: list[dict],
        pending_notifs: list[str],
    ) -> list[dict]:
        """The conversation plus everything injected in-band this turn.

        Everything after the conversation itself is adda speaking to the
        agent — a budget warning, the no-source nudge, the milestone backlog,
        a pushed notification — arriving in the SAME role ("user") the human's
        own task arrives in. Marked, for the same reason tool-result notices
        are (nodes/notices.py): unmarked, neither the agent nor a reader can
        tell the runtime's nudge from the human's brief, and the viewer cannot
        style it as anything else.
        """
        if self._silent:
            return _to_adapter_messages(state["messages"])
        injected = (
            self._constraint_refresh()
            + budget_warnings
            + self._no_source_nudge()
            + self._backlog_announcement()
            + [{"role": "user", "content": n} for n in pending_notifs]
        )
        return _to_adapter_messages(state["messages"]) + [
            {**m, "content": wrap_notice(str(m.get("content", "")),
                                         trailing="")}
            for m in injected
            if str(m.get("content", "")).strip()
        ]

    def _constraint_refresh(self) -> list[dict]:
        """This turn's constraint snapshot, recomputed NOW.

        The entry node used to be the one call site that did not re-snapshot.
        ``agent_runtime`` rendered a snapshot once at run start and
        concatenated it onto the problem statement, and that string is the
        standing first user turn — re-sent verbatim on every later turn, so
        the numbers inside it could never advance. Observed on run
        20260917T141603: four consecutive strategizer turns spanning 23.5
        minutes all read "2.7min/60.0min used (5%)", while the true figure at
        turn 4 was 26.2min (44%).

        The orchestrator was not blind -- every delegation report carries a
        fresh snapshot (``_append_budget_report``) -- which made this a
        CONTRADICTION rather than an absence: a frozen block and live blocks
        in one context. Worse, the frozen copy lived in the first user turn,
        which the context trim pins and never evicts, so it was the one
        guaranteed to survive while the fresh ones aged out.

        Recomputing per turn is what ``constraint_snapshot.py`` already asks
        of every caller: "call this at every delegation boundary rather than
        caching a value from earlier ... its entire purpose depends on being
        current". The orchestrator is the node that decides how much more to
        attempt, so it is the node that most needs the live clock.
        """
        from ..runtime import features
        from ..runtime.constraint_snapshot import snapshot_for_node
        if not features.enabled("budget_notes"):
            return []
        try:
            text = snapshot_for_node(self).as_text()
        except Exception:  # noqa: BLE001 — a missing snapshot never fails a turn
            return []
        return [{"role": "user", "content": text}] if text.strip() else []

    def _no_source_nudge(self) -> list[dict]:
        """Recommend registering a canonical source (soft, ≤3×).

        If this graph has a datagenerator (so a canonical ground-truth source
        CAN be authored) but none is registered, recommend delegating to it.
        Without a registered source every evaluation lands off-ledger and
        nothing is reproducible from the canonical store. Soft and capped —
        never blocks; the strategizer may ignore it for a genuinely
        source-free study.
        """
        if (
            self._no_source_nudges >= 3
            or self._find_datagenerator_name() is None
            or self._canonical_source_registered()
        ):
            return []
        self._no_source_nudges += 1
        _dg = self._find_datagenerator_name()
        self._record_intervention(
            "NO_SOURCE_NUDGE", self._name,
            "No canonical source registered; recommended delegating to "
            f"'{_dg}'.",
            notice=self._no_source_nudges, cap=3,
        )
        return [{
            "role": "user",
            "content": (
                "[SETUP] No canonical ground-truth source is registered "
                "for this study (no evaluator entrypoint or lookup pool). "
                f"A '{_dg}' agent is available — delegate to it to author "
                "and register the source, so evaluations flow through "
                "get_evaluator(), land in the canonical store, and the "
                "result is reproducible. If this is intentionally a "
                "source-free (surrogate-only) study, disregard this. "
                f"(notice {self._no_source_nudges}/3)"
            ),
        }]

    def _backlog_announcement(self) -> list[dict]:
        """Announce the process backlog ONCE, at the start of the run.

        As a conversation message, so the agent cannot claim it didn't know
        these gate the implementer. Injected the first time this node runs.
        """
        if self._milestones is None or getattr(self, "_backlog_announced", False):
            return []
        from ..epistemics.milestones import render_backlog
        _bl = render_backlog(self._milestones)
        self._backlog_announced = True
        return [{"role": "user", "content": _bl}] if _bl else []

    def _invoke_turn(self, messages: list[dict]) -> Any:
        """Run one model turn and account for what it spent."""
        from langchain_core.messages import AIMessage

        # DEBUG: stream this turn's full reasoning + tool-calls to
        # debug/transcripts/<node name>/turn_NNN.jsonl.
        from ..backends.base import (
            bind_run_context as _bind_rc,
        )
        from ..backends.base import (
            debug_enabled as _dbg,
        )
        from ..backends.base import (
            set_transcript_sink as _set_sink,
        )
        self._turn_count = getattr(self, "_turn_count", 0) + 1
        if _dbg() and self._current_notes_dir is not None:
            _set_sink(str(
                self._current_notes_dir.parent / "transcripts"
                / self._name / f"turn_{self._turn_count:03d}.jsonl"))
        _notes = self._current_notes_dir
        _rc_path = (
            str(_notes.parent / "run_config.json") if _notes is not None else None
        )
        with _bind_rc(f"{self._name}-turn-{self._turn_count:03d}", _rc_path):
            text = self.adapter.invoke(messages)
        # Accumulate this node's own token usage.
        self._record_usage(
            getattr(self.adapter, "last_usage", {}) or {},
            role=self._role_of(self._name),
            model=getattr(self.adapter, "model", None),
            phase=f"{self._name}_turn",
            delegation_id=None,
        )
        return AIMessage(content=text)

    # ── How the turn ends ────────────────────────────────────────────────────

    def _route_turn(self, state: AgenticState, ai_msg: Any) -> Any:
        """A turn ends in exactly one of three ways.

        1. Work is still in flight     → re-prompt, free (no attempt spent)
        2. It stopped without closing  → re-prompt, bounded (3 attempts)
        3. Otherwise                   → the run ends

        Reproduction is owned entirely by the Done() gate (it runs the
        controlled gate before any close and declares a FAILED run after a
        bounded number of sighted attempts — see RunNotebook(gate=True)), so there is
        no separate post-accept repro check here; this handles only deliverable
        presence and un-accepted termination.
        """
        from ..runtime import terminal
        held = self._wind_down_route(state, ai_msg)
        if held is not None:
            return held
        accepted = self._route.get("kind") == "done"
        present, missing = self._deliverable_status(state)
        if token_clock.stop_info() is not None:
            token_clock.note_stop(
                deliverables_present=present, deliverables_missing=missing)
            return self._terminate_run(
                state, ai_msg, False, missing,
                termination=terminal.BUDGET_STOP)
        for router in (self._reprompt_while_working,
                       self._reprompt_unfinished):
            held = router(ai_msg, accepted, missing)
            if held is not None:
                return held
        return self._terminate_run(state, ai_msg, accepted, missing)

    def _reprompt_while_working(
        self, ai_msg: Any, accepted: bool, missing: list
    ) -> Any | None:
        """Delegations still running: re-prompt WITHOUT spending an attempt.

        A healthy delegation still in flight is WORK IN PROGRESS, not a failed
        finish: the deliverables usually depend on its result, and it WILL
        report. Spending a bounded finish-attempt on it means a slow-but-healthy
        delegation (run-4: D004 at ~2.5 evals/s, ~100s from done, with wall
        budget to spare) burns 3 "finish attempts" across turns and force-
        terminates the run UNGATED. At the time budget the wind-down
        (nodes/wind_down.py) takes over and handles a delegation that
        truly hangs.
        """
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        if accepted:
            return None
        working = self._working_delegations()
        if not working:
            return None
        collect = "collect them with Wait" if self._holds("Wait") else (
            "let them finish")
        close = "call Done()" if self._holds("Done") else "end your turn"
        msg = (
            f"Delegations still running: {working}. They are"
            f" progressing — {collect} and {close} only"
            " once they report (then write any remaining deliverables"
            " from their results). Do NOT close early. This wait does"
            " NOT count against your finish attempts; the wind-down at"
            " the budget ends the run."
        )
        if missing:
            msg += "\n\nStill to write AFTER they finish: " + ", ".join(missing)
        return Command(
            goto=self._name,
            update={"messages": [ai_msg, HumanMessage(content=msg)]},
        )

    def _reprompt_unfinished(
        self, ai_msg: Any, accepted: bool, missing: list
    ) -> Any | None:
        """Bounded re-prompt when the turn ended without an accepted close."""
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        from ..runtime import features
        if (accepted and not missing) or self._finish_attempts >= 3 \
                or self._silent:
            return None
        if not features.enabled("reprompt_unfinished"):
            return None
        self._finish_attempts += 1
        problems: list[str] = []
        if missing:
            missing_list = "\n".join(f"- {p}" for p in missing)
            write = ("Write them via WriteDeliverable()"
                     if self._holds("WriteDeliverable")
                     else "Write them to the study directory")
            then = ("calling Done()" if self._holds("Done")
                    else "ending your turn")
            problems.append(
                "Required deliverables are missing from the"
                f" study directory:\n{missing_list}\n"
                f"{write} before {then}."
            )
        if not accepted:
            working = self._working_delegations()
            if working:
                collect = ("Collect them with Wait" if self._holds("Wait")
                           else "Let them finish")
                close = ("call Done()" if self._holds("Done")
                         else "end your turn")
                problems.append(
                    f"Delegations still running: {working}."
                    f" {collect} and {close} once they finish."
                )
            elif self._holds("Done"):
                problems.append(
                    "You ended your turn without an accepted"
                    " Done(). If Done() was refused (critic"
                    " verdict, two-shot confirmation, or another"
                    " gate), address the refusal and call Done()"
                    " again. A run only closes through an"
                    " accepted Done()."
                )
            else:
                problems.append(
                    "You ended your turn before the work was finished."
                    " Finish it, then end your turn."
                )
        return Command(
            goto=self._name,
            update={
                "messages": [
                    ai_msg,
                    HumanMessage(content=(
                        "Run cannot complete"
                        f" (attempt {self._finish_attempts}/3):\n"
                        + "\n\n".join(problems)
                    )),
                ],
            },
        )

    def _holds(self, tool: str) -> bool:
        """Whether this node was handed the adda tool ``tool``.

        Every text adda writes to a node names only tools the node holds.
        """
        return tool in self.adapter.closure_tools

    def _working_delegations(self) -> list[str]:
        """Delegation ids still in flight right now."""
        with self._registry_lock:
            return [
                d for d, e in self._registry.items()
                if e["status"] == "Working"
            ]

    def _terminate_run(
        self, state: AgenticState, ai_msg: Any, accepted: bool, missing: list,
        termination: str | None = None,
    ) -> Any:
        """Close the run: final counts, banner, ghost flush, terminal Command."""
        from langgraph.graph import END
        from langgraph.types import Command

        from ..runtime import terminal

        # total_new: only delegations created THIS turn (seq delta vs the
        # snapshot taken at turn start), not Done entries from prior turns.
        with self._registry_lock:
            total_new = self._delegation_seq - self._seq_at_turn_start
            evals_new = sum(e["evals"] for e in self._registry.values())

        summary = self._banner(
            self._route.get("summary") or ai_msg.content, accepted, missing,
            stopped=termination)
        self._flush_ghost_delegations()

        # The persisted (reported) eval total prefers the ledger aggregate over
        # the accumulator: the accumulator can drop evals a namespace-blind
        # guard mis-flagged as off-ledger, and never saw namespace stores at
        # all. The ledger across all namespaces is authoritative → run_status.
        _evals_persist = self._ledgered_eval_total(
            state.get("evals_used", 0) + evals_new)
        # The terminal triple, decided here rather than inferred from the
        # banner later. An un-accepted close never reached a gate, and a close
        # missing deliverables was never validated against them — both are
        # UNGATED regardless of what the route recorded. terminal.resolve
        # fails safe (unrecorded → UNGATED) and refuses GATED without a review.
        _outcome, _termination, _reviewed = terminal.resolve(
            self._route.get("outcome")
            if accepted and not missing else terminal.UNGATED,
            termination or (self._route.get("termination") if accepted
                            else terminal.NO_CLOSE),
            self._route.get("reviewed"),
        )
        return Command(
            goto=END,
            update={
                "messages": [ai_msg],
                "done": True,
                "last_report": summary,
                "total_delegations": state["total_delegations"] + total_new,
                "evals_used": _evals_persist,
                "token_totals": dict(self._token_totals),
                "error_counts": dict(self._error_counts),
                "outcome": _outcome,
                "termination": _termination,
                "reviewed": _reviewed,
            },
        )

    def _banner(self, summary: str, accepted: bool, missing: list,
                stopped: str | None = None) -> str:
        """Prepend the UNGATED banner when the run ends without an accepted Done().

        A FAILED-reproduction close carries its own ⛔ banner in the route
        summary and IS accepted=done, so it is not re-banner'd here.
        """
        from ..runtime import features, terminal

        if (accepted and not missing) or not features.enabled(
                "reprompt_unfinished"):
            return summary
        flags = []
        if stopped == terminal.BUDGET_STOP:
            from ..infra import token_clock
            trig = (token_clock.stop_info() or {}).get("budget_trigger")
            flags.append("the session was stopped at the "
                         + {"wall": "wall-clock"}.get(trig, "output-token")
                         + " budget")
        elif not accepted:
            flags.append(
                "the run terminated WITHOUT an accepted Done() —"
                " the final conclusions did NOT pass the"
                " adversarial critic gate"
            )
        if missing:
            flags.append(f"required deliverables missing: {missing}")
        return terminal.ungated_banner(flags) + summary

    def _flush_ghost_delegations(self) -> None:
        """Close out delegations whose threads die with the interpreter.

        Daemon threads still alive when the run closes are killed at process
        exit — their run() never reaches the DONE/FAILED record write, leaving
        orphan RUNNING entries in the log. Write an INTERRUPTED terminal record
        for each so query_all() (last-wins) collapses to a closed state instead
        of RUNNING.
        """
        with self._registry_lock:
            live = [
                (did, dict(entry))
                for did, entry in self._registry.items()
                if entry.get("status") == "Working"
            ]
        if not live or self._delegation_log is None:
            return
        _now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        for _did, _entry in live:
            self._delegation_log.record(
                id=_did,
                from_node=self._name,
                to_node=_entry.get("target", "unknown"),
                # An interrupted delegation never reached _finish_ok/_error,
                # so its partial writes are still uncommitted. Commit them
                # HERE, against the delegation that made them, or the next
                # delegation to commit absorbs them and the history says the
                # wrong worker wrote those files.
                workspace_sha=self._commit_workspace(
                    f"{_did} {self._name} -> "
                    f"{_entry.get('target', 'unknown')} [INTERRUPTED]"),
                task="",
                deliverable=(
                    "INTERRUPTED: run closed while this delegation was "
                    "still running (background thread killed at process exit)"
                ),
                hypothesis_ids=_entry.get("hypothesis_ids") or [],
                started_at=_entry.get("started_at") or "",
                completed_at=_now,
                status="INTERRUPTED",
                tokens_in=0,
                tokens_out=0,
                cost_usd=None,
                is_falsification_attempt=bool(
                    _entry.get("is_falsification_attempt")
                ),
                evals=_entry.get("evals", 0),
                phase=_entry.get("phase"),
            )
