# Spec 12 — Peer-interaction contract: Delegate / SendMessage / Wait

**Status:** spec only — not built. **Priority:** high (touches the delegation
record format and every worker/delegator tool surface; sequencing with other
in-flight work matters). **Depends on:** nothing structurally, but see
Migration — it retires spec 02's design entirely (Confer/FollowUp/Reply are
superseded here, not extended).

Requested by Elvis (via adda-boss-whopper), 2026-09-27, alongside tasks A
(reproduction-gate ablation) and B (roster topology/tools) landed the same
day. Task B's own review already surfaced one relevant fact worth carrying
into this spec: `Reply` (delegation.py:2167) is solely the answer-side of
`FollowUp` (one blocking question per delegation); `Confer` is a fully
separate, unlimited, async-both-directions protocol, replied to by calling
`Confer` again. Two coexisting question/answer protocols is itself evidence
for the consolidation this spec proposes.

## Open questions (for Elvis — flag before building, don't guess)

1. **Multiple delegations open for review at once.** If a delegator fans out
   three workers and two report back before it drains the first review, does
   it review sequentially (queue), or can several be "open for review"
   concurrently with the delegator choosing which to address? The "cannot
   start a new Delegate until it resolves" rule (design item 5) says nothing
   about MULTIPLE already-open reviews stacking up from a fan-out that
   already happened — only that no NEW one may start. Needs an explicit
   answer or `Wait()`'s fan-out semantics (`Wait()` with no id, "whichever
   finishes first" — `delegation.py:2000-2006`) get a second meaning
   layered on top by accident.
