# Agentic system — feature catalog

The single place that says **what the agentic system can do, why, and where it
lives.** Read this to get your bearings without reading code.

> **Contract (enforced):** every agent tool listed in an agent's `tools` set MUST
> appear in the "Tools" table below — `tests/test_features_documented.py`
> fails the build otherwise. Every new *capability* (tool OR infrastructure)
> MUST get an entry here in the same commit that adds it. The test can only
> enumerate tools; infrastructure features rely on this written contract.

Format per feature: **what** (plain language) · **why** · **where** (files) ·
**config** (if any) · **status**.

---

## A. Science & orchestration

### Hypothesis ledger
- **What:** the run's record of falsifiable hypotheses and their verdicts (OPEN /
  SUPPORTED / FALSIFIED / INCONCLUSIVE), append-only.
- **Where:** `hypothesis_ledger.py`; the strategizer's mutate closures
  (`HypothesisPropose`/`HypothesisUpdate`, whose `falsification_attempt=True`
  links an unflagged delegation as an attempt) in
  `nodes/tools/routing/ledger.py`; per-run file `debug/strategizer_notes/hypotheses.json`.
- **Status:** core.

### Falsification charter (the Popperian rules)
- **What:** the single binding text defining how a hypothesis may be tested and
  labelled (severity of the attempt, verdict follows the result, no goalpost-moving).
- **Why:** one shared standard both the strategizer and the critic cite.
- **Where:** `knowledge/charter.py`. **Status:** core (§4 user-owned).

### Live verdict validator (#9)
- **What:** when a hypothesis is closed, an independent referee checks — *live* —
  that the verdict obeys the charter, and nudges the strategizer if not.
- **Why:** the gate critic only checks at the end; this catches charter violations
  at the moment of assertion.
- **Where:** `verdict_validator.py` (judge logic); invoked by `nodes/tools/routing/ledger.py`
  HypothesisUpdate via `node._run_verdict_validator`, which is defined in
  `nodes/critic_gate.py`. Runs on the **critic's** model (reuses the critic adapter),
  not the strategizer's — one refereeing standard, decoupled from the agent it judges.
- **Memory (anti-oscillation):** the judge is fed its own prior rulings on the SAME
  hypothesis (from the ledger `status_log`, via `_prior_rulings_digest`) with a
  justify-any-reversal guard, so a borderline verdict can't silently flip between
  calls. Mirrors the gate critic's prior-reviews digest.
- **Bounded budget:** the advisory call runs with a tight `idle_timeout=120s` +
  `retry_max=1` (NOT the run-wide 5×600s agent-turn budget). A hung CLI stream once
  froze a whole run for ~89 min here; on any timeout/failure the verdict simply
  stands (the call is advisory).
- **Config:** kill switch `F3DASM_VERDICT_VALIDATOR=0`. **Status:** advisory, non-blocking.

### Science monitor
- **What:** background rules that flag scientific drift and escalate repeated
  drift to the critic. Two provenance-integrity rules bracket the eval ledger
  from both directions: **UNLEDGERED_EVALS** (a delegation reported evals but
  wrote no attributable rows — evals that never reached the store) and
  **UNSTAMPED_ROWS** (the store gained rows with no provenance owner — the
  reverse: rows written outside get_evaluator() via the public
  ExperimentData.store() door, neither counted nor reproducible). Both warn-only.
  A third rule, **DUPLICATE_EVALUATION**, flags a delegation re-evaluating a
  design point already FINISHED, unchanged, in the ledger (real incident: a
  delegation re-sampled an identical seed=42 LHS design three times, 122 of
  160 rows pure waste — backlog #24). Counter-based, not level-triggered: fires
  once 3 NEW duplicate rows land since the last check, then resets; capped at
  2 nudges per delegation; rate-limited to one per 60s. `Wait()`'s poll loop
  also drains the monitor on every 10s tick (not just on the next tool call),
  so a nudge reaches a strategizer blocked waiting on a live campaign instead
  of surfacing only after the whole delegation (and its budget) is spent.
  **DUPLICATE_EVALUATION is now also PREVENTED, not only detected:**
  `InstrumentedDataGenerator._flush` dedups on write — a buffered eval whose
  design (same rounded input coords, per-delegation, the detector's own key) is
  already in the store, or repeats within the batch, is dropped keep-first (an
  existing FINISHED row is never mutated) and logged as `DEDUP_SKIPPED`. This
  ends the retry/re-launch duplication that burned ~30h of eval wall-time on 2
  designs in run 20260715T191329. Per-delegation scope preserves legitimate
  cross-delegation concurrent evals. Correcting a stale/wrong FINISHED row (e.g.
  a pre-oracle-fix drift read) is the explicit opt-in `InstrumentedDataGenerator.
  supersede(sample)` — re-runs the oracle and REPLACES that design's row
  net-count-preservingly (old out, new in = same count, FINISHED preserved), so
  the PROTECTED-store shrink/regression guard still holds; reachable by agents as
  `get_evaluator().supersede(sample)`. The ledger is append-only otherwise.
- **Where:** `science_monitor.py` (`_check_unledgered`, `_check_unstamped_rows`,
  `_check_duplicate_evaluations`); `ledger_summary.py` `unstamped_row_count`,
  `duplicate_eval_stats`; `routing.py` `Wait()`. **Status:** core (§4 user-owned).

### Registered criterion reaches the worker (pre-registration the experimenter can read)
- **What:** when `Delegate` carries `hypothesis_ids`, the worker's task message
  gains a `<registered_hypothesis>` block built from the ledger: each
  hypothesis's statement (capped at 700 chars — context) plus its
  `falsification_criterion` and `prediction` **verbatim, never truncated**
  (the contract). With `is_falsification_attempt=True` the framing states that
  the evidence will be judged against those criteria exactly as written, and
  explicitly licenses reporting a mismatch instead of substituting a
  different test.
- **Where:** `nodes/tools/routing/` `_hypothesis_brief()`, injected in
  `Delegate`'s task assembly beside the constraint snapshot.
- **Why:** the criterion is immutable once registered and is the standard the
  verdict is judged by, but the only party adda showed it to was the
  delegator, and only at reconciliation time — `_falsification_checkpoint()`
  fires on a **Done** report, i.e. after the evidence exists. `Delegate`'s
  contract put context packaging on the delegator, so the worker saw the
  criterion only if the delegator remembered to paste it. Measured cost across
  52 cluster runs: INCONCLUSIVE is the largest verdict class (100 of 295
  hypotheses) and the most expensive (median lifetime 3.15h vs 1.49h
  FALSIFIED, 0.91h SUPPORTED, 45% resolving within an hour of the run ending),
  and its verdict comments name the mechanism — *"the registered H3
  falsification criterion required a 50-iter constrained BO in the high-Ixx
  region. This BO was never executed"*; *"Test is INADEQUATE relative to the
  registered 30-point LHS criterion"*. Fixed in code rather than by another
  prompt rule because the corpus already asks for this
  (`agents/strategizer.py` tells the strategizer to pre-commit the sampling
  plan and eval count **in** the criterion) and it did not take — §2's stated
  fallback is a guard at the tool boundary.
- **Note:** `Delegate` already refuses unknown `hypothesis_ids` outright, so
  the brief is never built from a dangling reference.
- **Status:** core.

### Process milestones
- **What:** a small backlog (assess-literature, oracle-ready, …) that gates the
  implementer until the strategizer resolves each (complete or skip).
- **Where:** `milestones.py`; the `Milestone*` tools in `nodes/tools/routing/ledger.py`.
  **Status:** core.

### Delegation + inter-agent messaging
- **What:** the strategizer delegates work to specialist agents and they report back;
  agents ask/answer/approve through one peer-and-human messaging tool (see
  `SendMessage` below).
- **Where:** `nodes/tools/routing/`, `nodes/orchestration.py`.
- **Tools:** `Delegate`*, `Wait`, `SendMessage`, `ReportEvals`.
  (`Wait(id, block=False)` is the status poll that used to be `GetStatus`.)
  (*Delegate is injected dynamically, not in a static `tools` set.)
  `Confer`/`Reply`/the worker-facing `FollowUp`/`ReportProgress` are the
  pre-spec-12 surface `SendMessage` replaced; they no longer exist in either
  `peer_interaction` arm (see below).
- **Fan-out harvesting:** `Wait()` takes an OPTIONAL delegation id. Bare
  `Wait()` blocks until whichever delegation becomes actionable first — a
  finish, a worker's question, or a report OPEN FOR REVIEW (delivered, hence
  read, so it can be approved/answered while siblings still run; the reply
  names what is still in flight) — and returns that one's report (labelled
  with its ID), marking it read so N in flight are
  drained by N calls; it refuses when nothing is in flight, and refuses rather
  than hanging when every open delegation is parked on a `FollowUp` or has
  already died without reporting (a blocking call ends no turn, so the run's
  time backstop cannot fire while inside it). A blocked `Wait` (bare or by id)
  also RETURNS EARLY when an operator note or a science-monitor message
  arrives — delivered in-band with a "still in flight" line, nothing harvested
  — so a human's correction or a live nudge is never held unread behind a
  long delegation; routine notices do not wake it. A science-monitor message
  that is unchanged since the last one Wait delivered does not wake it again
  (the monitor re-lists every live violation each poll); a changed or
  re-appearing one does. `Cancelled` is never harvested
  (its result is excluded from the run). Naming an id keeps the original
  single-target behaviour.
  **Why:** dispatching a fan-out was already cheap (85% of real `Delegate`
  calls use `wait=False`) but collecting one was not — a single-target `Wait`
  left `GetStatus` polling as the only way to harvest several, and the
  poll-count nudges discourage exactly that. Across 39 cluster runs, reliance
  on `Wait` predicted serial execution (r=-0.54 vs mean concurrent
  delegations, controlling for delegation duration) against a measured mean
  concurrency of 1.21 on a median 15 delegations per run.
- **Status:** core.

## B. The deliverable (pipeline.ipynb)

### Notebook authoring + reproduction gate
- **What:** the single deliverable is a Jupyter notebook; the runtime re-executes it
  lazily and accepts it only if it runs cleanly, adds zero new oracle evals, and
  leaves the ledger unchanged. The printed `REPRODUCED:` headline is informational —
  the critic checks its provenance (it must trace to a real ledger row); the runtime
  no longer machine-matches it to an objective extremum (that wrongly rejected
  constrained optima — audit 20260624T021359).
- **Where:** `notebook_exec.py`, `nodes/tools/routing/`, `nodes/reproduction_gate.py`
  (`_reproduction_gate`).
- **Tools:** `WriteCell` (create / edit / delete one named cell — what were
  four tools), `ShowNotebook`, `RunNotebook(gate=True)` (the Done() gate as a
  dry run), and `WriteDeliverable` for the study's declared extra files only.
- **Status:** core (the live deliverable).

### Hypotheses-cell status table is generated, not hand-maintained (NOTEBOOK-LEDGER SYNC, by construction)
- **What:** the hypotheses cell owns one delimited block (`<!-- adda:ledger-
  status:begin -->` … `end -->`) — an ID/status/posterior/one-line-statement
  table rendered straight from `hypotheses.json` — so a stale per-hypothesis
  status can never reach a gate check or the critic. The agent's own
  narrative around the block is free-form and untouched; the block itself is
  never hand-edited. Removes an error class rather than detecting it: 2 of 3
  `example_study` Haiku runs lost a gate round to a hand-maintained status
  cell drifting from the ledger (`20260928T024626` call_001 REVISE,
  `20260928T141126` call_002 REJECT CRITICAL).
- **Where:** `nodes/tools/routing/notebook.py` (`refresh_hypotheses_ledger_block`,
  `_refresh_ledger_block`, `_render_ledger_status_block`), wired into
  `WriteCell`'s hypotheses-cell create/edit paths and into
  `nodes/reproduction_gate.py`'s `_reproduction_gate` (one hook covers both
  `RunNotebook(gate=True)` and `Done()`'s pre-critic check, since both funnel
  through it). Stated to every role in `prompts/deliverable_format.py`'s
  `DELIVERABLE_FORMAT` (shared verbatim by the strategizer, implementer, and
  critic).
- **Status:** core (part of the deliverable contract).

### The reproduction gate is its own ablatable feature, independent of `pipeline_deliverable`
- **What:** the gate's mechanical enforcement, its agent-facing description,
  and the milestone that exists only because of it are now owned by a
  dedicated `reproduction_gate` Feature (`runtime/features.py`), separate
  from `pipeline_deliverable` (which decides only whether a notebook is
  REQUIRED at all). Off: `_reproduction_gate()` returns `None`
  unconditionally (Done()'s gate never runs; `RunNotebook(gate=True)`
  always reports a pass), the `<reproduction_gate_contract>` prompt
  injection is withheld, and the `oracle_gold_state` process milestone
  (`epistemics/milestones.py`) is not seeded — that milestone's whole
  reason to exist is this gate's store-row precondition. A study can
  require a notebook without requiring it to reproduce; the reverse is not
  possible — `reproduction_gate` `requires` `pipeline_deliverable`, so with
  no notebook required it resolves to off, and the run REFUSES TO START
  (`run_setup._init_canonical_store` raises on any `features.conflicts()`,
  naming both knobs and the fix) rather than log a warning an arm campaign
  would not read. A study that turns the notebook off sets
  `reproduction_gate: false` too; the same holds for every `requires` pair
  (e.g. `verdict_validator` needs `hypothesis_ledger`).
  Recorded per run in `run_config.json`'s `runtime` block like every other
  knob, so a sweep's arms can be told apart after the fact.
- **Arm recording and resume drift check:** `features.arm_config()` returns the
  effective value of every ablation feature plus `max_awake_nodes`, defaults
  included. It is written to `run_config.json["arms"]` (`run_setup._init_canonical_store`),
  to every `run_status.json` write, and to the `arm_*` columns of
  `studies/run_ledger.csv`. A resume under different arms raises unless
  `runtime.allow_arm_drift` is set, in which case the first arms stay in
  `arms_initial`.
