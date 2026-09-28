"""Delegation-family tools: Delegate/Wait/CancelDelegation/Reply/
FollowUp/Confer/RecallHistory.

Three objects, one per scope that used to be a level of closure nesting:

``DelegationTools``  the tools an orchestrating node is handed, bound to that
                     node (``self.node``).
``WorkerSession``    one dispatched delegation — the tools the worker itself is
                     handed, plus the thread body that runs it and records the
                     outcome.
``ConferTools``      Confer, bound to one sender. Identical for the
                     orchestrator and for every worker, which is why it is its
                     own object rather than a factory.

``build_delegation_closures(node)`` at the bottom is the registration table:
tool name -> bound method. Tools stay PascalCase methods so their docstrings
remain the model-facing description (``prompts.tool_catalog`` reads ``__doc__``)
and stay readable from source by the viewer's AST scan; ``inspect.signature``
drops ``self``, so the JSON schema the backends infer is unchanged.
"""
from __future__ import annotations

import re
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ....prompts.tool_catalog import tool_examples
from ..._constants import (
    backstop_enabled,
    budget_wrapup_message,
    delegate_cutoff_enabled,
    delegate_cutoff_multiple,
    run_backstop_multiple,
)
from ...notices import wrap_notice
from ...parsing import (
    _classify_response,
    _reconcile_delegation_evals,
    _stamped_eval_count,
)
from ._binding import with_doc
from ._decoding import decode_list_arg

# Roles whose delegations actually reach the ground-truth oracle and so are
# subject to the eval-ledger guards (raw-oracle nudge, unledgered bounce,
# off-ledger reconciliation). Role-based (not node-name-based) so it stays
# forward-compatible across node renames. The literature_reviewer (no oracle),
# datagenerator (legitimately builds/validates the oracle), and critic/
# strategizer are NOT evaluators and must be exempt — else they get bounced /
# nudged for work that never touches get_evaluator(). DebuggerAgent inherits
# role "implementer"; "debugger" is listed too for when it carries its own.
_LEDGER_GUARD_ROLES = frozenset({"implementer", "debugger"})

# MCP tool errors are infrastructure faults, not agent faults, and surface only
# as text in the worker's report — scanned for so they are recorded as system
# errors rather than counted against the agent.
_MCP_ERROR_PATTERNS = (
    r"(mcp__\w+__\w+)[^\n]*?(429|rate.?limit|timeout|timed.?out|unavailable|connection.?error)",
    r"(HTTP\s+(?:429|500|502|503))[^\n]*",
    r"(rate.?limit(?:ed|ing)?)[^\n]*",
)


# Forward-compatible delegation-target resolution. Agents repeatedly name a
# target by CAPABILITY rather than the exact graph node name — e.g.
# "pipeline"/"pipeline_executor" for the implementer (the "pipeline executor"),
# "data_generation" for the datagenerator — and bounce off "unknown target".
# Resolve in order: exact node name -> normalized name (case/separator-
# insensitive) -> normalized role. Resolution is by live node name or role
# only — there is NO hardcoded capability-synonym table. The strategizer prompt
# names targets by their hint/role, so it does not invent capability words like
# 'pipeline'/'oracle'; an unresolvable target returns None and the caller errors
# with the valid-target list (the agent then self-corrects). Resolving by role
# (not node name) keeps it forward-compatible across node renames.