2. **A delegator that is itself a worker (nested delegation).** `implementer`
   delegates to `math_expert` in the 6-node lcp_matlab_regression graph
   (confirmed via task B's regenerated roster). If math_expert's report is
   "open for review" and implementer is itself mid-delegation (blocked in
   its OWN worker session, not idle), does its worker thread block on BOTH
   its own work and the nested review simultaneously? Today's `FollowUp`
   blocks the CALLER (`evt.wait`, delegation.py:435) — the analogous
   question here is whether a nested delegator's own execution thread is
   the one that must review, or whether review can happen out-of-band via
   `SendMessage` while that thread keeps working. The two-tool
   Delegate/Wait/SendMessage model implies the latter, but it needs to be
   said, because the report-open-for-review state and "the delegator is
   busy" state can now overlap for the first time.
3. **The "read" definition.** Design item 4 requires "the report was
   delivered through Wait or a delegation result" before feedback can be
   given, to bar feedback on an unread report. Is "delivered through Wait"
   satisfied the instant `Wait()` RETURNS the report text (mechanical,
   checkable in code — `_wait_for_any`/`_wait_for_one`,
   delegation.py:2029+), or does it require some acknowledgement that the
   text was actually attended to (unverifiable — an LLM can be handed text
   and never engage)? Recommend the mechanical definition (had-the-
   opportunity, not proof-of-comprehension) since anything else is
   unenforceable, but this is a genuine design choice, not an
   implementation detail.
4. **Interaction with `Wait`'s fan-out.** `Wait` is now the single blocking
   primitive for "something addressed to me arrived" (design item 1,
   corrected) — a finished delegation's report, a `SendMessage` reply, or a
   review approval/question, whichever arrives first, for ANY node (a
   worker with only incoming edges calls `Wait` too, not only a
   delegator). Once a report is "open for review" and blocking new
   `Delegate()` calls (item 5), does calling `Wait()` again (a) refuse (the
   delegator must resolve first, matching the Delegate-block), (b) still
   fan out normally and hand back a SECOND finished delegation, compounding
   open question 1, or (c) block until the first review resolves? Pick
   one; each has a different failure mode for a delegator that dispatched
   many workers at once. This also now bears on the WORKER side: a worker
   that sent a synchronous question via `SendMessage` then `Wait` (item 1's
   "no `wait_for_reply` flag" pattern) is using the SAME primitive a
   delegator uses to collect a report — `Wait` must disambiguate what kind
   of "arrival" it is returning, or a worker awaiting a reply could be
   handed something else addressed to it instead.

## Problem, in the current implementation's own terms

Three tools instead of one coherent protocol, evidence from the source, not
memory:

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
  channel or a TTY, capped at `node._max_ask` per run
  (`max_ask` constructor param, `nodes/node.py`). Two different
  implementations of "ask one question," gated by whether the asker
  happens to be the entry node.
- **The report boundary is one-shot.** `_finish_ok`/`_finish_error`
  (delegation.py:746, 944) commit the workspace (`_commit_workspace`,
  spec 11's mechanism) and record the delegation as terminal in the SAME
  call that produces the report — there is no window in which the
  delegator can ask a question ABOUT the finished report before it is
  already committed and closed. A delegator that wants clarification on a
  report has no tool for it at all today; it either accepts the report or
  re-delegates from scratch.
- **The idle-timeout carve-out already exists and already generalizes.**
  `_stream_with_idle_timeout`'s `classify` callback (backends/claude.py:171,
  the `_phase` closure at :639) suspends the idle window uncapped
  (`_wait = None`) for ANY pending tool call, not a FollowUp-specific
  carve-out — confirmed by reading `ainvoke`'s `_phase` closure, which
  classifies on `ToolUseBlock` presence generically. A new blocking
  "wait for report review" tool is automatically covered by this
  mechanism with no changes to it — only a test confirming that, per
  design item 6.

## Design (the agreed high-level shape — spec the details here)

### 1. Three tools only

- **`Delegate(target, task, ...)`** — starts new work (today's tool,
  unchanged entry point). Topology-gated per task B's own fix, landed the
  same day: only a node with >=1 outgoing edge holds it at all
  (`nodes/tools/routing/__init__.py::build_routing_tools`).
- **`SendMessage(to, message, approve=False)`** — ALL peer and human
  messaging, BOTH asking and answering. Replaces `Confer` AND `FollowUp`
  (both directions, both the worker→delegator and entry→human paths —
  `infra/operator_channel.py`'s channel becomes `SendMessage(to="human",
  ...)`'s transport for the entry node specifically, not a separate tool)
  AND `Reply` (`Reply` is retired outright, not carried forward — see
  below). Works mid-delegation (Confer's today's use case), at the end
  (the new report-review use case, item 3), and at any boundary — the
  sender does not need to know which case applies; the tool does.
- **`Wait(delegation_id=None, block=True)`** — the single blocking
  primitive: it returns whatever addressed to THIS node arrives first — a
  finished delegation's report (today's meaning, unchanged), a
  `SendMessage` reply, or a review approval/question (item 3). One
  primitive, one name, kept as `Wait` rather than split into a
  delegation-specific variant (an earlier draft of this spec proposed
  renaming it `WaitDelegation`; Elvis's call was to keep the general name,
  since the whole point is that it is NOT delegation-specific once
  `Reply` retires — see open question 4 for how it disambiguates what kind
  of arrival it returns).

**`Reply` is retired, not carried forward**, same as `Confer` and
`FollowUp` — a delegator ANSWERS a worker's question with
`SendMessage(to=worker, message=...)`, exactly the same call it would use
to ask one. There is no `wait_for_reply` flag and no synchronous-vs-async
mode switch on `SendMessage` itself: a SYNCHRONOUS question is simply
`SendMessage(...)` immediately followed by `Wait()` — two tool calls
composing the behavior `FollowUp`'s single call used to hardcode. This is
the same "minimum cognitive load" principle driving the whole
consolidation: one way to send something, one way to wait for something,
instead of a matrix of tools crossed with blocking/non-blocking variants.

Design intent (Elvis's words): minimum cognitive load, maximum interaction.
Fewer tool NAMES to choose between, not fewer capabilities — an agent should
default to asking itself what it can learn from a peer, and one tool
(`SendMessage`) always being the answer to "how do I ask" removes the
FollowUp-vs-Confer-vs-(nothing, for a finished report) branching a model has
to get right today.

### 2. A human target

`SendMessage(to="human", ...)` is the entry node's own path to a person,
replacing its bespoke `FollowUp` (delegation.py:2193-2247) but reusing its
TRANSPORT unchanged: `infra/operator_channel.py`'s disk-based
`ask_question`/`close_question`/`is_watched`, the viewer, and the TTY
fallback (`_stdin_is_tty()`). `node._max_ask` (the per-run cap) applies to
`to="human"` calls specifically — it was never meant to cap peer traffic,
only how many times a run may interrupt a person.

### 3. Reports are open for review

A worker's report, delivered via `Wait`, does not end that delegation's
"reviewable" state — the delegator may still question it. Mechanism:

- On report delivery, the report text carries an `<adda-note>` (the
  existing wrapping convention, `nodes/notices.py`, already used to mark
  runtime-injected text) reading approximately: *"analyse this thoroughly
  and ask about anything unclear; take advantage of this moment, because
  you might not be able to wake this node again."* True in code, not just
  prose: the worker's thread is about to become unreachable in the sense
  that today's architecture already treats it (a daemon thread that has
  returned; nothing currently re-wakes it).
- If the delegator sends a question via `SendMessage` before some
  disposition (approve/further work) is recorded, the ORIGINAL worker
  session must still be reachable to answer it — this is the one place
  this design changes today's lifecycle: a delegation that has "finished"
  its `Done()`/report call is not YET fully torn down until the review
  resolves. The worker's own tool call that produces this state (see #6)
  is the mechanism that keeps its thread alive and blocked, waiting.
- The question reaches the worker framed by an `<adda-note>` too: *"you
  got this message from `<delegator>` — the delegation needs
  clarification; take your time if needed."*
- The worker can redo work, steer its own next actions, and UPDATE its
  report (a revised `Done()`/report call) in response — this is not a
  read-only Q&A, the delegation's outcome can genuinely change based on
  the review.
- Fully async, like `SendMessage` itself: the delegator is not forced to
  block waiting for the worker's answer any more than any other
  `SendMessage` call blocks.

### 4. `SendMessage` defaults

`approve=False` by default; `message` is REQUIRED — an empty message
raises rather than silently no-opping. No auto-approvals: a delegator that
never explicitly calls `SendMessage(..., approve=True)` never closes the
review. No empty/zero-feedback messages by default: the tool cannot be
called as a bare "yes" without saying why, forcing the delegator to
actually engage rather than rubber-stamp.

**Read-before-feedback is an ENFORCED precondition, not a convention.**
Giving feedback on a report the delegator has not yet received through
`Wait` or a delegation result must be a hard ERROR (matching the existing
style of `Delegate`'s "ERROR: unknown target" / `Reply`'s "ERROR: unknown
delegation" — a clean string, not an exception that breaks the turn). See
open question 3 for what "read" means precisely.

### 5. Soft resolution under the cooperative principle

While a delegation is open for review, the delegator is nudged
continuously (the SAME mechanism budget/backstop warnings already use —
the per-delegation queue prefixed onto a caller's next tool result,
`node._pending_worker_msgs`, delegation.py:317-321 in `Confer`'s own
delivery path — reused, not reinvented) and CANNOT call `Delegate` again
until the open review resolves. This is a SOFT block in the sense that
nothing kills the run — the delegator is refused with an explanatory
ERROR on a new `Delegate` attempt, exactly like today's milestone-backlog
block on the implementer (`implementer_block`,
`epistemics/milestones.py:225-231` — same shape: pending items block ONE
specific action, never the whole run, and are inspectable). Acceptance is
never hidden or implicit — there is always exactly one explicit
`SendMessage(..., approve=True)` call that resolves a review, findable in
the delegation record.

### 6. The worker's wait-for-approval is a blocking tool call

A new closure (name TBD in implementation — not `Wait`, which is the
DELEGATOR's collection tool) that the worker calls after its report,
blocking on an `Event` exactly like `FollowUp`'s `evt.wait(timeout=...)`
(delegation.py:429-435) — reusing that exact primitive, not a new one.
Per the Problem section above, `_stream_with_idle_timeout`'s `classify`
carve-out (backends/claude.py:171-224) already suspends the idle window
for ANY pending tool call generically; this needs no change, only a test
proving the blocking review-wait specifically does not trip a false idle
timeout (report-7's failure mode: a `STREAM_ENDED_WITHOUT_RESULT`
misclassification during a legitimately-long tool wait).

### 7. Provenance

The review dialogue (every `SendMessage` exchanged about this delegation,
in order, with timestamps) is part of the delegation's permanent record —
alongside `workspace_sha`, not a separate log a reader has to
cross-reference. The per-delegation git commit (`_commit_workspace`,
spec 11, delegation.py:775/954) moves to AFTER approval, not at
`_finish_ok`/`_finish_error` time as today — the commit should capture the
delegation's FINAL state (including any rework the review triggered), not
a snapshot that a subsequent review-triggered edit then silently
invalidates without a second commit. This is a real behavior change from
spec 11 as shipped and should be called out to whoever owns that spec's
"DONE" status.

### 8. Ablatable from day one

A `Feature` (`runtime/features.py`) gating this entire mechanism — default
ON, matching every existing feature's "off makes a normal run worse by
construction" ablation posture (`features.py`'s own module docstring).
Off should mean: reports close the way they do today (report = terminal,
no review window, no blocking wait-tool, `_commit_workspace` at
`_finish_ok`/`_finish_error` as today) — a real behavioral fallback, not a
withheld tool that leaves a dangling prompt reference (the exact failure
class `features.py`'s docstring warns about, BACKLOG #27/#28/#30).

### 9. Migration — every Confer/FollowUp/Reply/ReportProgress reference, one sweep

Grepped, not guessed — every file that will need touching in the same
commit(s) that build this:

- **Tools removed:** `ConferTools.Confer` (delegation.py:256), the worker
  `FollowUp`/`Reply` pair (delegation.py:410, 2167), the entry-node
  `FollowUp` (delegation.py:2193), `ReportProgress` (delegation.py:446) —
  its non-blocking "leave a note" use case is a `SendMessage` with no
  expectation of a reply, so it folds in rather than needing its own
  survivor.
- **Prompts:** wherever these four tool names are taught/exampled —
  `prompts/agent_prompts.py`, `prompts/deliverable_format.py` if it
  references worker communication conventions, and each `agents/*.py`
  role's own system prompt that currently names Confer/FollowUp/Reply
  explicitly (grep for the literal tool names at build time, not by
  memory, since a rename here is exactly the class of drift `internal/
  promptmap.html`'s generator exists to catch — regenerate it in the same
  commit).
- **`internal/promptmap.html` / `internal/tools/promptmap.py`:** the tool
  catalog and Done()-chain parsing both key off literal tool names;
  regenerate after the rename, and check `promptmap.py` itself for any
  hardcoded Confer/FollowUp reference in its own extraction logic (it
  parses `feedback.py`'s tuple for the Done chain — check whether an
  analogous hardcoded reference to FollowUp exists elsewhere in that
  generator).
- **Tests:** every test file under `tests/` asserting on Confer/FollowUp/
  Reply/ReportProgress behavior needs updating to the new tool, not just
  deleting — the BEHAVIORAL claims (async steering, one blocking
  question, non-blocking progress note) all still need coverage, now
  under `SendMessage`. `tests/test_delegation_roster.py`'s own assertions
  that `Reply`/`FollowUp` appear in a node's tool list (this session's own
  new tests, task B) will need updating the moment this spec is built —
  flagging that dependency explicitly so it is not missed.
- **`internal/FEATURES.md`:** the tool catalog documentation (enforced by
  `tests/test_features_documented.py` for any tool a role declares) needs
  the new `SendMessage` entry and the retirement of the four old ones
  noted, not silently dropped.
- **Viewer (`src/adda/_src/viewer/`):** `app.py`'s transcript rendering
  and any FollowUp-specific UI (the "a question is shown before it is
  answered" browser test, `tests/test_viewer_browser.py`, references this
  UI surface by name) needs to render `SendMessage` exchanges instead —
  including the NEW review-dialogue provenance (item 7), which has no
  current UI representation at all.
- **`infra/operator_channel.py`:** stays as the TRANSPORT for
  `SendMessage(to="human", ...)` (item 2) — not retired, but its callers
  change from the entry-node `FollowUp` method to whatever dispatches
  `SendMessage` when `to="human"`.
- **`epistemics/milestones.py` / `implementer_block`:** the new
  Delegate-blocked-during-open-review state (item 5) is structurally the
  same SHAPE as `implementer_block`'s pending-milestones gate — worth
  checking whether the SAME function can be generalized rather than a
  parallel one written, or whether they should stay separate because one
  gates "may this node delegate at all" and the other gates "may this
  SPECIFIC delegator start something new."

## Tests, named first (TDD) — for whoever builds this

- `test_send_message_replaces_confer_for_mid_flight_steering` — a running
  delegation receives a `SendMessage`, sees it prefixed on its next tool
  result (Confer's existing delivery mechanism, reused).
- `test_send_message_to_human_reaches_operator_channel` — `to="human"`
  routes through `infra/operator_channel.py`'s existing transport, capped
  by `node._max_ask`.
- `test_report_stays_open_for_review_until_approved` — after `Wait`
  delivers a report, `Delegate` from the SAME delegator is refused with
  an explanatory ERROR until `SendMessage(..., approve=True)` resolves it.
- `test_worker_can_be_questioned_after_reporting_and_revise_its_report` —
  the worker's blocking review-wait tool (item 6) receives a `SendMessage`
  question, and its UPDATED `Done()`/report call changes the delegation's
  final recorded report.
- `test_empty_message_is_a_hard_error` — `SendMessage(to=X, message="")`
  (or `message=None`) raises/returns an ERROR string, never a silent no-op.
- `test_feedback_on_an_unread_report_is_an_error` — `SendMessage` giving
  feedback on a delegation whose report was never delivered through `Wait`
  or a delegation result is refused (see open question 3 for exactly what
  "delivered" checks).
- `test_the_blocking_review_wait_does_not_trip_the_idle_timeout` — a
  mocked stream with a long gap while the review-wait tool is pending does
  not raise `STREAM_ENDED_WITHOUT_RESULT`/a stall timeout (report-7
  regression coverage for the NEW tool specifically, not just FollowUp's
  existing coverage).
- `test_review_dialogue_is_recorded_on_the_delegation` — every
  `SendMessage` exchanged about a delegation appears, in order, on that
  delegation's record (`delegation_log`), not only in a transcript.
- `test_workspace_commit_happens_after_approval_not_at_finish` —
  `_commit_workspace` fires once, after `approve=True`, and captures any
  rework the review triggered (supersedes spec 11's `_finish_ok`-time
  commit test).
- `test_feature_off_restores_todays_one_shot_report_behavior` — the
  ablation knob off: no review window, no blocking wait-tool exists on the
  worker's toolset, commit at `_finish_ok`/`_finish_error` as today.

## Risks / out of scope

- **Risk — deadlock.** A delegator that never resolves an open review
  (never calls `SendMessage(..., approve=True)`) permanently blocks its own
  further `Delegate` calls. This is intentional per item 5 ("cannot start
  a new Delegate until it resolves"), but needs a stated escape: does the
  RUN's own wall-clock/watchdog eventually force a resolution, or is an
  indefinitely-open review a legitimate way for a run to stall that only
  the existing stall/liveness detectors (BACKLOG #22) would ever catch?
  Not addressed here — flag for whoever builds it.
- **Risk — nested delegation depth.** Open question 2 (implementer→
  math_expert, a real edge in a real study) means this is not a
  hypothetical; a real graph already has a delegator that is itself
  always a worker.
- **Risk — spec 11 interaction.** Item 7's "commit after approval" is a
  real, not cosmetic, change to spec 11's shipped mechanism (its own
  "DONE" status assumed a review-free one-shot report). Whoever builds
  this must either amend spec 11 or explicitly document the divergence.
- **Out of scope:** streaming/live bidirectional chat during review (the
  worker is still a synchronous SDK session between tool calls — the
  review-wait tool call IS the synchronization point, same tractable
  block-and-reply model spec 02 already chose and this spec inherits);
  reviewing a delegation's WORKSPACE diff directly in the review UI
  (spec 11 lists this as a possible future `WorkspaceDiff` tool,
  deliberately unbuilt — orthogonal to this spec, composes with it later
  if built).

## Done when (KPI)

**Mechanism claim** (tests pass): the ten tests above are green; `Confer`,
`FollowUp` (both implementations), `Reply`, and `ReportProgress` no longer
exist as tool names anywhere in the migration-swept surface (item 9); the
ablation-off arm is byte-identical in behavior to today's shipped
one-shot-report path.

**Behavioral claim** (measured on a re-run, not assumed): on a repeat of a
study whose prior run showed a delegator accepting a report it later
turned out to misunderstand (a real re-run needed here, not predicted),
does the delegator ask at least one review question before approving, and
does the review measurably change the gate outcome or hypothesis
confidence versus the same study's one-shot (feature-off) arm? This is the
comparison the ablation exists to make possible — it is not evidence
until it is measured.