- **`--set key=value` on both CLIs:** `python -m adda` and `python -m adda.watchdog`
  take a repeatable `--set` (`runtime/cli_overrides.py`), the command-line
  spelling of `AgenticRun(runtime=...)`: explicit precedence, validated against
  `KNOWN_KEYS` before launch, forwarded by the watchdog to its child (refused
  with `--entrypoint`). `python -m adda --model` now defaults to None, so the
  study's `config.yaml` `model:` is no longer silently overridden by the
  haiku default.
- **`Feature.requires`:** a feature whose prerequisite is off is off —
  `features.enabled()` resolves it, `features.conflicts()` lists the cases
  where its own knob said on, and `_init_canonical_store` logs them. Today
  `verdict_validator` requires `hypothesis_ledger`. The pipeline/reproduction
  knobs are deliberately NOT coupled (four studies run `pipeline_deliverable:
  false` with the gate on), and they are now read through `features.enabled`
  everywhere instead of `get_bool(key, True)` literals.
- **Feature-gated prompt text:** `[[if key]]on[[else]]off[[/if]]` inline gates
  (nesting allowed) in any prompt or tool docstring, resolved by
  `features.resolve_gates` inside `system_prompt_with_catalog`; the on branch is
  kept byte-for-byte, an unknown key or unbalanced marker raises. KB chapters
  take a `feature:` frontmatter key and vanish from menu/TOC/search/`get` while
  it is off. The critic's GATE message is composed from the enabled features.
- **Topology gates:** `[[if node:<name>]]` follows the graph's composition, not a
  knob. `build_graph` records the node set (`settings.set_graph_nodes`, cleared by
  `settings.configure`), so with the critic or the literature reviewer removed the
  remaining prompts no longer mention or rely on it; `ConsultLiterature` stays
  (it reads whatever corpus exists) and says the corpus may be empty.
  `tests/test_prompt_arms.py` assembles both topology arms. Handbook chapter
  bodies are resolved the same way when served (`KnowledgeBase._live`), so a
  chapter can carry `[[if node:<name>]]` text too.
- **Grounded in code, not paraphrased:** what the gate mechanically checks
  (the canonical store must hold ≥1 row before it will even run the
  notebook; zero new evals on replay; no modified/deleted rows; headline
  self-consistency) reaches the agent via `gate_contract()`, which
  extracts `_reproduction_gate`'s own docstring live via `inspect.cleandoc`
  — the same idiom `prompts/tool_catalog.py` already uses for every tool's
  agent-facing description — so the prompt text IS what the code enforces
  and cannot drift into a hand-maintained paraphrase sitting beside it.
  Raised by run 20260927T034538 (`studies/lcp_matlab_regression`): the
  store-row precondition existed in code but was never stated anywhere the
  agent could read it up front — it was discoverable only after the gate
  had already bounced Done() several times. See `internal/BACKLOG.md` #42
  for the still-open epistemic question this does NOT resolve (a study
  that fakes a token evaluation purely to satisfy the row check currently
  gates cleanly; a study that honestly refuses to fake one does not).