def _norm_target(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def resolve_target(
    requested: str, outgoing: list[str], roles: dict[str, str]
) -> str | None:
    """Map a requested delegation target to a valid outgoing node name, or None.

    ``roles`` maps node name -> its configured role. Resolution is exact-name →
    normalized-name → normalized-role. Only a unique, confident match resolves;
    anything else returns None (caller errors)."""
    if requested in outgoing:
        return requested
    rn = _norm_target(requested)
    if not rn:
        return None
    for t in outgoing:  # normalized node name (case/separator-insensitive)
        if _norm_target(t) == rn:
            return t
    for t in outgoing:  # normalized role
        if _norm_target(roles.get(t, "")) == rn:
            return t
    return None


# A delegation's terminal registry statuses -- once one is reached, nothing
# will ever notify its `to_delegator` queue again, so a delegator blocked in
# SendMessage(wait_for_reply=True) on that delegation must wake on this too,
# not only on a reply arriving.
_TERMINAL_STATUSES = frozenset({"Done", "Errored", "Cancelled"})
_NO_SESSION_REPORT: Any = object()


def _peer_message_timeout() -> float:
    """How long SendMessage(wait_for_reply=True) waits, from config.

    A documented, validated runtime knob (``peer_message_wait_s``), not a
    magic number -- Elvis's standing rule is explicit config over env flags
    or hardcoded constants.
    """
    from ....runtime.settings import get_int as _get_int
    return _get_int("peer_message_wait_s", 300)


def _resolve_send_target(
    node: Any, to: str
) -> tuple[str, dict] | str:
    """Resolve a delegator's SendMessage ``to`` to (delegation_id, entry).

    Unlike Confer's ``target`` (delegation.py:279-296, which broadcasts to
    every LIVE delegation matching a bare role name), SendMessage requires
    an UNAMBIGUOUS single addressee (spec 12, design item 3's addressing
    rule) — a role name that currently matches more than one live
    delegation is an ERROR naming the candidates, never a guess.
    """
    with node._registry_lock:
        if to in node._registry:
            return to, node._registry[to]
        candidates = sorted(
            did for did, e in node._registry.items()
            if e.get("target") == to
            and e.get("status") in ("Working", "FollowUp", "OpenForReview")
        )
        if len(candidates) == 1:
            return candidates[0], node._registry[candidates[0]]
        if len(candidates) > 1:
            return (
                f"ERROR: {to!r} matches {len(candidates)} live delegations "
                f"({', '.join(candidates)}) — address one by its delegation "
                "id, not the role name."
            )
    return f"ERROR: no live delegation found for {to!r}."


def build_sandboxed_write(
    root: Path,
    *,
    strip_prefix: str | None = None,
    scope_label: str | None = None,
    study_workspace: Path | None = None,
) -> Any:
    """Build a ``Write`` closure hard-sandboxed to ``root``, PLUS the
    study's own ``workspace/`` when ``study_workspace`` is given.

    One implementation, two scopes: an orchestrating node's dispatched
    worker gets ``root`` set to that ONE delegation's ``{delegation_id}/``
    subfolder (``strip_prefix=delegation_id`` also absorbs the redundant
    ``{delegation_id}/…`` prefix agents naturally type, since the prompt
    calls it "your D### subfolder" even though the sandbox is already
    rooted there); a leaf reached via real graph routing (no delegation_id
    of its own) gets ``root`` set to its whole workspace instead, with no
    prefix to strip.

    ``study_workspace`` (``studies/<study>/workspace/``) is a SECOND
    permitted target, added because study problem statements routinely name
    deliverable paths there (e.g. "workspace/km_baseline.m") — a real,
    documented convention, not a mistake — and a worker had no sanctioned
    way to satisfy that instruction (run 20260926T214835: rejected by this
    guard, then written via Bash instead, which this guard cannot see at
    all). A bare relative path starting with ``workspace/`` (or exactly
    ``workspace``) is resolved against the STUDY root rather than the
    delegation root, matching how every problem statement writes it; an
    absolute path that already resolves under the study's workspace/ is
    accepted as-is either way.

    Both permitted roots reject, via ``Path.resolve()`` + ``relative_to``,
    any path that escapes them — collapsing '..' and symlinks so traversal
    is blocked at the tool level, not just the prompt. Nothing else changes:
    Bash stays trusted and unsandboxed (a deliberate, separate boundary, not
    this guard's job).
    """
    _root = root.resolve()
    _label = scope_label or f"the workspace ({_root})"
    _study_ws = study_workspace.resolve() if study_workspace is not None else None

    def _within(candidate: Path, base: Path) -> bool:
        try:
            candidate.relative_to(base)
            return True
        except ValueError:
            return False

    @tool_examples(
        "Write('fit_surrogate.py', body='import numpy as np\\n...')",
    )
    def Write(path: str, body: str) -> str:
        """Write a file. Restricted to your own delegation directory,
        plus the study's workspace/ when the task's deliverable belongs
        there (as stated in the task, e.g. 'workspace/model.m') — nowhere
        else."""
        _norm = (path or "").strip()
        if strip_prefix:
            _first, _sep, _rest = _norm.partition("/")
            if _first == strip_prefix and _rest:
                _norm = _rest
        _base = _root
        if (
            _study_ws is not None
            and not Path(_norm).is_absolute()
            and (_norm == "workspace" or _norm.startswith("workspace/"))
        ):
            _base = _study_ws.parent
        try:
            candidate = (_base / _norm).resolve()
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: invalid path {path!r}: {exc}"
        _allowed = [_root] + ([_study_ws] if _study_ws is not None else [])
        if not any(_within(candidate, r) for r in _allowed):
            _targets = _label if _study_ws is None else (
                f"{_label}, or the study's workspace/ ({_study_ws})"
            )
            return (
                f"ERROR: write rejected — path resolves to {candidate}. "
                f"You may write inside {_targets}. Nothing else."
            )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(body, encoding="utf-8")
        return f"Written: {candidate}"

    return Write


def build_report_evals(
    record: Any,
    drain: Any = None,
) -> Any:
    """Build a ``ReportEvals`` closure, one implementation for both scopes.

    ``record(count)`` writes the self-reported count wherever its caller's
    accounting expects it — a leaf's own per-turn dict, or one delegation's
    ``claimed_evals`` (reconciled against the provenance-stamped ledger
    before it is believed). ``drain``, when given, returns a text prefix
    from that scope's queued messages (a delegation's budget-warning
    queue, popped on the worker's next tool call) — a leaf reached via
    real graph routing has no such queue, so it is omitted there.
    """

    @tool_examples(
        'ReportEvals(25)',
        'ReportEvals(0)',
    )
    def ReportEvals(count: int) -> str:
        """Report the total ground-truth evaluations you performed this
        task. Call once per task, ALWAYS — even if 0. When you use
        get_evaluator() the canonical store is authoritative for the
        count, but this call also ARMS the unledgered-evals safety check,
        so never skip it."""
        n = int(count)
        record(n)
        prefix = drain() if drain is not None else ""
        return prefix + f"Recorded {n} evaluations."

    return ReportEvals


def build_recall_history(node: Any) -> Any:
    """Build the ``RecallHistory`` closure for ``node`` — the one
    implementation used whether ``node`` reaches it as an orchestrating
    node's declared tool or a leaf's default. Delegates to
    :class:`DelegationTools` so the entry-node guard and the str/int-n
    coercion (both regression fixes) are not reimplemented per node kind.
    """
    return DelegationTools(node).RecallHistory


class ConferTools:
    """Confer, bound to one sender node.

    Works identically for the orchestrating node, a worker, or a peer —
    ``sender_name`` is the only difference. Async: never blocks. A message is
    queued in the TARGET's inbox and delivered when the target next drains
    (orchestrator: each turn via _drain_notifications; worker: collect-on-send
    the next time IT calls Confer). Faithful port of the stashed Confer design.
    """

    def __init__(self, node: Any, sender_name: str) -> None:
        self.node = node
        self.sender_name = sender_name

    @tool_examples(
        "Confer('D004', 'Stop at 40 evals — the budget changed.')",
        "Confer('strategizer', 're #3: the surrogate R2 is 0.91')",
    )
    def Confer(self, target: str, message: str) -> str:
        """Send an async message to another node in the run.

        Returns immediately — neither side blocks. Use it to correct or
        steer a delegation that is ALREADY RUNNING, rather than waiting
        for a wrong result and re-delegating.

        target is a node name, a delegation id (D004 — address a
        specific delegation when two of one role are running), or the
        orchestrating node. A running delegation gets the message
        prefixed onto its next tool result; an idle node's message waits
        until that node itself Confers. The reply tells you which
        happened — read it, because "delivered" and "queued" are
        different outcomes.

        Reply by convention with Confer(sender_name, "re #N: <answer>").
        """
        node, sender_name = self.node, self.sender_name
        with node._registry_lock:
            # A delegation id is a legitimate address: when two
            # delegations of one role are running, the role name cannot
            # say which is meant, and the sender is reduced to
            # broadcasting "ignore this if you are D003".
            by_id = node._registry.get(target)
            live = [
                did for did, e in node._registry.items()
                if e.get("target") == target
                and e.get("status") in ("Working", "FollowUp")
            ]
            ever_woken = (
                target == node._name
                or by_id is not None
                or any(e.get("target") == target
                       for e in node._registry.values())
            )
        if by_id is not None:
            live = (
                [target]
                if by_id.get("status") in ("Working", "FollowUp")
                else []
            )
        if not ever_woken:
            return (
                f"ERROR: {target!r} is neither a node that has been "
                "delegated to this run nor a delegation id — cannot "
                "Confer with something that was never woken."
            )
        seq = node._next_confer_seq()
        envelope = (
            f"[Confer #{seq} from {sender_name} → {target}]: {message}\n"
            f"→ reply with Confer(\"{sender_name}\", \"re #{seq}: "
            "<answer>\")"
        )
        # Deliver on the path that actually reaches a BUSY worker: the
        # per-delegation queue whose contents are prefixed onto that
        # worker's next tool result (the same mechanism the budget and
        # backstop warnings ride). The name-keyed _confer_inbox alone is
        # drained only collect-on-send — i.e. only if the recipient
        # happens to call Confer itself — so a mid-flight correction to a
        # worker that never calls Confer was accepted, reported as
        # queued, and silently never delivered.
        if live:
            with node._pending_worker_msgs_lock:
                for did in live:
                    node._pending_worker_msgs.setdefault(
                        did, []).append(envelope)
        with node._confer_inbox_lock:
            if by_id is None:
                node._confer_inbox.setdefault(target, []).append(envelope)
            # Collect-on-send: drain any messages addressed to this sender
            # so replies arrive alongside the send confirmation.
            inbox = node._confer_inbox.pop(sender_name, [])
        inbox_text = ("\n\n".join(inbox) + "\n\n") if inbox else ""
        # Say which it was. "Queued" for an idle target and "delivered"
        # to a running one are different outcomes, and the sender's next
        # move depends on which happened.
        if live:
            where = (
                f"Delivered to {len(live)} running delegation"
                f"{'s' if len(live) != 1 else ''} of {target!r} "
                f"({', '.join(sorted(live))}); it appears on their next "
                "tool result."
            )
        else:
            where = (
                f"Queued for {target!r} (confer #{seq}) — no delegation "
                "of it is running right now, so it is delivered only if "
                "that node itself Confers later. Nothing is waiting on "
                "it; do not block."
            )
        return inbox_text + where


class WorkerSession:
    """One dispatched delegation: the worker's own tools and its thread body.

    Constructed by :meth:`DelegationTools.Delegate` once the delegation has an
    id and a task message, then run on its own daemon thread. Everything the
    run needs is an attribute here — what used to be twelve names captured from
    an enclosing scope.

    ``run`` is the thread body and is deliberately a sequence of named phases:
    bind the backend context, invoke (with one corrective retry), harvest what
    the invocation produced, then record the outcome. Any exception from any of
    them lands in :meth:`_finish_error`, so a worker crash is always recorded as
    a FAILED delegation rather than a lost thread.
    """

    def __init__(
        self,
        node: Any,
        *,
        worker: Any,
        delegation_id: str,
        target: str,
        intent: str,
        task_msg: str,
        hypothesis_ids: list[str],
        is_falsification_attempt: bool,
        phase: str | None,
        namespace: str | None,
        started_at: str,
    ) -> None:
        self.node = node
        self.worker = worker
        self.delegation_id = delegation_id
        self.target = target
        self.intent = intent
        self.task_msg = task_msg
        self.hypothesis_ids = hypothesis_ids
        self.is_falsification_attempt = is_falsification_attempt
        self.phase = phase
        self.namespace = namespace
        self.started_at = started_at
        # This delegation's OWN session id, handed back by each worker.invoke
        # from inside the adapter's lock (see ClaudeAdapter.invoke's
        # ``on_session_end``); _NO_SESSION_REPORT until an invoke reports one.
        self._own_session_id: Any = _NO_SESSION_REPORT
        # Per-invoke usage dicts handed back the same way, drained (summed and
        # cleared) each time this delegation records its usage; empty AND
        # never-reported means the adapter takes no callbacks.
        self._pending_usage: list[dict] = []
        self._usage_reported = False
        # Honour-system eval count, set by ReportEvals; reconciled against the
        # provenance-stamped ledger rows before it is believed.
        self.claimed_evals: int = 0
        # Set by _bind_backend_context, read by the report-retry and the
        # eval reconciliation.
        self.guard_agent: Any = None
        self.enforce_ledger: bool = False

    # ── The tools the WORKER itself is handed ────────────────────────────────

    def _drain_pending_msgs(self) -> str:
        """Pop and render this delegation's queued budget-warning messages."""
        node = self.node
        with node._pending_worker_msgs_lock:
            msgs = node._pending_worker_msgs.pop(self.delegation_id, [])
        return wrap_notice("\n".join(msgs))

    @tool_examples(
        "FollowUp('Is the 0.5 kg mass cap a hard constraint or a target?')",
    )
    def FollowUp(self, question: str) -> str:
        """Ask your delegating party one clarifying question before proceeding.

        Routes to whoever sent you this task: the agent that delegated
        to you.  One FollowUp per delegation.  The answer is injected
        directly into your context.  If no answer arrives, proceed with
        best judgment.
        """
        node, delegation_id = self.node, self.delegation_id
        with node._registry_lock:
            entry = node._registry.get(delegation_id, {})
            if entry.get("followup_count", 0) >= 1:
                return (
                    "FollowUp limit reached (1 per delegation). "
                    "Proceed with best judgment."
                )
            node._registry[delegation_id]["followup_question"] = question
            node._registry[delegation_id]["followup_count"] = 1
            node._registry[delegation_id]["status"] = "FollowUp"
            evt = node._registry[delegation_id]["followup_event"]
        with node._notifications_lock:
            node._notifications.append(
                f"[{delegation_id} FollowUp: {question!r} "
                f"→ call Reply('{delegation_id}', answer)]"
            )
        evt.wait(timeout=300)  # 5-minute patience; proceed if no reply
        with node._registry_lock:
            answer = node._registry[delegation_id].get("followup_answer")
            node._registry[delegation_id]["status"] = "Working"
        # Drain any queued budget warnings alongside the answer.
        with node._pending_worker_msgs_lock:
            msgs = node._pending_worker_msgs.pop(delegation_id, [])
        budget_prefix = wrap_notice("\n".join(msgs))
        base = answer or "No answer received. Proceed with best judgment."
        return budget_prefix + base

    def ReportProgress(self, note: str) -> str:
        """Leave a short progress note (<=200 chars) your delegator sees
        when it polls you. NON-BLOCKING — you keep working immediately;
        no answer comes back. Use it so the delegator can tell you are
        making progress rather than stuck (which prevents needless
        cancellation): e.g. 'LHS done, 250 evals; fitting GP next' or
        'BO round 3/10, best f=-0.81 so far'.
        """
        _n = (note or "").strip()[:200]
        with self.node._registry_lock:
            e = self.node._registry.get(self.delegation_id)
            if e is not None:
                e["progress_note"] = (_n, time.monotonic())
        return "Progress noted (your delegator will see it on poll)."

    def install_worker_tools(self) -> None:
        """Grant this delegation's worker its own per-delegation tools."""
        node, worker, target = self.node, self.worker, self.target
        worker.closure_tools["ReportEvals"] = build_report_evals(
            record=lambda n: setattr(self, "claimed_evals", n),
            drain=self._drain_pending_msgs,
        )  # never errors
        # ReportProgress/FollowUp(peer-facing)/Confer: the pre-spec-12
        # surface, retired in favour of SendMessage (already on the
        # worker's copied adapter from its own routing tools, see
        # build_routing_tools) whenever peer_interaction is on -- its
        # default now. Off is the old-contract ablation arm: install these
        # exactly as before so that arm stays byte-identical.
        from ....runtime import features as _features
        if not _features.enabled("peer_interaction"):
            worker.closure_tools["ReportProgress"] = self.ReportProgress
            worker.closure_tools["FollowUp"] = node._wrap_closure(
                self.FollowUp, target)
            # Confer: async messaging to the orchestrator (or any woken
            # peer). The worker drains its OWN inbox collect-on-send (see
            # ConferTools).
            worker.closure_tools["Confer"] = node._wrap_closure(
                ConferTools(node, target).Confer, target)
        # ConsultHandbook is injected universally at adapter construction
        # (agent_runtime._make_adapter) — every node gets it equally there.

    # ── The thread body ──────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the delegation to completion and record its outcome."""
        try:
            self._bind_backend_context()
            text = self._invoke_with_report_retry()
            self._record_oracle_nudges()
            usage = self._record_usage_once()
            self._flag_mcp_errors(text)
            evals, off_ledger, stamped = self._reconcile_evals()
            text = self._append_budget_report(text)
            # Spec 12 item 3: a non-error report does not finalize itself
            # when peer_interaction is on -- it moves to OPEN-FOR-REVIEW
            # and only an explicit SendMessage(..., approve=True) runs
            # _finish_ok. An ERRORED delegation (the except branch below)
            # has no report to review and stays terminal immediately
            # either way (edge 1).
            from ....runtime import features as _features
            if _features.enabled("peer_interaction"):
                self._open_for_review(text, evals, usage, off_ledger, stamped)
            else:
                self._finish_ok(text, evals, usage, off_ledger, stamped)
        except Exception:  # noqa: BLE001
            self._finish_error(traceback.format_exc())

    def _bind_backend_context(self) -> None:
        """Bind this worker thread's backend context before it is invoked.

        The backend reads these thread-locals when it builds the worker's
        session environment, so they must be set on the worker's OWN thread —
        which is this one.
        """
        from ....backends.base import (
            debug_enabled as _dbg,
        )
        from ....backends.base import (
            set_delegation_id as _set_did,
        )
        from ....backends.base import (
            set_namespace as _set_ns,
        )
        from ....backends.base import (
            set_oracle_registered as _set_oracle_reg,
        )
        from ....backends.base import (
            set_run_config_path as _set_rc,
        )
        from ....backends.base import (
            set_transcript_sink as _set_sink,
        )
        node = self.node

        # Bind the delegation id for this worker thread so the
        # backend can inject F3DASM_DELEGATION_ID into the session
        # env → get_evaluator() resolves without a mandatory cd
        # into D### (audit Finding 2).
        _set_did(self.delegation_id)

        # Scope this worker to its design namespace (Axis 3a/3b) so the
        # backend injects F3DASM_NAMESPACE → get_evaluator() resolves the
        # namespace's oracle + ledger. None → single-study canonical path.
        _set_ns(self.namespace or None)

        # Bind the run_config.json path too, so the backend injects
        # F3DASM_RUN_CONFIG → get_evaluator() resolves by explicit path
        # rather than walking up from the worker's cwd (study_dir),
        # which can never reach runs/<id>/debug/run_config.json.
        _notes_for_rc = node._current_notes_dir
        if _notes_for_rc is not None:
            _set_rc(str(_notes_for_rc.parent / "run_config.json"))

        # The eval-ledger guards apply ONLY to evaluator roles
        # (implementer/debugger) AND only once a canonical oracle is
        # registered. Non-evaluator roles (literature_reviewer,
        # datagenerator, critic) never reach get_evaluator(), so
        # nudging/bouncing them is a false positive. This one flag
        # gates all three guards below.
        self.guard_agent = (
            node._spec.nodes.get(self.target) if node._spec else None
        )
        _target_role = getattr(self.guard_agent, "role", None)
        self.enforce_ledger = (
            node._canonical_source_registered()
            and _target_role in _LEDGER_GUARD_ROLES
        )
        _set_oracle_reg(self.enforce_ledger)

        # DEBUG: stream this worker's full reasoning + tool-calls
        # to debug/transcripts/{delegation_id}.jsonl (thread-local;
        # run() is the worker's own thread).
        if _dbg() and node._current_notes_dir is not None:
            _set_sink(str(
                node._current_notes_dir.parent / "transcripts"
                / f"{self.delegation_id}.jsonl"))

    def _capture_invoke_result(
        self, session_id: str | None, usage: dict | None = None,
    ) -> None:
        self._own_session_id = session_id
        self._pending_usage.append(dict(usage or {}))
        self._usage_reported = True

    def _take_usage(self) -> dict:
        """This delegation's own token usage since it last recorded, summed
        over every worker.invoke it made (first attempt, report-retry, resume).

        Taken from the per-call reports, never from ``worker.last_usage``:
        that is shared adapter state a queued same-role delegation
        overwrites, and it holds only the LAST invoke anyway, so a report
        retry dropped the first attempt's spend. Falls back to
        ``last_usage`` only for an adapter that reports nothing.
        """
        if not self._usage_reported:
            return getattr(self.worker, "last_usage", {}) or {}
        total: dict = {}
        for u in self._pending_usage:
            for k, v in u.items():
                if k == "total_cost_usd":
                    if v is not None:
                        total[k] = (total.get(k) or 0) + v
                    else:
                        total.setdefault(k, None)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    total[k] = total.get(k, 0) + v
                else:
                    total[k] = v
        self._pending_usage = []
        return total

    def _record_usage_once(self) -> dict:
        """Take this delegation's usage and record it against the node."""
        usage = self._take_usage()
        self.node._record_usage(
            usage, role=self.node._role_of(self.target),
            model=getattr(self.worker, "model", None),
            phase="delegation", delegation_id=self.delegation_id)
        return usage

    def _worker_invoke(
        self, messages: list[dict], *, first: bool = False, **kw: Any,
    ) -> str:
        """worker.invoke with this delegation's per-call callbacks attached.

        ``on_session_end`` returns this call's session id from inside the
        adapter's lock; ``first`` additionally attaches ``on_session_start``
        (the queued->started patch). A worker whose invoke accepts neither
        (an older/custom stub) falls back to a plain call.
        """
        cbs: dict[str, Any] = {"on_session_end": self._capture_invoke_result}
        if first:
            cbs["on_session_start"] = self._mark_session_started_if_queued
        try:
            return self.worker.invoke(messages, **kw, **cbs)
        except TypeError:
            return self.worker.invoke(messages, **kw)

    def _mark_session_started_if_queued(self) -> None:
        """Patch in the real session-start time, once, for a delegation that
        was dispatched QUEUED (registry/log session_started_at is None).

        Passed to worker.invoke() as ``on_session_start`` -- the backend
        calls it the INSTANT its serializing lock is actually acquired, so
        this fires exactly when this delegation's real work begins, not
        when it merely asked to start. A worker whose adapter doesn't accept
        ``on_session_start`` (an older/custom stub) never calls this at all,
        which is safe: session_started_at then simply stays whatever
        dispatch set it to. No-op if this delegation wasn't queued
        (session_started_at already set at dispatch) -- never fires a
        redundant patch.
        """
        node = self.node
        with node._registry_lock:
            entry = node._registry.get(self.delegation_id)
            already_started = (
                entry is None
                or "session_started_at" not in entry
                or entry["session_started_at"] is not None
            )
            if not already_started:
                _now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
                entry["session_started_at"] = _now
        if not already_started and node._delegation_log is not None:
            node._delegation_log.mark_session_started(self.delegation_id, _now)

    def _invoke_with_report_retry(self) -> str:
        """Invoke the worker; one corrective retry if its report is malformed.

        Validated against THIS agent's declared report_sections (audit
        Finding 4 — report_sections is the single source of truth, not a
        hardcoded list), so e.g. a missing ### Retrospective earns one
        corrective retry.

        This retry is a FRESH worker.invoke() call — a new CLI session, the
        ORIGINAL task_msg still its bulk — so it is indistinguishable from a
        from-scratch restart of the delegation to anyone reading the run
        after the fact, UNLESS this fires the diagnostic below. Report 7
        (run 20260830T004106, Oscar): a stream that ends mid-tool with no
        ResultMessage (backends/claude.py's STREAM_ENDED_WITHOUT_RESULT)
        returns near-empty text that reads as malformed here, and this path
        used to retry with zero logging anywhere — silently discarding
        whatever the interrupted first attempt was doing (e.g. a background
        process it launched) with no trace in the run record.
        """
        from ....prompts.agent_prompts import build_report_retry_prompt

        messages = [{"role": "user", "content": self.task_msg}]
        text = self._worker_invoke(messages, first=True)

        _req_sections = list(
            getattr(self.guard_agent, "report_sections", None) or []
        ) or None
        diagnosis = _classify_response(text, _req_sections)
        if diagnosis is not None:
            self.node._record_intervention(
                "REPORT_RETRY", self.target,
                f"{self.delegation_id}: worker's first reply was classified "
                f"malformed and is being retried once with a corrective "
                f"prompt (same original task). Diagnosis: {diagnosis}",
                snippet=text[:300],
            )
            retry_messages = messages + [
                {"role": "ai", "content": text},
                {
                    "role": "user",
                    "content": (
                        f"{build_report_retry_prompt(_req_sections)}"
                        f"\n\nDiagnosis: {diagnosis}"
                    ),
                },
            ]
            text = self._worker_invoke(retry_messages)
        return text

    def _record_oracle_nudges(self) -> None:
        """Log any raw-oracle nudge firings from this delegation.

        Direct evidence, drained from the adapter's own budget.
        """
        _onb = getattr(self.worker, "_oracle_nudge", None)
        for _ev in list(getattr(_onb, "events", []) or []):
            self.node._record_intervention(
                "RAW_ORACLE_NUDGE", self.target,
                f"{self.delegation_id}: a {_ev.get('tool')} call "
                "reached the oracle directly; nudged toward "
                "get_evaluator().",
                snippet=_ev.get("snip", ""),
            )
        if _onb is not None:
            _onb.events = []

    def _flag_mcp_errors(self, text: str) -> None:
        """Record MCP tool errors named in the report as infrastructure faults.

        MCP errors appear as lines containing "error" near tool names in the
        report; they are the provider's fault, not the agent's, and
        _record_tool_error classifies them as such.
        """
        for _pat in _MCP_ERROR_PATTERNS:
            for _m in re.finditer(_pat, text, re.IGNORECASE):
                self.node._record_tool_error(
                    self.target,
                    _m.group(1) if _m.lastindex and _m.lastindex >= 1
                    else "mcp_tool",
                    "MCP_REPORTED",
                    _m.group(0)[:200],
                )
                break  # one log entry per pattern match per delegation

    def _run_experiment_root(self) -> Path | None:
        """The run's experiment_data root, holding every namespace's store."""
        _notes = self.node._current_notes_dir
        return (
            _notes.parent.parent / "experiment_data"
            if _notes is not None else None
        )

    def _reconcile_evals(self) -> tuple[int, bool, int]:
        """Believe the store, not the worker's self-report.

        Counts this delegation's provenance-stamped rows across EVERY
        experiment store; falls back to the honour-system count only when it
        wrote no rows anywhere. Reading the canonical store alone undercounts a
        namespaced delegation to 0 and falsely flags it off-ledger (run
        20260627T011059 D006/annular: 100 real evals logged as 0).

        Returns ``(evals, off_ledger, stamped)``.
        """
        return _reconcile_delegation_evals(
            self._run_experiment_root(),
            self.delegation_id,
            self.claimed_evals,
            self.enforce_ledger,
        )

    def _append_budget_report(self, text: str) -> str:
        """Append the completion constraint snapshot and per-eval KPI footer.

        The snapshot is ALWAYS appended (unlike the KPI footer, which only
        makes sense when this delegation actually wrote store rows), so every
        delegation's report is budget-aware — not just the ones that happened
        to evaluate something. Taken from the single source of truth
        (constraint_snapshot.py), shared with the RUNNING entry logged at
        dispatch and the terminal record below.

        Best-effort: a KPI footer must never fail a delegation.
        """
        from ....runtime.constraint_snapshot import snapshot_for_node
        text = text + "\n\n" + snapshot_for_node(self.node).as_text()

        _run_exp = self._run_experiment_root()
        try:
            from ....evaluation.ledger_summary import (
                RunStateSummary,
                experiment_stores,
            )
            _summary = None
            for _st in (experiment_stores(_run_exp)
                        if _run_exp is not None else []):
                _s = RunStateSummary.from_store(_st)
                if _s is not None and _s.n_per_delegation.get(
                        self.delegation_id, 0) > 0:
                    _summary = _s
                    break
            if _summary is not None:
                # Peak RAM this delegation reached (watcher high-water, free)
                # against the hard cap, so the strategizer learns the memory
                # footprint like it learns the time cost. (Wall/eval budget is
                # in the constraint snapshot above, not recomputed here.)
                from ....infra.watchdog_cleanup import delegation_peak_rss
                _footer = _summary.delegation_footer(
                    self.delegation_id,
                    peak_rss_bytes=delegation_peak_rss(self.delegation_id),
                    ram_cap_bytes=self._mem_cap_bytes(),
                )
                if _footer:
                    text = text + _footer
        except Exception:  # noqa: BLE001
            pass
        return text

    def _mem_cap_bytes(self) -> int | None:
        """The run's hard per-delegation memory cap, or None if unreadable."""
        _notes = self.node._current_notes_dir
        if _notes is None:
            return None
        try:
            import json as _json
            _cfgp = _notes.parent.parent / "debug" / "run_config.json"
            if _cfgp.exists():
                return _json.loads(_cfgp.read_text()).get("mem_cap_bytes")
        except Exception:  # noqa: BLE001
            return None
        return None

    def _commit_workspace(self, status: str) -> str | None:
        """This delegation's workspace commit, via the shared Node helper.

        One commit per delegation, whatever its outcome: a FAILED delegation's
        partial edits are exactly as worth inspecting as a successful one's.
        """
        node = self.node
        return node._commit_workspace(
            f"{self.delegation_id} {node._name} -> {self.target} [{status}]")

    def _open_for_review(
        self,
        text: str,
        evals: int,
        usage: dict,
        off_ledger: bool,
        stamped: int,
    ) -> None:
        """Hold this delegation open instead of finalizing it (spec 12
        item 3, ratified trigger: every non-error report, automatically,
        whenever ``peer_interaction`` is on -- no opt-in, no implicit
        close). ``_finish_ok`` does not run until an explicit
        ``SendMessage(to=<id>, message=..., approve=True)`` calls
        :meth:`finalize_after_review` below; anything else RESUMES the
        worker's session (:meth:`resume_and_revise`) with that message.

        Called on the FIRST report and on every REVISED one alike (the
        same path, not a special case): ``"waited"`` resets to False
        each time, since a fresh report -- original or revised -- must be
        delivered to the delegator again (through ``Wait`` or a
        ``Delegate(wait=True)`` result) before it can be approved or
        given feedback on (the "read" enforcement, spec item 4/design
        item 3's open question 3). If a SECOND message arrived in
        ``to_worker`` while this report was being (re)written -- the
        worker's session took it as a direct argument, never drained
        that queue -- it is still sitting there unread; noted to the
        delegator explicitly rather than lost silently.

        Everything ``_finish_ok`` will eventually need is stashed on the
        registry entry now, since this WorkerSession instance itself is
        also kept live in ``node._worker_sessions`` for exactly that
        later call.

        Durability, not just the in-memory registry: this ALSO writes an
        ``OPEN_FOR_REVIEW`` delegation-log row immediately (persist AT
        TRANSITION, not swept in at close) -- adda-boss-whopper's review
        of an earlier version of this mechanism found that sweeping only
        at ``_finalize_run`` missed the crash path entirely and could
        never see a watchdog kill at all (no in-process code runs on that
        exit), silently losing every open review on either path. The
        delegation log's own last-wins collapse means this row IS the
        honest final record if the process dies before approval — no
        close-time code required to make that true; ``finalize_after_review``
        below (via the unchanged ``_finish_ok``) simply appends the DONE
        row that supersedes it if and when approval actually happens.
        """
        node, delegation_id = self.node, self.delegation_id
        # This delegation's own id, captured inside the adapter's lock. NOT
        # worker.last_session_id: that is shared adapter state a queued
        # same-role delegation can overwrite once the lock is released.
        session_id = (
            self._own_session_id
            if self._own_session_id is not _NO_SESSION_REPORT
            else getattr(self.worker, "last_session_id", None)
        )
        with node._registry_lock:
            entry = node._registry[delegation_id]
            entry.update({
                "status": "OpenForReview",
                "result": text,
                "evals": evals,
                "usage": usage,
                "_review_off_ledger": off_ledger,
                "_review_stamped": stamped,
                "session_id": session_id,
                # Reset on EVERY (re-)open, original or revised: a fresh
                # report must be delivered to the delegator again (Wait,
                # Delegate(wait=True), or Wait(id, block=False) -- see
                # each of those three sites for where this flips back to
                # True) before it can be approved or given feedback on.
                # Reuses "waited" (the SAME meaning "waited" already has
                # for a collected Done/Errored delegation) rather than a
                # second field for the same concept.
                "waited": False,
            })
        with entry["worker_cond"]:
            unread_pending = bool(entry.get("to_worker"))
        if node._delegation_log is not None:
            from ....runtime.constraint_snapshot import snapshot_for_node
            node._delegation_log.record(
                id=delegation_id,
                from_node=node._name,
                to_node=self.target,
                task=self.intent,
                deliverable=text,
                hypothesis_ids=self.hypothesis_ids,
                started_at=self.started_at,
                completed_at=datetime.now(
                    tz=timezone.utc
                ).isoformat(timespec="seconds"),
                # Distinct from DONE/FAILED/RUNNING on purpose -- this is
                # a real report, but nobody has approved it yet.
                status="OPEN_FOR_REVIEW",
                tokens_in=(usage.get("input_tokens", 0) or 0),
                tokens_out=(usage.get("output_tokens", 0) or 0),
                cost_usd=usage.get("total_cost_usd"),
                is_falsification_attempt=bool(self.is_falsification_attempt),
                evals=evals,
                phase=self.phase,
                constraints=snapshot_for_node(node).as_dict(),
                # Not committed yet -- spec 12 item 8 moves the workspace
                # commit to AFTER approval; _finish_ok's own
                # _commit_workspace call (unchanged) provides it then.
                workspace_sha=None,
            )
        with node._notifications_lock:
            node._notifications.append(
                f"[Delegation {delegation_id} report ready for review -- "
                "SendMessage(id, ..., approve=True) to finalize it, or "
                "ask a question first]"
            )
            if unread_pending:
                node._notifications.append(
                    f"[Delegation {delegation_id} re-reported with an "
                    "UNREAD message still queued for it -- it may not "
                    "have seen your last SendMessage before revising; "
                    "check and resend if it still applies, not lost, "
                    "just not yet acted on]"
                )

    def finalize_after_review(
        self, evals: int, usage: dict, off_ledger: bool, stamped: int,
    ) -> None:
        """Run the SAME finalization ``_finish_ok`` always ran, just
        deferred until approval instead of running it the instant the
        report text existed. Called by ``SendMessage(..., approve=True)``
        via the registry entry's stashed values, never by the worker
        itself."""
        with self.node._registry_lock:
            text = self.node._registry[self.delegation_id].get("result", "")
        self._finish_ok(text, evals, usage, off_ledger, stamped)

    def resume_and_revise(self, message: str, sender_label: str) -> None:
        """Resume this delegation's worker session with a new message
        from its delegator (spec 12 item 3) and re-open for review with
        whatever it produces. Runs on its OWN thread -- the SendMessage
        call that triggered this already returned.

        Tries a REAL session resume first (``ClaudeAgentOptions(resume=
        session_id, fork_session=False)`` — the worker's own prior
        context, loaded fresh); falls back to a RECONSTRUCTED context
        (its original task + its own report only, never the intermediate
        transcript, which exists only when debug is on and would make
        fidelity silently depend on that flag) when resume is
        unavailable or fails, recording that fallback as a diagnostic
        every time — never silently.
        """
        node, delegation_id = self.node, self.delegation_id
        try:
            with node._registry_lock:
                entry = node._registry.get(delegation_id) or {}
                session_id = entry.get("session_id")
                prior_report = entry.get("result", "")
            note_body = (
                f"You got this message from {sender_label} -- the "
                "delegation needs clarification; take your time if "
                "needed.\n\n" + message
            )
            text = self._try_resume(wrap_notice(note_body), session_id)
            if text is None:
                rebuild_note = (
                    "Your original session could not be resumed, so your "
                    "context has been REBUILT from your original task "
                    "and your own report -- not from anything in "
                    "between. Everything you produced is still on disk "
                    f"in your delegation directory {delegation_id}/ -- "
                    "re-read those files rather than assume you "
                    "remember intermediate steps.\n\n" + note_body
                )
                text = self._fallback_reconstructed_invoke(
                    wrap_notice(rebuild_note), prior_report)
            self._record_oracle_nudges()
            usage = self._record_usage_once()
            self._flag_mcp_errors(text)
            evals, off_ledger, stamped = self._reconcile_evals()
            text = self._append_budget_report(text)
            self._open_for_review(text, evals, usage, off_ledger, stamped)
        except Exception:  # noqa: BLE001
            self._finish_error(traceback.format_exc())

    def _try_resume(
        self, wrapped_message: str, session_id: str | None,
    ) -> str | None:
        """A real session resume, or None (fallback needed) -- always
        recording WHY when it can't."""
        if not session_id:
            self._record_resume_fallback(
                "no session_id was recorded for this delegation", None)
            return None
        try:
            return self._worker_invoke(
                [{"role": "user", "content": wrapped_message}],
                resume=session_id)
        except Exception as exc:  # noqa: BLE001
            self._record_resume_fallback(str(exc), session_id)
            return None

    def _record_resume_fallback(self, reason: str, session_id) -> None:
        self.node._record_intervention(
            "REVIEW_RESUME_FALLBACK", self.target,
            f"{self.delegation_id}: session resume unavailable/failed "
            f"(session_id={session_id!r}) -- falling back to a "
            "reconstructed context (task + report only, not the "
            f"intermediate transcript). Reason: {reason}",
            delegation_id=self.delegation_id,
            session_id=session_id,
            reason=reason,
        )

    def _fallback_reconstructed_invoke(
        self, wrapped_message: str, prior_report: str,
    ) -> str:
        messages = [
            {"role": "user", "content": self.task_msg},
            {"role": "ai", "content": prior_report},
            {"role": "user", "content": wrapped_message},
        ]
        return self._worker_invoke(messages)

    def _finish_ok(
        self,
        text: str,
        evals: int,
        usage: dict,
        off_ledger: bool,
        stamped: int,
    ) -> None:
        """Record a completed delegation: registry, log, handoffs, retrospective."""
        node, delegation_id, target = self.node, self.delegation_id, self.target
        from ....runtime.constraint_snapshot import snapshot_for_node
        snapshot = snapshot_for_node(node)

        with node._registry_lock:
            _detached = (
                node._registry[delegation_id].get("status") == "Cancelled"
            )

        # A delegation must not become OBSERVABLE as finished before its
        # completion is DURABLE. The delegator polls Wait(block=False), which reads
        # the registry, and acts the moment it stops saying "Working" — in
        # particular HypothesisUpdate resolves triggered_by via
        # DelegationLog.last_completed_id(), which needs this delegation's
        # terminal row to already be on disk. Flipping the registry first left
        # a window in which the delegator could ask for a provenance link that
        # did not exist yet and silently receive None, permanently unlinking a
        # verdict from the evidence that produced it. The window used to be
        # microseconds and is now a git subprocess wide (spec 11), so write
        # the log first and publish the status second.
        workspace_sha = self._commit_workspace("DONE")
        if node._delegation_log is not None:
            node._delegation_log.record(
                id=delegation_id,
                from_node=node._name,
                to_node=target,
                task=self.intent,
                deliverable=text,
                hypothesis_ids=self.hypothesis_ids,
                started_at=self.started_at,
                completed_at=datetime.now(
                    tz=timezone.utc
                ).isoformat(timespec="seconds"),
                status="DONE",
                tokens_in=(usage.get("input_tokens", 0) or 0),
                tokens_out=(usage.get("output_tokens", 0) or 0),
                cost_usd=usage.get("total_cost_usd"),
                is_falsification_attempt=bool(self.is_falsification_attempt),
                evals=evals,
                phase=self.phase,
                constraints=snapshot.as_dict(),
                workspace_sha=workspace_sha,
            )

        with node._registry_lock:
            if _detached:
                # CancelDelegation detached this while it ran: keep it
                # Cancelled and DISCARD the deliverable. Usage was
                # already recorded above (the worker did spend tokens).
                node._registry[delegation_id]["evals"] = evals
            else:
                node._registry[delegation_id].update({
                    "status": "Done",
                    "result": text,
                    "evals": evals,
                    "usage": usage,
                })
                # This target made progress → clear its consecutive
                # error streak (the repeated-errors halt is for a
                # target stuck failing, not one that recovers).
                node._consecutive_errors[target] = 0
            # SendMessage (spec 12): wake a delegator blocked in
            # SendMessage(wait_for_reply=True) on THIS delegation -- once
            # Done/Cancelled, nothing will ever notify its `to_delegator`
            # queue again, so the delegator must not sit out the whole
            # timeout waiting on a question this now-finished worker will
            # never answer. Nested inside registry_lock (outer) -> cond
            # (inner), the same order used everywhere else this cond is
            # acquired.
            _parent = node._registry[delegation_id].get("parent", "entry")
            _cond = node._get_delegator_cond(_parent)
            with _cond:
                _cond.notify_all()
        with node._notifications_lock:
            node._notifications.append(
                f"[Delegation {delegation_id} "
                + ("completed after cancellation — result discarded]"
                   if _detached else "Done]")
            )
        # Loud, single record of the off-ledger condition — on the
        # cancel/detach path too (which the bounce never reaches), so a
        # cancelled delegation that evaluated off-ledger can no longer
        # vanish silently.
        #
        # Unledgered evals are a CORRECTIVE flag, not a re-run: re-running a
        # whole campaign to re-ledger is wasted wall-time (it helped blow the
        # watchdog in run 20260627T045747), and the critic's headline-
        # provenance check at the gate is the real floor. Cooperative agents
        # rarely bypass get_evaluator() on purpose; when they do, the tip
        # says so — once.
        if off_ledger:
            node._record_intervention(
                "OFF_LEDGER_EVALS", target,
                f"{delegation_id} claimed {self.claimed_evals} evals but none "
                "are provenance-stamped in any experiment store — counted "
                "as 0"
                + (" (delegation was cancelled/detached)"
                   if _detached else "")
                + ". These didn't go through get_evaluator(), so they "
                "cannot anchor a reproducible headline — that wasn't the "
                "right way to evaluate. If this delegation's numbers feed "
                "your conclusion, re-run it through get_evaluator(); and "
                "route evaluations through get_evaluator() from the start "
                "next time. (Not re-run for you: re-running a whole "
                "campaign to re-ledger wastes wall-time.)",
                claimed=self.claimed_evals,
                stamped=stamped,
                detached=_detached,
            )
        if node._delegation_log is not None and node._science_monitor is not None:
            try:
                node._science_monitor.on_delegation_complete(delegation_id)
            except Exception:  # noqa: BLE001
                pass

        self._register_authored_evaluator()

        # Worker retrospective (every node has a 'job done'
        # moment — see _record_retrospective).
        node._record_retrospective(
            node._role_of(target), delegation_id, text
        )

    def _register_authored_evaluator(self) -> None:
        """Point the canonical entrypoint at an oracle this delegation authored.

        When a datagenerator delegation authors an oracle, it drops a
        registration.json manifest in its workspace. Repoint the canonical
        entrypoint at it so the next get_evaluator() (re-reads config each
        call) resolves it — no manual config edit. Best-effort: never fail a
        delegation over registration.
        """
        node, delegation_id = self.node, self.delegation_id
        _notes = node._current_notes_dir
        try:
            _tgt = node._spec.nodes.get(self.target)
            if (
                _notes is None
                or getattr(_tgt, "role", None) != "datagenerator"
            ):
                return
            _run_dir = _notes.parent.parent
            _ws = (
                _run_dir / "debug" / "delegations"
                / delegation_id / "generators"
            )
            _manifest = _ws / "registration.json"
            if not _manifest.exists():
                return
            import json as _json

            from ....runtime.run_setup import register_evaluator_entrypoint
            _m = _json.loads(_manifest.read_text())
            _gf = _m["generator_file"]
            _gf_path = Path(_gf)
            if not _gf_path.is_absolute():
                # Resolve against the manifest dir, then the run dir;
                # take the first that exists.
                for _base in (_ws, _run_dir):
                    _cand = (_base / _gf).resolve()
                    if _cand.exists():
                        _gf_path = _cand
                        break
            # A datagenerator delegation scoped to a design namespace
            # registers that namespace's oracle (its own isolated store); the
            # manifest may also name one. The delegation's namespace is
            # authoritative.
            _ns = self.namespace or _m.get("namespace") or None
            # "I extended the file that is already canonical; do not repoint
            # anything." Without a way to SAY that, a delegation told to
            # extend the existing oracle faced three instructions it could not
            # jointly satisfy: its role contract makes the manifest mandatory,
            # dropping a manifest repoints the canonical entrypoint, and its
            # brief forbade repointing. The only escape was to encode the
            # intent in a path — name the already-canonical file by absolute
            # path so the repoint lands where it already pointed — which two
            # delegations had to invent independently. Stating the intent is
            # better than a path trick that happens to work.
            _in_place = bool(_m.get("extends_canonical"))
            if _in_place:
                _ep = _gf_path
            else:
                _ep = register_evaluator_entrypoint(
                    _run_dir / "debug" / "run_config.json",
                    _gf_path,
                    _m["attr"],
                    output_names=_m.get("output_names"),
                    namespace=_ns,
                )
            # Provenance is recorded either way: which delegation touched the
            # canonical source is part of the run record, and skipping the
            # REPOINT must not also skip the RECORD.
            with node._notifications_lock:
                _ns_tag = f" ns={_ns}" if _ns else ""
                _verb = "extended in place" if _in_place else "registered"
                node._notifications.append(
                    f"[Evaluator {_verb} by {delegation_id}: {_ep}{_ns_tag}]"
                )
        except Exception:  # noqa: BLE001
            pass

    def _finish_error(self, tb: str) -> None:
        """Record a delegation whose worker raised: registry + FAILED log row."""
        node, delegation_id, target = self.node, self.delegation_id, self.target
        usage = self._record_usage_once()

        # Durable before observable — the same ordering invariant as
        # _finish_ok. A poller watching Wait(block=False) leaves "Working" the
        # instant the registry flips, and a FAILED delegation is just as
        # citable as a DONE one.
        workspace_sha = self._commit_workspace("FAILED")
        if node._delegation_log is not None:
            from ....runtime.constraint_snapshot import snapshot_for_node
            node._delegation_log.record(
                id=delegation_id,
                from_node=node._name,
                to_node=target,
                task=self.intent,
                # Keep the TAIL: the root exception is on
                # the last line of a traceback.
                deliverable="ERROR: " + tb[-2000:],
                hypothesis_ids=self.hypothesis_ids,
                started_at=self.started_at,
                completed_at=datetime.now(
                    tz=timezone.utc
                ).isoformat(timespec="seconds"),
                status="FAILED",
                tokens_in=(usage.get("input_tokens", 0) or 0),
                tokens_out=(usage.get("output_tokens", 0) or 0),
                cost_usd=usage.get("total_cost_usd"),
                is_falsification_attempt=bool(self.is_falsification_attempt),
                phase=self.phase,
                constraints=snapshot_for_node(node).as_dict(),
                workspace_sha=workspace_sha,
            )

        with node._registry_lock:
            node._registry[delegation_id].update({
                "status": "Errored",
                "result": tb,
                "evals": self.claimed_evals,
                "usage": usage,
            })
            node._consecutive_errors[target] = (
                node._consecutive_errors.get(target, 0) + 1
            )
            # SendMessage (spec 12): see the matching comment in _finish_ok
            # -- a delegator blocked on this now-Errored delegation must
            # wake rather than sit out the full timeout.
            _parent = node._registry[delegation_id].get("parent", "entry")
            _cond = node._get_delegator_cond(_parent)
            with _cond:
                _cond.notify_all()
        with node._notifications_lock:
            node._notifications.append(
                f"[Delegation {delegation_id} Errored]"
            )


class DelegationTools:
    """The delegation-family tools, bound to one orchestrating node."""

    def __init__(self, node: Any) -> None:
        self.node = node

    # ── Per-node prose ───────────────────────────────────────────────────────

    def delegate_doc(self) -> str:
        """Delegate's model-facing description, with THIS node's targets."""
        node = self.node
        # Names only: what each agent does is in the team roster every agent
        # reads, so repeating the descriptions here said it twice.
        _targets = ", ".join(t for t in node._outgoing if t in node._spec.nodes)
        return (
            "Fire a task to a connected agent.\n\n"
            "CHOOSE THE MODE DELIBERATELY — neither is the default-good answer:\n"
            "  wait=False (async): returns a D### ID immediately and the worker\n"
            "    runs in the background. Multiple workers can then be alive at\n"
            "    once — which is the ONLY way Confer (live worker-to-worker\n"
            "    messaging) can do anything, and the only way the run's wall-clock\n"
            "    is the longest single chain rather than the sum of every\n"
            "    delegation. Collect them with a bare Wait() per worker — it\n"
            "    returns whichever finishes first, so a fan-out costs no polling.\n"
            "  wait=True (sync): blocks until the worker finishes and returns its\n"
            "    report directly, with zero polling. Simpler when this task must\n"
            "    fully finish before you can even decide the next one.\n"
            "  Ask yourself: could this run alongside other work, or might a peer\n"
            "  worker need to Confer with it mid-flight? If yes, async. If it is a\n"
            "  hard prerequisite for your very next decision, sync. Decide per\n"
            "  delegation; do not pick one mode reflexively for the whole run.\n\n"
            "CONTEXT PACKAGING: workers start each delegation with no memory of\n"
            "prior delegations. Include in the task message everything the worker\n"
            "needs: relevant paths, key findings from prior delegations, and the\n"
            "precise question to answer. You do NOT need to restate the\n"
            "hypotheses you name in hypothesis_ids — their statement, registered\n"
            "falsification_criterion and prediction are attached to the worker's\n"
            "task automatically, verbatim from the store. Spend the space on\n"
            "what the store does not already hold.\n\n"
            "hypothesis_ids must be non-empty when the ledger is active.\n"
            "The worker writes exclusively to {id}/ (relative to their workspace\n"
            "in debug/delegations/).\n\n"
            "Set is_falsification_attempt=True when this delegation attacks"
            " a hypothesis's stated falsification criterion.\n\n"
            "phase (optional): the f3dasm process stage this delegation advances —"
            " one of literature, doe, data_generation, ml, optimization, setup."
            " Tags the work's intent in the larger data-driven process; used by"
            " milestone gates, timing, and the critic.\n\n"
            "namespace (optional): open a NEW design parametrization as its own"
            " oracle + ledger. Leave it UNSET (the default) for the baseline study —"
            " that is most problems. Set namespace='some_name' only when the"
            " scientific question is a fundamentally different design REPRESENTATION"
            " (new variables / new geometry — e.g. 'elliptical_rings'): delegate a"
            " datagenerator with that namespace to build + register its oracle, then"
            " delegate implementers with the SAME namespace to evaluate in it. Each"
            " namespace keeps its own isolated ledger and the baseline is untouched;"
            " results compare across namespaces only insofar as they share the"
            " objective evaluator. A tool for creativity, not a requirement — open as"
            " many (or as few) as the science needs.\n\n"
            f"Available targets: {_targets}."
        )

    # ── Hypothesis plumbing ──────────────────────────────────────────────────

    def _hypothesis_brief(
        self,
        hypothesis_ids: list | None,
        is_falsification_attempt: bool,
    ) -> str:
        """The registered hypotheses this delegation is meant to test, for
        the WORKER's task message.

        A hypothesis's falsification_criterion is immutable once registered
        and is the standard its verdict will be judged against — but until
        now the only party shown it was the delegator, and only at
        reconciliation time (``_falsification_checkpoint`` below, which
        fires on a Done report). The worker that actually produces the
        evidence never saw it: Delegate's contract put context packaging on
        the delegator ("Include in the task message everything the worker
        needs"), so the criterion reached the worker only if the delegator
        remembered to paste it.

        The measured cost of that gap: INCONCLUSIVE is the largest verdict
        class (100 of 295 hypotheses over 52 cluster runs) and the most
        expensive (median lifetime 3.15h vs 1.49h FALSIFIED, 0.91h
        SUPPORTED), and its verdict comments say why in so many words —
        "the registered H3 falsification criterion required a 50-iter
        constrained BO in the high-Ixx region. This BO was never executed";
        "Test is INADEQUATE relative to the registered 30-point LHS
        criterion". The work that ran was not the test that was registered,
        and nothing could notice until it was time to render a verdict.

        Injected in-band and automatically, on the same principle as the
        constraint snapshot: pre-registration the experimenter cannot read
        is not pre-registration.
        """
        node = self.node
        if node._ledger is None or not hypothesis_ids:
            return ""
        blocks = []
        for hid in hypothesis_ids:
            entry = node._ledger.get(str(hid)) or {}
            if not entry:
                continue
            stmt = (entry.get("statement") or "").strip()
            crit = (entry.get("falsification_criterion") or "").strip()
            pred = (entry.get("prediction") or "").strip()
            if not (stmt or crit or pred):
                continue
            # The statement is context and is capped; the criterion and the
            # prediction are the CONTRACT and go verbatim — truncating the
            # test a result will be judged against would reintroduce exactly
            # the mismatch this block exists to prevent.
            if len(stmt) > 700:
                stmt = stmt[:700].rstrip() + " […]"
            part = [f"**{hid}** — {stmt}" if stmt else f"**{hid}**"]
            if crit:
                part.append(f"  · REGISTERED FALSIFICATION CRITERION: {crit}")
            if pred:
                part.append(f"  · REGISTERED PREDICTION: {pred}")
            blocks.append("\n".join(part))
        if not blocks:
            return ""
        head = (
            "<registered_hypothesis>\n"
            "This delegation is a FALSIFICATION ATTEMPT on the hypotheses "
            "below. Their criteria were registered BEFORE this work and are "
            "immutable: your evidence will be judged against them exactly as "
            "written, so the test you run must be the test they specify "
            "(sampling plan, eval count, region, thresholds). If you cannot "
            "run that test, or find it cannot decide the criterion, say so in "
            "your report — an honest mismatch is usable; a different test "
            "reported as if it were this one is not."
            if is_falsification_attempt else
            "<registered_hypothesis>\n"
            "Context — the registered hypotheses this task is filed against. "
            "Their criteria are immutable and are what any verdict will be "
            "judged against; treat them as the standard your numbers have to "
            "speak to."
        )
        return head + "\n\n" + "\n\n".join(blocks) + "\n</registered_hypothesis>"

    def _falsification_checkpoint(self, delegation_id: str) -> str:
        """Read-time ritual text for a freshly-read Done report.

        Forces the strategizer to classify whether this delegation was a
        falsification ATTEMPT of a registered hypothesis and, if so, link it
        and record the verdict against the hypothesis's pre-registered
        (immutable) prediction. Returns "" when there is nothing to reconcile.
        Fires once per delegation (sets reconciled=True) to avoid nagging.
        """
        node = self.node
        if node._ledger is None:
            return ""
        try:
            hyps = node._ledger.list_all()
        except Exception:  # noqa: BLE001
            return ""
        if not hyps:
            return ""

        def _pred(hid: str) -> str:
            e = node._ledger.get(hid) or {}
            return (
                e.get("prediction")
                or e.get("falsification_criterion")
                or "(no prediction on record)"
            )

        with node._registry_lock:
            entry = node._registry.get(delegation_id)
            if (
                entry is None
                or entry.get("status") != "Done"
                or entry.get("reconciled")
            ):
                return ""
            is_fals = bool(entry.get("is_falsification_attempt"))
            linked = list(entry.get("hypothesis_ids") or [])
            entry["reconciled"] = True  # fire once

        if is_fals and linked:
            tested = "; ".join(
                f"{h} (prediction: \"{_pred(h)}\")" for h in linked)
            return (
                f"⚖ FALSIFICATION CHECKPOINT — {delegation_id} was declared "
                f"a falsification attempt of {tested}. Record the VERDICT now "
                "via HypothesisUpdate(<id>, "
                "status=SUPPORTED|FALSIFIED|INCONCLUSIVE, "
                f"evidence={{'delegation': '{delegation_id}', "
                "'numbers': {…}}), judging THIS report against that "
                "pre-registered prediction. Do not move on with the "
                "hypothesis left OPEN."
            )
        open_h = [h for h in hyps if h.get("current_status") == "OPEN"]
        if not open_h:
            return ""  # nothing open to test → don't nag exploration
        listing = "; ".join(
            f"{h['id']} (prediction: \"{_pred(h['id'])}\")" for h in open_h)
        return (
            f"⚖ FALSIFICATION CHECKPOINT — was {delegation_id} an attempt to "
            "test a registered hypothesis's pre-registered prediction? Open: "
            f"{listing}. If YES: record the verdict with HypothesisUpdate "
            f"against that prediction, citing {delegation_id} as evidence with "
            f"falsification_attempt=True. Mark it ONLY if {delegation_id} "
            "genuinely tested "
            "that prediction — do not retrofit an exploratory result onto a "
            "hypothesis. If it was exploration/setup, there is no hypothesis "
            "to attach — continue (no action needed)."
        )

    # ── Delegate, and the dispatch steps it runs in order ────────────────────

    @tool_examples(
        "Delegate('implementer', 'Run a 50-pt Latin sweep of t/L in "
        "[0.02,0.20]; evaluate via get_evaluator(); report top-5 by "
        "buckling_load_norm + the results CSV path + feasible count.', "
        "'top-5 t/L, their values, CSV path, n feasible', "
        "hypothesis_ids=['H1','H2'])",
        "Delegate('implementer', 'Falsification probe: dense grid n=20 of "
        "t/L in [0.10,0.14]; does any point beat buckling_load_norm 1.47?', "
        "'best value in range + pass/fail', hypothesis_ids=['H1'], "
        "is_falsification_attempt=True, phase='optimization')",
    )
    def Delegate(
        self,
        target: str,
        intent: str,
        expected_report: str,
        hypothesis_ids: list | str | None = None,
        wait: bool = False,
        is_falsification_attempt: bool = False,
        phase: str | None = None,
        namespace: str | None = None,
    ) -> str:
        """Fire a task to a connected agent.

        hypothesis_ids should be a list, e.g. hypothesis_ids=['H1','H2'] — a
        string is accepted too (JSON/Python-repr/comma-joined/bare) and
        decoded the same way, defensively, for a model that emits one.

        Replaced per node by :meth:`delegate_doc`, which appends this node's
        own connected targets. This text is the fallback when a node is built
        without a graph spec (tests).
        """
        node = self.node
        review_refusal = self._check_open_reviews()
        if review_refusal is not None:
            return review_refusal
        cutoff_refusal = self._check_delegate_cutoff()
        if cutoff_refusal is not None:
            return cutoff_refusal
        resolved = self._resolve_target(target)
        if resolved is None:
            return (
                f"ERROR: unknown target {target!r}."
                f" Valid targets: {node._outgoing}"
            )
        target = resolved
        worker_template = node._worker_adapters.get(target)
        if worker_template is None:
            return (
                f"ERROR: no worker adapter for target {target!r}."
                f" Available: {list(node._worker_adapters)}"
            )

        if isinstance(wait, str):  # MCP string-in tools may pass "false"
            wait = wait.strip().lower() not in ("false", "0", "no", "")

        h_ids = _parse_hypothesis_ids(hypothesis_ids)
        refusal = self._check_hypothesis_links(h_ids)
        if refusal is not None:
            return refusal

        # Resolve the optional process-phase tag (DoE/DataGeneration/ML/…).
        # Unknown/None → None (soft; never refuses), stored as the canonical
        # value string for the log + critic flags + downstream grouping.
        from ....runtime.phases import resolve_phase
        _phase_obj = resolve_phase(phase)
        _phase = _phase_obj.value if _phase_obj is not None else None

        nudge = self._milestone_gate(target, namespace)
        if nudge is not None:
            return nudge

        delegation_id = self._allocate_delegation_id()
        started_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        # ClaudeAdapter/OpenAICompatibleAdapter.copy() returns self -- every
        # delegation to the SAME role shares the literal same adapter object,
        # serialized by that adapter's own _lock (invoke() acquires it for
        # the whole turn). A same-role delegation already Working WILL block
        # this one behind it the moment its thread calls worker.invoke(); this
        # is real serialization, not a race — surface it truthfully instead
        # of the prior "Delegation started" implying it runs right away (run
        # 20260928T141126: D003 dispatched at 14:14:38 while D002 -- same
        # role -- was still running; D003's session only began a second
        # after D002's ended, though Delegate()'s own reply and the RUNNING
        # log row both said D003 started at 14:14:38).
        queued_behind = self._same_role_running(target)
        self._register_dispatch(
            delegation_id, target, h_ids, is_falsification_attempt,
            _phase, namespace, started_at, queued_behind,
        )

        # Constraint snapshot NOW, at dispatch — single source of truth (see
        # constraint_snapshot.py) shared by the logged RUNNING entry below and
        # the banner prepended to the worker's own task message, so what gets
        # persisted and what the agent is shown can never drift apart the way
        # four independent, partial computations of this previously did.
        from ....runtime.constraint_snapshot import snapshot_for_node
        _snapshot = snapshot_for_node(node)

        # Provenance: log a RUNNING entry NOW, at dispatch — before the worker
        # runs and flushes ledger rows. If this delegation is cancelled or
        # killed mid-flight (wall/eval budget), its ledgered evals stay traceable
        # to a logged delegation instead of becoming orphan rows. The terminal
        # DONE/FAILED record (same id) supersedes this via last-wins collapse.
        if node._delegation_log is not None:
            node._delegation_log.record_started(
                id=delegation_id,
                from_node=node._name,
                to_node=target,
                task=intent,
                hypothesis_ids=h_ids,
                started_at=started_at,
                is_falsification_attempt=bool(is_falsification_attempt),
                phase=_phase,
                constraints=_snapshot.as_dict(),
                session_started_at=None if queued_behind else started_at,
            )

        task_msg = self._compose_task_message(
            delegation_id, target, intent, expected_report,
            h_ids, is_falsification_attempt, _snapshot,
        )

        # Each delegation gets its OWN adapter copy (D1/D2 concurrency fix).
        worker = (
            worker_template.copy() if hasattr(worker_template, "copy")
            else worker_template
        )
        self._sandbox_worker_writes(worker, delegation_id, target)

        session = WorkerSession(
            node,
            worker=worker,
            delegation_id=delegation_id,
            target=target,
            intent=intent,
            task_msg=task_msg,
            hypothesis_ids=h_ids,
            is_falsification_attempt=is_falsification_attempt,
            phase=_phase,
            namespace=namespace,
            started_at=started_at,
        )
        session.install_worker_tools()
        node._worker_sessions[delegation_id] = session

        t = threading.Thread(
            target=session.run, daemon=True, name=delegation_id)
        with node._registry_lock:
            node._threads[delegation_id] = t  # A3: inside lock
        t.start()

        # Reset two-shot Done() gate so the next Done() warns again.
        node._done_warned = False

        if wait:
            # Synchronous mode: block until the delegation finishes.
            t.join()
            with node._registry_lock:
                entry = dict(node._registry.get(delegation_id, {}))
            status = entry.get("status", "Errored")
            if status == "Done":
                cp = self._falsification_checkpoint(delegation_id)
                body = f"Done\n\n{entry['result']}"
                return body + (("\n\n" + cp) if cp else "")
            if status == "OpenForReview":
                # peer_interaction on: a successful sync Delegate() still
                # does not finalize on its own (spec 12 item 3) -- the
                # report is real and readable now, just not yet approved.
                # This delivery IS a "read" event (design item 3's open
                # question 3) -- mark it so approval/feedback isn't
                # refused as unread.
                with node._registry_lock:
                    _live = node._registry.get(delegation_id)
                    if _live is not None:
                        _live["waited"] = True
                return (
                    f"[{delegation_id}] report ready but OPEN FOR REVIEW "
                    f"-- {entry.get('result', '')}\n\nSendMessage("
                    f"{delegation_id!r}, ..., approve=True) to finalize "
                    "it, or ask a question first."
                )
            return f"Errored:\n{entry.get('result', '(no details)')}"

        if queued_behind:
            return (
                f"Delegation {delegation_id!r} QUEUED behind "
                f"{', '.join(queued_behind)} ({target!r} already has a "
                "delegation running — same-role delegations run one at a "
                "time, never in parallel). It will actually start once "
                f"that finishes. Collect it with Wait('{delegation_id}'), "
                f"or check on it with Wait('{delegation_id}', block=False)."
            )
        return (
            f"Delegation started. ID: {delegation_id!r}. "
            f"Collect it with Wait('{delegation_id}'), or check on it with "
            f"Wait('{delegation_id}', block=False)."
        )

    def _check_open_reviews(self) -> str | None:
        """Refuse a NEW delegation while ANY of this node's own
        delegations is OPEN-FOR-REVIEW (spec 12 item 6/design item 1) --
        a soft block, exactly like ``_check_delegate_cutoff`` below:
        nothing kills the run, the delegator is refused with an
        explanatory error naming every open review, not just the first.

        ERRORED delegations do NOT count (edge 1) -- they have no report
        to review and are already terminal; only OPEN-FOR-REVIEW (and
        ``Revising`` -- a review the delegator ALREADY sent a question
        on, mid-resume, still unresolved) does.
        """
        with self.node._registry_lock:
            open_ids = sorted(
                did for did, e in self.node._registry.items()
                if e.get("status") in ("OpenForReview", "Revising")
            )
        if not open_ids:
            return None
        return (
            "ERROR: you have report(s) open for review -- "
            f"{', '.join(open_ids)} -- resolve every one (SendMessage("
            "id, ..., approve=True) to finalize, or a question first) "
            "before starting a new Delegate."
        )

    def _check_delegate_cutoff(self) -> str | None:
        """Refuse a NEW delegation once elapsed time passes
        delegate_cutoff_multiple x the (soft) time budget, or None to
        proceed.

        Only NEW delegations are gated here — an in-flight one is untouched
        (this fires before a target is even resolved, so it never reaches
        anything that would register/cancel a delegation). Every other tool
        the strategizer needs to close a run (Wait, Done,
        WriteDeliverable, …) lives outside DelegationTools.Delegate and is
        unaffected, so the run always has a path to close.
        """
        node = self.node
        if not delegate_cutoff_enabled():
            return None
        budget, start = node._budget_seconds, node._run_start
        if budget is None or start is None:
            return None
        mult = delegate_cutoff_multiple()
        elapsed = time.time() - start
        if elapsed <= budget * mult:
            return None
        node._record_intervention(
            "DELEGATE_CUTOFF", "(refused)",
            f"new delegation refused past {mult:g}x time budget "
            f"({elapsed:.0f}s / {budget:.0f}s)",
        )
        return (
            f"ERROR: new delegations are refused past {mult:g}x the time "
            f"budget ({elapsed:.0f}s elapsed / {budget:.0f}s budget). This "
            "delegation was NOT started. Wrap up instead: Wait() on any "
            "delegation still in flight and read its report, then call "
            "Done() with what you have. Do not cancel a progressing "
            "delegation — its ledgered evals persist regardless."
        )

    def _resolve_target(self, target: str) -> str | None:
        """Resolve a requested target to an outgoing node name, or None."""
        node = self.node
        outgoing = node._outgoing
        resolved = resolve_target(
            target, outgoing,
            {t: getattr(node._spec.nodes.get(t), "role", "") for t in outgoing},
        )
        if resolved is not None and resolved != target:
            # Forward-compatible alias resolution: the agent named the target by
            # capability (e.g. 'pipeline_executor' -> 'implementer'). Proceed and
            # record it for observability instead of bouncing the agent.
            node._record_intervention(
                "TARGET_ALIAS", resolved,
                f"delegation target {target!r} resolved to {resolved!r}.",
            )
        return resolved

    def _check_hypothesis_links(self, h_ids: list[str]) -> str | None:
        """Enforce hypothesis linkage when the ledger is active; else None."""
        node = self.node
        if node._ledger is None:
            return None
        if not h_ids:
            # Defer this requirement while the process backlog is still open
            # (setup phase): you propose hypotheses AFTER engaging with the
            # problem and setting up the oracle, so early setup delegations
            # (oracle wrapping, literature review) have nothing to link to
            # yet. Once the backlog is cleared, every delegation must cite a
            # hypothesis. (Tied to the existing milestone backlog, not a
            # phase taxonomy the agent controls.)
            # DISABLED is not EMPTY. `_ms is None` means the milestone feature
            # is switched off, not that its backlog is cleared — but this read
            # it as "no pending milestones" and so demanded a hypothesis link
            # from the very first delegation, including the setup ones the
            # comment above exempts. Turning milestones off silently tightened
            # an unrelated gate: a change that would have shown up as a
            # milestone effect while belonging to neither feature.
            #
            # So the exemption takes its signal from the hypothesis ledger too
            # — before anything has been proposed there is nothing to cite,
            # whatever the milestone feature is doing. With milestones on this
            # is exactly today's behaviour (the backlog clause still decides);
            # with them off the rule keeps working instead of inverting.
            _ms = getattr(node, "_milestones", None)
            if _ms is not None:
                _setup_phase = bool(_ms.pending())
            else:
                # No milestone feature, so no backlog to read. Fall back to a
                # signal this rule owns: before any hypothesis exists there is
                # nothing to cite. It still CLOSES — the first proposal ends
                # the exemption for good — which is what makes it a phase and
                # not a permanent escape.
                _setup_phase = not node._ledger.list_all()
            if not _setup_phase:
                return (
                    "ERROR: hypothesis_ids must not be empty. "
                    "Every delegation must be linked to at "
                    "least one hypothesis. Call "
                    "HypothesisList() to see open hypotheses."
                )
            # backlog still open → permit this setup-phase delegation
            # without a hypothesis link.
        known = {h["id"] for h in node._ledger.list_all()}
        unknown = [h for h in h_ids if h not in known]
        if unknown:
            return (
                f"ERROR: unknown hypothesis IDs {unknown}."
                f" Valid IDs: "
                f"{sorted(known) or '(none proposed yet)'}."
            )
        return None

    def _milestone_gate(self, target: str, namespace: str | None) -> str | None:
        """The process backlog nudge, or None to proceed.

        Applies ONLY to delegations to the f3dasm implementer (the agent that
        runs experiments) — never the literature_reviewer/datagenerator that
        satisfy a milestone. Keyed on the resolved TARGET ROLE (reliable), not
        the agent's self-declared phase. This is a NUDGE, not a hard block: the
        milestones (assess-literature, oracle-ready, …) are good prompts, not a
        safety invariant, and a new design legitimately needs its own setup.
        Two-shot confirm, RECURRING PER NAMESPACE — nudge once per namespace,
        proceed on a re-delegate. Closing each milestone (MilestoneSet) remains
        the clean path.
        """
        node = self.node
        _ms = getattr(node, "_milestones", None)
        _target_role = getattr(
            node._spec.nodes.get(target), "role", "") if node._spec else ""
        if _ms is None or _target_role != "implementer":
            return None
        from ....epistemics.milestones import implementer_block
        _pend = implementer_block(_ms, node)
        if not _pend:
            return None
        _ns_key = namespace or "__default__"
        if not hasattr(node, "_milestone_ack"):
            node._milestone_ack = set()
        if _ns_key in node._milestone_ack:
            # confirmed (and re-nudges for each new namespace) → proceed
            return None
        node._milestone_ack.add(_ns_key)
        _ids = ", ".join(f"{m['id']} ({m['description'][:50]}…)"
                         for m in _pend)
        node._record_intervention(
            "MILESTONE_BLOCK", target,
            f"{len(_pend)} backlog item(s) precede the implementer")
        _scope = f"design '{namespace}'" if namespace else "this study"
        return (
            f"[CONFIRM] process backlog still open for {_scope}: "
            f"{_ids}. The usual path is to resolve each first — "
            "MilestoneSet(id, 'DONE', note=…), or MilestoneSet(id, "
            "'SKIPPED', note=…) if it doesn't apply. If you mean to run the "
            "implementer anyway, re-delegate (same target) to "
            "confirm. (Not a tool error; a process nudge.)"
        )

    def _allocate_delegation_id(self) -> str:
        """Allocate this delegation's id.

        Called AFTER the milestone gate so a blocked attempt does not BURN an
        id (next_id() advances a monotonic counter on every call; allocating
        before the gate left a permanent gap in the sequence, e.g. the
        milestone-blocked first implementer attempt always ate D002). Globally
        unique when a shared DelegationLog is present (multiple orchestrating
        nodes share one log); per-node counter otherwise.
        """
        node = self.node
        if node._delegation_log is None:
            with node._registry_lock:
                node._delegation_seq += 1
                return f"D{node._delegation_seq:03d}"
        delegation_id = node._delegation_log.next_id()
        # Keep per-node seq in sync so checkpoint paths that read
        # _delegation_seq stay consistent.
        with node._registry_lock:
            try:
                node._delegation_seq = int(delegation_id[1:])
            except (ValueError, IndexError):
                pass
        return delegation_id

    def _same_role_running(self, target: str) -> list[str]:
        """IDs of this node's OWN OTHER delegations to ``target`` that are
        still Working -- i.e. delegations a NEW dispatch to the same role
        will be serialized behind (see Delegate()'s call site for why)."""
        node = self.node
        with node._registry_lock:
            return [
                d_id for d_id, e in node._registry.items()
                if e.get("target") == target and e.get("status") == "Working"
            ]

    def _register_dispatch(
        self,
        delegation_id: str,
        target: str,
        h_ids: list[str],
        is_falsification_attempt: bool,
        phase: str | None,
        namespace: str | None,
        started_at: str,
        queued_behind: list[str] | None = None,
    ) -> None:
        """Open this delegation's registry entry."""
        from ....backends.base import get_delegation_id
        node = self.node
        # This delegation's DELEGATOR identity, resolved from the CALLING
        # thread's own thread-local delegation_id (SendMessage, spec 12) --
        # "entry" if the call came from the orchestrating node's own turn,
        # else the delegation_id of whichever worker thread is itself
        # delegating further (e.g. implementer/D001 dispatching math_expert
        # gives the new entry parent="D001", not just "implementer" --
        # D001 and a concurrent D002 of the same role share this Node
        # object but must never see each other's children's messages).
        parent = get_delegation_id() or "entry"
        with node._registry_lock:
            node._registry[delegation_id] = {
                "status": "Working",
                "result": None,
                "evals": 0,
                "start_time": time.monotonic(),
                "hypothesis_ids": h_ids,
                "is_falsification_attempt": bool(is_falsification_attempt),
                "phase": phase,
                "started_at": started_at,
                # None while genuinely QUEUED (see Delegate()'s call site);
                # set once this delegation's thread actually calls
                # worker.invoke() (_invoke_with_report_retry). Not queued at
                # dispatch: same as started_at, no wait to record.
                "session_started_at": None if queued_behind else started_at,
                "queued_behind": list(queued_behind or []),
                "target": target,
                "namespace": (namespace or None),
                "followup_question": None,
                "followup_answer": None,
                "followup_event": threading.Event(),
                "getstatus_count": 0,
                "followup_count": 0,
                # Read-time falsification ritual: flips True once the
                # checkpoint has been shown for this delegation's report
                # (fire-once anti-nag). The Done()-gate dangling check is
                # content-based and independent of this flag.
                "reconciled": False,
                # SendMessage (spec 12): this delegation's own delegator
                # identity (see the comment above), a FIFO queue of
                # messages FROM the delegator TO this worker and one the
                # other way, and a Condition the WORKER itself waits on for
                # its own inbound queue (the delegator side waits on the
                # AGGREGATE per-parent Condition instead --
                # node._get_delegator_cond(parent) -- since a delegator may
                # be collecting from several children at once).
                "parent": parent,
                "to_worker": deque(),
                "to_delegator": deque(),
                "worker_cond": threading.Condition(),
            }

    def _compose_task_message(
        self,
        delegation_id: str,
        target: str,
        intent: str,
        expected_report: str,
        hypothesis_ids: list | None,
        is_falsification_attempt: bool,
        snapshot: Any,
    ) -> str:
        """Build the worker's task message: constraints, edge preamble, brief."""
        node = self.node
        edge = node._spec.edge(node._name, target)
        preamble = edge.preamble if edge else ""
        task_msg = (
            f"<workspace_subfolder>{delegation_id}/</workspace_subfolder>\n\n"
            + intent
        )
        if expected_report:
            task_msg += (
                f"\n\n**Required deliverables / acceptance"
                f" criteria:**\n{expected_report}"
            )
        # The registered hypothesis this work is filed against, from the
        # ledger adda already holds — so the worker that produces the
        # evidence can see the criterion its evidence will be judged by,
        # instead of that criterion first surfacing at reconciliation time
        # when the work is already done. See _hypothesis_brief.
        _hyp_brief = self._hypothesis_brief(
            hypothesis_ids, is_falsification_attempt)
        if _hyp_brief:
            task_msg += "\n\n" + _hyp_brief

        if preamble:
            task_msg = preamble + "\n\n" + task_msg

        # Prepend the constraint snapshot so every worker starts budget-aware
        # (eval AND wall-clock, not wall-clock only) — automatically, in-band;
        # not something it has to go query for. Wait(block=False) handles mid-run
        # updates.
        return snapshot.as_text() + "\n\n" + task_msg

    def _sandbox_worker_writes(
        self, worker: Any, delegation_id: str, target: str
    ) -> None:
        """Confine this worker's Write to its own ``{delegation_id}/``.

        Also strips native "Write" from ``worker.native_tools``, mirroring
        ``Node._setup_sandboxed_write``'s existing pattern for a node
        reached via real graph routing. Without this, a role that declares
        "Write" (e.g. implementer.py) keeps BOTH the SDK's own native Write
        tool enabled AND this sandboxed closure registered under the same
        name -- the model sometimes calls the bare, still-enabled native
        one (unsandboxed, and apparently refused server-side regardless --
        observed: "not enabled in this context") before falling back to the
        MCP-qualified closure that actually works, real friction reported
        from a live run (retrospective D006, accidental_20260927T012131).
        """
        node = self.node
        if node._study_dir is None:
            return
        if hasattr(worker, "native_tools") and "Write" in worker.native_tools:
            worker.native_tools = [
                t for t in worker.native_tools if t != "Write"
            ]
        _workspace = (
            node._workspace_dir.resolve()
            if node._workspace_dir is not None
            else (Path(node._study_dir or ".") / "debug" / "delegations").resolve()
        )
        _delegation_ws = (_workspace / delegation_id).resolve()

        # Strip prefix absorbs a redundant leading "{delegation_id}/": the
        # sandbox is ALREADY rooted at {delegation_id}/, but the prompt calls
        # it "your D### subfolder", so agents naturally prefix paths with it
        # — which would nest D###/D###/ without this.
        Write = build_sandboxed_write(
            _delegation_ws,
            strip_prefix=delegation_id,
            scope_label=f"{delegation_id}/ ({_delegation_ws})",
            study_workspace=Path(node._study_dir) / "workspace",
        )
        worker.closure_tools["Write"] = node._wrap_closure(Write, target)

    # ── Polling, waiting, cancelling ─────────────────────────────────────────

    def _status(self, delegation_id: str) -> str:
        """Wait(delegation_id, block=False): one delegation's status, now.

        Returns one of:
          'Working (running for Xs, polled N times)' — still running
          'Done\\n\\n<full report>'                  — completed
          'Errored:\\n<traceback>'                   — failed
        """
        node = self.node
        prefix = node._drain_notifications()

        # Drain any budget warnings queued for this delegation.
        with node._pending_worker_msgs_lock:
            worker_msgs = node._pending_worker_msgs.pop(delegation_id, [])
        if worker_msgs:
            prefix += wrap_notice("\n".join(worker_msgs))

        with node._registry_lock:
            entry = node._registry.get(delegation_id)
            if entry is None:
                return self._status_from_log(delegation_id, prefix)
            status = entry["status"]
            if status in ("Working", "FollowUp"):
                # Increment poll count and record timing.
                entry["getstatus_count"] = entry.get("getstatus_count", 0) + 1
                poll = {
                    "count": entry["getstatus_count"],
                    "last": entry.get("last_getstatus_time"),
                    "start": entry["start_time"],
                    "prev_stamped": entry.get("last_stamped", 0),
                    "last_progress": entry.get(
                        "last_progress_time", entry["start_time"]),
                    "note": entry.get("progress_note"),
                }
                entry["last_getstatus_time"] = time.monotonic()

        # Status token FIRST (contract: callers dispatch on the
        # leading word); queued notifications follow the report.
        _tail = ("\n\n" + prefix.rstrip()) if prefix.strip() else ""
        if status == "Done":
            cp = self._falsification_checkpoint(delegation_id)
            body = f"Done\n\n{entry['result']}"
            if cp:
                body += "\n\n" + cp
            return body + _tail
        if status == "OpenForReview":
            # Same bug class as Delegate(wait=True)'s earlier misreport:
            # a successful-but-unapproved report is neither Working nor
            # an error -- reporting it as "Errored:" would be a false
            # negative on a real, readable report.
            # Delivering the full report text IS a "read" event (design
            # item 3's open question 3) -- mark it.
            entry["waited"] = True
            return (
                f"[{delegation_id}] report ready but OPEN FOR REVIEW -- "
                f"{entry['result']}"
            ) + _tail
        if status == "Revising":
            return (
                f"[{delegation_id}] resuming its session to revise its "
                "report -- not ready yet; check back shortly."
            ) + _tail
        if status not in ("Working", "FollowUp"):
            return f"Errored:\n{entry['result']}" + _tail
        if "session_started_at" in entry and entry["session_started_at"] is None:
            _behind = entry.get("queued_behind") or []
            return (
                f"QUEUED behind {', '.join(_behind) or 'another delegation'} "
                f"-- {entry.get('target')!r} runs one same-role delegation "
                "at a time; it has not actually started yet."
            ) + _tail
        return self._working_report(delegation_id, poll) + _tail

    def _status_from_log(self, delegation_id: str, prefix: str) -> str:
        """Registry cache miss: consult the authoritative delegation log.

        The in-memory registry can be rebuilt empty after a node
        reconstruction while the log retains every delegation — this is the
        "Known IDs: []" symptom (audit BF-0).
        """
        node = self.node
        _lstatus, _ldeliv = node._log_status(delegation_id)
        if _lstatus == "DONE":
            return prefix + f"Done\n\n{_ldeliv}"
        if _lstatus == "FAILED":
            return prefix + f"Errored:\n{_ldeliv}"
        if _lstatus == "RUNNING":
            return prefix + (
                "Working (still running; live progress is unavailable "
                "after a session rebuild — re-poll shortly and the "
                "result will appear here when it completes)"
            )
        return (
            prefix +
            f"ERROR: unknown delegation ID {delegation_id!r}. "
            f"Known IDs: {list(node._registry)}"
        )

    def _working_report(self, delegation_id: str, poll: dict) -> str:
        """The 'still Working' status line: real progress, then any nudges."""
        now_mono = time.monotonic()
        elapsed = int(now_mono - poll["start"])
        poll_count = poll["count"]

        progress_desc, cur_stamped = self._progress_description(
            delegation_id, poll, now_mono, elapsed)

        note_desc = ""
        if poll["note"]:
            _ntext, _nts = poll["note"]
            note_desc = (
                f" · worker note: {_ntext!r} ({int(now_mono - _nts)}s ago)")

        hints: list[str] = []
        last_poll = poll["last"]
        # Rate warning: polled too recently.
        if last_poll is not None and (now_mono - last_poll) < 30:
            hints.append(
                f"NOTE: you polled {delegation_id} only "
                f"{now_mono - last_poll:.0f}s ago. "
                "The worker runs in a background thread — polling faster "
                "does not make it finish sooner. Do other work in the "
                "meantime."
            )
        hints.extend(self._poll_escalation(
            delegation_id, poll_count, elapsed, cur_stamped, progress_desc))
        hints.extend(self._budget_broadcast(delegation_id))

        # Status token FIRST (documented contract: callers may
        # dispatch on the leading word); hints and queued
        # notifications follow.
        hint_str = (
            ("\n\n" + wrap_notice("\n".join(hints), trailing=""))
            if hints else ""
        )
        return (
            f"Working (running for {elapsed}s, polled {poll_count}× · "
            + progress_desc + note_desc + ")" + hint_str
        )

    def _progress_description(
        self, delegation_id: str, poll: dict, now_mono: float, elapsed: int
    ) -> tuple[str, int]:
        """Real ledger progress for a running delegation, plus its RSS.

        Surfaces real progress so the delegator can tell "progressing" from
        "stuck" instead of inferring it from wall-time (the blindness that
        drove over-cancelling). Also folds in backlog #6: zero stamped after a
        long wall-time IS the stuck signal. Counts the delegation's rows across
        EVERY experiment store (provenance-based) — else a campaign that wrote
        to its experiment's store polls as 0 progress and the stuck signal
        would over-cancel a healthy worker.
        """
        node = self.node
        _run_exp = (
            node._current_notes_dir.parent.parent / "experiment_data"
            if node._current_notes_dir is not None else None
        )
        cur_stamped = (
            _stamped_eval_count(_run_exp, delegation_id) if _run_exp else 0
        )
        prev_stamped = poll["prev_stamped"]
        delta = cur_stamped - prev_stamped
        last_progress = poll["last_progress"]
        if cur_stamped > prev_stamped:
            last_progress = now_mono
        with node._registry_lock:
            _e = node._registry.get(delegation_id)
            if _e is not None:
                _e["last_stamped"] = cur_stamped
                _e["last_progress_time"] = last_progress
        stale = int(now_mono - last_progress)
        if cur_stamped > 0 and delta > 0:
            progress_desc = (
                f"{cur_stamped} evals stamped (+{delta} since last poll) "
                "— progressing")
        elif cur_stamped > 0:
            progress_desc = (
                f"{cur_stamped} evals stamped, none new for {stale}s")
        else:
            progress_desc = f"0 evals stamped after {elapsed}s"
        # Per-delegation memory telemetry (resource-governance L3): surface this
        # delegation's process-tree RSS so the strategizer can SEE a fat campaign
        # and Confer the implementer. Best-effort; appended only if known.
        if _run_exp is not None:
            try:
                from ....infra.watchdog_cleanup import (
                    delegation_peak_rss,
                    delegation_rss,
                )
                _rss = delegation_rss(_run_exp.parent, delegation_id)
                if _rss > 0:
                    progress_desc += f"; ~{_rss / 1024 ** 2:.0f} MB RSS"
                    _peak = delegation_peak_rss(delegation_id)
                    if _peak > _rss:
                        progress_desc += f" (peak ~{_peak / 1024 ** 2:.0f} MB)"
            except Exception:  # noqa: BLE001
                pass
        return progress_desc, cur_stamped

    def _poll_escalation(
        self,
        delegation_id: str,
        poll_count: int,
        elapsed: int,
        cur_stamped: int,
        progress_desc: str,
    ) -> list[str]:
        """Nudges for an agent polling in a tight loop.

        Polling does NOT make the worker finish sooner, so from the first
        escalation we spell out the three real ways forward (same options as
        the premature-Done nudge) — so the agent never grinds out 30 status
        checks when it could just wait.
        """
        if poll_count < 5:
            return []
        firmness = "STOP polling in a tight loop. " if poll_count >= 15 else ""
        if cur_stamped > 0:
            # Demonstrably progressing — anchor the nudge on the numbers so
            # the agent doesn't cancel a healthy campaign out of impatience.
            return [
                f"{firmness}Polled {poll_count}× ({elapsed}s) — but "
                f"{delegation_id} IS progressing ({progress_desc}). Polling "
                "won't speed it up. Best move: (a) do other work now; or "
                "(b) call Wait() with NO argument — it blocks until "
                "whichever delegation finishes first and hands you its "
                "report, so several in flight need no polling at all. "
                "Do NOT cancel a "
                "progressing delegation to save time — its ledgered evals "
                "persist regardless, so cancelling only discards its report."
            ]
        # Zero stamped (backlog #6 stuck signal): cancelling is now a
        # defensible call, but only here.
        return [
            f"{firmness}Polled {poll_count}× ({elapsed}s) and "
            f"{progress_desc}. Options: (a) do other work; (b) call "
            "Wait() with NO argument — it blocks until whichever "
            "delegation finishes first, with zero polling — a worker "
            "may still be setting up before its first "
            "eval. A delegation that has stamped NOTHING for a long "
            "time may be genuinely stuck; the run watchdog will reclaim "
            "it."
        ]

    def _budget_broadcast(self, delegation_id: str) -> list[str]:
        """Broadcast a newly-crossed 10%-overbudget threshold, once.

        Returned for THIS delegation (folded into the strategizer's own
        Wait text — the strategizer polled, so it gets the
        strategizer-shaped message, e.g. "call Done()"); queued for every
        OTHER Working delegation, which gets the worker-shaped message
        instead (a worker cannot call Done() — see
        nodes/_constants.py:budget_wrapup_message). The two used to share
        one Done()-mentioning string that a worker had no way to act on.
        """
        node = self.node
        budget = node._budget_seconds
        run_start = node._run_start
        if budget is None or run_start is None:
            return []
        elapsed = time.time() - run_start
        pct = (elapsed / budget) * 100
        # Thresholds: 80, 90, 100, 110, 120, …
        threshold = int(pct // 10) * 10
        if threshold < 80:
            return []
        with node._pending_worker_msgs_lock:
            if threshold in node._budget_notified_pcts:
                return []
            node._budget_notified_pcts.add(threshold)
            if threshold >= 100:
                strategizer_msg = budget_wrapup_message(
                    elapsed, budget, can_call_done=True)
                worker_msg = budget_wrapup_message(
                    elapsed, budget, can_call_done=False)
                _backstop_mult = run_backstop_multiple()
                if backstop_enabled() and pct >= _backstop_mult * 100:
                    _bk = (
                        f" BACKSTOP IMMINENT: past the "
                        f"{int(_backstop_mult)}x cost backstop — the run "
                        "will be force-closed."
                    )
                    strategizer_msg += _bk
                    worker_msg += _bk
            else:
                strategizer_msg = worker_msg = (
                    f"BUDGET: {pct:.0f}% of time budget consumed. "
                    "Wrap up your current work and return a partial "
                    "report as soon as possible."
                )
            # Queue the worker-shaped message for all OTHER currently
            # Working delegations.
            with node._registry_lock:
                active = [
                    did for did, e in node._registry.items()
                    if e["status"] in ("Working", "FollowUp")
                    and did != delegation_id
                ]
            for did in active:
                node._pending_worker_msgs.setdefault(did, []).append(worker_msg)
        return [strategizer_msg]

    @tool_examples(
        "CancelDelegation('D004')",
    )
    def CancelDelegation(self, delegation_id: str) -> str:
        """Detach a delegation whose RESULT you no longer want.

        Cancel ONLY when the output is genuinely unwanted — a wrong approach, a
        superseded plan, a true dead-end. Do NOT cancel a delegation because it
        is slow: a Working delegation is almost always still producing real,
        ledgered evaluations (the worker runs in a background thread — slow is
        not stuck). Cancelling discards its REPORT, so its findings never reach
        your conclusion; its already-written store rows remain (and still
        count). If you just want to make progress meanwhile, do other work in
        parallel and let it finish. A delegation that has already produced
        ledgered evals is two-shot: call twice to confirm."""
        node = self.node
        with node._registry_lock:
            entry = node._registry.get(delegation_id)
            if entry is None:
                return (
                    f"No delegation {delegation_id!r}. "
                    f"Known: {list(node._registry)}"
                )
            st = entry.get("status")
            if st not in ("Working", "FollowUp"):
                return (
                    f"Delegation {delegation_id} is {st!r}, not "
                    "running — nothing to cancel."
                )
            # Harden against impatience: a delegation already writing ledgered
            # evals is progressing, not stuck. Require a deliberate second call
            # so a slow-but-healthy campaign can't be discarded on a whim. Count
            # across EVERY experiment store (provenance-based) — else a campaign
            # that wrote to its experiment's store reads 0 and loses this guard.
            _run_exp = (
                node._current_notes_dir.parent.parent / "experiment_data"
                if node._current_notes_dir is not None else None
            )
            _stamped = (
                _stamped_eval_count(_run_exp, delegation_id) if _run_exp else 0
            )
            if _stamped > 0 and not entry.get("cancel_pending"):
                entry["cancel_pending"] = True
                return (
                    f"HOLD: {delegation_id} has already written "
                    f"{_stamped} provenance-stamped evaluation(s) to the "
                    "canonical store — it is progressing, not stuck. "
                    "Cancelling discards its REPORT (its findings won't reach "
                    "your conclusion); the evals remain. If it is merely slow, "
                    "do other work in parallel and let it finish. If its result "
                    "is genuinely unwanted, call CancelDelegation('"
                    + delegation_id + "') again to confirm."
                )
            entry["status"] = "Cancelled"
        with node._notifications_lock:
            node._notifications.append(
                f"[Delegation {delegation_id} cancelled — detached; its "
                "report is discarded (its ledgered evals remain)]"
            )
        return (
            f"Delegation {delegation_id} cancelled (detached): "
            "excluded from the run, its result will be ignored. You may "
            "proceed (e.g. call Done() if nothing else is running) or start "
            "other work."
        )

    @tool_examples("Wait()", "Wait('D004')", "Wait('D004', block=False)")
    def Wait(self, delegation_id: str | None = None, block: bool = True) -> str:
        """Block until a delegation finishes (Done or Errored), then return its
        result — holds the current turn open with no extra turns consumed.

        OMIT delegation_id to wait for whichever delegation finishes FIRST.
        That is how you collect a fan-out: dispatch several with
        Delegate(wait=False), then call Wait() once per worker — each call
        hands back one finished delegation's report (labelled with its ID) and
        blocks only while nothing is ready. Naming an ID instead waits for that
        specific worker, which leaves any others finishing unread, so prefer
        the bare form whenever more than one delegation is in flight.

        block=False (with a delegation_id) → do not wait: return that
        delegation's status right now — 'Working (running for Xs, polled N
        times)', 'Done' + its full report, or 'Errored' + the traceback. Use it
        to read a delegation that is gone without reporting, or to check on
        one while you do other work; polling it faster does not make it finish
        sooner.

        Refuses when there is nothing to wait for, and refuses rather than
        hanging when waiting cannot make progress — every in-flight delegation
        parked on a FollowUp (answer it with Reply), or already gone without
        reporting (read it with block=False)."""
        if not block:
            if delegation_id is None:
                return ("ERROR: block=False reads ONE delegation's status — "
                        "pass its delegation_id.")
            return self._status(delegation_id)
        prefix = self.node._drain_notifications()
        if delegation_id is None:
            return self._wait_for_any(prefix)
        return self._wait_for_one(delegation_id, prefix)

    def _wait_for_any(self, prefix: str) -> str:
        """Wait for whichever delegation finishes first -- OR (spec 12,
        peer_interaction) a SendMessage question from any of THIS caller's
        own children, whichever arrives first.

        There is no join() across threads, so poll the registry: a short
        tick for responsiveness, draining notifications and monitor drift
        on the same ~10s cadence :meth:`_wait_for_one` uses. The tick is a
        Condition wait, not a bare sleep, so a SendMessage to one of this
        delegator's children wakes this loop immediately rather than
        waiting out the tick -- but nothing can ever notify that Condition
        unless SendMessage is actually called (the peer_interaction
        feature's own tool), so this is a no-op change in duration when
        that feature is off: identical to a bare sleep(_tick) with nothing
        to wake early.
        """
        from ....backends.base import get_delegation_id
        node = self.node
        my_identity = get_delegation_id() or "entry"
        cond = node._get_delegator_cond(my_identity)
        _tick, _n = 1.0, 0
        while True:
            with node._registry_lock:
                ready = [
                    (i, e) for i, e in node._registry.items()
                    # Cancelled is terminal but its result is explicitly
                    # excluded from the run, so it is never harvestable.
                    if e.get("status") in ("Done", "Errored")
                    and not e.get("waited")
                ]
                if ready:
                    did, entry = ready[0]
                    entry["waited"] = True
                    cp = entry.get("checkpoint", "")
                    body = (f"[{did}] {entry['status']}\n\n"
                            f"{entry.get('result', '')}")
                    return prefix + body + (("\n\n" + cp) if cp else "")
                # Every access to a `to_delegator` queue -- append (in
                # _send_upward), or check-and-pop (here) -- must share ONE
                # lock acquisition with its own check, under the SAME
                # per-parent-identity Condition (`cond`, already `my_identity`
                # 's own). Checking and popping as two separate acquisitions
                # (the previous shape) let a second consumer of this same
                # queue pop between them: a lost or double-consumed message,
                # or an IndexError on an empty deque. Lock order is
                # registry_lock (outer, already held) -> cond (inner) --
                # the same order used everywhere else this cond is touched,
                # so nesting it here cannot deadlock.
                with cond:
                    messaged = [
                        (i, e) for i, e in node._registry.items()
                        if e.get("parent") == my_identity
                        and e.get("to_delegator")
                    ]
                    if messaged:
                        did, entry = messaged[0]
                        sender_label, msg = entry["to_delegator"].popleft()
                        return prefix + (
                            f"[{did}] message from {sender_label}: {msg}")
                open_ids = sorted(
                    i for i, e in node._registry.items()
                    if e.get("status") in
                    ("Working", "FollowUp", "OpenForReview", "Revising")
                )
                # Classify what is actually still capable of finishing.
                # A blocking tool call ends no turn, so the run's time
                # backstop cannot fire while we are in here (same trap
                # ReadNote guards against) — this loop must therefore
                # never be able to wait on something that will never
                # arrive. A thread that has died without recording a
                # terminal status is exactly that: nothing else in the
                # runtime marks the registry on its behalf. OPEN-FOR-
                # REVIEW is the same shape as FollowUp here: nothing
                # finishes it but the delegator's OWN SendMessage.
                waitable, blocked, dead, reviewing = [], [], [], []
                for i in open_ids:
                    status = node._registry[i].get("status")
                    t = node._threads.get(i)
                    if status == "OpenForReview":
                        reviewing.append(i)
                    elif t is not None and not t.is_alive():
                        dead.append(i)
                    elif status == "FollowUp":
                        blocked.append(i)
                    else:
                        # No registered thread means we cannot prove it is
                        # gone; assume it is still coming.
                        waitable.append(i)
            if not open_ids:
                return prefix + (
                    "ERROR: nothing to wait for — no delegation is in "
                    "flight and every finished one has already been read. "
                    "Delegate(...) work first."
                )
            if not waitable:
                bits = []
                if reviewing:
                    bits.append(
                        "open for review "
                        f"({', '.join(reviewing)}) — call SendMessage(id, "
                        "..., approve=True) to finalize it, or ask a "
                        "question first"
                    )
                if blocked:
                    bits.append(
                        "parked on a FollowUp question "
                        f"({', '.join(blocked)}) — call Wait(id, block=False) to "
                        "read it, then Reply(id, answer) to unblock it"
                    )
                if dead:
                    bits.append(
                        f"no longer running but never reported "
                        f"({', '.join(dead)}) — call Wait(id, block=False) for its "
                        "state"
                    )
                return prefix + (
                    "ERROR: waiting cannot make progress; every in-flight "
                    "delegation is " + "; and ".join(bits) + "."
                )
            with cond:
                cond.wait(timeout=_tick)
            _n += 1
            if _n % 10 == 0:
                prefix += self._drain_while_waiting()

    def _wait_for_one(self, delegation_id: str, prefix: str) -> str:
        """Wait for one named delegation."""
        node = self.node
        with node._registry_lock:
            entry = node._registry.get(delegation_id)
            if entry is None:
                return prefix + (
                    f"ERROR: unknown delegation {delegation_id!r}. "
                    f"Known: {list(node._registry)}"
                )
            if entry["status"] in ("Done", "Errored"):
                entry["waited"] = True
                cp = entry.get("checkpoint", "")
                body = f"{entry['status']}\n\n{entry.get('result', '')}"
                return prefix + body + (("\n\n" + cp) if cp else "")
            t = node._threads.get(delegation_id)

        if t is not None:
            while t.is_alive():
                t.join(timeout=10.0)
                prefix += self._drain_while_waiting()

        with node._registry_lock:
            entry = node._registry.get(delegation_id, {})
            # Read once: a bare Wait() must not hand this same report back.
            if entry:
                entry["waited"] = True
        cp = entry.get("checkpoint", "")
        body = f"{entry.get('status', 'Unknown')}\n\n{entry.get('result', '')}"
        return prefix + body + (("\n\n" + cp) if cp else "")

    def _drain_while_waiting(self) -> str:
        """Notifications and science drift, drained mid-Wait.

        Wakes a strategizer that's asleep in Wait() for a live campaign —
        without this, a science-monitor nudge (e.g. DUPLICATE_EVALUATION) only
        surfaces on the NEXT tool call, by which point the whole delegation
        (and its eval budget) has already finished. Same drain() every other
        call site uses; each poll tick is a fresh check, not a repeat.
        """
        node = self.node
        out = ""
        with node._notifications_lock:
            _notifs = list(node._notifications)
            node._notifications.clear()
        # Marked, exactly as _drain_notifications marks the same messages
        # outside a Wait. These are adda speaking to the agent; emitting
        # them bare here made an identical notification render as the tool's
        # own output purely because it arrived DURING a Wait rather than
        # before one (see nodes/notices.py for why the marker, not a regex).
        if _notifs:
            out += wrap_notice("\n".join(_notifs))
        if node._science_monitor is not None:
            drift = node._science_monitor.drain()
            if drift:
                out += wrap_notice(drift)
        return out

    @tool_examples(
        "Reply('D004', answer='Yes — treat the mass cap as hard.')",
    )
    def Reply(self, delegation_id: str, answer: str) -> str:
        """Answer a worker's FollowUp question and unblock it.

        Call this after Wait returns 'FollowUp: <question>'.
        The answer is injected into the worker's context and it resumes.
        """
        node = self.node
        with node._registry_lock:
            entry = node._registry.get(delegation_id)
            if entry is None:
                return f"ERROR: unknown delegation {delegation_id!r}."
            if entry.get("status") != "FollowUp":
                return (
                    f"ERROR: delegation {delegation_id!r} is not awaiting a "
                    f"FollowUp (status: {entry.get('status')!r})."
                )
            entry["followup_answer"] = answer
            evt = entry["followup_event"]
        evt.set()
        return f"Reply sent to {delegation_id}. Worker resuming."

    # ── SendMessage: spec 12, peer_interaction feature (default OFF) ─────────
    # ONE shared, node-level closure -- exactly like Delegate/Wait -- used by
    # EVERY thread regardless of role: the entry node's own turn, a pure
    # worker (only incoming edges) messaging its delegator, and a node that
    # is BOTH a worker AND a delegator (e.g. implementer, which itself
    # delegates to math_expert) messaging in EITHER direction from the same
    # tool call. "My own identity" is resolved per CALL from the calling
    # thread's own thread-local delegation_id (never fixed at construction,
    # since one Node object is shared across concurrent same-role
    # delegations that are each their own separate identity -- D001 and a
    # concurrent D002 must never see each other's traffic).

    def _my_identity(self) -> str:
        from ....backends.base import get_delegation_id
        return get_delegation_id() or "entry"

    def _sender_label(self) -> str:
        my_id = self._my_identity()
        return (self.node._name if my_id == "entry"
                else f"{self.node._name} ({my_id})")

    @tool_examples(
        "SendMessage('D004', 'Which surrogate is that R2=0.91 from?')",
        "SendMessage('D004', 'Approved.', approve=True)",
        "SendMessage('strategizer', 'Stopping at 40 evals.', "
        "wait_for_reply=True)",
    )
    def SendMessage(self, to: str, message: str,
                     wait_for_reply: bool = False,
                     approve: bool = False) -> str:
        """Ask, answer, or approve — the one tool for peer and human
        messaging (spec 12). ``message`` is REQUIRED; empty is an error,
        never a silent no-op. ``to`` is a delegation id (unambiguous), your
        OWN delegator's name (if you are a worker — resolved to whoever
        actually delegated to you, not looked up), or another role name —
        but a role name matching more than one currently-running
        delegation of that role is an ERROR naming the candidates, never a
        guess: address the specific id instead. ``wait_for_reply=True``
        blocks and returns IN THIS SAME CALL — it wakes on ANY message from
        that peer, whichever arrives (their reply, or a fresh question of
        their own, labelled as what it is, not implied to answer this one),
        so two peers SendMessage-ing each other at once never both hang. If
        the peer ends (finishes/errors) before replying, you are told that
        instead of waiting out the full timeout. If nothing arrives before
        the timeout (config: ``peer_message_wait_s``, default 300s), your
        message is NOT dropped — it stays queued and its answer, once the
        pending-for-you mechanism lands, will surface in a later tool
        result; the timeout return says so rather than reading as "no
        reply". ``to="human"`` is offered only to the entry node — every
        other node has no direct path to a person and should route a
        human-worthy question through its own delegator instead.
        """
        msg = (message or "").strip()
        if not msg:
            return "ERROR: message is required and cannot be empty."
        node = self.node
        my_id = self._my_identity()

        if to == "human":
            if my_id != "entry" or getattr(
                    node._spec, "entry", None) != node._name:
                return (
                    "ERROR: only the entry node may SendMessage a human "
                    "directly — route this through your own delegator "
                    "instead.")
            return self.FollowUp(msg)

        # Downward: `to` names one of MY OWN children (I am delegating to
        # it, regardless of whether I am also myself a worker elsewhere).
        resolved = _resolve_send_target(node, to)
        if not isinstance(resolved, str):
            delegation_id, entry = resolved
            if entry.get("parent") == my_id:
                status = entry.get("status")
                if status == "OpenForReview":
                    return self._handle_review_message(
                        node, delegation_id, entry, msg, approve)
                if status == "Revising":
                    if approve:
                        # The report being approved would be the STALE
                        # one -- a newer one is mid-flight on its own
                        # thread right now. Refuse rather than finalize
                        # something that is about to be superseded.
                        return (
                            f"ERROR: {delegation_id!r} is revising its "
                            "report; approve once it re-opens for "
                            "review, not while it is still being "
                            "written."
                        )
                    # A second message while revising: still recorded
                    # for the worker (queued in to_worker) -- never
                    # silently dropped, and _open_for_review's re-open
                    # notice names it if the revision finishes before
                    # anything reads it.
                    return self._send_downward(
                        node, my_id, delegation_id, entry, msg,
                        wait_for_reply)
                return self._send_downward(
                    node, my_id, delegation_id, entry, msg, wait_for_reply)

        # Upward: I am a worker (my_id != "entry") with no matching CHILD
        # for `to`, so this must be addressed to my own delegator instead
        # -- resolved via my own entry's recorded parent, not by looking
        # `to` up a second time (a worker's delegator is never ambiguous).
        if my_id != "entry":
            with node._registry_lock:
                my_entry = node._registry.get(my_id)
            if my_entry is not None:
                return self._send_upward(
                    node, my_id, my_entry, msg, wait_for_reply)

        # Neither: `to` did not resolve to a child of mine, and I have no
        # delegator of my own to fall back to (I am the entry node).
        if isinstance(resolved, str):
            return resolved
        return f"ERROR: {to!r} is not one of your delegations."

    def _handle_review_message(
        self, node, delegation_id, entry, msg, approve,
    ) -> str:
        """SendMessage to an OPEN-FOR-REVIEW delegation (spec 12 item 3).

        ``approve=True`` finalizes it now -- ``_finish_ok`` runs, the
        workspace commits, the record becomes terminal. Anything else
        RESUMES the worker's own CLI session with this message as its
        next turn, on its OWN thread (this call returns immediately,
        exactly like an async ``Delegate`` -- the delegator collects the
        REVISED report the same way it collected the first one, via
        ``Wait`` or another ``SendMessage``).

        The "read" enforcement (spec item 4, design item 3's open
        question 3, ratified mechanical definition): feedback OR
        approval on a report never actually delivered to the delegator
        -- through ``Wait``, ``Wait(id, block=False)``, or a
        ``Delegate(wait=True)`` result, the three sites that flip
        ``"waited"`` back to True -- is refused. This is the guard
        against "approve without reading," not a formality: an LLM can
        be handed a report and never engage with it, so the mechanical
        signal is that the text was actually RETURNED to the delegator
        at least once since this report (this exact one, original or
        revised -- ``_open_for_review`` resets it on every re-open), not
        an unverifiable "did it think about it."
        """
        if not entry.get("waited"):
            return (
                f"ERROR: {delegation_id!r}'s report has not been "
                "delivered to you yet -- read it first with Wait(id) "
                "(or Delegate(wait=True)'s own result), then approve or "
                "give feedback."
            )
        if approve:
            session = node._worker_sessions.pop(delegation_id, None)
            if session is None:
                return (
                    f"ERROR: {delegation_id!r} has no live session to "
                    "finalize -- already finalized, or the run restarted."
                )
            with node._registry_lock:
                e = node._registry[delegation_id]
                evals = e.get("evals", 0)
                usage = e.get("usage", {}) or {}
                off_ledger = bool(e.get("_review_off_ledger"))
                stamped = e.get("_review_stamped", 0)
            session.finalize_after_review(evals, usage, off_ledger, stamped)
            return f"Approved. {delegation_id} finalized."

        session = node._worker_sessions.get(delegation_id)
        if session is None:
            return (
                f"ERROR: {delegation_id!r} has no live session to resume "
                "-- already finalized, or the run restarted."
            )
        sender_label = self._sender_label()
        with node._registry_lock:
            node._registry[delegation_id]["status"] = "Revising"
        t = threading.Thread(
            target=session.resume_and_revise, args=(msg, sender_label),
            daemon=True, name=f"{delegation_id}-resume")
        with node._registry_lock:
            node._threads[delegation_id] = t
        t.start()
        return (
            f"Delivered -- {delegation_id} is resuming its session to "
            "revise its report. Collect the revision the same way you "
            "collected the first one (Wait(id), or SendMessage again)."
        )

    def _send_downward(self, node, my_id, delegation_id, entry, msg,
                        wait_for_reply) -> str:
        sender_label = self._sender_label()
        with entry["worker_cond"]:
            entry["to_worker"].append((sender_label, msg))
            entry["worker_cond"].notify_all()
        if not wait_for_reply:
            return f"Delivered to {delegation_id}."
        timeout = _peer_message_timeout()
        cond = node._get_delegator_cond(my_id)
        # The predicate AND the pop happen under the SAME lock this queue's
        # other consumer (a bare Wait() draining `to_delegator` for the same
        # parent identity, see _wait_for_any) also uses — checking then
        # popping as two separate acquisitions let a second consumer pop
        # between them (a lost message, a double-consumed one, or an
        # IndexError on an empty deque). Also wakes on the TARGET reaching a
        # terminal status (_finish_ok/_finish_error notify this same cond)
        # so a delegator never sits out the full timeout waiting on a
        # question a dead worker will never answer.
        #
        # KNOWN GAP (deferred to the pending-for-you commit, design item
        # 10): `cond` here is keyed by MY identity, not by `delegation_id`
        # -- it is the SAME Condition every one of my other children's
        # SendMessage-upward calls notify too (any child's message wakes
        # whichever of my SendMessage/Wait calls is currently blocked on
        # this cond). The predicate above only looks at THIS ONE entry, so
        # a DIFFERENT child's message wakes this wait, finds the predicate
        # false, and goes straight back to sleep -- that child's message is
        # still safely queued in its own `to_delegator` (Wait() or a later
        # SendMessage will surface it), but I am not told about it NOW,
        # which is exactly what "no agent is ever left waiting unaware"
        # forbids. Fixing this needs the pending-for-you notice mechanism
        # itself (item 10) to report it, not a bigger predicate here.
        with cond:
            woke = cond.wait_for(
                lambda: bool(entry.get("to_delegator"))
                or entry.get("status") in _TERMINAL_STATUSES,
                timeout=timeout,
            )
            if woke and entry.get("to_delegator"):
                reply_sender, reply_msg = entry["to_delegator"].popleft()
                # Labelled as what it IS -- a message from that sender --
                # never as "the reply", since the deadlock guard means an
                # older unrelated message already queued there is returned
                # here too, and calling it a reply implies an answer to
                # THIS question that it may not be.
                return f"[{delegation_id}] message from {reply_sender}: {reply_msg}"
        if not woke:
            # NOT "no reply" -- that reads as dropped. It is still queued;
            # a later tool result will carry the answer once it lands (the
            # pending-for-you mechanism, design item 10, not yet built).
            return (
                f"No reply yet from {delegation_id} after {timeout}s — "
                "your message is still queued there, not dropped. Its "
                "answer, if any, will surface in a later tool result once "
                "the pending-for-you mechanism is built. Proceed with "
                "other work meanwhile if you can."
            )
        return (
            f"{delegation_id} ended ({entry.get('status')}) without "
            "replying to your message."
        )

    def _send_upward(self, node, my_id, my_entry, msg,
                      wait_for_reply) -> str:
        parent = my_entry.get("parent", "entry")
        sender_label = self._sender_label()
        cond = node._get_delegator_cond(parent)
        with cond:
            my_entry["to_delegator"].append((sender_label, msg))
            cond.notify_all()
        if not wait_for_reply:
            return "Delivered to your delegator."
        timeout = _peer_message_timeout()
        # See _send_downward's comment: predicate check + pop must share one
        # lock acquisition with any other consumer of `to_worker`.
        #
        # No peer-death wake here: the delegator side of a delegation has no
        # terminal "status" of its own to notice (only WORKERS — registry
        # entries — do), so a worker waiting upward can still sit out the
        # full timeout if its delegator never replies. Deferred, same as
        # the pending-for-you mechanism this whole timeout message points
        # at.
        with my_entry["worker_cond"]:
            woke = my_entry["worker_cond"].wait_for(
                lambda: bool(my_entry["to_worker"]), timeout=timeout)
            if woke:
                reply_sender, reply_msg = my_entry["to_worker"].popleft()
                return f"message from {reply_sender}: {reply_msg}"
        return (
            f"No reply yet from your delegator after {timeout}s — your "
            "message is still queued there, not dropped. Its answer, if "
            "any, will surface in a later tool result once the "
            "pending-for-you mechanism is built. Proceed with other work "
            "meanwhile if you can."
        )

    # ── Asking the human ─────────────────────────────────────────────────────

    @tool_examples(
        "FollowUp('Is the 0.5 kg mass cap a hard constraint or a target?')",
    )
    def FollowUp(self, question: str) -> str:
        """Ask your delegating party one clarifying question before proceeding.

        Routes to whoever sent you this task: the human operator if you are
        the entry node, or the agent that delegated to you if you are a worker.
        One FollowUp per delegation.  The answer is injected directly into
        your context.  If no answer is available, proceed with best judgment.
        Use it only for genuine briefing ambiguities (or a result so
        surprising it may signal a bug) — never for rhetorical/confirmatory
        questions or to replace your own reasoning.
        """
        node = self.node
        if node._ask_count >= node._max_ask:
            return (
                f"FollowUp limit reached ({node._max_ask} per run). "
                "Proceed autonomously with the information you have."
            )
        # _ask_count is incremented only once the question can actually be
        # put to someone (below). Counting it here spent the run's whole
        # quota on questions that were closed unattended microseconds later
        # and never displayed anywhere.
        # Two ways to reach a human, and the run takes whichever answers
        # first: a terminal (as before) and the viewer, which answers by
        # writing into the run's own debug/followups/ directory. The viewer
        # is a separate process and may not exist, so the channel is on
        # disk rather than in memory.
        from ....infra.operator_channel import (
            ask_question,
            close_question,
            is_watched,
        )

        _tty = bool(node._interactive) and _stdin_is_tty()
        _run_dir = node._current_run_dir
        if _run_dir is None:
            # Nowhere to publish the question, so nowhere an answer could
            # come from except a terminal.
            # No deadline is available on this path and there is no viewer
            # to answer, so it must not block: an unattended terminal would
            # otherwise hang the run forever, which is the exact failure the
            # isatty() guard was introduced to prevent.
            typed = self._typed_answer_now() if _tty else None
            return typed or _UNATTENDED

        qid = ask_question(_run_dir, node._name, question)
        if qid is None:
            # The channel could not be written — a full or read-only debug
            # dir. Degrade to the unattended answer rather than raising a
            # disk error out of a clarifying question.
            return _UNATTENDED

        # Waiting is only reasonable when somebody could actually answer.
        # With no terminal and nobody watching in the viewer, a wait is not
        # patience, it is a stall that spends the run's wall budget on a
        # question no one will ever see — so that case returns immediately,
        # exactly as it did before this channel existed.
        if not (_tty or is_watched(_run_dir)):
            close_question(_run_dir, qid, "unattended")
            return _UNATTENDED

        node._ask_count += 1
        if _tty:
            print(f"\n[Node {node._name}] {question}\nAnswer: ",
                  end="", flush=True)
        return self._await_operator_answer(_run_dir, qid, _tty)

    def _typed_answer_now(self) -> str | None:
        """A non-blocking read of anything already typed at the terminal."""
        import select as _select
        import sys as _sys
        try:
            if _select.select([_sys.stdin], [], [], 0)[0]:
                typed = _sys.stdin.readline()
                if typed.strip():
                    self.node._ask_count += 1
                    return typed.strip()
        except (OSError, ValueError):
            pass
        return None

    def _await_operator_answer(
        self, run_dir: Path, qid: str, tty: bool
    ) -> str:
        """Poll the terminal and the viewer together until one answers.

        Blocking on input() would make a terminal-attached run deaf to the
        viewer, which is the case the operator is most likely to be using.
        """
        import select as _select
        import sys as _sys

        from ....infra.operator_channel import (
            answer_question as _answer_q,
        )
        from ....infra.operator_channel import (
            close_question,
            is_watched,
        )
        from ....infra.operator_channel import (
            read_answer as _read_answer,
        )
        from ....runtime.settings import get_int as _get_int

        _deadline = time.monotonic() + _get_int("followup_wait_s", 600)
        while time.monotonic() < _deadline:
            typed_ready = False
            if tty:
                try:
                    typed_ready = bool(
                        _select.select([_sys.stdin], [], [], 0.5)[0])
                except (OSError, ValueError):
                    # A stdin that cannot be selected is not a stdin that can
                    # answer; stop treating it as one, and keep pacing from
                    # the sleep below rather than spinning.
                    tty = False
                    typed_ready = False
            if not tty:
                time.sleep(0.5)

            if typed_ready:
                try:
                    typed = _sys.stdin.readline()
                except (EOFError, KeyboardInterrupt, ValueError):
                    typed = ""
                if typed == "":
                    # EOF. select() reports a closed tty readable forever and
                    # readline() returns instantly, so continuing to poll it
                    # spins a core flat out for the whole deadline — measured
                    # at ~285k iterations/second. Nobody can type into a
                    # closed stdin, so stop watching it.
                    tty = False
                elif typed.strip():
                    # Only return a typed answer if it was actually recorded.
                    # Losing this race to the viewer means the record holds a
                    # DIFFERENT answer from the one handed to the agent.
                    if _answer_q(run_dir, qid, typed.strip()):
                        return typed.strip()
                    from_typed_race = _read_answer(run_dir, qid)
                    if from_typed_race:
                        return from_typed_race

            from_viewer = _read_answer(run_dir, qid)
            if from_viewer:
                if tty:
                    print(f"[answered in the viewer: {from_viewer}]", flush=True)
                return from_viewer

            # A watcher that goes away mid-wait ends the wait: the reason
            # for waiting was that someone was there.
            if not tty and not is_watched(run_dir):
                close_question(run_dir, qid, "unattended")
                return _UNATTENDED

        close_question(run_dir, qid, "timeout")
        return _UNATTENDED

    # ── Episodic memory ──────────────────────────────────────────────────────

    @tool_examples(
        'RecallHistory(n=3)',
    )
    def RecallHistory(self, n: int = 5) -> str:
        """Return the last n delegations received by this node as (task, deliverable) pairs.
        Call at the start of a delegation to recall prior work. Returns oldest-first."""
        node = self.node
        if node._delegation_log is None:
            return "No delegation log available."
        try:
            n = int(n)  # the model may pass "5"; query_received does [-n:]
        except (TypeError, ValueError):
            n = 5
        # The graph's entry/orchestrating node is ALWAYS the from_node on
        # every delegation, never the to_node — query_received filters on
        # to_node, so this is structurally, permanently empty for it (not
        # a transient "nothing happened yet" state). Granting the tool
        # here at all is a topology accident (a node holds these tools
        # because it has outgoing edges, entry or not); a plain "No prior
        # delegations found" reads as amnesia rather than as "wrong tool
        # for this role" (misdiagnosed as a context/turn-boundary memory
        # bug in run 20260718T132852's DONE retrospective — 7 delegations
        # and 102 evals already existed at the time).
        _entry = getattr(node._spec, "entry", None)
        if _entry is not None and node._name == _entry:
            return (
                "RecallHistory recalls delegations RECEIVED by this "
                "node — the entry/orchestrating node dispatches "
                "delegations, it never receives one, so this is always "
                "empty here (not a memory gap). Use QueryStore "
                "for evaluation history, or HypothesisList "
                "for the hypothesis ledger, instead."
            )
        records = node._delegation_log.query_received(node._name, n)
        if not records:
            return "No prior delegations found."
        parts = []
        for i, r in enumerate(records, 1):
            parts.append(
                f"== Prior delegation {i} ==\n"
                f"Task: {r['task']}\n\n"
                f"Deliverable:\n{r['deliverable']}"
            )
        return "\n\n---\n\n".join(parts)


_UNATTENDED = (
    "No operator is present to answer. Proceed autonomously "
    "using only information available in the task message "
    "and files in the study directory."
)


def _stdin_is_tty() -> bool:
    """Whether stdin is a terminal somebody could type an answer into.

    ``.isatty()`` on a CLOSED stream raises ValueError rather than returning
    False — reachable under supervisors that close fd 0 instead of reopening
    it on /dev/null.
    """
    import sys as _sys
    try:
        return bool(getattr(_sys.stdin, "isatty", lambda: False)())
    except (ValueError, OSError):
        return False


def _parse_hypothesis_ids(hypothesis_ids: list | str | None) -> list[str]:
    """Decode the several shapes an LLM passes hypothesis ids in.

    ``'["H1","H2"]'`` (JSON), ``"['H1','H2']"``/``"('H1','H2')"`` (Python
    repr/tuple), ``'H1, H2'`` (joined), ``'H1'``, or a real list. Delegates
    to :func:`decode_list_arg` in ``_decoding`` — the one decoder shared by
    every list-valued tool argument, not just this one.
    """
    if not isinstance(hypothesis_ids, str):
        return [str(h) for h in (hypothesis_ids or [])]
    return decode_list_arg(hypothesis_ids)


def build_delegation_closures(node) -> dict:
    """The delegation-family tools for one node, by registered name.

    Every value is a bound method of :class:`DelegationTools` — ``self`` is
    dropped by ``inspect.signature``, so the schema the backends infer and the
    catalog the prompt renders are exactly those of the method's own
    parameters and docstring.
    """
    t = DelegationTools(node)
    return {
        "Delegate": with_doc(t.Delegate, t.delegate_doc()),
        "Wait": t.Wait,
        "Reply": t.Reply,
        "FollowUp": t.FollowUp,
        "SendMessage": t.SendMessage,
        "RecallHistory": t.RecallHistory,
        "CancelDelegation": t.CancelDelegation,
        # One Confer for every node: the orchestrator's used to be a wrapper
        # whose only job was to drain notifications first, carrying a copy of
        # this docstring — two definitions of one prompt. The dispatch
        # wrapper drains for every tool now, so the wrapper had nothing left.
        "Confer": ConferTools(node, node._name).Confer,
    }
