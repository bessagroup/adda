# Spec 12 — Peer-interaction contract: Delegate / SendMessage / Wait

**Status:** ratified 2026-09-27 — build starting. **Priority:** high (touches the delegation
record format and every worker/delegator tool surface; sequencing with other
in-flight work matters). **Depends on:** nothing structurally, but see
Migration — it retires spec 02's design entirely (Confer/FollowUp/Reply are
superseded here, not extended).

Requested by Elvis (via adda-boss-whopper), 2026-09-27, alongside tasks A
(reproduction-gate ablation) and B (roster topology/tools) landed the same
day. This is the design's THIRD and final round with Elvis; the first two
(a general, unqualified `Wait` with no `wait_for_reply` flag; then a rename
to `WaitDelegation`) are superseded entirely by what follows — no need to
reconstruct them from git history, this document is the current design.

Task B's own review surfaced one fact worth carrying forward: `Reply`
(delegation.py:2167) is solely the answer-side of `FollowUp` (one blocking
question per delegation); `Confer` is a fully separate, unlimited,
async-both-directions protocol, replied to by calling `Confer` again. Two
coexisting question/answer protocols today is itself evidence for the
consolidation below.

## Open questions — RATIFIED by Elvis, 2026-09-27 (via adda-boss-whopper)

All four confirmed as recommended below. Kept as a numbered list for
reference from the design/tests sections; no longer open.

1. **Multiple delegations open for review at once.** Allowed — a
   delegator that fanned out several workers may have several reports
   open for review simultaneously, addressed in any order it chooses. No
   NEW `Delegate` call is permitted while ANY of them is still open (not
   just the first).