- **Where:** `runtime/features.py` (`reproduction_gate` Feature),
  `nodes/reproduction_gate.py` (`gate_contract`, the feature-flag check in
  `_reproduction_gate`), `runtime/agent_runtime.py` (the injection site,
  mirroring `pipeline_deliverable`'s), `epistemics/milestones.py`
  (`seed_defaults(include_reproduction_gate=...)`, `ORACLE_GOLD_STATE`).
  **Status:** core — ablation only; does not change WHAT the gate checks.

### `SendMessage` — the peer/human messaging tool (spec 12, migration complete)
- **What:** one tool for all peer and human messaging —
  `SendMessage(to, message, wait_for_reply=False, approve=False)` —
  replacing `Confer`/worker-`FollowUp`/`Reply`/`ReportProgress` (`internal/specs/
  12-peer-interaction.md`). A shared, node-level closure (used by every thread
  regardless of role, exactly like `Delegate`/`Wait` — the calling
  thread's own thread-local delegation id resolves "who am I" per call,
  since one Node object is shared across concurrent same-role
  delegations that are each their own separate delegator identity) that
  can message either a node's own children (downward) or its own
  delegator (upward) from the SAME call, disambiguated by whether `to`
  resolves to one of the caller's own children. `wait_for_reply=True`
  blocks and returns the reply in the same call, with a DEADLOCK GUARD:
  it wakes on ANY message from that peer (their reply, or a fresh
  question of theirs), so two peers `SendMessage`-ing each other at once
  never both hang — verified with real `threading.Thread`s, not mocked
  waits (`tests/test_send_message.py`): a two-worker fan-out where both
  ask at once (each answer reaches only its own asker), the two-sided
  deadlock case, a delegator blocked in `Wait()` waking on a worker's
  question mid-fan-out, and a nested worker-delegator only ever seeing
  its OWN children's messages, never a sibling delegation's.
- **Ablation:** gated behind `peer_interaction` (`runtime/features.py`),
  default **True** like every other Feature since the migration-sweep
  commit. On: `SendMessage` is granted and reports open for review. Off
  (**meaning changed**: it used to restore the pre-spec-12 `Confer`/`Reply`/
  worker-`FollowUp` surface, which has been deleted): the "no peer
  messaging" arm — no `SendMessage`, no pending-for-you notice, reports
  finalize on delivery, and only the entry node keeps a human channel
  (`FollowUp`, the operator question; with the feature on the same channel is
  `SendMessage(to="human")`). Prompt text that names the channel is gated on
  the knob. `Wait` was
  also re-tightened to outgoing-edges-only in this same series (a separate
  commit, `1d7e14c`) — see BACKLOG's spec 12 entry.
- **Cross-node lookup:** a delegation's registry entry lives in its
  DELEGATOR's Node, but a worker calls the closure bound to its OWN Node.
  `Node._delegation_entry` finds an entry across the graph's nodes
  (`Node._peers`, set by `build_graph`), which is what the worker's
  upward `SendMessage` and its pending-for-you notice both use. Before
  this a worker could never reach its delegator (run 20260928T225501, D014:
  "no live delegation found for 'strategizer'"). Test:
  `tests/test_send_message_cross_node.py`.
- **The review gate (spec 12 item 3).** With `peer_interaction` on, a
  worker's non-error report NEVER finalizes on its own: `WorkerSession.
  run()` calls `_open_for_review` instead of `_finish_ok`, moving the
  delegation to a new `OpenForReview` status (recording the worker's CLI
  `last_session_id`, captured on every `ainvoke()` — the actual RESUME
  invocation for a non-approve message is still not built, see below).
  Only `SendMessage(id, ..., approve=True)` runs `_finish_ok`
  (`finalize_after_review`, reusing the stashed report/evals/usage
  unchanged); any other message is recorded (queued) but says plainly
  that it will not reach the worker until session-resumption itself
  lands. Automatic, not opt-in — every delegation, whenever the feature
  is on. `Delegate` refuses a new dispatch while ANY delegation is
  `OpenForReview` (`_check_open_reviews`), naming every open one; an
  ERRORED delegation never counts (it has no report to review). `Wait()`
  treats an open review the same shape as a `FollowUp` block — named in
  its own bucket, never silently absorbed into "nothing to wait for" —
  while `Wait(id)` still returns its report text (the "read" event).
  `Delegate(wait=True)` also names an open review explicitly rather than
  misreporting a successful worker as `Errored`. If the run closes
  (`Done()`, watchdog, budget cutoff) with reviews still open, each is
  swept and recorded honestly as `OPEN_UNAPPROVED` in the delegation log
  and the delegating node's retrospective — never as `DONE`, never
  silently dropped. Persisted at the TRANSITION itself, not swept in at
  close: `WorkerSession._open_for_review` writes an `OPEN_FOR_REVIEW`
  delegation-log row the instant the report exists, so the log's own
  last-wins collapse makes that row the honest final record even if the
  process dies (a crash, a watchdog kill) before anyone approves it —
  `AgenticRun._sweep_open_reviews` (called from `_finalize_run` AND the
  crash path) only appends a small close-time `OPEN_UNAPPROVED` status
  update on top, and reaches every live Node via `AgenticRun._live_nodes`
  (populated by `graph_builder.build_graph`'s own `node_registry`
  out-param — an explicit reference, never LangGraph's compiled-graph
  internals). `write_watchdog_retrospective` calls out any delegation
  whose LAST log row is `OPEN_FOR_REVIEW` explicitly, for the hard-kill
  case where no in-process sweep ever runs.
- **The pending-for-you notice (design item 10(a)).** Every tool result
  from a node's OWN closures carries a compact notice naming what that
  CALL's own delegator identity currently owes — computed FRESH every
  call (`Node._pending_for_you`), never a drained queue, so "nothing
  owed" stays silent indefinitely rather than firing once. Reuses the
  exact insertion point `_drain_notifications` already uses in
  `_wrap_closure`. As a DELEGATOR (`entry.get("parent") == identity` —
  never a sibling's or a nested child's): an open review, a worker's
  unanswered `FollowUp`, a finished delegation not yet collected, and —
  Elvis's own first-named case, "respond [to a] delegation" — an unread
  `SendMessage` question sitting in a child's `to_delegator` queue
  (`"D003 (implementer) asked you: ..."`, truncated to ~100 chars —
  FollowUp's bucket alone doesn't cover this, since FollowUp is being
  retired). As a WORKER (`identity` is itself a registry entry): an
  unread `SendMessage` from its OWN delegator sitting in that entry's
  `to_worker` queue (`"your delegator sent you a message: ..."`). Every
  queue check is a PEEK under the queue's own lock (never a pop — that
  stays `Wait`'s/`SendMessage`'s job), sharing one lock acquisition with
  the check, the same race rule as the queues' real consumers.
- **Where:** `nodes/tools/routing/delegation.py` (`DelegationTools.
  SendMessage`, `_resolve_send_target`, `_check_open_reviews`,
  `_handle_review_message`, `WorkerSession._open_for_review`/
  `finalize_after_review`, `_status`'s `OpenForReview` branch, the
  `parent`/`to_worker`/`to_delegator`/`worker_cond` fields
  `_register_dispatch` now stamps on every registry entry),
  `nodes/orchestration.py` (`_get_delegator_cond`, the
  per-delegator-identity `Condition` registry this all synchronizes
  through, `_worker_sessions`, `_pending_for_you`, its hook in
  `_wrap_closure`), `nodes/tools/routing/__init__.py`
  (`build_routing_tools`'s feature gate), `backends/claude.py`/
  `backends/openai_compatible.py` (`last_session_id` capture — always
  `None` on the latter, which has no server-side session; parity with
  `ClaudeAdapter` enforced by `tests/test_backend_parity.py`),
  `runtime/agent_runtime.py` (`_live_nodes`, the close-time sweep),
  `runtime/graph_builder.py` (`node_registry` out-param),
  `infra/watchdog_cleanup.py` (`write_watchdog_retrospective`'s
  open-review callout), `runtime/features.py` (`peer_interaction`).
- **Session-resumption itself, built.** A non-approve `SendMessage` to an
  open review (`_handle_review_message`) resumes the worker's session on
  its OWN background thread (`WorkerSession.resume_and_revise`) — the
  `SendMessage` call returns immediately, and the delegation moves to a
  new `Revising` status meanwhile (blocks a new `Delegate` the same as
  `OpenForReview`; `Wait`/`_status` report it distinctly, never as
  `Errored`). `ClaudeAdapter.ainvoke`/`invoke` gained a `resume`
  parameter → `ClaudeAgentOptions(resume=session_id, fork_session=
  False)`. Resume failure (or no `session_id` at all, e.g.
  `openai_compatible`) falls back to a THIN 2-message reconstruction
  (original task + original report, never the intermediate transcript —
  that exists only when debug is on, and fidelity silently depending on
  a debug flag would be worse than an honest thin reconstruction),
  telling the worker its context was rebuilt and pointing it at its own
  `D###/` delegation directory to re-read rather than assume it
  remembers intermediate steps. Every fallback records a
  `REVIEW_RESUME_FALLBACK` diagnostic (delegation id, session id,
  reason) — never silently. The revised report re-opens for review
  through the SAME `_open_for_review` path the first report used.
  Three more correctness fixes on top (second review round): approving
  WHILE a revision is in flight is refused (it would finalize the STALE
  pre-revision report); a second `SendMessage` sent during that same
  window still reaches the worker (queued in `to_worker`, surfaced via
  its own pending-for-you peek) and, if the revision finishes before
  anything reads that queued message, `_open_for_review`'s re-open
  explicitly tells the delegator it's still sitting there unread; and
  the "read" enforcement (spec item 4) is now actually CHECKED, not just
  documented — `_handle_review_message` refuses both `approve=True` and
  feedback unless the entry's `"waited"` flag (reused from its existing
  Done/Errored meaning) shows the report was actually delivered via
  `Wait`/`Wait(block=False)`/`Delegate(wait=True)`, reset on every
  `_open_for_review` so a revised report must be read again too.
  **Where:** `nodes/tools/routing/delegation.py`
  (`WorkerSession.resume_and_revise`/`_try_resume`/
  `_fallback_reconstructed_invoke`/`_record_resume_fallback`,
  `_handle_review_message`, the `Revising` status in
  `_check_open_reviews`/`_wait_for_any`/`_status`, `_open_for_review`'s
  `"waited"` reset and unread-message notice), `backends/claude.py`
  (`ainvoke`/`invoke`'s `resume` parameter).
  **Status:** done — migration sweep landed (`peer_interaction` defaults
  True; the legacy `Confer`/`Reply`/worker-`FollowUp`/`ReportProgress` surface deleted,
  see `internal/specs/12-peer-interaction.md`). The corrective
  retry-on-malformed cycle NOT repeating for a revised report is a
  deliberate, accepted tradeoff (every revision is reviewed by the
  delegator anyway), not an open gap.

### Per-cell notebook debugger (#13)
- **What:** run pipeline.ipynb against a *copy* of the ledger and get a per-cell
  pass/error trace, so a failing cell can be pinpointed instead of guessing.
- **Where:** `notebook_exec.py` `diagnose_notebook`, `RunNotebook` closure.
- **Tools:** `RunNotebook` (its default mode). **Status:** done.

### Unified, SDK-compatible Bash surface: `Bash` + `BashOutput` + `KillShell` (#24)
- **What:** one tool SURFACE across every backend. `Bash(command, timeout?,
  run_in_background?, description?, dangerouslyDisableSandbox?)` runs foreground
  by default; a command that exceeds its timeout is **backgrounded, not killed**,
  and returned with a `bash_id` the agent polls via `BashOutput(bash_id)` and
  stops via `KillShell(bash_id)`. Matches the Claude-Agent-SDK Bash param and
  tool names so agents don't relearn behavior. The one deliberate deviation:
  auto-background is made **visible** (an `interrupted` notice + `bash_id`) so an
  agent never wakes up thinking a still-running job finished. Declared by the
  implementer, datagenerator, and debugger.
- **Two implementations, one surface** (the standard pattern here): on Claude the
  SDK executes Bash/BashOutput/KillShell natively (they are SDK built-ins — we
  now enable the two companions we had omitted from `NATIVE_TOOLS`); on
  ollama/vllm/openrouter the framework provides them via `_BashSession` +
  `_make_bash_tool`/`_make_bashoutput_tool`/`_make_killshell_tool`.
- **Safety:** framework-backgrounded children stay in the run's process group
  (no `start_new_session`), so the watchdog group-kill reaches them; the bg pid
  is best-effort registered in `governor_pids.jsonl`; `KillShell` is the only
  per-delegation teardown.
- **Where:** `backends/claude.py` (`NATIVE_TOOLS`), `backends/openai_compatible.py`
  (`_BashSession`, the three factories, `_native_tool_map`).
- **Status:** done. Supersedes the earlier `WaitForProcess` stopgap.

### Sandbox study-root anchor (`F3DASM_STUDY_ROOT`)
- **What:** the reproduction gate, `RunNotebook` (both modes), and the
  scratch tool run against a *temp copy* of the ledger, so the store path has no
  relationship to the study repo. They now also inject `F3DASM_STUDY_ROOT` (a
  read-only anchor to the real study root) so a pillar cell can locate non-ledger
  repo resources (e.g. `bo/cei_core.py` for a surrogate self-check) deterministically
  instead of hand-rolling multi-candidate path search. Store isolation is unchanged —
  only the store is a copy; the study root is read-only reference code. The three
  duplicated sandbox-env blocks are unified in one `sandbox_env()` helper.
- **Where:** `notebook_exec.py` `sandbox_env`; call sites in `nodes/reproduction_gate.py`
  (`_reproduction_gate`) and `nodes/tools/routing/` (`RunNotebook`, scratch).
- **Status:** telemetry/ergonomics, not a new cap. Run 20260705T181941 friction.

### Sandboxed Write also reaches the study's workspace/
- **What:** `build_sandboxed_write` (a worker's `Write` tool) used to hard-reject
  any path outside the delegation's own `D###/` subfolder, full stop — but study
  problem statements routinely name deliverable paths under the study's OWN
  `workspace/` (e.g. `studies/lcp_matlab_regression/PROBLEM_STATEMENT.md`'s
  "workspace/km_baseline.m", the same convention `config.yaml`'s
  `evaluator.entrypoint` uses), and a worker had no sanctioned way to satisfy
  that instruction (run 20260926T124841/20260926T214835: rejected, then written
  via Bash instead — trusted and unsandboxed, so invisible to this guard
  entirely; Elvis's decision: keep Bash trusted, extend Write instead of
  sandboxing Bash). `Write` now accepts an OPTIONAL second permitted root, the
  study's `workspace/`, passed as `study_workspace=` at both call sites
  (`node.py::_setup_sandboxed_write`, `delegation.py::_setup_worker_write`). A
  bare relative path (`workspace/foo.m`, or exactly `workspace`) resolves
  against the STUDY root rather than the delegation root; an absolute path
  that already resolves under the study's workspace/ is accepted either way.
  Both permitted roots resolve symlinks/`..` before the containment check, so
  the allowance can't be used to escape into `runs/<other>/` or the study root.
  The rejection message now states BOTH permitted directories honestly — no
  implied sandbox the toolset lacks, and no mention of Bash (a separate,
  deliberately-unsandboxed boundary, not this guard's concern).
- **Naming fix, same commit:** the corpus used "workspace"/"workspace_dir" for
  TWO different directories — the study's own `workspace/` (this feature) and
  the per-delegation sandbox root (`<run>/debug/delegations/`) — under the
  SAME bare word. `RUN_PATHS_PREAMBLE_TEMPLATE`, `WORKSPACE_PREAMBLE_TEMPLATE`,
  the `<bash_tool>` block, and `deliverable_format.py` now say "delegation
  directory" / "delegations_dir" / "delegation_root" for the sandbox root,
  reserving "workspace/" exclusively for the study folder everywhere an agent
  reads it.
- **Where:** `nodes/tools/routing/delegation.py` (`build_sandboxed_write`,
  `_setup_worker_write`); `nodes/node.py` (`_setup_sandboxed_write`);
  `prompts/agent_prompts.py`; `prompts/deliverable_format.py`.
- **Status:** done. Bash remains trusted and unsandboxed by design — not
  addressed here; see the (deferred) Bash-boundary discussion this same
  finding raised.

### Output-column guidance fix
- **What:** notebook guidance requires naming the objective column EXPLICITLY. The
  earlier "first non-provenance output" auto-detect was unsafe: `output_names` is
  sorted, so with multiple outputs a constraint flag (e.g. `coilable`) sorts before
  the objective and gets silently picked (audit 20260624T021359). If derived, read
  `run_config['evaluator_output_names'][0]`, not column order.
- **Where:** `notebook_exec.py`. **Status:** done.

## C. Workers & ground truth

### Metered oracle (get_evaluator) + canonical ledger
- **What:** the one door to the registered ground-truth oracle; every evaluation is
  written to the canonical store with provenance, under a file lock.
- **Where:** `instrumented.py` (the wrapper), `oracle_resolution.py` (`get_evaluator`).
  **Tools (worker scratch):** `RunScratch`, `ReportEvals`.
- **Status:** core.

### Design namespaces — multiple oracles + ledgers per run (#20, Axis 3)
- **What:** a run may carry more than one oracle, one per design parametrization the
  agent invents (open design-space discovery). `get_evaluator(namespace=None)` resolves
  `run_config["oracles"][namespace]` — its own oracle + its own isolated, protected
  store; `Delegate(..., namespace="…")` scopes a worker to a namespace (injected as
  `F3DASM_NAMESPACE`, so the agent's call site stays `get_evaluator()`); the
  datagenerator registers a namespace oracle without disturbing the canonical default.
  ADDITIVE: `namespace=None` is byte-for-byte the single-study path. Comparable-by-
  construction (a new design reuses the fixed objective evaluator; see
  `OPEN_DESIGN_SPACE_FRAMEWORK.md`).
- **Where:** `oracle_resolution.py` (`get_evaluator`, `_effective_oracle_config`),
  `run_setup.py` (`register_evaluator_entrypoint(namespace=…)`), `backends/base.py`
  + `backends/claude.py` (`set_namespace`/`F3DASM_NAMESPACE`), `graph_state.py`
  (`Delegation.namespace`), `routing.py` (`Delegate` + registration handoff).
- **Report-time provenance:** `QueryStore()` with no arguments (it absorbed `RecallStore` and `LedgerBreakdown`) shows per-experiment
  / per-delegation ledgered eval counts read live from the stores
  (`ledger_summary.ledger_breakdown`), so a writeup DERIVES counts from the ledger instead
  of hardcoding stale plan numbers (run 20260628T001710 hardcoded 70 polar evals; the
  ledger held 90 → UNGATED). It also reads `eval_budget` from run_config and prints
  `spent of budget — N remaining`, so the agent READS that number rather than hand-
  computing it and flipping spent↔remaining (run 20260628T130525 asserted "200 remain"
  with 200 spent of 300 → UNGATED). Read-only; spends no eval budget.
- **Multi-experiment load idiom:** `adda.load_experiments()` (`ledger_summary.
  load_experiments`) loads every experiment store of a run as `{name: ExperimentData}`
  (default + each design experiment, at their nested paths). A namespaced run has N
  stores and no namespace column, so the single-study `from_file` idiom silently loads
  only the default; this is the one call pipeline.ipynb uses to load them all. Wired
  into the deliverable spec (`notebook_exec.py`) and the injected paths block
  (`agent_prompts.py`).
- **Tools:** `QueryStore`.
- **Status:** plumbing complete (branch `exp/open-design-space`); gated on the 2D
  experiment before the baseline study adopts it.

### Literature reviewer
- **What:** a specialist agent that searches papers (arXiv / Semantic Scholar /
  OpenAlex) and returns findings; degrades to lexical search without the heavy
  extras. It holds five tools: `ConsultLiterature` and `CorpusAdd` (the corpus;
  `CorpusAdd` also takes a PDF URL), `SearchPapers` (all three databases in
  parallel, the same paper merged into one entry, and a first line saying how
  each provider did — so a throttled or failing provider is visible without
  being a separate tool; the arXiv leg requires EVERY word, as
  `all:w1 AND all:w2 ...`, because arXiv ORs bare words, and relaxes to the
  longest ~60% of the words and then the raw text if that returns
  nothing — `discovery._arxiv_queries`), `CitationGraph` (citing / references / similar,
  OpenAlex first with Semantic Scholar as fallback) and `PaperDetails`.
  Every one of them (and `ConsultLiterature`, which reads a whole paper) takes
  `offset` and returns a capped page (`throttle._cap_result`; 6000 chars, 12000
  for a paper read) whose end names the exact call for the next page, so
  nothing past the cap is lost.
  These replaced thirteen per-provider tools and the
  `wait=False` / `CollectSearches` async pool the agent used to fan them out by
  hand; the per-provider calls stay as plain functions with their own tests. Its
  corpus (`runs/lit_reviewer_notes/`) is STUDY-scoped, not per-run — it
  persists across every run of a study, since a paper's relevance to a
  domain doesn't go stale between runs the way scientific findings can
  (deliberately NOT extended to cross-run hypothesis/mechanism memory,
  which was rejected as too risky — a study's actual question can drift run
  to run). Lives under `runs/`, not the study root, to stay out of the
  user-facing study folder; guarded by a `FileLock` (not a `threading.Lock`)
  since two runs of the same study are now real, separate processes that can
  overlap and both write to it.
- **Semantic Scholar key handling:** one client builder
  (`semantic_scholar.get_semantic_scholar_client`, `retry=False`) serves every
  tool path; `_throttled_ss` is the sole retry authority. A 403 means the key
  was rejected (429 is quota): the first 403 with a key drops it for the
  process, retries that call once keyless, and writes one `LIT_KEY_REJECTED`
  diagnostics event (`source=semantic_scholar`). Tests:
  `tests/test_semantic_scholar_client.py`.
- **Where:** `agents/literature.py` (prompt + agent), `agents/literature_tools/`
  (`discovery.py` — the three discovery tools; one module per provider:
  `corpus.py`, `semantic_scholar.py`, `openalex.py`; `throttle.py`), `literature/` (`literature_corpus.py` —
  the on-disk corpus; `http_client.py` — the shared per-domain rate limiter,
  circuit breaker, GET cache and `_robust_get`/`_robust_post`; `embedder.py`
  + `_embed_worker.py` — the out-of-process dense embedder),
  `runtime/agent_runtime.py`'s `_make_adapter`. **Status:** core.

### Universal read-only corpus lookup
- **What:** EVERY agent (not just the literature_reviewer) gets `ConsultLiterature`
  for free (no query lists the corpus, a paper_id reads that paper, anything
  else searches passages) — read-only lookup against the
  study's persistent literature corpus, injected via
  `Agent.build_closure_tools`'s own default. Same rationale as `QueryStore`
  letting every node read the canonical evaluation ledger without delegating
  to the data generator: the corpus is shared, queryable infrastructure, not
  something only its specialist may read. ACQUIRING a new paper (`CorpusAdd`,
  external search) stays literature_reviewer-only — finding/vetting a new
  paper needs judgment a raw tool call can't supply, so it stays gated behind
  an actual delegation. `LiteratureReviewAgent.build_closure_tools` overrides
  the base default entirely (its own read tools + `CorpusAdd` + search) rather
  than extending it.
- **Where:** `backends/base.py`'s `Agent.build_closure_tools` default.
  **Status:** core.

### Retrieval degradation is a diagnostics event, not just a log line
- **What:** when `ConsultLiterature`'s dense embedder is unavailable (fastembed
  unimportable in-process AND the out-of-process embed-worker probe failed —
  the sandboxed/Slurm case), retrieval silently downgraded to BM25 (lexical)
  only, and the only trace was a `log.warning()` nobody watching a run would
  see — not even the agent doing the searching. `LiteratureCorpus` now records
  *why* on `embedder_fallback_reason` and exposes it exactly once per corpus
  instance via `pop_diagnostic_event()` (`("RETRIEVAL_DEGRADED", reason)`).
  `ConsultLiterature` tags itself with its corpus
  (`fn._adda_diagnostic_source = corpus`); `orchestration.py`'s
  `_wrap_closure` — the one place with both a per-run diagnostics path and,
  via the tag, a handle back to the corpus — pops the event, records it to
  `debug/diagnostics.jsonl` via `_record_intervention` (event `RETRIEVAL_DEGRADED`,
  same neutral `fault="nudge"` classification as other environment-fact
  events, not an agent error), and prepends an `<adda-note>`-wrapped notice
  (`nodes/notices.py`) to that call's result so the agent itself learns its
  literature coverage is reduced. One diagnostics path, fired once per run —
  not a second writer, not a node-handle threaded into the corpus module.
- **Where:** `literature/literature_corpus.py` (`embedder_fallback_reason`,
  `pop_diagnostic_event`); `agents/literature_tools/corpus.py`
  (`build_corpus_read_closures` tagging); `nodes/orchestration.py`
  (`_wrap_closure`). **Status:** core.

### A stream ending mid-turn, and the retry it silently triggers, are now diagnostics events
- **What:** `ainvoke()` (backends/claude.py) can reach the end of the CLI's
  stream with neither a `ResultMessage` nor a deliberate `route_watcher`
  break — the CLI session ended (or the SDK's async generator was
  exhausted) before the turn actually finished, e.g. mid a Bash/TaskOutput
  call. That used to return silently: whatever near-empty `text` the last
  `AssistantMessage` carried (often nothing, since a tool-call message
  rarely has a `TextBlock`) came back as if the turn had completed
  normally. Reaching `WorkerSession._invoke_with_report_retry`
  (`nodes/tools/routing/delegation.py`), that near-empty text reads as a
  MALFORMED report (`_classify_response`), which silently triggers ONE
  corrective retry — a fresh `worker.invoke()` call, a new CLI session,
  with the delegation's ORIGINAL `task_msg` still the bulk of the prompt.
  Nothing was logged on either side: an investigation (`root-cause work,
  Oscar run 20260830T004106`) could see a delegation's first CLI session
  end abruptly and a new one start seconds later with essentially the same
  task, with no trace anywhere of what happened or why — indistinguishable,
  after the fact, from a silent restart-from-scratch. Two new diagnostics
  events close that gap (report only; neither changes the retry/return
  behaviour itself — that's a separate, still-open decision, see the repo's
  private notes on report 7): `STREAM_ENDED_WITHOUT_RESULT` (`ainvoke`,
  fault="system" — the last tool in flight and the CLI's own session id,
  when available) and `REPORT_RETRY` (`_invoke_with_report_retry`,
  fault="nudge" — the classification reason, fired every time a report-retry
  actually happens).
- **Where:** `backends/claude.py` (`_record_stream_diagnostic`, the
  `_deliberate_break`/`last_result is None` check in `ainvoke`);
  `nodes/tools/routing/delegation.py`
  (`WorkerSession._invoke_with_report_retry`). **Status:** core — observability
  only; the underlying retry-on-a-possibly-in-flight-turn behaviour is a
  deliberate open question, not addressed here.

### SystemMessage is a transcript record, not a silent drop
- **What:** `ainvoke()`'s `_record()` (backends/claude.py) mapped every
  `claude_agent_sdk.SystemMessage` to `None` — including `compact_boundary`,
  the SDK's own signal that it force-compacted a session's context mid-turn.
  A run that got compacted mid-delegation left no trace anywhere: not the
  transcript, not diagnostics, nothing an analyst could grep for short of
  reasoning backward from a sudden context/behaviour discontinuity. A live
  probe of a real short session's raw message stream (bypassing adda's own
  filtering) showed the actual subtype distribution is `{'init': 1,
  'status': 2, 'thinking_tokens': 47}` — `thinking_tokens` alone accounts for
  the bulk (measured 47-103 of 50-119 SystemMessages per short session) and
  carries no information an analyst needs. `_record()` now denylists only
  that one subtype (`_SYSTEM_MESSAGE_NOISE_SUBTYPES`) and records everything
  else — including any future/unknown subtype, which defaults to VISIBLE —
  as a `{"type": "system", "subtype": ..., "data": ...}` transcript record.
  `compact_boundary` additionally fires an unconditional `CONTEXT_COMPACTED`
  diagnostics event (`_record_stream_diagnostic`, thread-local delegation
  id/run dir, the SDK's own compaction metadata verbatim) so the event shows
  up in `debug/diagnostics.jsonl` without anyone reading transcripts.
- **Where:** `backends/claude.py` (`_SYSTEM_MESSAGE_NOISE_SUBTYPES`,
  `_record()`'s new `SystemMessage` branch, the `compact_boundary` check in
  `ainvoke`). The viewer (`viewer/app.py::_bubble_html`) already renders any
  unrecognized event type as an empty fragment, so the new `"system"` type
  needed no viewer change — only a regression test confirming it. **Status:**
  core — observability only.

### CONTEXT_COMPACTED/STREAM_ENDED_WITHOUT_RESULT now fire on every node, not just worker delegations
- **What:** `_record_stream_diagnostic` (above) finds `debug/` through the
  thread-local `run_config_path`, and `WorkerSession._bind_backend_context`
  (`nodes/tools/routing/delegation.py`) was the ONLY place that ever bound
  it — so any `ClaudeAdapter.invoke()` call OUTSIDE a worker delegation ran
  with neither `run_config_path` nor `delegation_id` set, and the diagnostic
  silently found nowhere to write. That reached the entry node's OWN turns
  (`orchestration.py::_invoke_turn`) — the longest-lived sessions in a run
  (one Oscar strategizer session measured 542k tokens) and thus the most
  likely to hit a forced compaction — plus the critic gate's two call sites
  (`critic_gate.py::_invoke_critic`, `_invoke_verdict_validator` — the
  latter a per-strategizer-tool-round nested side-query) and the one-shot
  pre-run problem-statement review (`runtime/agent_runtime.py`,
  `_review_problem_statement`). A new context manager,
  `backends/base.py::bind_run_context`, binds both thread-locals for the
  duration of a call and restores whatever was bound before on exit; all
  four call sites now wrap their `adapter.invoke()`/`worker.invoke()` with
  it, each keyed to a stand-in id (`"{node}-turn-{NNN}"`, `"critic-{n}"`,
  `"verdict-validator"`, `"problem_statement_reviewer"`) since none of these
  calls has a real delegation id. One writer (`_record_stream_diagnostic`),
  no new global state — only the binding reaches more call sites.
  Separately checked: the SDK's `init` SystemMessage's `data` (now recorded
  verbatim per the entry above) was confirmed via a live probe to carry no
  token/credential value — `cwd`, `session_id` (a UUID), `model`, tool/skill/
  agent lists, capability flags, and a `memory_paths` directory path, nothing
  secret.
- **Where:** `backends/base.py` (`bind_run_context`); `nodes/orchestration.py`
  (`_invoke_turn`); `nodes/critic_gate.py` (`_invoke_critic`,
  `_invoke_verdict_validator`); `runtime/agent_runtime.py`
  (`_review_problem_statement`). **Status:** core — observability only.

### Delegation-bounded version control of the run workspace
- **What:** one git repository per run, rooted at the run's own
  `debug/delegations/` workspace, with one commit per delegation (DONE and
  FAILED alike) and the resulting sha stamped onto that delegation's record as
  `workspace_sha`. Answers "which files did this delegation change" from the
  record rather than from the deliverable's own prose — the reproduction gate
  proves the notebook runs, it cannot prove a delegation's account of its own
  edits is faithful. Agents get no git tool and never see the repo: the
  harness commits on their behalf, since an agent that can rewrite the history
  recording its work defeats the purpose. The workspace normally sits INSIDE a
  checkout of adda, so every git call pins `--git-dir`/`--work-tree`
  explicitly (no directory discovery, no walking up into the parent repo, and
  those flags outrank inherited `GIT_DIR`/`GIT_WORK_TREE`), config is passed
  per-invocation so a global `commit.gpgsign` or `core.hooksPath` cannot block
  or hijack a commit, and `.gitignore` excludes `studies/*/runs/` so the parent
  cannot absorb the nested repo as a gitlink. Never fatal: no git, no repo, or
  a failed commit records `workspace_sha=None` and the run proceeds.
- **Where:** `infra/workspace_vcs.py` (`init_workspace_repo`,
  `commit_workspace`), initialised in `runtime/agent_runtime.py::_prepare_run`,
  committed in `nodes/tools/routing/delegation.py::WorkerSession._commit_workspace`
  from both `_finish_ok` and `_finish_error`; `workspace_sha` on
  `infra/delegation_log.py::DelegationLog.record`. Retires the `### Files
  touched` report subsection: with a mechanical record, an agent re-narrating
  the same list could only agree (noise) or disagree (a contradiction with no
  rule for which wins). Intent that a diff cannot express ("rewrote main.py to
  substitute before differentiating") belongs in `### Actions taken`, which is
  already the intent section. See
  `internal/specs/11-delegation-bounded-version-control.md`. **Status:** core

### The critic's brief carries a generated evidence index
- **What:** every critic call (GATE and FEEDBACK) gets an `<evidence_index>`
  block: per delegation its id, role, status, one-line intent, a report file
  the critic can Read directly (`debug/delegation_reports/<id>.md`, written
  from the delegation log, which keeps the report only inside one JSONL row)
  and the files the delegation touched (from the workspace git history via
  `workspace_sha`). Generated from the run's records, never hand-written; capped
  inline with a pointer to the full `debug/evidence_index.md`. A reviewer gets
  pointers to the evidence, not only the conclusions. Acceptance criteria
  unchanged.
- **Where:** `infra/evidence_index.py::evidence_index_block`,
  `infra/workspace_vcs.py::files_by_commit`; injected next to `<paths>` in
  `nodes/critic_gate.py::_build_feedback_task_msg` and
  `nodes/tools/routing/feedback.py::_gate_task_msg`. Test:
  `tests/test_evidence_index.py`.

### MathExpert — verified symbolic derivation
- **What:** a specialist agent (NOT part of `_default_graph()` — opt-in via a
  custom `Graph`, same precedent as `DebuggerAgent`) that authors and runs a
  Python script against `adda.Workspace` per derivation "edition"
  (`runs/math_workspace/<edition>.py`). Forces every algebraic/domain/
  dimensional claim through SymPy and reports a genuine three-valued verdict
  (`CONFIRMED`/`REFUTED`/`INCONCLUSIVE`) rather than a restated confidence; a
  physical assumption is recorded via `assume()` with a fourth verdict,
  `ASSERTED`, never adjudicated by the library. Durability and "revise an
  assumption" are both plain filesystem operations (read/rerun; copy-edit-
  rerun for a counterfactual) — no bespoke trace-replay or dependency-graph
  mechanism. `Workspace` seeds SymPy's own RNG at construction so a verdict
  that depends on `.equals()`'s randomized numerical fallback is reproducible
  across reruns of the identical script (verified directly: unseeded, this
  flips between `REFUTED`/`INCONCLUSIVE` across process runs).
  `write_summary()` writes a self-describing document
  (`{schema, workspace, counts, steps}`, each step carrying its `residual`)
  and appends that document to a sibling `<name>.history.jsonl` — one entry
  per execution, so the verdicts an edited-and-rerun script used to report
  survive being overwritten. Additive and write-only: the summary file
  remains the current state and the only thing a consumer reads.
- **Where:** `math_dsl.py` (the `Workspace` library, re-exported publicly as
  `adda.Workspace`), `agents/math_expert.py` (`MathExpertAgent`),
  `knowledge/entries/0011-symbolic-derivation-patterns.md` (worked-example
  guidance, `audience: [math_expert]`). See
  `internal/specs/10-math-expert-agent.md` for the full design rationale,
  including two heavier alternatives (a JSONL trace log, a dependency DAG)
  tried and dropped against a real published derivation. **Status:** core
  (library layer tested; graph wiring is per-study, not in the default
  topology).

### Delegation-ID allocation fix (D002)
- **What:** delegation IDs are allocated *after* the milestone gate, so a blocked
  attempt no longer burns an ID (IDs stay contiguous).
- **Where:** `nodes/tools/routing/`. **Status:** done.

## D. Resource governance (this is the big recent addition)

### Soft eval-budget nudge
- **What:** when the shared ledger crosses 80/100/150% of the eval budget, the
  *offender* (the running campaign) is nudged via its own output — capped at one per
  band. **Soft: never stops the campaign** (the eval budget is the agent's call).
- **Where:** `instrumented.py` `_flush` governor; budget plumbed via `agent_runtime.py`.
- **Config:** `eval_budget` (config.yaml / `F3DASM_EVAL_BUDGET`). **Status:** done.

### Hard memory cap (the one hard boundary)
- **What:** a 5-second watchman sums each delegation's process-tree **resident (RSS)**
  memory and kills the tree if it exceeds the cap. Real-usage based; verifies the
  process is still ours (start-time match) before killing, so a recycled PID is never
  hit. Per-delegation, absolute (not a share of system RAM), does not sum across
  delegations.
- **Where:** `studies/.../run.py` `_memory_watcher`; `watchdog_cleanup.py`
  (`check_memory_and_kill`, `_owned_pids`); `resource_backend.py`; the cap is
  resolved (config → env → SLURM allocation → default) by `runtime/run_setup.py`
  `resolve_mem_cap_bytes`.
- **Config:** `mem_cap` (config.yaml / `F3DASM_MEM_CAP`); default 4 GiB. On SLURM set
  below the job's `--mem`. **Status:** done (cgroup-native HPC backend = future seam).

### Resource backend (OS abstraction)
- **What:** one interface (`set_self_limit` / `read_rss` / `kill` / `proc_start_time`)
  so memory/kill OS-specifics live in one place; psutil impl + stdlib fallback.
- **Where:** `resource_backend.py`. **Status:** done (Linux cgroup backend = future).

### Computed cost from an explicit price table (2026-09-28)
`infra/model_prices.yaml` holds per-model list prices (USD/MTok, sourced from
the Anthropic pricing page, cited in the file). `Telemetry.record_call` adds
`cost_usd_computed` = exact tokens x that table to every row, ALONGSIDE the
SDK's `total_cost_usd` (never merged into it); `summary.json` carries
`cost_usd_computed` / `computed_cost_calls` per bucket and `run_ledger.csv` a
`cost_usd_computed` column. Exists because the strategizer's stream is cut by
`route_watcher` before the SDK prices it. A model absent from the table gives
None plus a logged warning, never zero. Validated in `tests/test_model_prices.py`
against the SDK's cost on calls it did price.

### Declared study objective (2026-10-04)
- **What:** optional `objective:` block in config.yaml (`column`, `direction`
  max|min, optional `feasible` 0/1 column). Parsed and validated by
  `evaluation/objective.py::parse_objective` in `run_setup._init_canonical_store`
  (unknown key, missing direction, or a column the declared oracle does not
  produce refuses the run), recorded as `objective` in run_config.json.
  `objective_values` / `best_so_far` are the one definition of "a row counts"
  (finite, below the sentinel magnitude, and `feasible == 1` when declared):
  `viewer/readers.py::read_figure_of_merit` (`GET /api/runs/{id}/figure_of_merit`,
  the store's best counted row; `declared: false` when absent) and the run
  ledger both use it. Absent block = undeclared: finite rule, running min AND
  max, nothing ranked. A study whose oracle is authored mid-run (no output names
  at start) skips the start check; each later oracle registration
  (`delegation.py::_check_objective_columns`, from the manifest's `output_names`)
  that lacks a declared column writes an `OBJECTIVE_COLUMN_MISSING` diagnostics
  event (namespace, column, key) and notifies the delegator; a manifest without
  `output_names` is reported the same way (key `output_names`). It never refuses a
  registration. Tests: `tests/test_objective_declaration.py`.

### Run-ledger process KPIs (2026-10-04)
`studies/run_ledger.py` adds `error_returns` (ERROR_RETURN count, target 0),
`objective` (`column:direction[:feasible=col]` or `undeclared`),
`first_feasible_eval` / `first_feasible_s` (position and seconds since
`debug/run_started_at` of the first canonical-store row that counts under the
declared objective; blank = never) and `best_trace` (JSON, <=20 evenly spaced
eval counts: `best` when declared, `min` and `max` when not). Gate attempts
remain `critic_consults` (count of `critic_reviews/call_NNN.md`). Tests:
`tests/test_run_ledger_kpis.py`.

### Per-delegation resource telemetry
- **What:** `Wait(id, block=False)` shows a delegation's eval count, current RSS, and **peak
  RSS** (the high-water across the watcher's ticks), so the strategizer can see a
  fat or fattening campaign (and tell the implementer with `SendMessage`).
- **Where:** `nodes/tools/routing/`, `watchdog_cleanup.py`
  `delegation_rss` / `delegation_peak_rss`.
- **Status:** done.

### Resource AWARENESS (telemetry, NOT enforcement)
- **What:** primes agents to be efficient with the things models ignore — time,
  RAM, disk, parallelism width — via two surfaces:
  - **Static envelope at delegation start:** a `<resources>`-style stanza in the
    worker/strategizer preamble — `~N CPU cores · RAM cap X GB (HARD — exceed it
    and your process is killed; stream/cache) · disk free Y GB · parallelize up to
    ~N ways, sized to RAM`.
  - **Measured peak RAM in the KPI footer:** `peak RAM (this delegation): Z GB of
    X GB hard cap`, the watcher's high-water — so memory cost travels with the
    result like wall-time already does.
- **Footprint (by design):** peak RAM rides the memory watcher's existing 5s poll
  (one `max()` per tick — no new poll/thread/I/O); the envelope is one
  `os.cpu_count()` + one `shutil.disk_usage` (O(1) `statvfs`, **never** a recursive
  `du`); the per-eval hot path is untouched (no per-eval RSS/disk stamping).
- **Where:** `watchdog_cleanup.py` `resource_envelope` / `delegation_peak_rss`
  (high-water recorded in `check_memory_and_kill`); `agent_runtime.py`
  `_resource_stanza`; `agent_prompts.py` `{resources}` placeholder;
  `ledger_summary.py` `delegation_footer` peak-RAM line.
- **Status:** awareness only — the hard memory cap stays the one enforced boundary.

### Parallel nodes: `runtime.max_awake_nodes` (2026-09-30)
- **What:** every delegation runs on its OWN adapter (`adapter.copy()` with its
  own `closure_tools`, lock and `last_*`), so same-role delegations run
  concurrently instead of serialising behind one lock (run 20260928T225501:
  D022 queued 2h08m behind D021). What bounds concurrency is an explicit
  config knob, `runtime.max_awake_nodes` (default 5, strategizer included).
  The strategizer holds one reserved slot for the whole run and is never
  queued; workers share `max_awake_nodes - 1`. Over the cap a delegation is
  QUEUED with "too many nodes working (N/N)" and starts FIFO when a slot frees.
  A worker gives its slot back only while blocked in `Wait`/`Delegate(wait=True)`
  on its own QUEUED child or while OPEN_FOR_REVIEW (a resume re-acquires ahead
  of new spawns); it keeps it in `FollowUp` and while waiting on running
  children. A critic call (gate or feedback) runs inside its caller's slot, the
  caller being blocked on it, so it needs no slot of its own and cannot
  deadlock the pool. Verdict-validator side-calls are NOT counted.
- **Where:** `nodes/slots.py` (`AwakeSlots`), `nodes/tools/routing/delegation.py`,
  `backends/{claude,openai_compatible}.py::copy`, `runtime/graph_builder.py`.
- **Launch guidance:** 5 awake needs `--mem >= 32G`.
- **Status:** done. Not handled: a cancelled/detached delegation still holds its slot until its thread ends.

### `mode="parallel"` host-safety hard cap
- **What:** `InstrumentedDataGenerator.call()` refuses `mode="parallel"`
  outright (raises `ValueError` before f3dasm's `DataGenerator.call()` ever
  runs) instead of relying on a prompt warning. f3dasm's own `mode="parallel"`
  falls through to a LOCAL `multiprocessing.Pool` — every solve spawns as a
  subprocess on the run's own shared orchestration node, which is CPU
  oversubscription and OOM that kills the whole run, not just one evaluation.
  A companion hard cap to `mem_cap_bytes` (§4 of the working contract): a run
  must not be able to OOM the shared node it runs on. `mode="sequential"` is
  unaffected; real parallelism belongs on a cluster scheduler (one evaluation
  per SLURM array task), not a local pool.
- **Where:** `instrumented.py` `InstrumentedDataGenerator.call`.
- **Status:** done.

### Per-delegation ledger KPIs auto-appended to the report
- **What:** when a delegation completes, a KPI footer is appended to the result
  the strategizer auto-receives (Wait/Done) — per-eval wall-time
  (median, max), this delegation's total eval wall-time, the ledger total, and —
  when a wall budget is set — the time remaining (telemetry, not a hard stop), so
  the median is actionable (≈ remaining / median = sims still affordable).
  Measured from the rows the delegation actually wrote, so budget planning runs
  on observed sim cost instead of an a priori per-sim estimate. Auto-delivered,
  not on-demand. Plain measurements only — interpretation is the strategizer's.
- **Where:** `ledger_summary.py` `RunStateSummary.{wall_per_delegation,
  delegation_footer}`; appended in `nodes/tools/routing/`.
- **Status:** done.

### Framework-owned local LLM on a SLURM GPU node (vLLM)
- **What:** instead of a hosted API, the framework can own the LLM *behind the
  nodes* on a separate SLURM GPU allocation: it sizes the allocation from the
  checkpoint's own published metadata (parameter count + dtype, fetched
  weights-free — local HF cache first, then a single `requests` GET of the HF
  Hub model-info JSON; no `huggingface_hub`/`transformers` dependency), so a
  full HF id or a short alias gets correctly-sized GPUs/mem with **zero user
  config**. GPU-count derivation uses a built-in per-GPU VRAM table keyed by the
  cluster's exact Slurm gres names (the Oscar/Brown-CCV inventory; hardware
  drifts far slower than model releases), defaulting to `l40s` (48 GB,
  schedulable on the general `gpu` partition) when the study names no GPU, and
  emits a type-qualified `--gres=gpu:<name>:<n>` so Slurm grants the exact card
  the size was computed for. VRAM is sized as weight-bytes(serve quant) +
  **context-aware KV cache** (read from the model config — layer count, KV
  heads/head-dim, and the sliding/global attention split, at the served context
  length and `--kv-cache-dtype`; a flat multiplier is the fallback only when the
  config lacks those fields) + a small fixed overhead. The serve quant is a
  `runtime` knob whose default is **GPU-aware**: FP8 only on FP8-capable cards
  (Ada/Hopper/Blackwell — `l40s`/`h100`/`nvidia_rtx_pro_6000_blackwell`/
  `nvidia_b200`), else a safe BF16/Q4 path, so a zero-config Ampere/Turing run
  never asks for FP8 it cannot serve; an explicit `llm_quantization=fp8` still
  wins. This lets the power model `gemma-4-31b` (30.7B) fit a single 48 GB L40S
  at FP8 weights + FP8 KV (~44 GB) while keeping its full 256K context; the same
  quant is applied to the `vllm serve` launch so sizing and serve agree. A tiny
  built-in alias table maps `gemma-4`/`gemma-4-e4b` → the cheap default and
  `gemma-4-31b` → the max-power-for-48 GB variant. Optional, purely-override layers,
  most-explicit-wins: `llm_slurm` config fields > metadata-derived sizing >
  basename-matched family *serve-hints* (context length etc. that metadata
  can't publish) > conservative default. Metadata unavailable (offline, gated
  repo, unknown GPU) degrades to a loud warning + conservative default — never
  raises, never silently mis-sizes. Then submits a `vllm serve` job (reusing f3dasm's
  `SlurmCluster` + the plain `sbatch` idiom — a persistent server is NOT routed
  through the eval-oriented `Pipeline`/`SlurmExecutor`), waits for the granted
  node, polls `/v1/models` past the cold model load, publishes `VLLM_BASE_URL`
  so the existing vLLM adapter reaches it over the cluster network, and
  scancels the job on EVERY exit path (normal close, crash, and the watchdog's
  `os._exit` hard-kill via `reap_run_serve_job`). A build-time, leading-order
  throughput bound (decode is memory-bandwidth-bound; GPU/model-size/dtype/
  tensor-parallel are all config-known) warns loudly when a config is likely to
  choke — a nudge at config time, never a block. Same physics the token
  telemetry measures after the fact (parity). Phase 1: one allocation for the
  run's lifetime; Phase 2 (walltime chaining + a stable local proxy) is designed
  but deferred.
- **Config (all optional overrides):** `llm_slurm:` block — `enabled`
  (default off), `model`, `aliases` (short-name→canonical-HF-id map; wins over
  the built-in aliases), resource overrides
  (`gres`/`mem`/`time`/`cpus_per_task`/`vllm_args`), sizing/throughput inputs
  (`gpu_model`/`params_b`/`dtype_bytes`/`tensor_parallel`), `queue_timeout`/
  `serve_timeout`, and a nested `cluster:` (`partition`/`account`/`env_setup`/
  `env_vars`/`runner`). Metadata knobs live in the `runtime:` block via
  `settings.get_*`: `llm_metadata_fetch` (bool, default on — off = local-cache
  only, no network), `llm_metadata_timeout_s` (float, default 8), and
  `llm_quantization` (str; unset → GPU-aware default: FP8 on FP8-capable cards
  else BF16/Q4. Set `fp8`/`bf16`/`fp16`/`awq`/`gptq`/`q4`/…, or `auto` for the
  checkpoint's native dtype) — the serve dtype used for both VRAM sizing and the
  `vllm serve --quantization` flag; an explicit value wins over the GPU-aware
  default. Requires
  `backend: vllm`. Disabled → hosted-API runs are unchanged.
- **Where:** `agentic/slurm_llm.py` (aliases, metadata fetch, VRAM sizing,
  serve-hints, resolve, render/submit, wait, throughput bound, teardown,
  reaper); `agent_runtime.py`
  (`_maybe_start_slurm_llm` + the teardown `finally` in `execute()`);
  `studies/*/run.py` watchdogs (serve-job reap). Reuses
  `pipeline/resources.py` (`SlurmCluster`/`SlurmResources`) unchanged.
- **Status:** Phase 1 done, headless-tested; validate on a real GPU cluster
  before making it a default anywhere (greenfield + cluster-specific).

## E. Runtime safety

### Wall-clock watchdog + recursive reap (#11/#14)
- **What:** a hard wall-clock timer force-exits a stalled run; on exit it recursively
  kills every campaign process tree — including detached/new-session ones that a
  process-group kill misses.
- **Where:** `studies/.../run.py` `_watchdog` (the out-of-repo benchmarks harness'
  own launcher) and, in THIS repo, `python -m adda.watchdog` (BACKLOG #41 — see
  below), both driving `watchdog_cleanup.py`'s `reap_process_group` /
  `reap_governor_pids`.
- **Config:** watchdog = 2× the run's time budget (a floor, not a default —
  `adda.watchdog`'s `--watchdog-multiple` can only raise it). Operational
  kill-switch `F3DASM_DISABLE_WATCHDOG=1` turns the wall-clock force-exit OFF
  (the memory-cap watcher stays on) on the out-of-repo harness — for long
  supervised runs. **Status:** done.

### Delegate() time cutoff + escalating budget wrap-up ladder
- **What:** two additions to the existing soft-budget/backstop ladder, both a
  NEW HARD CAP on the time budget (a science budget — CLAUDE.md §4 — approved
  explicitly by the maintainer for this one case; eval budgets remain soft
  and untouched):
  1. Past `runtime: delegate_cutoff_multiple` × the (soft) time budget (default
     `1.5`, disabled at `<= 0`), `Delegate()` refuses to start a NEW
     delegation — an actionable `ERROR:` string, no delegation registered.
     Every other close-out tool (`Wait`, `Done`, deliverable
     tools) is untouched, and an in-flight delegation started before the
     cutoff is never cancelled or disturbed — only NEW ones are refused, so
     the run always has a path to close. Sits one rung below
     `run_backstop_multiple` (which force-closes the run); if the cutoff
     multiple is misconfigured `>=` the backstop multiple, `AgenticRun`
     warns loudly at startup (it can never fire — the run closes first).
  2. Every node — not only an orchestrating one — gets an escalating
     wrap-up message once per newly-crossed 10%-of-budget band at/past 100%
     (100, 110, 120, …), instead of the strategizer's old every-turn repeat
     past 1.0×. A worker (leaf node, or a `Delegate()`-spawned WorkerSession)
     is told what it can actually do (finish the step, report what you have,
     return) — never "call Done()", which only the strategizer holds. The
     strategizer's own band message additionally says new delegations are
     now refused once the cutoff multiple is actually passed.
- **Where:** knobs in `nodes/_constants.py` (`delegate_cutoff_multiple`,
  `delegate_cutoff_enabled`); the refusal in
  `nodes/tools/routing/delegation.py::DelegationTools._check_delegate_cutoff`
  (called first thing in `Delegate()`); the shared ladder
  (`budget_band_due`, `budget_wrapup_message`) in `nodes/_constants.py`,
  called from `orchestration.py::_budget_warnings` (every node's own turn —
  `_respond`/`leaf.py` are gone; every node now runs the same
  `_orchestrate` loop) and `delegation.py::_budget_broadcast` (the
  WorkerSession path a `Delegate()` worker actually runs through in the
  built-in graph); the misconfiguration warning in
  `runtime/agent_runtime.py::_warn_if_delegate_cutoff_unreachable`.
  Diagnostic: `DELEGATE_CUTOFF` via the existing `_record_intervention`
  mechanism (`diagnostics.jsonl`), the same channel `MILESTONE_BLOCK` uses.
- **Status:** done, headless-tested (`tests/test_delegate_time_cutoff.py`,
  `tests/test_budget_wrapup_ladder.py`).

### `python -m adda.watchdog` — the in-package run launcher (#41)
- **What:** a launcher that runs a study as a CHILD process, in its own process
  group, and owns the wall-clock deadline from the PARENT — the maintainer's
  explicit design (`paper/sections/03-method.tex`, "A wall-clock watchdog
  outside the run": an in-process backstop that may itself be stuck cannot be
  trusted). `python -m adda <study-dir>` itself stays exactly as unprotected as
  before — this is an additional, safer way to launch the same run. On timeout
  it SIGTERMs the whole child process group, escalates to SIGKILL if anything
  survives a short grace period, reaps any registered campaign PIDs the run
  spawned (`reap_governor_pids` — catches the detached/new-session descendants
  a plain process-group signal misses), and appends a labelled
  `write_watchdog_retrospective` post-mortem to the run's
  `retrospectives.jsonl`. Exits `124` (distinguishable from any real exit code
  the run itself could produce) on a kill; propagates the run's own exit
  status otherwise.
- **Deadline:** derived from the SAME budget value the run itself resolves
  (`--budget`, or `config.yaml`'s `budget:`, via the shared
  `run_setup._parse_budget_str`) at the 2× floor (`--watchdog-multiple`, never
  settable below 2.0) — the two values can never silently disagree. Refuses to
  run at all if no budget can be resolved.
- **Interrupt:** a SIGINT/SIGTERM to the watchdog itself (terminal Ctrl-C, the
  viewer's Kill) reaps the child's whole tree (same path as a timeout, minus
  the timeout post-mortem) and exits 130/143. The child is its own session, so
  before this the watchdog's death orphaned the run.
- **Where:** `_src/infra/watchdog_launcher.py` (`run_under_watchdog`,
  `resolve_deadline_seconds`, `main`); thin top-level forwarding package
  `adda/watchdog/` mirrors `adda/viewer/`'s own convention.
- **Status:** done, headless-tested (`tests/test_watchdog_launcher.py`) against
  a trivial sub-second child, including a grandchild-reap assertion.

### `--entrypoint`: the watchdog CLI can launch a study's own script
- **What:** `python -m adda.watchdog <study-dir>` always builds
  `AgenticRun`'s built-in DEFAULT graph — it never forwards a custom
  `graph=`. A study whose own `run.py` declares a different `Graph` (extra
  roles, different edges — e.g. `studies/lcp_matlab_regression/run.py`'s
  6-node graph, which adds `math_expert` and isn't reachable through
  `python -m adda` at all) used to be launch-protectable only by hand-calling
  `run_under_watchdog` directly, which is exactly why it wasn't: the Oscar
  zero-shot harness launched bare `python run.py` and a hang went unwatched
  (bug report, adda-boss-whopper). `--entrypoint SCRIPT` (a path relative to
  `study-dir`, e.g. `run.py`) launches that script directly instead of
  `python -m adda <study-dir>`, under the SAME watchdog protection (deadline,
  kill, reap, retrospective) — a bug fix to the documented safe launcher, not
  a new capability. Incompatible with `--model`/`--budget`: an arbitrary
  entrypoint script takes no CLI arguments of its own to forward them to, so
  those two must error out together with `--entrypoint` rather than be
  silently dropped; the deadline still derives from the study's
  `config.yaml` `budget:`.
- **Where:** `_src/infra/watchdog_launcher.py` (`_build_parser`, `main`).
- **Status:** done, headless-tested (`tests/test_watchdog_launcher.py`) —
  entrypoint spawn, the two-flag rejection, a missing-script error before
  anything is spawned, and unchanged behaviour when the flag is omitted.

### Synthetic watchdog retrospective (#12)
- **What:** a watchdog kill leaves a labelled post-mortem so the analysis protocol
  isn't blind.
- **Where:** `watchdog_cleanup.py` `write_watchdog_retrospective`, called from
  `_src/infra/watchdog_launcher.py` on a timeout (see #41 above) and from the
  out-of-repo campaign runner's own watchdog. **Status:** done, and now has an
  in-package caller — a watchdog kill via `python -m adda.watchdog` is
  protected locally, not only on the out-of-repo harness.

### Fallback retrospective on a non-compliant close
- **What:** every in-process close that never reaches the entry node's real,
  first-person retrospective — UNGATED, FAILED, an external stop, or an
  unhandled crash (`GraphRecursionError`/`KeyboardInterrupt`/OOM) — still
  gets one, synthesized from disk state and clearly marked as such
  (`source_id` prefixed `SYNTHESIZED:`). "Capture, don't request": the
  post-Done exit interview asks for one more cooperative Done() call, and
  nothing used to enforce the reply actually arrived.
- **The claim must stay true.** `_record_retrospective` (nodes/recording.py)
  used to silently drop a Done() summary that arrived but whose `###
  Retrospective` heading carried trailing text (e.g. "### Retrospective
  (System & Framework)") — `_extract_report_section`'s regex required
  nothing but whitespace before the newline. That made THIS mechanism's own
  claim false: `write_fallback_retrospective`'s "never arrived" reason fired
  even though a real, substantive reply had (run 20260926T214835 — a
  self-consistency wet-test finding, not from a crash). Fixed two ways:
  the extraction regex now only requires a word boundary after the heading
  name (rejects a genuinely different heading like "### RetrospectiveNotes",
  accepts trailing text on the same line); and `_record_retrospective` now
  records ANY non-empty Done() summary even if the section still fails to
  parse for some other reason — flagged `"parse_failed": true`, raw text
  preserved — rather than silently returning. Both close the same class of
  gap: a real reply must never be indistinguishable from one that never
  arrived.
- **Where:** `watchdog_cleanup.py` `write_fallback_retrospective` (shares its
  disk-reading/append core with `write_watchdog_retrospective` rather than
  duplicating it); called from `agent_runtime.py`
  `AgenticRun._fallback_retrospective`, wired at both the normal close
  (`_finalize_run`) and the crash path (`_invoke_graph`'s
  `except BaseException`, before the re-raise). Idempotent — a compliant
  close's real entry (including a parse-failed one — it is still non-
  synthesized) is never duplicated. `nodes/parsing.py`
  `_extract_report_section`; `nodes/recording.py`
  `_record_retrospective`. **Status:** done for every in-process close; a
  watchdog kill is a SEPARATE case, covered by `write_watchdog_retrospective`
  instead (see #12 above).

### KB (handbook) entries
- **What:** curated knowledge the agents consult (incl. running on SLURM, pipeline
  patterns). **Where:** `knowledge/entries/`. **Status:** core.
- **Injected menu:** an audience-filtered, one-line-per-entry MENU
  (`KnowledgeBase.menu(audience)`) is injected into every agent's system prompt
  (`agent_runtime._kb_menu` → the `{knowledge}` placeholder in both preambles), so
  an agent always SEES the latent chapters it can pull — the same way it always
  sees its tool list — instead of only discovering one if it already thought to
  call `ConsultHandbook`. The descriptor is the entry `title`, capped to one terse
  line by a ≤100-char invariant (`test_knowledge_base.py`).

### Run architecture diagram
- **What:** `AgenticRun.render_architecture(out_path=None)` renders THIS run's
  actual agent graph — every node, its role, its description, its RESOLVED
  backend/model (each node can override either independently, matching
  `agent_runtime.py`'s own `agent.model or self._model` resolution — shown
  per card, never as one run-wide banner), its full tool surface, and the
  delegation edges between nodes — as a single self-contained, hand-laid-out
  SVG. No cap on the tool list: a card grows to fit everything rather than
  truncating with a "see agent source for the full list" cop-out. Generated
  straight from the live `Graph`/`Agent` objects (BFS-layered from the entry
  node, not run through Graphviz or any auto-layout engine), so it cannot
  silently go stale the way this repo's previous diagram did (a hand-authored
  `internal/class_diagram.dot` describing classes that no longer existed —
  deleted in favor of this). The tool surface includes each agent's REAL
  runtime-injected closures (`Agent.build_closure_tools()`), not just its
  statically declared `.tools` — LiteratureReviewAgent declares only
  `{Read, Grep, Glob, ReadProblemStatement}`; every actual capability
  (CorpusAdd, ConsultLiterature, SearchPapers, CitationGraph, PaperDetails) is
  injected at runtime, and a first draft that only read `.tools` silently
  showed it as having almost no tools. Every edge renders identically — one
  delegation mechanism (`Delegate`) exists in the code, so there is no
  invented "primary vs lateral" edge taxonomy or line-style split (an early
  draft fabricated one; caught and removed). Each tool name is color-coded by
  a real per-tool key: the SAME tool renders in the SAME color everywhere it
  appears, so a repeating color across different cards is what visually says
  "these nodes share this capability" — a tool unique to one node renders in
  plain neutral ink instead. No title, no node/edge-count banner, no legend —
  the cards and edges are the whole diagram. SVG is the native, checked-in
  format: vector by construction, so any DPI or pixel size is one
  rasterization step away with zero quality loss — no separate PNG-export
  code ships here.
- **Where:** `run_diagram.py` (the renderer); `agent_runtime.py`'s
  `AgenticRun.render_architecture`. **Status:** done.

### Live run viewer (read-only)
- **What:** `python -m adda.viewer <study-dir>` (or
  `AgenticRun.serve_viewer(host, port)`) serves a local, read-only web UI for
  watching a run WHILE it's in progress — a live network diagram of the
  agent graph (node positions reuse `run_diagram.py`'s own `_bfs_layers`
  layout; a status dot per node, driven by Server-Sent Events, distinguishes
  running/done/failed), and clicking a node opens a docked bottom panel
  (maximizable to full-screen, VS Code's own `toggleMaximizedPanel`
  convention) showing that node's live tool-call/conversation transcript as
  Claude-Code-style chat bubbles with tool calls collapsed by default. Reuses
  existing data wholesale rather than adding new instrumentation:
  `delegation_log.jsonl`/`diagnostics.jsonl` (already live, append-only) via
  `DelegationLog.query_all()`'s own collapse-by-id logic, and
  `debug/transcripts/` (only present when a run was started with the debug
  flag on — the UI says so plainly rather than showing a blank pane).
  Explicit, honest limitation surfaced in the UI itself: the on-disk
  delegation-status vocabulary is richer than a fixed enum (confirmed for
  real: a Done()-gate check logs `"GATE:PASS"`, not a plain `RUNNING`/`DONE`)
  and still lacks the in-process registry's finer states (`Working`/
  `FollowUp`/`Cancelled` are never persisted) — a delegation blocked on a
  human follow-up question is indistinguishable on disk from one simply
  still running. No build step: Tailwind CDN + htmx + Alpine.js, one static
  HTML template. Binds to `127.0.0.1`. Reads are open on the bound
  interface; writes (note, answer) need a per-launch random token, set as an
  HttpOnly SameSite=Strict cookie by the `/session?token=…` URL the viewer prints
  at start, a same-origin `Origin`, no cross-site `Sec-Fetch-Site`, and
  `Content-Type: application/json` (so a cross-origin `text/plain` "simple
  request" is refused). `GET /api/session` reports `can_write`; the page shows
  a read-only hint instead of failing silently.
  Markdown (notebook, Brief, reports) renders `$…$` / `$$…$$` math with KaTeX
  (cdnjs), lifted out before parsing so `*` and `_` in a formula are not emphasis.
  Overview is a VERTICAL timeline (time runs down, linear, with a ruler): one
  column per concurrent slot, assigned from the TRUE session start/end (the column
  count is the run's peak concurrency; short delegations are compact cards at their
  true height), a hatched gutter strip for the wait of a queued delegation,
  mid-run critic audits as cards with a feedback chip, gate reviews as full-width rules labelled with
  the verdict, status chips with a glyph as well as a colour, and hover linking
  between a card and the hypotheses it carries (`hypothesis_ids`). The ledger shows
  prior to posterior, the falsification criterion, the whole status history
  (a verdict retracted to OPEN is visible) and open/closed/reopened filters. Header
  Header
  vitals add wall against the budget with 1x/1.5x/2x marks, awake worker slots
  N/max with the queue length, and cost as a lower bound ("≥$") when any call
  recorded none (unknown, never 0). Orchestrator RSS is not shown: no record holds it.
  Narrow screens stack the panes instead of overlaying them. No longer read-only: see
  **Operator channel** below for the write path (answering a `FollowUp`,
  queueing a note, nudging a running delegation).
  The Oracle tab draws one best-so-far chart over every store (`/api/runs/<id>/trajectory`,
  read from output.csv `_ts` plus run_config's declared output names). The reader returns each
  numeric/binary output column and ranks nothing. The default objective is the study's first
  declared output; min/max, the 0/1 feasible column, the 0/1 salvaged column ("count salvaged
  rows" toggles whether they enter the best) are the page reader's choice, with
  `feasible`/`salvaged` pre-picked by name. One colour per namespace, circle = row, diamond =
  salvaged, filled = feasible, hollow = not; a run copied off its machine falls back to the
  store beside it. The linear y axis is fitted to the best-so-far lines and the counted points
  (+15%); points beyond it are triangles pinned at the edge. The readout names what it counts
  ("best feasible X (incl. salvaged) in <ns> at #i (Dnnn)"), hover shows flags and delegation,
  and direction reads "(default)" until changed. No reference line: no record carries a
  machine-readable target.
  `GET /api/runs/<id>/funnel[?stages=a,b]` (`readers.read_funnel`) is the stage funnel as data:
  per store, the 0/1 stage columns in order, each with `pass` (alone), `cumulative` (passed every
  earlier stage; an unrecorded value does not pass), `dropped`, `unrecorded`; plus `binding` (the
  stage that dropped most), `skipped` (requested but never recorded as 0/1) and `available`.
  With no `stages`, the order is picked by column name (coil, prefilter, ran/solved, converged,
  feas). The server computes it; a front end only draws it.
  `GET /api/runs/<id>/log[?name=run&after=<byte>&limit=<bytes>]` (`readers.read_log_tail`) tails
  a run's log like `tail -f`: no `after` gives the last window, echoing `next_cursor` gives every
  byte once, a file that shrank comes back from the start with `reset: true`. The log is chosen
  by name (only `run` = `debug/run.log` today), never by path. The watchdog's own log joins once
  the viewer starts runs (5.3) and so owns its stdout.
  **Start / Kill (spec 14 5.3, 5.4):** `viewer/run_control.py`.
  `GET /api/study/preflight` returns `{checks:[{name, ok, detail}], can_start, launched}`
  (problem statement, config, budget, evaluator present and parsed without importing it, backend
  CLI on PATH, no live run; `ok: null` = not decidable without side effects: the evaluator's
  one-sample run and the problem statement's content are left to a person). `POST /api/study/start`
  runs exactly `python -m adda.watchdog <study>` (budget/model from the committed config), 409
  with the checks if any is false. One live run per study: a viewer-started process still alive,
  or a run dir with no `run_status.json` that wrote to `debug/` in the last 600 s (`LIVE_WINDOW_S`;
  a crashed run that stopped writing does not block forever). What it started is in
  `runs/_viewer/registry.json` (PID + process start time, so a viewer restart still knows and a
  recycled PID is never mistaken for ours), its output in `runs/_viewer/watchdog_<ts>.log`.
  `POST /api/study/kill` SIGTERMs that watchdog (which reaps the run's tree), then SIGKILLs the
  survivors among the descendants of that exact PID after 30 s; 404 if the viewer started nothing
  alive. Every start/kill is a `viewer_actions.jsonl` line with the exact command and signal.
  **Start via study launcher (spec 14 5.5):** `run_control.start_via_launcher` / `stop_via_launcher`.
  A study declares `runtime.launch: {command, id_pattern, stop_command, timeout_s}` in `config.yaml`
  (argv lists, no shell; `id_pattern` has one capture group applied to the launcher's stdout;
  `stop_command` contains `{id}` and requires `id_pattern`). `POST /api/study/launch` runs exactly
  `command` in the study directory, stores its output and the printed id in the registry
  (`kind: launcher`); `POST /api/study/launch/stop` runs `stop_command` with the latest stored,
  not-yet-stopped id and never any other. No `runtime.launch` means the control is absent
  (`launcher: null` in the preflight, 409 on the endpoints); a malformed declaration is refused
  with the reason, never read as "none". A second launch is refused while a run is live or the
  viewer submitted one in the last 600 s. Every run (before, timeout, after with return code and
  output) is a `viewer_actions.jsonl` row (`launch` / `launch_stop`) with the exact command line.
  **Re-execute the deliverable (spec 14 5.8):** `viewer/notebook_replay.py`.
  `POST /api/runs/<id>/notebook/reexecute` replays the run's notebook (`readers.notebook_path`:
  the live file only if its stamp names the run, else that run's archive) through
  `evaluation/notebook_exec.py` against a throwaway COPY of the run's `experiment_data/`
  (`replay_sandbox`, the same sandbox the reproduction gate uses), in a child process so the
  kernel's environment never leaks into the server. The live ledger and the notebook file are
  never written. Returns `passed` (the gate's contract: clean exit, zero new rows, existing rows
  unchanged), `rows_before/after`, `reproduced`, `stdout_tail`/`stderr_tail`, `timed_out`.
  Timeout is the gate's rule (a tenth of the wall budget, at least 180 s). One replay per study
  at a time (409), write token required, 409 when the run has no notebook or no ledger. Audited
  as `reexecute` rows (exact command before, outcome after).
  **Transcript events (spec 14 Phase 2):** `GET /api/runs/<id>/transcript/<key>/events?after=N&limit=M`
  (`viewer/transcript_events.py`) returns `{events, next_cursor, total}`: both backends' transcripts
  normalised server-side to `{ts, kind: user|assistant|tool_use|tool_result|thinking|system, role,
  text, tool?, result?, notices?, pending?}`, so a front end renders events and never parses a
  backend format. `after` and `next_cursor` count RAW records (most are streaming partials that emit
  nothing); `limit` caps emitted events (default 200, max 1000). `next_cursor` is always an int, so a
  live transcript is polled from it. Tool names and `pending` calls resolve against the whole file.
  **Pagination (spec 14 Phase 2):** `GET /api/runs/<id>/transcript/<key>?after=N&limit=M` returns
  `{events: <raw records>, next_cursor, total}` (limit counts raw records, default 200, max 1000).
  `GET /api/runs/<id>/oracle?after=N&limit=M[&namespace=<name>]` pages each store's ledger rows,
  newest first (default 400, max 5000); a store's `next_cursor` (offset into that order, `null`
  once drained) replaces the old `truncated` flag. Cursors are per store: `namespace=<name>` pages
  one store only (`namespace=` with no value is the canonical store), and `total_evals` still counts
  every store. A non-integer `after`/`limit` is a 400.
  **Retrospectives (spec 14 2.10):** `GET /api/runs/<id>/retrospectives` returns
  `{retrospectives, missing}`. Each retrospective is split into `sections`
  (CONSISTENCY / DECISION / FRICTION / BLOCKED / TIME; `- **X**:`, `**X**:` and `#### X:` headers
  all parse), with unstructured text kept in `preamble` and the raw `text` always present, plus
  `delegation_id` when `source_id` is a delegation of the run. `missing` is the run's
  `RETROSPECTIVES_MISSING` diagnostics rows, so a node that never answered is visible.
  **Critic reviews (spec 14 2.5):** `GET /api/runs/<id>/critic_reviews` returns `{reviews}`, one per
  `critic_reviews/call_NNN.md`: `verdict` and `findings` come from the gate's own parsers
  (`nodes/parsing`), `numbers` is the `findings_*` counts, `source_id` (`critic-N`) matches the
  retrospective of the same call, and `delegation_id` names the critic delegation with
  `delegation_id_source`: `recorded` (the delegation row's `critic_review` field, written by every
  GATE, FEEDBACK and escalation call), `ordinal` (runs that predate the field: Nth critic delegation
  for the Nth file, only when the counts match) or null.
  **Diagnostics (spec 14 2.4):** `GET /api/runs/<id>/diagnostics?after&limit[&kind=]` pages
  `diagnostics.jsonl` by line cursor (`next_cursor` is always an int, a live run keeps appending;
  compare with `total`). `kind` filters on the row's `error_type`, else `tool`; `counts` is the whole
  file's kind vocabulary regardless of the filter. Unparseable lines are skipped.
  **Strategizer notes (spec 14 2.8):** `GET /api/runs/<id>/notes` returns `{notes}`: every `*.md` in
  `debug/strategizer_notes/` (`strategy_NN_*.md`, `study_summary.md`) as `{name, mtime, text}`, oldest
  first. Any subset, or none, is valid.
  **Evidence (spec 14 2.6):** `GET /api/runs/<id>/evidence` returns `{index, repo, delegations}`:
  `debug/evidence_index.md`, and per delegation its `workspace_sha`, `predecessor_sha` (the commit
  before it in the workspace history) and the `files` that commit touched. `GET
  /api/runs/<id>/evidence/<delegation_id>` adds `git show --stat` and `git diff --stat` against the
  predecessor. All git goes through `viewer/safe_git.py`: fixed argv, no shell, commit ids
  `[0-9a-f]{7,40}` only, explicit `--git-dir` (never discovers a parent repo), scrubbed env, timeout,
  capped output; it can only read. The same module serves study history (5.10).
  **Literature (spec 14 2.9):** `GET /api/runs/<id>/literature` returns the study-scoped corpus
  (`corpus.csv`, shared by every run) with each paper's `in_run` (its own `added_at` inside this run's
  start..end window; null when unparseable), `added_in_run`, `run_window`, this run's literature-tool
  `errors` (cooldowns and other failures are the same ERROR_RETURN row, so indistinguishable) and its
  `RETRIEVAL_DEGRADED` rows.
  **Study history (spec 14 5.10):** `GET /api/study/history?limit=` returns the commits touching the
  study directory (repo root = nearest ancestor with `.git`; log confined to the study path after
  `--`, path chosen server-side), each with its changed files and +/- counts, via `safe_git`. Empty
  with `repo: null` when the study is under no repository; 502 if git fails.
  The viewer binds loopback only: any other `--host` needs `--allow-network` and prints a warning.
- **Where:** `src/adda/_src/viewer/` (`readers.py` pure data functions,
  `app.py` the Starlette app, `templates/graph.html` the UI);
  `agent_runtime.py`'s `AgenticRun.serve_viewer`; `pyproject.toml`'s `viewer`
  optional-dependency group. **Status:** done (v1, read-only).
- **Lifecycle, event colours, fonts (2026-09-28):** the delegation panel
  names the stage (`lifeOf`: queued / running / awaiting review / done /
  failed / gate outcome) with one explanatory sentence, distinguishing a
  QUEUED delegation (`session_started_at` present and null) and an
  OPEN_FOR_REVIEW one from plain running/done. adda notices are classified
  by their marker (`app.py::_notice_kind`: adda / science monitor / verdict
  validator / operator), each with its own colour AND a text tag; critic
  verdicts and review approvals colour their result block. Science-monitor
  injections render collapsed in the transcript and are also listed run-wide,
  behind a count badge, from `readers.read_monitor_injections`
  (`GET /api/runs/{id}/monitor`). Light theme (Catppuccin Latte) follows the
  OS. IBM Plex Sans/Mono are bundled under `viewer/static/fonts` (SIL OFL) and
  served from `/static`; Alpine and marked are still CDN-loaded.

---

### Notice provenance — telling adda's voice from a tool's output
- **What:** every piece of text adda injects into an agent's context —
  nudges, science-monitor drift, budget warnings, operator notes, peer
  messages, delegation notifications — is wrapped in an `<adda-note>` marker
  at the point of injection. The viewer lifts marked blocks out of the tool
  result and renders them in their own band (`--surface0`, peach left rule)
  above the tool's own output (`--crust`).
- **Where:** `nodes/notices.py` (`wrap_notice` / `split_notices` and the
  marker); seven injection sites in `nodes/orchestration.py`
  (`_drain_notifications`) and `nodes/tools/routing/` (worker-message
  drains in `ReportEvals`/`FollowUp`/the status poll, the status-poll hints,
  and the science-monitor drains in both `Wait` branches);
  `viewer/app.py::_tool_result_html` renders them.
- **Why marked at the source, not detected by the reader:** the pre-existing
  `[TAG …]` convention is incomplete (the status-poll hints are bare
  prose), and brackets are not a safe signal because tools emit their own
  (`[exited 1]`, `[output truncated to last …]`). A marker is a tag rather
  than a control character because this text is part of the agent's prompt
  and has to stay readable; it matches the `<role>`/`<tools>` idiom the
  prompt corpus already uses.
- **Side effect, deliberate:** `ERROR_RETURN` styling in the viewer is now
  tested on the tool's output with the notice removed. It was tested on the
  raw text, so any result carrying an injected prefix failed the
  `startswith("ERROR")` check and silently lost its error styling.
- **Status:** core. Note the marker is visible to the agent as well as the
  reader — it labels the text truthfully, but it does change prompt content.

### Operator channel — answering, noting, and nudging a live run
- **What:** a human can act on a run in flight, from the viewer or a
  terminal. Three things move across it, all as small JSON in the run's own
  `debug/` dir (the run and the viewer are separate processes, so the file
  system is the channel; it also makes the whole exchange part of the run
  record rather than terminal scrollback): **answers** to a `FollowUp`
  question; **notes** queued for the entry node's next tool call; and a
  **watch heartbeat**, which is what lets a run tell waiting-for-an-answer
  apart from stalling on a question nobody can see.
  A note may carry the **delegation id** it is aimed at. Addressed at a
  RUNNING delegation it is routed onto that worker's per-delegation queue
  and prefixed onto its next tool result — the same path the
  budget warnings use — so the operator can correct work already in flight
  instead of waiting for a wrong result. Addressed at a finished delegation
  it goes to the entry node with the intended recipient named, never
  silently dropped. Routing happens in the orchestrator's note drain because
  that drain claims the queue destructively; anywhere else and an addressed
  note would be swallowed before the router saw it. Delivery therefore
  depends on the orchestrator taking a tool call (it drains on
  `Wait`), so a strategizer blocked in a long synchronous
  `Delegate(wait=True)` will not route a nudge until it returns.
- **Where:** `src/adda/_src/operator_channel.py` (`ask_question`,
  `answer_question`, `queue_note`, `drain_note_rows`, `touch_watch`,
  `is_watched`); routing in `nodes/orchestration.py`'s `_drain_notifications`;
  HTTP surface in `viewer/app.py` (`/answer`, `/note`); composers in
  `viewer/templates/graph.html`. **Status:** done.

---

## Graceful stop (`debug/stop_request.json`)

SIGTERM unwinds nothing in a run, so a watchdog or operator kill used to lose
every agent's real retrospective. A **stop request** is the graceful
alternative: whoever wants the run over writes `debug/stop_request.json`
(`{requested_at, by, reason, grace_s, termination}`, `infra/stop_request.py`); the entry
node notices it at its next checkpoint — the start of a turn, every tool
result, and each tick of a blocking `Wait` — and:

1. tells every live delegation, on the per-delegation queue the
   budget warnings use, to report what it has and finish (a worker mid a
   single long tool call sees it at its next tool boundary);
2. refuses new `Delegate` calls (a refusal by design, not an `ERROR:`);
3. returns a blocking `Wait` early, once, on first sight;
4. lets `Done()` skip the milestone, first-call, reproduction and critic
   gates and take only the retrospective round, then close **STOPPED** —
   `run_status.json` `status: STOPPED`, `outcome: UNGATED`, `termination:
   stopped`, `resumable: true`. `terminal.STOPPED` is a censored termination
   like the backstops: never GATED, never a failure.

A worker's report is its retrospective, so winding down first is what saves
them. Only a delegation still running after `grace_s` is cancelled (registry
`Cancelled`, a delegation-log row `CANCELLED` stating "operator stop … no
retrospective was given"); no placeholder retrospective is invented for it.
A request stamped before the run's start is a leftover and is ignored, so a
resumed run does not stop on arrival; the file is renamed
`stop_request.consumed.json` once honoured.

**Time accountability.** Every stop (watchdog, backstop, operator) states its
cause plainly to the agents; a time cap says "the hard time cap is being
reached; the run did not finish on time". Workers' wind-down notice and the
entry node's retrospective prompt both require a `- TIME:` bullet in the
`### Retrospective` block (`nodes/stop.py` `TIME_SECTION`): (a) diagnosis —
the problem too hard for the allotment, or the work inefficient; (b) where
the time went, with evidence from the agent's own work; (c) REQUIRED — what
would have avoided it, each change attributed to the agent's strategy, the
tools/harness, or the problem setup. The retrospective cap is 16000 chars so
the bullet is not cut off behind a long report.

**Resuming a crashed run (`runtime.resume_close_with_retrospectives`, default
off).** When a resume finds a mid-flight checkpoint (the process was lost:
crash, SIGKILL, OOM), this knob writes a stop request (`by="resume"`,
`termination=crashed`) before the graph restarts, so the entry node gives the
retrospective the crash cost it, with a crash variant of the TIME bullet
(`CRASH_SECTION`: what brought it down, where the time went, what would have
avoided it), and the run closes `crashed`/`halted`, resumable. Delegations the
log last saw RUNNING are named in `RETROSPECTIVES_MISSING` ("process lost");
no text is ever synthesized for them. A crash that already ran past the time
budget needs no knob for the entry node: the resumed run keeps its original
start, so the time backstop trips on the first turn.

**Backstops go through it too.** A time, USD or repeated-errors backstop
no longer jumps to END: it writes a stop request (`by="backstop"`) carrying
its own `termination` (`backstop_time` / `backstop_usd` / `repeated_errors`),
so the run winds down, collects every retrospective and closes with that
value (banner "HALTED", not "STOPPED"). The wind-down is bounded: past
`grace_s` for workers plus an equal allowance for the entry node, or if the
request cannot be written, the old hard halt fires. A turn that raises during
the wind-down closes anyway. Whoever never gave a retrospective is logged by
delegation id (`RETROSPECTIVES_MISSING`). After a USD cap fires the wind-down
spends a little more; that is accepted. **Where:** `infra/stop_request.py`,
`nodes/stop.py` (`StopMixin`), the `Done` stop path in
`nodes/tools/routing/feedback.py`, `terminal.STOPPED`. **Watchdog:**
`runtime.stop_grace_s` (only under `watchdog_launcher`; ON by default at `min(900 s, deadline/10)`, an explicit value overrides it, `0` opts out, a value at or past the deadline is refused) writes
the request that many seconds before the deadline (`by="watchdog"`,
`grace_s = stop_grace_s/2`: half for workers to wind down, half for the entry
node's retrospective round); the deadline and the kill do not move.
**Viewer:**
`POST /api/runs/{id}/stop` (write-token gated like notes) writes the same file
with `by="viewer"`, refuses a closed run or an already-pending stop (409), and
appends the action to `studies/<study>/viewer_actions.jsonl`. **Status:** node
side, watchdog and the endpoint are done; the Stop button (front end) and
Kill (needs the PID registry that Start, 5.3, creates) follow.

---

- **Thinking display (`runtime.thinking_display`):** `summarized` (default) or
  `omitted`, passed as `thinking={"type": "adaptive", "display": ...}` to
  `ClaudeAgentOptions` on models that support adaptive thinking (Opus/Sonnet
  4.6+, the 5.x families); other models are untouched. Newer models default to
  `omitted`, which returns every ThinkingBlock with empty text and only a
  signature, so transcripts and the viewer read as bare tool calls (Sonnet 5.5
  smoke 20260928T233115: 0 of 78 assistant records carried thinking, against
  126 of 369 on Haiku). Billing is the same either way — the full thinking
  tokens are charged (Anthropic docs, "Controlling thinking display"). Each
  assistant transcript record now also carries `thinking_omitted`, the count
  of thinking blocks that arrived empty, so "thought but hidden" is
  distinguishable from "did not think". A value outside the two is a
  `ValueError`, not a silent default.

- **Context compaction is visible, on every backend:** the Claude SDK's
  `compact_boundary` and the local backends' `trim`/`compact` policy both write
  a `CONTEXT_COMPACTED` row to `debug/diagnostics.jsonl` unconditionally (debug
  off included; a local compaction is recorded once and again only when it
  moves, not on every model call), via the one shared
  `backends.base.record_stream_diagnostic`. With debug on, the transcript also
  carries the record (`system`/`compact_boundary` for Claude,
  `ContextCompaction` with `policy` and, for `compact`, the `summary` text for
  local). The viewer draws one inline marker at that point in the node's
  transcript (before -> after tokens, policy, messages dropped; the summary
  collapsed), and the run-wide Monitor list shows the diagnostic. **Where:**
  `backends/openai_compatible.py` (`_context_hook`), `backends/claude.py`,
  `viewer/app.py` (`_compaction_facts`, `_compaction_html`). **Status:** done.

### Prompt prose names tools the way the backend exposes them
- **What:** on the Claude backend the SDK exposes closure tools only as
  `mcp__f3dasm_agent_tools__<Tool>`, so `ClaudeAdapter._render_system_prompt`
  rewrites every unambiguous tool reference in the assembled prompt (prose and
  tool docstrings) to that name: `Name(`, `` `Name` `` and multi-word names
  such as `ReportEvals`. A lone capitalised word without call syntax
  (`Wait for...`) is left alone. Other backends are unchanged. Without it the
  model called the bare name: "No such tool" (`ReportEvals`, `SendMessage`) or
  the CLI's own disabled native `Write`.

### Store integrity guard (RunScratch / RunNotebook)
- **What:** both tools run against a sandbox copy, and a guard fingerprints the
  real store around the call. Only a change that destroys existing content
  (a rewritten row, a deletion) is reverted, and only when no delegation was
  running and the file is still exactly as the call left it. A change that
  keeps every old row/key and only adds rows or columns (a concurrent
  campaign's flush, including one that declares a new provenance column and so
  rewrites the header and domain.json) is an append: reported, never reverted.

## Tools (every one must be documented above; the test enforces it)

| Tool | Feature |
|---|---|
| `Delegate` | Delegation (dynamically injected) |
| `Wait` · `FollowUp` (entry node: operator channel) · `ReportEvals` | Delegation + messaging + telemetry (`Wait(id, block=False)` is the status poll) |
| `WriteCell` · `ShowNotebook` · `WriteDeliverable` | Notebook authoring (`WriteDeliverable`: the study's declared extra files only). Three markdown-cell names are RESERVED with an auto-added canonical heading (`problem`, `hypotheses`, `verdict` — the last is `<deliverable_format>` step 7, `## Verdict & result`, ahead of the analysis pillar); any other name is a free-form custom narrative cell (content used verbatim, no forced heading), mirroring the custom code-phase philosophy — the deliverable's structure must not block what an agent needs to say. Only a pillar name or `<pillar>__why` collides and is rejected |
| `RunNotebook` | Per-cell notebook debugger (#13), and with `gate=True` the reproduction gate as a dry run |
| `RunScratch` | Worker scratch execution against a ledger copy |
| `WriteNote` · `ReadNote` | Agent scratch notes |
| `QueryStore` | Canonical evaluation-store read: no arguments → the store summary and budget line (formerly `RecallStore`); any argument → rows (declaration-gated; shared verbatim across node types — strategizer, workers, and the critic). `QueryStore` accepts `where=` (a pandas `query()` expression over the joined inputs+outputs frame — compound feasibility predicates + arithmetic on input columns in one call) and `limit=` (sets the listing cap, default 20); bad `where` returns a column-listing ERROR, never raises (spec 09). The list/`where` view surfaces INPUT columns (a design's coordinates, not just its outputs), and an empty match reports `0 of N scanned` as an unambiguous TRUE zero (distinct from a missing column, which ERRORs) — UNLESS `where=` exact-`==`s a float-dtype column, in which case the zero-row message hedges ("NOT necessarily a true zero") and hints at an `abs(x-v)<1e-6` tolerance predicate instead, since a real row can be silently excluded by float representation error alone (run 20260825T012642: independently hit by the strategizer and 3 of 8 critic rounds, each paying a diagnosis cycle to find the same workaround). Heuristic (regex `col==literal`, checked against the joined frame's dtype), scoped to int/bool exact-`==` staying untouched (e.g. `feasible==1` is not float-precision-sensitive). `columns=` narrows which COLUMNS are shown (the row-narrowing analogue of `where=`/`limit=`) — a wide store's rows can overflow the response token limit even after `where=`/`limit=` have already cut the row count down (run 20260816T013744, 449,879 chars from a single call); `_namespace` is always kept regardless of `columns=`, and a requested column that doesn't exist returns a column-listing ERROR, same convention as `where=` |
| `OracleStatus` | On-demand read of the CURRENT canonical oracle registration — `run_config.json`'s `evaluator_entrypoint`/`evaluator_lookup`/`evaluator_output_names` plus any per-namespace `oracles` entries, read fresh on every call. Declaration-gated to strategizer/datagenerator/implementer/critic/debugger. Exists because `register_evaluator_entrypoint()` can repoint the canonical entrypoint BETWEEN delegations (whenever a datagenerator delegation authors/extends the generator), and the only prior signal was a one-shot `[Evaluator registered: ...]` notification a busy agent could fail to reconcile with its own stated beliefs — which cost one wasted real evaluation job before a delegation self-diagnosed via bit-identical outputs (run 20260717T014507) |
| `HypothesisPropose` · `HypothesisUpdate` · `HypothesisList` | Hypothesis ledger — read (`HypothesisList`, full entries when given ids) is declaration-gated to any node; mutate (Propose/Update, which also links a post-hoc falsification attempt) is strategizer-only |
| `MilestoneList` · `MilestoneSet` | Process milestones (strategizer-only); `MilestoneSet` adds, completes or skips |
| `ReadProblemStatement` | Verbatim read of PROBLEM_STATEMENT.md — declaration-gated, uniform across all 6 default agents (strategizer, literature_reviewer, data_generator, implementer, critic, debugger). Replaces the old `inject_problem_statement` push flag (which only ever set `True` on the literature reviewer and was checked only inside `Delegate()`, so no other worker could reach the run's actual goal/success-criteria text at all) with one pull-based tool every agent has equally |
| `BashOutput` · `KillShell` | Bash companions: poll / stop a backgrounded shell (#24) |
| `Read` · `Write` · `Edit` · `Bash` · `Glob` · `Grep` | Workspace file/shell primitives |
| `Done` | Close the run for the gate |

### Token usage survives a raised stream and sums over retried attempts
- **What:** `ClaudeAdapter` folds each attempt's streamed usage
  (`message_start`/`message_delta`, or the ResultMessage) in `ainvoke`'s
  `finally`, so a stream that raises (idle TimeoutError, API error) still
  reports the tokens it had streamed. `invoke` sums every attempt
  (`_combine_attempt_usage`) and hands the total to `on_session_end`.
  `total_cost_usd` is the sum of the known values, or None if any attempt
  lacks one ("unknown, not zero"; a raised attempt has no ResultMessage, so
  its cost is unknown).
- **Reading older summaries:** before this change "tokens per delegation"
  counted only the final SUCCESSFUL attempt, and a delegation whose every
  attempt raised recorded 0 (run 20260928T225501, D035: 8 billed sessions,
  $0). From now on it counts every billed attempt, failed and retried ones
  included, so a delegation that retried reads higher than it would have.
  **Status:** done.

### Every LLM retry leaves a diagnostics row
- **What:** `retry_on_transient` reports each retry through an `on_retry`
  callback; every node installs `Node._record_llm_retry` on its adapter, which
  appends one `event: "LLM_RETRY"` row to `debug/diagnostics.jsonl` (node,
  delegation_id, attempt, max_attempts, exception type, message, delay_s). A
  retry is not an error, so it does not bump the node's error count.
- **Why:** a run had 32 retries across 14 delegations and none was visible.
- **Where:** `backends/base.py::retry_on_transient`,
  `nodes/recording.py::_record_llm_retry`. **Status:** done.

### Transcript tool calls carry their `tool_use_id`
- **What:** each entry of an `assistant` transcript record's `tools[]` has the
  `tool_use_id` (Claude backend) that the matching `tool_result` record cites,
  so a call and its result pair exactly instead of by position or name.
- **Where:** `backends/claude.py::_record`. **Status:** done.

### An errored delegation's traceback is capped for the delegator
- **What:** `Wait()` / `Delegate(wait=True)` return an errored delegation's
  traceback as its first 1500 and last 3500 chars, with the middle replaced by
  a note naming `debug/delegations/<id>/error.txt`, which holds the full text.
  The root exception is on the last line, so the tail stays whole.
- **Why:** an exception carrying a huge payload came back as ~100k chars in
  the delegator's context (run 20260928T225501).
- **Where:** `WorkerSession._cap_traceback` in `routing/delegation.py`.
  **Status:** done.

### The delegation id stamped on ledger rows: env var first, cwd second
- **What:** `get_evaluator()` stamps `F3DASM_DELEGATION_ID` when set (the
  backend injects the session's own id; an agent may set another on purpose,
  e.g. to re-ledger a predecessor's record) and falls back to a `D###` cwd
  name only when it is unset.
- **Why:** the cwd used to win, so a process standing in another delegation's
  directory was stamped as that delegation (run 20260928T225501: env D035,
  cwd D036, row stamped D036).
- **Where:** `evaluation/oracle_resolution.py::_resolve_delegation_id`.
  **Status:** done.