2. **A delegator that is itself a worker (nested delegation).**
   `implementer` delegates to `math_expert` in the 6-node
   lcp_matlab_regression graph (confirmed via task B's regenerated
   roster) — not hypothetical. Such a node reviews its own
   sub-delegation's report via `SendMessage` while it continues its own
   work (review is async from the delegator's side, item 6 below) — but
   it may NOT submit its OWN report upward to ITS delegator while a
   review IT OWES (as a delegator, downward) is still open. This forces
   nested reviews to resolve bottom-up: a middle node cannot pass a
   problem up the chain by reporting before it has itself dealt with an
   open review of the work underneath it.
3. **The "read" definition.** Design item 4 requires "the report was
   delivered through `Wait` or a delegation result" before feedback can be
   given. The mechanical definition — satisfied the instant `Wait()` (or
   an inline delegation result) RETURNS the report text, checkable in
   code, not "the delegator demonstrably engaged with it" (unverifiable —
   an LLM can be handed text and never engage).
4. **Interaction with `Wait`'s fan-out while a review is open.** `Wait`
   keeps collecting normally — a review being open does not change what
   `Wait` returns for OTHER, still-running delegations — but each such
   return is accompanied by a nudge listing the delegator's currently-open
   reviews (subsumed by, and now stated generally as, design item 11's
   "nothing pending goes unknown" rule below).

## Design rule — no agent is ever left waiting unaware (Elvis, ratified 2026-09-27)

**Stated as a principle, not a workaround for one case:** at minimum,
every agent must always KNOW what it currently owes — an open review
awaiting its response, a question awaiting its answer, a finished
delegation not yet collected, or nothing (in which case: silence, not a
notice for its own sake). Nudges are fine; an agent silently unaware that
something needs doing is not. See design item 11 for the concrete
mechanism this becomes (a pending-for-you notice on every tool result,
plus early-wake rules on every blocking call) — that item exists because
of this principle, not the other way around.

## Problem, in the current implementation's own terms

Evidence from the source, not memory:

- **`Confer`** (delegation.py:256-346): fully async, either direction, no
  limit, fire-and-forget with a delivered/queued distinction. Correct for
  steering a RUNNING delegation, but nothing STRUCTURALLY connects a
  Confer exchange to the delegation's eventual report — it is a side
  channel, not part of the record.
- **`FollowUp`/`Reply`** (delegation.py:410-444, 2167-2247): ONE blocking
  question per delegation, worker→delegator only, 300s timeout then
  proceed unattended (delegation.py:435). The entry node's OWN `FollowUp`
  (delegation.py:2193-2247) is a SEPARATE implementation reaching a human
  via `infra/operator_channel.py`'s disk-based `ask_question`/viewer
  channel or a TTY, capped at `node._max_ask` per run. Two different
  implementations of "ask one question," gated by whether the asker
  happens to be the entry node.
- **The report boundary is one-shot.** `_finish_ok`/`_finish_error`
  (delegation.py:746, 944) commit the workspace (`_commit_workspace`,
  spec 11's mechanism) and record the delegation as terminal in the SAME
  call that produces the report — there is no window in which the
  delegator can ask a question ABOUT the finished report before it is
  already committed and closed.
- **The idle-timeout carve-out already exists and already generalizes.**
  `_stream_with_idle_timeout`'s `classify` callback (backends/claude.py:171,
  the `_phase` closure at :639) suspends the idle window uncapped
  (`_wait = None`) for ANY pending tool call, not a FollowUp-specific
  carve-out — confirmed by reading `ainvoke`'s `_phase` closure, which
  classifies on `ToolUseBlock` presence generically. The new blocking
  report call (item 2 below) needs no change to this mechanism, only a
  test confirming it (report-7 regression coverage, item 6).

## Design (the agreed high-level shape — spec the details here)

### 1. `SendMessage(to, message, wait_for_reply=False, approve=False)` — asks, answers, and approves

One tool for all peer and human messaging, replacing `Confer`, `FollowUp`
(both implementations), and `Reply` outright — none of the four are
carried forward (see Migration).

- **`message` is REQUIRED** — empty (or `None`) is a hard ERROR, not a
  silent no-op.
- **`approve` defaults to `False`.** No auto-approvals: a delegator that
  never explicitly passes `approve=True` never closes a review it owes.
  Giving feedback (or approving) on a report the delegator has not yet
  received through `Wait` or a delegation result is a hard ERROR (see open
  question 3 for "received").
- **`wait_for_reply=True` blocks and returns the reply IN THE SAME CALL.**
  One tool call, one turn — the ergonomics Elvis wants (fewer round trips
  than a separate ask-tool plus a separate wait-tool). This is how a
  SYNCHRONOUS question is asked: there is no second tool needed and no
  mode-switching flag beyond this one.
- **Deadlock guard.** A `SendMessage(..., wait_for_reply=True)` call wakes
  on ANY message arriving from that same peer — the peer's reply to THIS
  message, or a question the peer sends the other way at the same time.
  Without this, two peers each blocked on `wait_for_reply=True` toward
  each other at once would both hang forever; with it, whichever message
  a peer sends next (an answer or a fresh question) always unblocks the
  waiter, and the conversation can proceed by either side responding to
  what it was actually handed.
- **Who may call it.** Every node with at least one edge (in either
  direction) may `SendMessage` any peer it is structurally connected to.
  `to="human"` is different: see item 2.

### 2. A human target is entry-node-only to SEND; every node can RECEIVE from a human

`SendMessage(to="human", wait_for_reply=..., ...)` is offered to — and
accepted from — **the entry node only**, stated by TOPOLOGY (the node
that is this run's entry point), not by a role name like "strategizer":
a graph whose entry node is some other role still gets exactly this
behavior for whichever node it actually is. This is the entry node's
path to a person, replacing its bespoke `FollowUp`
(delegation.py:2193-2247) but reusing its TRANSPORT unchanged:
`infra/operator_channel.py`'s disk-based `ask_question`/`close_question`/
`is_watched`, the viewer, and the TTY fallback (`_stdin_is_tty()`).
`node._max_ask` (the per-run cap) applies to `to="human"` calls
specifically.

**Every node can still RECEIVE a message from a human** — this is not new
machinery, it is today's "nudge a running delegation" path
(`node._pending_worker_msgs`, delegation.py:317-321, already reachable via
the viewer/operator channel regardless of which node is running) continuing
to work exactly as it does now. Every node's output is also viewable in the
served viewer regardless of whether it can itself address a human.

**A non-entry node with something that needs a human decision routes it
through its own delegator via `SendMessage`** — it has no direct path to
"human" itself. This keeps the human-facing surface small and centralized
(one place a run can interrupt a person) while every node still has SOME
path to escalate a human-worthy question: upward, through the chain of
delegators, to the entry node, which is the one node that can actually ask.

### 3. The delegation stays open until approval — by SESSION RESUMPTION, not a blocking tool call

Resolved ambiguity (an earlier draft of this item proposed a blocking
report call held open via `evt.wait(...)`, mirroring `FollowUp`;
superseded — Elvis's decision, with the reason below).

- **The report itself is unchanged**: the worker's final turn's plain
  text, validated by `_classify_response` against `report_sections`
  exactly as today. No new tool, nothing new for the agent to remember to
  call, no validation change.
- **On that final text, the runtime does NOT call `_finish_ok`.** The
  delegation moves to an OPEN-FOR-REVIEW state instead, recording the
  worker's CLI session id (`AssistantMessage.session_id` — already read
  in one diagnostic path today, `backends/claude.py:831`'s
  `cli_session_id=getattr(last_assistant, "session_id", None)`) and, for
  the `openai_compatible` backends (which have no server-side session to
  resume), its retained message history instead.
- **When the delegator `SendMessage`s that delegation** (a question, or
  `approve=True`), the runtime RESUMES THE SAME WORKER SESSION rather
  than holding anything open in the meantime: for Claude,
  `ClaudeAgentOptions(resume=session_id)` (confirmed present in the
  installed SDK — `resume: str | None`, "Session ID to resume. Loads the
  conversation history from the specified session"; `fork_session` stays
  `False` so review resumes the SAME session rather than branching);
  for `openai_compatible`, append to the retained history and re-invoke.
  The message arrives as the next user turn in the worker's own
  context, framed with an `<adda-note>` (the existing wrapping
  convention, `nodes/notices.py`): *"you got this message from
  `<delegator>` — the delegation needs clarification; take your time if
  needed."* The worker can redo work, steer, and its new final text
  becomes the REVISED report, validated again exactly as the first was.
  Repeats until `approve=True`; only then does `_finish_ok` run, spec
  11's workspace commit fires, and the record becomes terminal.
- **Why resumption instead of a held-open blocking call:** a review can
  take arbitrarily long — the delegator may be fanning out several other
  workers first — and holding a live CLI stream open that long is exactly
  where report 7 and the class of idle-timeout failures it named live
  (see item 7's note on scope). With resumption, nothing is held open
  between review turns, so the review cannot be killed by any stream or
  idle timeout, however long the delegator takes to get to it.
- **Fallback, never silent.** If session resume is unavailable or fails
  (the SDK errors, the session expired, ...), fall back to re-invoking
  with the worker's own recorded history plus the new message — and
  record that fallback as a diagnostic event (the established
  `_record_stream_diagnostic` pattern, `backends/claude.py`), never
  silently. A resumed-vs-reconstructed session is a fact worth knowing
  when reading a run after the fact, the same reasoning that motivated
  recording `CONTEXT_COMPACTED`/`STREAM_ENDED_WITHOUT_RESULT` earlier
  this session.
- On report delivery (the delegator's side, via `Wait`), the report text
  still carries an `<adda-note>` reading approximately: *"analyse this
  thoroughly and ask about anything unclear; take advantage of this
  moment, because you might not be able to wake this node again"* — now
  literally true in a stronger sense than the earlier blocking-call draft
  claimed: the worker's ORIGINAL CLI process really has exited between
  review turns; "waking it" means resuming its session, not just calling
  back into an already-suspended one.

**Trigger, ratified explicitly (Elvis, via adda-boss-whopper, 2026-09-27,
after the core mechanism above was reviewed in code):** review is
EVERY delegation, automatically, whenever `peer_interaction` is ON — no
opt-in flag on `Delegate`, and no "only if the delegator happens to
`SendMessage` it." An opt-in, or an implicit close when the delegator
never writes back, would let a review be silently skipped, which is
exactly what "no auto-approvals" and "acceptance is never hidden or
implicit" (item 6) already rule out. So: every worker's non-error final
report moves its delegation to OPEN-FOR-REVIEW; it becomes terminal
(`_finish_ok`, spec 11's commit) ONLY via an explicit
`SendMessage(to=<id>, message=..., approve=True)`. Any other message to
an open review resumes the worker in its own session instead of
finalizing. With the feature OFF, today's behavior (immediate
`_finish_ok`) is unchanged.

**Edge 1 — an ERRORED delegation has no report to review.** A worker that
raises (`_finish_error`) produces no final report text, so there is
nothing to hold open for review — it is terminal immediately, exactly as
today, feature on or off. It must NOT count toward item 6's "any review
open" block on the delegator's next `Delegate` call; only OPEN-FOR-REVIEW
entries do.

**Edge 2 — the run ends while reviews are still open.** `Done()`, the
watchdog, or a budget/backstop cutoff can close a run while one or more
delegations are still OPEN-FOR-REVIEW. These must be recorded HONESTLY —
"open, never approved" — in both the delegation log (a distinct terminal
status, not `DONE`) and the closing retrospective. Never silently
recorded as approved, and never silently dropped: an unreviewed report
that the run simply ran out of time to look at is a real fact about that
run, not a bookkeeping inconvenience to paper over.

### 4. `Delegate` and `Wait` are for delegators only

Both exist ONLY for a node with >=1 outgoing edge — the node that started
the work is the only one entitled to collect it. `Wait` has exactly one
job under this design: collecting FINISHED delegations, including
fan-out (whichever finishes first, `Wait()` with no id) — it needs no
qualifier in its name because it no longer does double duty as a generic
"something arrived" primitive (that ambiguity existed in the prior,
superseded draft; `SendMessage(..., wait_for_reply=True)` now owns all
peer-reply waiting, leaving `Wait` exactly the tool it already is today).

**Interaction with task B (this session, already shipped as `f613427`):**
that commit gated `Wait` on having an edge in EITHER direction (so a pure
worker with only incoming edges could still block on "something addressed
to me"). Under THIS design, that broader gating is no longer the right
shape — a pure worker never calls `Wait` (it uses `SendMessage(...,
wait_for_reply=True)` to ask, and its own report call to be asked). When
spec 12 is actually built, `f613427`'s gating should be TIGHTENED BACK to
outgoing-edges-only for `Wait`. Not changed now — this spec is not being
built yet — but noted here so it is not missed later, and so nobody reads
`f613427`'s current "any edge" rule as still the target state.

### 5. Reply is retired, together with Confer, FollowUp, and ReportProgress

All four are the same family under this design: `Reply` and `FollowUp`
fold into `SendMessage`'s ask/answer symmetry (item 1); `Confer` folds
into `SendMessage`'s always-available async messaging; `ReportProgress`'s
non-blocking "leave a note" use case is a `SendMessage` with
`wait_for_reply=False` (the default) and no expectation of a reply, so it
needs no survivor of its own. None of the four exist after this spec is
built — see Migration for the full sweep.

### 6. Soft resolution under the cooperative principle (unchanged from the prior draft)

While a delegation is open for review, the delegator is nudged
continuously (the SAME mechanism budget/backstop warnings already use —
the per-delegation queue prefixed onto a caller's next tool result,
`node._pending_worker_msgs`, delegation.py:317-321 in `Confer`'s own
delivery path today) and CANNOT call `Delegate` again until EVERY open
review resolves (open question 1). This is a SOFT block: nothing kills
the run, the delegator is refused with an explanatory ERROR on a new
`Delegate` attempt, exactly like today's milestone-backlog block on the
implementer (`implementer_block`, `epistemics/milestones.py:225-231` —
same shape: pending items block ONE specific action, never the whole
run). Acceptance is never hidden or implicit — there is always exactly
one explicit `SendMessage(..., approve=True)` call that resolves a
review, findable in the delegation record.

### 7. The idle-timeout carve-out: needed for `SendMessage`, NOT for report review

Narrower than an earlier draft of this item claimed, now that item 3 is
resumption-based rather than a held-open blocking call:

- **Report review no longer needs it at all.** Nothing stays blocked
  inside a live CLI stream between a report and its review — the worker's
  session fully exits after each turn, and review resumes it fresh. There
  is no stream for an idle timeout to misfire against.
- **`SendMessage(..., wait_for_reply=True)` still needs it, unchanged.**
  That call IS a genuine blocking wait inside a live, ongoing turn (a
  worker asking its delegator something mid-task, or a delegator asking a
  worker something while the delegator's OWN turn is still open) —
  exactly today's `FollowUp` shape (`evt.wait(...)`, delegation.py:429-435)
  and exactly what the existing carve-out already covers.
  `_stream_with_idle_timeout`'s `classify` callback (backends/claude.py:171,
  `_phase` at :639) already suspends the idle window uncapped for ANY
  pending tool call generically — confirmed by reading `_phase`'s
  `ToolUseBlock`-presence classification, not assumed. No mechanism
  change needed; only a test (item 7's own test in the Tests section)
  confirming `SendMessage(..., wait_for_reply=True)` specifically doesn't
  trip a false idle timeout, the same report-7 regression class as
  `FollowUp`'s existing coverage.

### 8. Provenance

The review dialogue (every `SendMessage` exchanged about this delegation,
in order, with timestamps) is part of the delegation's permanent record —
alongside `workspace_sha`, not a separate log a reader has to
cross-reference. The per-delegation git commit (`_commit_workspace`,
spec 11, delegation.py:775/954) moves to AFTER approval, not at
`_finish_ok`/`_finish_error` time as today — the commit should capture the
delegation's FINAL state (including any rework the review triggered).
This is a real behavior change from spec 11 as shipped ("DONE" assumed a
review-free one-shot report) and should be called out to whoever owns
that spec's status.

### 9. Ablatable from day one

A `Feature` (`runtime/features.py`) gating this entire mechanism —
default ON, matching every existing feature's "off makes a normal run
worse by construction" ablation posture. Off should mean: reports close
the way they do today (report = terminal on submission, no review
window, `_commit_workspace` at `_finish_ok`/`_finish_error` as today) — a
real behavioral fallback, not a withheld tool that leaves a dangling
prompt reference (BACKLOG #27/#28/#30's failure class).

### 10. No agent is ever left waiting unaware — the mechanism

The concrete form of the design rule ratified above:

- **(a) A "pending for you" notice on every tool result.** Every tool
  result a node receives carries a compact, adda-voiced notice (the
  existing `<adda-note>` provenance convention, `nodes/notices.py`)
  listing what it currently owes: open reviews awaiting its response,
  questions awaiting its answer, finished delegations not yet collected.
  Nothing pending → no notice, so there is no noise on the common case.
  This reuses the SAME insertion point the existing pending-notification
  drain already uses (`_wrap_closure`/`_drain_notifications`,
  `nodes/orchestration.py` — budget/backstop warnings ride this path
  today), not a second mechanism bolted on beside it.
- **(b) Every blocking call wakes early when something ELSE needs that
  agent, and returns it, not just its originally-awaited thing:**
  - A delegator blocked in `Wait` also wakes on a QUESTION from any of
    its workers (via `SendMessage`), not only on a finished report — so
    no worker's question waits behind an unrelated fan-out the delegator
    happens to be collecting.
  - A node blocked in `SendMessage(..., wait_for_reply=True)` wakes on
    ANY message from that peer (the deadlock guard, item 1), AND its
    return carries a pending-for-you notice if something else arrived in
    the meantime.
  - A worker under review (item 3) is not "blocked" in the stream sense
    at all — its session has exited and is resumed fresh per review
    turn — so there is nothing to wake early; each resume already IS the
    delegator's next question or its approval, by construction. Restated
    here only so a reader of this list does not go looking for a
    blocking-call analogue that no longer exists for this path.

### 11. Migration — every Confer/FollowUp/Reply/ReportProgress reference, one sweep

Grepped, not guessed — every file that will need touching in the same
commit(s) that build this:

- **Tools removed:** `ConferTools.Confer` (delegation.py:256), the worker
  `FollowUp`/`Reply` pair (delegation.py:410, 2167), the entry-node
  `FollowUp` (delegation.py:2193), `ReportProgress` (delegation.py:446).
- **Tools added:** `SendMessage(to, message, wait_for_reply=False,
  approve=False)`, gated per item 1 (any node with an edge) and item 2
  (`to="human"` restricted to the entry node by topology).
- **Session resumption plumbing (item 3), new:** capturing and recording
  the worker's CLI `session_id` at report time (already read once today,
  `backends/claude.py:831`); a resume path through the same backend
  (`ClaudeAgentOptions(resume=session_id)` for Claude; retained-history
  re-invocation for `openai_compatible`); the OPEN-FOR-REVIEW delegation
  state itself (a new status alongside today's `Working`/`Done`/`Errored`
  in the delegation registry, `nodes/tools/routing/delegation.py`); and
  the fallback-with-diagnostic path when resume fails.
- **`Delegate`/`Wait` gating (`nodes/tools/routing/__init__.py::
  build_routing_tools`):** `Delegate` stays outgoing-edges-only (task B,
  already shipped). `Wait` TIGHTENS from task B's "any edge" back to
  outgoing-edges-only (item 4) — a real, deliberate un-shipping of part
  of `f613427`, not an oversight.
- **Prompts:** wherever Confer/FollowUp/Reply/ReportProgress are
  taught/exampled — `prompts/agent_prompts.py`, `prompts/
  deliverable_format.py` if it references worker communication
  conventions, and each `agents/*.py` role's own system prompt (grep for
  the literal tool names at build time, not by memory — regenerate
  `internal/promptmap.html` in the same commit, and check
  `internal/tools/promptmap.py` itself for any hardcoded reference to the
  retired names in its own extraction logic).
- **Tests:** every test file asserting on Confer/FollowUp/Reply/
  ReportProgress behavior needs updating to `SendMessage`'s equivalent,
  not just deleting — the BEHAVIORAL claims (async steering, blocking
  question-and-answer, non-blocking progress note) all still need
  coverage. `tests/test_nodes.py`'s `test_delegate_and_wait_present_with_
  an_outgoing_edge` / `test_wait_present_but_not_delegate_with_only_an_
  incoming_edge` (task B, already shipped) need updating for item 4's
  tightened `Wait` gating; `tests/test_delegation_roster.py`'s own
  Reply/FollowUp references need the same sweep.
- **`internal/FEATURES.md`:** the enforced tool catalog documentation
  needs the new `SendMessage` entry and the four retirements noted.
- **Viewer (`src/adda/_src/viewer/`):** `app.py`'s transcript rendering
  and any FollowUp-specific UI (`tests/test_viewer_browser.py`'s "a
  question is shown before it is answered" surface) needs to render
  `SendMessage` exchanges instead — including the NEW review-dialogue
  provenance (item 8), which has no current UI representation.
- **`infra/operator_channel.py`:** stays as the TRANSPORT for
  `SendMessage(to="human", ...)` (item 2) — not retired, but its callers
  change from the entry-node `FollowUp` method to whatever dispatches
  `SendMessage` when `to="human"`.
- **`epistemics/milestones.py` / `implementer_block`:** the new
  Delegate-blocked-while-any-review-open state (item 6) is structurally
  the same SHAPE as `implementer_block`'s pending-milestones gate — worth
  checking whether the same function generalizes, or whether they should
  stay separate (one gates "may this node delegate at all," the other
  "may this SPECIFIC delegator start something new").

## Tests, named first (TDD) — for whoever builds this

- `test_a_workers_question_reaches_a_delegator_blocked_in_wait_on_a_fan_out`
  — item 10(b): a delegator is blocked in `Wait()` (no id) collecting a
  fan-out of several dispatched workers; a DIFFERENT worker's question
  (via `SendMessage`) reaches and unblocks it immediately — it does not
  wait for any of the fanned-out delegations to actually finish.
- `test_a_pending_item_appears_in_the_next_tool_result` — item 10(a): a
  node with an open review/question/uncollected delegation owed to it
  sees a `<adda-note>` naming it on its very next tool result, whatever
  tool that call happened to be.
- `test_no_pending_notice_when_nothing_is_owed` — item 10(a)'s converse:
  a node with nothing pending gets no notice at all — the mechanism must
  not manufacture noise on the common case.
- `test_send_message_wait_for_reply_returns_in_one_call` — a
  `wait_for_reply=True` call blocks and its return value IS the peer's
  reply, no second tool call needed.
- `test_send_message_deadlock_guard_wakes_on_either_direction` — two
  peers each call `SendMessage(..., wait_for_reply=True)` toward each
  other at roughly the same time; neither hangs — each unblocks on
  whatever the other sent, whether reply or fresh question.
- `test_send_message_to_human_is_entry_node_only` — a non-entry node's
  `SendMessage(to="human", ...)` is refused with an explanatory ERROR;
  the entry node's succeeds and reaches `infra/operator_channel.py`.
- `test_non_entry_node_routes_a_human_question_through_its_delegator` —
  a worker with a human-worthy question sends it to its OWN delegator,
  not `to="human"` directly.
- `test_report_moves_to_open_for_review_not_finish_ok` — a worker's final
  turn text does NOT call `_finish_ok`; the delegation records an
  OPEN-FOR-REVIEW state plus a resumable CLI `session_id` (or, for
  `openai_compatible`, its retained history) instead.
- `test_a_review_question_resumes_the_same_worker_session` — a
  delegator's `SendMessage` (no approve) to an open report causes the
  runtime to RESUME the worker's recorded session (mocked
  `ClaudeAgentOptions(resume=...)`/SDK equivalent) with the question as
  its next user turn, framed with the `<adda-note>`; asserts the resume
  parameter used, not a fresh unrelated session.
- `test_worker_revises_report_after_a_review_question` — the resumed
  session's new final text replaces the delegation's recorded report, and
  it is validated by `_classify_response` again exactly as the first was.
- `test_resume_failure_falls_back_and_is_recorded_as_a_diagnostic` — a
  mocked resume failure (SDK error / expired session) falls back to
  reconstructing from recorded history AND fires a diagnostic event
  (never silent) naming the fallback.
- `test_delegate_refused_while_any_review_is_open` — with two reports
  open for review, a new `Delegate` is refused referencing BOTH open
  reviews, not just the first (open question 1).
- `test_worker_delegator_cannot_report_up_with_an_open_review_owed` —
  open question 2: a node that is itself mid-delegation cannot submit its
  own report to ITS delegator while it still owes a review on its own
  sub-delegation.
- `test_empty_message_is_a_hard_error` — `SendMessage(to=X, message="")`
  (or `message=None`) is refused, never a silent no-op.
- `test_feedback_on_an_unread_report_is_an_error` — `approve`/feedback on
  a delegation whose report was never delivered through `Wait` or a
  delegation result is refused.
- `test_wait_still_collects_during_an_open_review_with_a_nudge` — open
  question 4: `Wait()` for OTHER delegations still returns normally while
  a review is open, accompanied by a nudge naming the open review(s).
- `test_send_message_wait_for_reply_does_not_trip_the_idle_timeout` — a
  mocked stream with a long gap while `SendMessage(...,
  wait_for_reply=True)` is pending does not raise
  `STREAM_ENDED_WITHOUT_RESULT` (item 7 — report-7 regression coverage;
  NOT needed for report review itself, which holds no stream open).
- `test_review_dialogue_is_recorded_on_the_delegation` — every
  `SendMessage` exchanged about a delegation appears, in order, on that
  delegation's record.
- `test_workspace_commit_happens_after_approval_not_at_finish` —
  `_commit_workspace` fires once, after `approve=True`, capturing any
  rework the review triggered (supersedes spec 11's `_finish_ok`-time
  commit test).
- `test_wait_tightens_back_to_outgoing_edges_only` — item 4's un-shipping
  of task B's broader `Wait` gating: a node with only incoming edges no
  longer holds `Wait` once this spec is built.
- `test_feature_off_restores_todays_one_shot_report_behavior` — the
  ablation knob off: report = terminal on submission, no review window,
  commit at `_finish_ok`/`_finish_error` as today.
- `test_errored_delegation_does_not_block_a_new_delegate` — edge 1: a
  worker that raises finishes `Errored` immediately (no review window)
  and a subsequent `Delegate` call is NOT refused on its account, even
  with `peer_interaction` on.
- `test_run_ending_with_an_open_review_is_recorded_as_never_approved` —
  edge 2: the run closes (`Done()`) while a delegation is still
  OPEN-FOR-REVIEW; the delegation log records it as open/never-approved
  (a distinct status from `DONE`), and the retrospective states the same
  — never silently treated as approved.

## Risks / out of scope

- **Risk — deadlock beyond the two-peer case.** Item 1's deadlock guard
  covers two peers waiting on EACH OTHER. A longer cycle (A waits on B,
  B waits on C, C waits on A) is not addressed — flag for whoever builds
  it; likely needs either a cycle detector or an argument that the
  topology (delegators only wait on their own workers, never laterally)
  makes a longer cycle structurally impossible. Not verified here.
- **Risk — an open review that never resolves.** A delegator that never
  calls `SendMessage(..., approve=True)` permanently blocks its own
  further `Delegate` calls (item 6, intentional) — does the run's own
  wall-clock/watchdog eventually force a resolution, or is an
  indefinitely-open review a legitimate stall only BACKLOG #22's
  liveness detectors would ever catch? Not addressed here.
- **Risk — spec 11 interaction.** Item 8's "commit after approval" is a
  real, not cosmetic, change to spec 11's shipped mechanism (its "DONE"
  status assumed a review-free one-shot report). Whoever builds this
  must either amend spec 11 or explicitly document the divergence.
- **Risk — un-shipping part of task B.** Item 4 deliberately reverts part
  of an already-shipped, already-tested commit (`f613427`). This needs to
  land as its OWN commit with its own root-cause body when spec 12 is
  built, not silently folded into a larger diff — the git history should
  show that `Wait`'s gating changed twice, and why, not once.
- **Risk — session resumption's cross-backend parity.** Item 3's
  mechanism is native for Claude (`resume=session_id`) but emulated for
  `openai_compatible` backends (replay retained history) — these are not
  the same guarantee (a genuinely resumed session may retain state a
  replayed-history reconstruction cannot, e.g. anything the CLI itself
  cached). Whoever builds this should verify the two paths' BEHAVIORAL
  equivalence with a real test on each backend, not assume parity from
  the design alone.
- **Out of scope:** streaming/live bidirectional chat beyond the
  deadlock-guarded blocking exchange above (the worker is still a
  synchronous SDK session between tool calls); reviewing a delegation's
  WORKSPACE diff directly in the review UI (spec 11 lists a possible
  future `WorkspaceDiff` tool, deliberately unbuilt — orthogonal here,
  composes with this spec later if built).

## Done when (KPI)

**Mechanism claim** (tests pass): the tests above are green; `Confer`,
`FollowUp` (both implementations), `Reply`, and `ReportProgress` no longer
exist as tool names anywhere in the migration-swept surface; the
ablation-off arm is byte-identical in behavior to today's shipped
one-shot-report path; `Wait`'s gating matches item 4 exactly (outgoing
edges only, not task B's broader "any edge" rule).

**Behavioral claim** (measured on a re-run, not assumed): on a repeat of a
study whose prior run showed a delegator accepting a report it later
turned out to misunderstand (a real re-run needed here, not predicted),
does the delegator ask at least one review question before approving, and
does the review measurably change the gate outcome or hypothesis
confidence versus the same study's one-shot (feature-off) arm? This is
the comparison the ablation exists to make possible — it is not evidence
until it is measured.
