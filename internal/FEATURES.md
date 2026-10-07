# Agentic system — feature catalog

The single place that says **what the agentic system can do, why, and where it
lives.** Read this to get your bearings without reading code.

> **Contract (enforced):** every agent tool listed in an agent's `tools` set MUST
> be named in this file (and belongs in the "Tools" table below) —
> `tests/test_features_documented.py` fails the build if the name is absent
> anywhere. Injected closures are not enumerated by the test. Every new *capability* (tool OR infrastructure)
> MUST get an entry here in the same commit that adds it. The test can only
> enumerate tools; infrastructure features rely on this written contract.

Format per feature: **what** (plain language) · **why** · **where** (files) ·
**config** (if any) · **status**.

---

## A. Science & orchestration

### Hypothesis ledger
- **What:** the run's record of falsifiable hypotheses and their verdicts (OPEN /
  SUPPORTED / FALSIFIED / INCONCLUSIVE), append-only.
- **Where:** `hypothesis_ledger.py`; the mutate tools `HypothesisPropose`/
  `HypothesisUpdate` (`LedgerTools`, `nodes/tools/routing/ledger.py`;
  `falsification_attempt=True` links an unflagged delegation as an attempt);
  read-only `HypothesisList` in `nodes/tools/routing/store.py`, grantable to any
  node; per-run file `debug/strategizer_notes/hypotheses.json`.
- **Guards (HypothesisUpdate):** SUPPORTED with no completed attempt is a
  two-shot confirm (re-call with a ≥30-char justification); a closing verdict
  cites ONE completed (DONE) delegation; `falsification_attempt=True` refuses a
  delegation that started before the hypothesis was registered or is not Done;
  `D000` (precomputed pool) is citable, as evidence or attempt, only when the
  store holds D000 rows (`_check_d000_pool_exists`). Over 3 OPEN (`MAX_OPEN`) is
  a nudge, not a block.
- **Status:** core.

### Falsification charter (the Popperian rules)
- **What:** the single binding text defining how a hypothesis may be tested and
  labelled (severity of the attempt, verdict follows the result, no goalpost-moving).
- **Why:** one shared standard the strategizer, the critic and the live verdict
  validator all cite.
- **Where:** `knowledge/charter.py`. **Status:** core (§4 user-owned).

### Live verdict validator (#9)
- **What:** when a hypothesis is closed, an independent referee checks — *live* —
  that the verdict obeys the charter, and nudges the strategizer if not
  (critique appended to the HypothesisUpdate result and written onto the ledger
  entry; `VERDICT_SUBSTANCE_FLAG` diagnostic; from the 2nd flag on the same
  hypothesis, a louder gate-critic warning). Closing verdicts only; no-op when no
  critic is connected.
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
- **Config:** knob `verdict_validator` (default on; ablation arm in
  `runtime/features.py`, requires `hypothesis_ledger`); settable from a study's
  `runtime:` block. **Status:** advisory, non-blocking.

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
  Skips are announced by ONE bounded `[EVAL NOT STORED]` line per flush (count,
  how many outputs differ from the stored rows, one example), never one line per
  design: a per-design line once made a 150-design campaign print 185 KB, which
  the CLI cut to a 2 KB preview that never held the warning. The first examples
  and the counts are also in the `DEDUP_SKIPPED` diagnostic (`detail`). A skip is
  **Notice placement** (`nodes/notices.py::insert_notice`): notices that adda
  adds to its own tool results go right after the result's first line. The
  leading word stays first, and the notice stays in the head of a large result
  whatever the host keeps. A long first line that opens like JSON gets the
  notice in front of it.
  **Pending-notice bridge** (`infra/pending_notices.py`): the wrapper also
  queues each aggregate `[EVAL NOT STORED]` / `[ORACLE CHANGED]` notice in
  `<run>/debug/pending_notices/<delegation>.jsonl`. The backend's post-tool hook
  (Claude: the PostToolUse hook that carries the raw-oracle nudge; OpenAI-
  compatible: the Bash/Write tool's nudge slot, one shared
  `post_tool_context`) drains the file for its own delegation and returns the
  text, wrapped in `<adda-note>`, as extra context outside the tool result. The
  CLI keeps only the first 2 KB of a large result, so stdout alone can hide the
  line behind a long campaign log. stdout keeps its copy. Status: delivered as
  `additionalContext`; that the model receives it is NOT yet verified against a
  real transcript record.
  A skip is also printed in-band to the campaign and counts toward the soft
  eval-budget nudge (not the canonical store tally). The key is computed from the
  inputs as submitted, before `execute()`, so evaluator-stamped kwargs can't
  defeat dedup or supersede. `dedup_scope="all"` (reproduction/deliverable
  replay) treats every store row as already seen.
  **Oracle revisions.** The registered oracle is a file that can change mid-run,
  so each row carries `_oracle_rev` (provenance column): a 12-hex hash of the
  entrypoint file plus the study-local modules it imports
  (`oracle_resolution.oracle_revision`, passed by `get_evaluator`). Delegation-
  scope dedup matches a stored row only when its revision equals the current
  one, so a design re-evaluated by an edited oracle is stored as a new row and
  the old row stays, distinguishable. Rows without a revision match any
  revision. The skip notice names the stored revision; a changed oracle prints
  one `[ORACLE CHANGED]` line per flush. QueryStore lists `_oracle_rev`; its
  best-rows table shows it only when revisions are mixed. Replay scope stays
  revision-blind. Run 20261007T002015 stored 150 rows of a broken oracle and
  skipped 1050 corrected re-evaluations before this existed.
- **Where:** `science_monitor.py` (`_check_unledgered`, `_check_unstamped_rows`,
  `_check_duplicate_evaluations`); `ledger_summary.py` `unstamped_row_count`,
  `duplicate_eval_stats`; `nodes/tools/routing/delegation.py` `Wait()`
  (`_drain_while_waiting`, `_NOTICE_POLL_S`). **Status:** core (§4 user-owned).

### Registered criterion reaches the worker (pre-registration the experimenter can read)
- **What:** when `Delegate` carries `hypothesis_ids`, the worker's task message
  gains a `<registered_hypothesis>` block built from the ledger: each
  hypothesis's statement (capped at 700 chars — context) plus its
  `falsification_criterion` and `prediction` **verbatim, never truncated**
  (the contract). With `is_falsification_attempt=True` the framing states that
  the evidence will be judged against those criteria exactly as written, and
  explicitly licenses reporting a mismatch instead of substituting a
  different test.
- **Where:** `nodes/tools/routing/delegation.py` `_hypothesis_brief()`,
  injected by `_compose_task_message` beside the constraint snapshot.
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
- **What:** a small backlog (`craft_pipeline` if `pipeline_deliverable`,
  `assess_literature_need`, `oracle_gold_state` if `reproduction_gate`). Any node
  holding the milestone tools gets a one-time, two-shot reminder per namespace on
  its first Delegate to ANY target while items are open (no role check; the
  default graph's strategizer is reminded on literature/datagenerator/critic
  delegations too); `Done` stays hard until each is resolved. Pipeline and oracle milestones auto-satisfy when their
  condition holds; the rest close via `MilestoneSet(id, DONE|SKIPPED, note=…)`
  (note required). The strategizer can add its own with
  `MilestoneSet(description=…)`.
- **Where:** `milestones.py`; `MilestoneList`/`MilestoneSet` in
  `nodes/tools/routing/ledger.py`. **Config:** `milestones_enabled`.
  **Status:** core.

### Delegation + inter-agent messaging
- **What:** the strategizer delegates work to specialist agents and they report back;
  agents ask/answer/approve through one peer-and-human messaging tool (see
  `SendMessage` below).
- **Where:** `nodes/tools/routing/`, `nodes/orchestration.py`.
- **Tools:** `Delegate`*, `Wait`, `SendMessage`, `ReportEvals`, `RecallHistory`.
  (`Wait(id, block=False)` is the status poll that used to be `GetStatus`.)
  (*`Delegate`/`Wait` are injected only on a node with ≥1 outgoing edge;
  `SendMessage` only when `peer_interaction` is on; with it off, the entry node
  keeps `FollowUp` as its human channel. `RecallHistory` is injected whenever a
  delegation log exists.)
  `Confer`/`Reply`/the worker-facing `FollowUp`/`ReportProgress` are the
  pre-spec-12 surface `SendMessage` replaced; they no longer exist in either
  `peer_interaction` arm (see below).
- **Fan-out harvesting:** `Wait()` takes an OPTIONAL delegation id. Bare
  `Wait()` blocks until whichever delegation becomes actionable first among the
  CALLER's own children (another delegator's are never harvested by, nor block,
  it) — a
  finish, a worker's question, or a report OPEN FOR REVIEW (delivered, hence
  read, so it can be approved/answered while siblings still run; the reply
  names what is still in flight) — and returns that one's report (labelled
  with its ID), marking it read so N in flight are
  drained by N calls; it refuses when nothing is in flight, and refuses rather
  than hanging when every in-flight delegation is open for review (finalize with
  `SendMessage(id, …, approve=True)`) or has already died without reporting
  (read with `Wait(id, block=False)`) (a blocking call ends no turn, so the run's
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
  lazily and accepts it only if the store already holds ≥1 row and the notebook
  runs cleanly within a time ceiling, adds zero new oracle evals, leaves the
  ledger unchanged, and (when both are printed) `REPRODUCED` = `CLAIMED_HEADLINE`. The printed `REPRODUCED:` headline is informational —
  the critic checks its provenance (it must trace to a real ledger row); the runtime
  no longer machine-matches it to an objective extremum (that wrongly rejected
  constrained optima — audit 20260624T021359).
- **Where:** `evaluation/notebook_exec.py`, `nodes/tools/routing/notebook.py`,
  `nodes/reproduction_gate.py` (`_reproduction_gate`).
- **Tools:** `WriteCell` (create / edit / delete one named cell — what were
  four tools), `ShowNotebook`, `RunNotebook(gate=True)` (the Done() gate as a
  dry run; uncapped since 068616c, each call shows a running count),
  `WriteDeliverable` for the study's declared extra files only, and
  `RunScratch(code)` (scratch snippet against a ledger copy). Both execution
  tools hash the real store and pipeline.ipynb before and after, revert any
  destructive change and report it as an ERROR (see **Store integrity guard**);
  a backstop, not a filesystem sandbox.
- **Known inconsistency (code):** `RunNotebook`'s agent-facing docstring still
  says "Limited to 10 per run" (`notebook.py`), though the cap is off (`_BUDGET = None`).
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
  through it). A refresh that changes the table returns the cell's new rev,
  surfaced in `RunNotebook(gate=True)`'s reply, so a following
  `WriteCell('hypotheses', expected_rev=…)` doesn't stale-bounce; a no-op
  refresh never touches the file. Stated to every role in
  `prompts/deliverable_format.py`'s `DELIVERABLE_FORMAT` (gated on
  `hypothesis_ledger`; shared verbatim by the strategizer, implementer, and
  critic).
- **Known inconsistency (code):** the same `DELIVERABLE_FORMAT`'s RULES
  (NOTEBOOK-LEDGER SYNC bullet) still ask for a manual hypotheses-cell rewrite
  after every HypothesisUpdate.
- **Status:** core (part of the deliverable contract).

### The reproduction gate is its own ablatable feature, independent of `pipeline_deliverable`
- **What:** the gate's mechanical enforcement, its agent-facing description,
  and the milestone that exists only because of it are now owned by a
  dedicated `reproduction_gate` Feature (`runtime/features.py`), separate
  from `pipeline_deliverable` (which decides only whether a notebook is
  REQUIRED at all). Off: `_reproduction_gate()` returns `None`
  unconditionally (Done()'s gate never runs; `RunNotebook(gate=True)`
  reports a pass once a notebook exists and the store has rows), the `<reproduction_gate_contract>` prompt
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
  Recorded per run in `run_config.json["arms"]` (effective value, defaults
  included), so a sweep's arms can be told apart after the fact.
- **Arm recording and resume drift check:** `features.arm_config()` returns the
  effective value of every ablation feature plus `max_awake_nodes`, defaults
  included. It is written to `run_config.json["arms"]` (`run_setup._init_canonical_store`),
  to every `run_status.json` write, and to the `arm_*` columns of
  `studies/run_ledger.csv`. A resume under different arms raises unless
  `runtime.allow_arm_drift` is set, in which case the first arms stay in
  `arms_initial`.
- **`runtime.bash_timeout_s` (shell-call bound, same on every backend):** seconds a
  Bash call may run before it is moved to the background (default 120, the Claude
  SDK's). The OpenAI-compatible/Ollama shell reads it at call time; the Claude
  backend passes it to the SDK as `BASH_DEFAULT_TIMEOUT_MS` on every session
  (`claude.py::_build_session_env`). The agent's own per-call `timeout` still applies, capped at 600 s.
- **`--set key=value` on both CLIs:** `python -m adda` and `python -m adda.watchdog`
  take a repeatable `--set` (`runtime/cli_overrides.py`), the command-line
  spelling of `AgenticRun(runtime=...)`: explicit precedence, validated against
  `KNOWN_KEYS` before launch, forwarded by the watchdog to its child (refused
  with `--entrypoint`). `python -m adda --model` now defaults to None, so the
  study's `config.yaml` `model:` is no longer silently overridden by the
  haiku default.
- **`Feature.requires`:** a feature whose prerequisite is off is off —
  `features.enabled()` resolves it, `features.conflicts()` lists the cases
  where its own knob said on, and `_init_canonical_store` refuses to start on
  any. Today `verdict_validator` requires `hypothesis_ledger` and
  `reproduction_gate` requires `pipeline_deliverable`. The four studies that run
  `pipeline_deliverable: false` set `reproduction_gate: false` too. Both knobs
  are read through `features.enabled` everywhere instead of
  `get_bool(key, True)` literals.
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
  chapter can carry `[[if node:<name>]]` text too, and its `feature:` key may be
  `node:<name>`, which hides the whole chapter while that node is absent. Tool
  examples are resolved the same way; an example whose gate is off is dropped.
  `tests/test_feature_arms.py` builds every arm through the real run path.
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
  commit, `1d7e14c`) — see `internal/specs/12-peer-interaction.md` item 4.
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
  `last_session_id`, captured on every `ainvoke()` — used to resume the worker
  on a non-approve message, see below).
  Only `SendMessage(id, ..., approve=True)` runs `_finish_ok`
  (`finalize_after_review`, reusing the stashed report/evals/usage
  unchanged); any other message resumes the worker (`Revising`, below). Automatic, not opt-in — every delegation, whenever the feature
  is on. `Delegate` refuses a new dispatch while ANY delegation is
  `OpenForReview` (`_check_open_reviews`), naming every open one; an
  ERRORED delegation never counts (it has no report to review). `Wait()`
  names an open review in its own bucket, never silently absorbed into "nothing to wait for" —
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
  never a sibling's or a nested child's): an open review, a finished
  delegation not yet collected, and —
  Elvis's own first-named case, "respond [to a] delegation" — an unread
  `SendMessage` question sitting in a child's `to_delegator` queue
  (`"D003 (implementer) asked you: ..."`, truncated to ~100 chars). As a WORKER (`identity` is itself a registry entry): an
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
- **Where:** `evaluation/notebook_exec.py` `diagnose_notebook`, `RunNotebook` closure.
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
  implementer, datagenerator, and debugger; math_expert takes `Bash` alone.
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
  duplicated sandbox-env blocks are unified in one `sandbox_env()` helper. It also
  sets `F3DASM_DEDUP_SCOPE=all` (+ a synthetic `D999` delegation id) so a replay
  dedups against every delegation's rows.
- **Where:** `evaluation/notebook_exec.py` `sandbox_env`, `replay_sandbox` (gate
  and viewer replay); `nodes/tools/routing/notebook.py` `_ledger_sandbox`
  (RunNotebook, RunScratch). The copy-store logic exists twice.
- **Status:** telemetry/ergonomics, not a new cap.

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
  (`node.py::_setup_sandboxed_write`, `delegation.py::_sandbox_worker_writes`). A
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
  `_sandbox_worker_writes`); `nodes/node.py` (`_setup_sandboxed_write`);
  `prompts/agent_prompts.py`; `prompts/deliverable_format.py`.
- **Status:** done. Bash remains trusted and unsandboxed by design — not
  addressed here; see the (deferred) Bash-boundary discussion this same
  finding raised.

### Output-column guidance fix
- **What:** notebook guidance requires naming the objective column EXPLICITLY. The
  earlier "first non-provenance output" auto-detect was unsafe: `output_names` is
  sorted, so with multiple outputs a constraint flag (e.g. `coilable`) sorts before
  the objective and gets silently picked. If derived, read
  `run_config['evaluator_output_names'][0]`, not column order.
- **Where:** `prompts/deliverable_format.py` (`DELIVERABLE_FORMAT`). **Status:** done.

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
  (`Delegation.namespace`), `nodes/tools/routing/delegation.py` (`Delegate` +
  registration handoff, `WorkerSession._bind_backend_context` → `set_namespace`).
- **Report-time provenance:** `QueryStore()` with no arguments (it absorbed `RecallStore` and `LedgerBreakdown`) shows per-experiment
  / per-delegation ledgered eval counts read live from the stores
  (`store.py` `_summary` → `RunStateSummary.from_store` per store; `_budget_line`), so a writeup DERIVES counts from the ledger instead
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
  into the deliverable spec (`prompts/deliverable_format.py`), the paths block
  (`agent_prompts.py`) and `RunScratch`'s docstring (`notebook.py`).
- **Tools:** `QueryStore`.
- **Status:** on main (since `34e984c`); opt-in per delegation (`namespace`
  unset = single-study path). The open-design-space *method* (BACKLOG #20) still
  awaits the 2D experiment; BACKLOG #20's branch pointer is stale.

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
  persists across every run of a study, because a paper's relevance to a
  domain does not go stale between runs. Cross-run memory of findings
  (hypotheses, mechanisms) is not built yet; it is planned future work
  (BACKLOG #46, spec 13). Lives under `runs/`, not the study root, to stay out of the
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
  literature coverage is reduced. One diagnostics path, fired once per corpus
  instance (≈ once per agent adapter) — not a second writer, not a node-handle
  threaded into the corpus module.
- **Where:** `literature/literature_corpus.py` (`embedder_fallback_reason`,
  `pop_diagnostic_event`); `agents/literature_tools/corpus.py`
  (`build_corpus_read_closures` tagging); `nodes/orchestration.py`
  (`_wrap_closure`); `nodes/node.py` (re-wraps tagged build-time closures by
  tag). **Status:** core.

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
  behaviour itself — that's a separate, still-open decision, recorded at
  `backends/claude.py` above the `last_result is None` check): `STREAM_ENDED_WITHOUT_RESULT` (`ainvoke`,
  fault="system" — the last tool in flight and the CLI's own session id,
  when available) and `REPORT_RETRY` (`_invoke_with_report_retry`,
  fault="nudge" — the classification reason, fired every time a report-retry
  actually happens).
- **Where:** `backends/base.py` (`record_stream_diagnostic`); `backends/claude.py`
  (the `_deliberate_break`/`last_result is None` check in `ainvoke`);
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
  diagnostics event (`record_stream_diagnostic`, thread-local delegation
  id/run dir, the SDK's own compaction metadata verbatim) so the event shows
  up in `debug/diagnostics.jsonl` without anyone reading transcripts. The
  OpenAI-compatible backends emit the same event when adda's own context policy
  fires (`openai_compatible.py`), so one grep covers both backends.
- **Where:** `backends/claude.py` (`_SYSTEM_MESSAGE_NOISE_SUBTYPES`,
  `_record()`'s new `SystemMessage` branch, the `compact_boundary` check in
  `ainvoke`). The viewer renders `compact_boundary` as a compaction event
  (`viewer/transcript_events.py::_compaction_facts`, shared with the local
  backends' `ContextCompaction`); other system subtypes stay unrendered.
  **Status:** core — observability only.

### CONTEXT_COMPACTED/STREAM_ENDED_WITHOUT_RESULT now fire on every node, not just worker delegations
- **What:** `record_stream_diagnostic` (above) finds `debug/` through the
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
  calls has a real delegation id. One writer (`record_stream_diagnostic`),
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
  `debug/delegations/` workspace, with one commit per delegation-log row (DONE,
  FAILED, critic/feedback audits, and `[INTERRUPTED]` at close, so a killed
  delegation's partial writes are not credited to the next committer) and the resulting sha stamped onto that delegation's record as
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
  committed via `nodes/node.py::Node._commit_workspace` (worker
  `_finish_ok`/`_finish_error`, `orchestration.py` escalation + close-time
  reconciliation); `workspace_sha` on
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
- **Where:** `epistemics/math_dsl.py` (the `Workspace` library, re-exported publicly as
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

### Knowledge-provider contract (`Consult<Corpus>`)
- **What:** one call shape, dispatch, failure mode and reply cap for every
  reference an agent consults: a description returns a menu, an exact
  name/page id returns that entry, `source=True` returns code; an unconfigured
  provider returns no tool at all. Five providers: `ConsultF3dasm`,
  `ConsultAdda`, `ConsultAbaqus`, `ConsultBasilisk`, `ConsultLiterature`.
- **Why:** retrieval is deliberately NOT uniform — rankers differ per corpus, on
  measured r@1 (protocol docstring); only the contract is shared.
- **Where:** `knowledge/protocol.py` (`providers()`, `clip`). **Status:** core.

### f3dasm API lookup (`ConsultF3dasm`)
- **What:** the INSTALLED f3dasm's API, introspected at run time, so it cannot
  drift from the version the run executes. Each entry leads with the canonical
  PUBLIC import path (`__module__` reports the private definition site for
  public symbols); private symbols are answerable but marked "do not import".
- **Why:** replaced guessing beyond the fixed `F3DASM_CORE_IDIOMS` excerpt.
- **Who:** strategizer, implementer, datagenerator (`agents/*.py`
  `build_closure_tools`).
- **Config:** `f3dasm_api` feature (default on; off removes the tool and the
  `f3dasm_api_lookup` prompt section — an ablation arm); no tool if f3dasm fails
  to import.
- **Where:** `knowledge/f3dasm_api.py` (`F3dasmApi`, `build_f3dasm_api_closures`),
  `runtime/features.py`. Test: `tests/test_f3dasm_api.py`. **Status:** core.

### Abaqus reference docs (`ConsultAbaqus`, `AbaqusDataGeneratorAgent`)
- **What:** offline Abaqus manual lookup for the datagenerator. Hits are TOC
  locations (book > chapter > section + page_id) built on each book's
  `structure.xml`; a page_id returns the whole page; a miss can say "exists, not
  held" (the media is a HotFix delta holding ~45% of the pages the TOC lists).
- **Why:** an oracle-construction benchmark gave 4/4 solver-traceable runs with
  the tool and 0/5 without; wrong keyword guesses get corrected
  (`*FREQ`→`*FREQUENCY`). v1 (flat chunks + BM25) discarded the publisher's
  structure; v2 makes the TOC the spine (`reader.py` docstring).
- **Config:** `ADDA_ABAQUS_DOC_CORPUS` or `corpus_dir=`. The corpus is licensed
  and NOT shipped; build it with `reader.AbaqusDocs.build` (`abaqus` extra, lxml).
  Reading is stdlib sqlite FTS. Unconfigured = no tool.
- **Where:** `knowledge/abaqus/` (`__init__.py`, `reader.py`, `builder.py`),
  `agents/abaqus_datagenerator.py`. **Status:** core, opt-in by class (not in
  `_default_graph()`).

### Basilisk source lookup (`ConsultBasilisk`, `BasiliskDataGeneratorAgent`)
- **What:** lexical index over a Basilisk checkout at authoring granularity
  (solver header + worked case); a query returns a menu, a filename that entry.
- **Why:** Basilisk C is a qcc DSL and its headers don't parse standalone, so an
  AST index fails; dense retrieval did not earn its cost. Measured against
  ripgrep (`tests/test_basilisk_vs_ripgrep.py`).
- **Config:** `ADDA_BASILISK_SRC` (dir containing `src/`) or `corpus_dir=`.
  Unconfigured = no tool. Not redistributed.
- **Where:** `knowledge/basilisk/` (`extract.py`, `index.py`),
  `agents/basilisk_datagenerator.py`. **Status:** core, opt-in by class.

### adda self-lookup (`adda-docs`, `ConsultAdda`)
- **What:** an index of the INSTALLED adda package for an outside coding agent.
  Four unit kinds: symbol, module docstring, constant, shipped KB markdown.
- **Why:** all four together beat ripgrep on 128 labelled queries (r@1 0.375 vs
  0.23); symbols alone lose to it (`internal/tools/source_index_baseline.py`).
- **Surfaces:** console script `adda-docs` (`--source`, `--overview`);
  `adda.explain.explain()`; the viewer's `GET /api/docs`; `ConsultAdda`
  (registered in `protocol.providers()`, given to no default agent).
- **Where:** `src/adda/explain.py`, `knowledge/f3dasm_api.py` (`AddaApi`),
  `knowledge/protocol.py` (`_build_adda_closures`). **Status:** core
  (`ConsultAdda` available, unwired).

### DoE playbook prompt section
- **What:** a method prior on every implementer call (~625 tokens): space-filling
  LHS recipe, eval-budget arithmetic, the surrogate-guided exploit loop. Split
  out of `<f3dasm_api>` so API facts, the oracle contract (not ablatable) and the
  method prior can be ablated separately.
- **Why:** tests whether an explicit method prior substitutes for capability in
  small models (`runtime/features.py` comment).
- **Config:** `runtime: doe_playbook` (default on).
- **Where:** `<doe_playbook>` in `agents/implementer.py`; `runtime/features.py`.
  **Status:** core, ablatable.

### DebuggerAgent
- **What:** a specialist that reproduces a failure from the exact command,
  traces it to root cause, and optionally applies a minimal fix. Report sections
  `### Root cause`, `### Fix applied`, `### Conclusions`, `### Numbers`. Tools:
  Bash/Read/Grep/Edit/Write, read-only store (`QueryStore`, `OracleStatus`,
  `HypothesisList`), `BashOutput`/`KillShell`, `ReadProblemStatement`.
  `role="debugger"`, so implementer-only nudges don't fire on it.
- **Where:** `agents/debugger.py`; exported as `adda.DebuggerAgent`.
  **Status:** available, not in `_default_graph()`.

### Pre-run problem-statement review
- **What:** an advisory check that PROBLEM_STATEMENT.md is well-posed before an
  autonomous run, on five domain-agnostic elements (objective, design_space,
  ground_truth, validity, deliverable). Always writes
  `debug/problem_statement_review.md`; never blocks. Interactive runs may offer a
  per-gap refine and append accepted clarifications. Fresh runs only (skipped on
  resume and with an injected graph).
- **Config:** `AgenticRun(review_statement=True)`.
- **Known inconsistency (code):** the `cfg["review_statement"]` fallback applies
  only when the caller passes `None`; the parameter defaults to `True`, so the
  config key is effectively ignored (`agent_runtime.py`).
- **Where:** `epistemics/reviewer.py` (`ProblemStatementReviewerAgent`,
  `REVIEW_ELEMENTS`, `parse_review`), `runtime/agent_runtime.py::_review_problem_statement`.
  **Status:** core.

### Per-node tools from config: the `Default` token and `nodes.<node>.tools`

A node's tools come from its Agent class. `config.yaml` may replace them for one
node: `nodes: {<node>: {tools: [Default, Bash, ...]}}`. The list REPLACES the
class set (no merge). An unknown node name, a non-mapping block, or a `tools`
that is not a list of strings is a startup error (`runtime/node_tools.py`);
`viewer/study_edit.py::validate_config` rejects the same. A node entry also takes
`model`, `backend` and `base_url` (non-empty strings; `node_tools.NODE_KEYS`),
which `apply_node_config` installs on the Agent; the class values come back when
a second config is applied.

A `tools` list from config also WITHHOLDS the always-on closures it does not name
(`node_tools.DROPPABLE_CLOSURES`: ConsultHandbook, ConsultLiterature, ReportEvals,
RecallHistory, sandboxed Write). `withheld_closures(agent)` is applied in
`Node._init_capabilities`, `build_routing_tools` (RecallHistory) and the worker
install path (ReportEvals, Write). A node with no config list is unchanged; a
`Default` node keeps the sandboxed Write. `TOOLS_CONFIG_DIFFERS` names them.

`Default` is a token (a class `tools` set may hold it too). On the Claude backend
a node holding it gets the CLI's whole default built-in set (SDK preset
`claude_code`) and NONE of the floor that blocks WebSearch, WebFetch, Task and
ExitPlanMode for other nodes. Two blocks stay, as correctness: the bare built-in
name of every closure the node declares (the sandboxed `Write` replaces the
native one). On other backends it is that adapter's native set
(`DEFAULT_NATIVE_TOOLS`: Bash, BashOutput, KillShell, Read, Write, Edit, Glob,
Grep). `Default` is never inherited or injected; the critic keeps its explicit set.

Records, all informational and never blocking: `debug/node_tools.json` (source
class|config, class set, resolved set, diff, `feature_withheld` = tool -> the
disabled ablation feature that took it, `effective` = resolved minus those); diagnostics rows (fault `nudge`)
`TOOLS_CONFIG_DIFFERS` (config differs from the class), `DEFAULT_TOOLS_BYPASS`
(once per Default node: it can bypass Delegate, the literature rate limiter and
cache, the reproduction gate via NotebookEdit, FollowUp via AskUserQuestion),
`TOOLS_RESOLVED` (the built-ins the CLI init record really listed, and those
adda has not reviewed), `DEFAULT_TOOLS_EXPANDED` (non-Claude backend).

## D. Resource governance

### Soft eval-budget nudge
- **What:** when the shared ledger crosses 80/100/150% of the eval budget, the
  *offender* (the running campaign) is nudged via its own output — capped at one per
  band. **Soft: never stops the campaign** (the eval budget is the agent's call).
- **Where:** `evaluation/instrumented.py` `_maybe_nudge_budget` (from `_flush`;
  bands `_NUDGE_BANDS` = 0.8/1.0/1.5); `eval_budget` reaches it via
  run_config.json (`run_setup`) → `oracle_resolution`. Audit row: `BUDGET_WARN`
  in diagnostics.jsonl.
- **Config:** `eval_budget` (config.yaml or `AgenticRun(eval_budget=)`; no env
  var). **Status:** done.

### Hard memory cap (host safety)
- **What:** a 5-second watchman sums each delegation's process-tree **resident (RSS)**
  memory and kills the tree if it exceeds the cap. Real-usage based; verifies the
  process is still ours (start-time match) before killing, so a recycled PID is never
  hit. Per-delegation, absolute (not a share of system RAM), does not sum across
  delegations.
- **Where:** out-of-repo harness `_memory_watcher`; `infra/watchdog_cleanup.py`
  (`check_memory_and_kill`, `_owned_pids`); `infra/resource_backend.py`; the cap is
  resolved (config → SLURM allocation → default) by `runtime/run_setup.py`
  `resolve_mem_cap_bytes`.
- **Config:** `mem_cap` (config.yaml; an exported `F3DASM_MEM_CAP` is refused); default 4 GiB. On SLURM set
  below the job's `--mem`. **Status:** enforced only by the out-of-repo
  harness' `_memory_watcher` (not in this repo). In-package launches
  (`python -m adda`, `adda.watchdog`) resolve and advertise `mem_cap_bytes`, but
  nothing calls `check_memory_and_kill`, so there is no kill and no peak-RSS
  sample (the `Wait`/KPI-footer peak-RAM lines stay absent). `set_self_limit` is
  a no-op on both backends. Gap: wire a watcher into `AgenticRun` or the
  `adda.watchdog` parent. cgroup-native HPC backend = future seam.

### Resource backend (OS abstraction)
- **What:** one interface (`set_self_limit` / `read_rss` / `kill` / `proc_start_time`)
  so memory/kill OS-specifics live in one place; psutil impl + stdlib fallback.
- **Where:** `infra/resource_backend.py`. **Status:** done (Linux cgroup backend = future).

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
  max|min, optional `feasible` 0/1 column, optional `lines` = labelled chart reference lines
  `[{value, label}]` in the column's own units, optional `unit_label` = display-only axis scaling
  `{divide_by, label}`; both validated in `parse_objective` and served by `/figure_of_merit`). Parsed and validated by
  `evaluation/objective.py::parse_objective` in `run_setup._init_canonical_store`
  (unknown key, missing direction, or a column the declared oracle does not
  produce refuses the run), recorded as `objective` in run_config.json.
  `objective_values` / `best_so_far` are the one definition of "a row counts"
  (finite, below the sentinel magnitude, and `feasible == 1` when declared):
  `viewer/readers.py::read_figure_of_merit` (`GET /api/runs/{id}/figure_of_merit`,
  the best counted row over EVERY store that records the declared columns — canonical and each namespace — with its `namespace`, `scored`, and `not_scored` (store + missing columns); `declared: false` when absent) and the run
  ledger both use it. Absent block = undeclared: finite rule, running min AND
  max, nothing ranked. A study whose oracle is authored mid-run (no output names
  at start) skips the start check; each later oracle registration
  (`delegation.py::_check_objective_columns`, from the manifest's `output_names`)
  that lacks a declared column writes an `OBJECTIVE_COLUMN_MISSING` diagnostics
  event (namespace, column, key) and notifies the delegator; a manifest without
  `output_names` is reported the same way (key `output_names`). It never refuses a
  registration. Tests: `tests/test_objective_declaration.py`.
- **Declared funnel and the problem-agnostic viewer (2026-10-05):** optional top-level
  `funnel: [col, ...]` in config.yaml, the ordered 0/1 stage columns of the viewer's stage funnel;
  `objective.py::parse_funnel` validates it like `objective` (non-empty, no repeats, each a declared
  oracle output, refused at start otherwise) and `run_setup` records it as `funnel` in
  run_config.json. With none, `/funnel` lists every 0/1 column in store order; no column name is
  privileged. `tests/test_viewer_problem_agnostic.py` is the spec 15 §6a guard: it fails if the
  shipped viewer, `internal/tools/viewer_shots.py`, or the viewer's UI/objective tests contain a term
  from the denylist (every study's declared columns and names in `studies/`, plus the benchmark
  suite's physics and columns). Tests: `tests/test_objective_declaration.py`, `tests/test_viewer_readers.py`.

### Run-ledger process KPIs (2026-10-04)
`studies/run_ledger.py` adds `error_returns` (ERROR_RETURN count, target 0),
`objective` (`column:direction[:feasible=col]` or `undeclared`),
`first_feasible_eval` / `first_feasible_s` (position and seconds since
`debug/run_started_at` of the first row, over every scored store (canonical + namespaces,
`evaluation/objective.py::score_stores`), that counts under the
declared objective; blank = never) and `best_trace` (JSON, <=20 evenly spaced
eval counts: `best` when declared plus `stores` {scored, not_scored}, `min` and `max` when not). Gate attempts
remain `critic_consults` (count of `critic_reviews/call_NNN.md`). Tests:
`tests/test_run_ledger_kpis.py`.

### Run report: Verdict audit (2026-10-05)
`studies/run_ledger.py::analysis_brief` adds a "## Verdict audit" section after the ERROR_RETURN section: one block per
closing status entry (SUPPORTED / FALSIFIED / INCONCLUSIVE) in `strategizer_notes/hypotheses.json`, with the
statement, falsification criterion, prior -> posterior, evidence numbers and validator note verbatim, and
`N_NEW_EVALS`, the store rows stamped with the cited delegation over the canonical store and every
namespace (precomputed-pool rows are reported apart, not counted). `NO_NEW_EVIDENCE` flags a verdict with
0 such rows, and the header counts them ("k of n"). It is a flag for review, not an error, and it classifies
no claim. A run with no ledger reads "no hypothesis ledger". Tests: `tests/test_run_ledger_verdict_audit.py`.

### Per-delegation resource telemetry
- **What:** `Wait(id, block=False)` shows a delegation's eval count, current RSS, and **peak
  RSS** (the high-water across the watcher's ticks), so the strategizer can see a
  fat or fattening campaign (and tell the implementer with `SendMessage`).
- **Where:** `nodes/tools/routing/`, `infra/watchdog_cleanup.py`
  `delegation_rss` / `delegation_peak_rss`.
- **Status:** done; the peak is filled only where `check_memory_and_kill` runs
  (out-of-repo harness), so in-package it stays absent (see **Hard memory cap**).

### Resource AWARENESS (telemetry, NOT enforcement)
- **What:** primes agents to be efficient with the things models ignore — time,
  RAM, disk, parallelism width — via two surfaces:
  - **Static envelope at delegation start:** "resources: ~N CPU cores · RAM cap
    X GB per delegation … disk free Y GB" for every role; only campaign workers
    (`for_worker`) get a "parallelize your EVALUATIONS, sized to the RAM cap"
    line. The strategizer is deliberately not nudged to fan out (run
    20260628T224159).
  - **Known inconsistency (code):** the worker line still names
    `gen.call(mode='parallel')`, which the `mode="parallel"` hard cap refuses.
  - **Measured peak RAM in the KPI footer:** `peak RAM (this delegation): Z GB of
    X GB hard cap`, the watcher's high-water — so memory cost travels with the
    result like wall-time already does.
- **Footprint (by design):** peak RAM rides the memory watcher's existing 5s poll
  (one `max()` per tick — no new poll/thread/I/O); the envelope is one
  `sched_getaffinity` (SLURM/cgroup-aware; `cpu_count` fallback off-Linux) + one
  `shutil.disk_usage` (O(1) `statvfs`, **never** a recursive
  `du`); the per-eval hot path is untouched (no per-eval RSS/disk stamping).
- **Where:** `infra/watchdog_cleanup.py` `resource_envelope` / `delegation_peak_rss`
  (high-water recorded in `check_memory_and_kill`); `agent_runtime.py`
  `_resource_stanza`; `agent_prompts.py` `{resources}` placeholder;
  `ledger_summary.py` `delegation_footer` peak-RAM line.
- **Status:** awareness only — enforcement lives in the hard caps (memory,
  parallel refusal, Delegate cutoff, USD ceiling).

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
- **Launch guidance:** 5 awake needs `--mem >= 32G`. Values <2 are clamped to 2
  (strategizer + one worker). Read via `runtime/features.py` `max_awake_nodes()`.
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
  weights-free — local HF cache first, then two `requests` GETs (HF Hub
  model-info JSON + config.json for KV fields); no `huggingface_hub`/`transformers` dependency), so a
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
  node, polls `/v1/models` past the cold model load, keeps the endpoint on the run
  (`_served_base_url`, never `os.environ`) so the vLLM adapters reach it over the cluster network, and
  scancels the job on normal close and crash (`execute()`'s `finally`).
  **Known gap:** the hard-kill reap (`reap_run_serve_job`, reads
  `debug/serve_job.jobid`) is called only by the out-of-repo harness;
  `adda.watchdog` does not call it, so a watchdog timeout leaks the serve job. A build-time, leading-order
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
  default. Needs backend
  vllm (or openai/openai_compatible); any other backend only logs a warning and
  the served endpoint is ignored. Disabled → hosted-API runs are unchanged.
- **Where:** `infra/slurm_llm.py` (aliases, metadata fetch, VRAM sizing,
  serve-hints, resolve, render/submit, wait, throughput bound, teardown,
  reaper); `agent_runtime.py`
  (`_maybe_start_slurm_llm` + the teardown `finally` in `execute()`);
  out-of-repo harness watchdog only (serve-job reap). Reuses f3dasm's
  `SlurmCluster`/`SlurmResources` unchanged.
- **Status:** Phase 1 done, headless-tested; validate on a real GPU cluster
  before making it a default anywhere (greenfield + cluster-specific).

## E. Runtime safety

### Wall-clock watchdog + recursive reap (#11/#14)
- **What:** a hard wall-clock timer force-exits a stalled run; on exit it recursively
  kills every campaign process tree — including detached/new-session ones that a
  process-group kill misses.
- **Where:** the out-of-repo benchmarks harness' own launcher `_watchdog`
  (out-of-repo harness only) and, in THIS repo, `python -m adda.watchdog` (BACKLOG #41 — see
  below), both driving `infra/watchdog_cleanup.py`'s `reap_process_group` /
  `reap_governor_pids`.
- **Config:** watchdog = 2× the run's time budget (a floor, not a default —
  `adda.watchdog`'s `--watchdog-multiple` can only raise it). Operational
  kill-switch `F3DASM_DISABLE_WATCHDOG=1` turns the wall-clock force-exit OFF
  (the memory-cap watcher stays on) on the out-of-repo harness — for long
  supervised runs (out-of-repo harness only; `adda.watchdog` has no disable
  switch). **Status:** done.

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
  (called in `Delegate()` after the stop and open-review refusals, before
  target resolution); the shared ladder
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
- **Where:** `infra/watchdog_cleanup.py` `write_watchdog_retrospective`, called from
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
- **Where:** `infra/watchdog_cleanup.py` `write_fallback_retrospective` (shares its
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

### Handbook (knowledge base) + `ConsultHandbook`
- **What:** a curated, on-demand handbook of DISCRETIONARY conventions (idioms,
  how-tos, gotchas: evaluate via get_evaluator, one delegation = one experiment,
  pipeline patterns, running on SLURM, symbolic derivation, surrogate-guided
  optimisation). One self-contained markdown chunk per file, frontmatter
  id/title/tags/audience (+ optional `feature`). The falsification charter is
  always chapter 1, built from the `FALSIFICATION_CHARTER` constant (no duplicate
  file). Two surfaces: (1) an audience-filtered MENU (`- id: title`; entries with
  no audience shown to all) injected into every preamble's `{knowledge}` slot, so
  agents SEE what they can pull, the same way they see their tool list;
  (2) `ConsultHandbook(query="")` on EVERY node: no arg → TOC, exact id → full
  chapter, else keyword search (top 3; title×3, tags×2, body×1). A chapter with
  `feature: <key>` is absent from menu, TOC, search and get while that feature is
  off; `[[if …]]` gates in bodies resolve per run (`features.resolve_gates`).
  Never raises into the agent loop.
- **Why:** "enforce invariants, retrieve conventions" (`knowledge/README.md`):
  mandatory rules are enforced by the ScienceMonitor and the critic gate, never
  left to optional retrieval ("a critical rule that lives only in a KB is a rule
  that silently fails"); the long tail lives here instead of bloating every
  prompt. The menu closes the discovery gap: a TOC is seen only if the agent
  already thought to call the tool.
- **Where:** `knowledge/kb.py` (`KBEntry`, `KnowledgeBase.load/menu/toc/get/search/consult`);
  `knowledge/entries/*.md` (12 + charter); `nodes/parsing.py::_consult_handbook`;
  injected once in `runtime/agent_runtime.py::_make_adapter`; menu via
  `_kb_menu(role)` → `prompts/agent_prompts.py` `{knowledge}`.
- **Config:** none; per-chapter `feature:` frontmatter (today: 0005 ←
  `pipeline_deliverable`). Title ≤100 chars (`tests/test_knowledge_base.py`).
  Curation is human-gated, fed by retrospectives; never auto-ingest.
- **Status:** done; the keyword scorer is a stand-in for semantic retrieval (API
  stable). `knowledge/README.md` and the `kb.py` docstring still say "draft" and
  name a future `ConsultKnowledge` tool (stale).

### Live constraint snapshot (`<constraints>` block)
- **What:** one frozen dataclass, `ConstraintSnapshot` (eval_budget, evals_used,
  wall budget/elapsed; derived remaining/EXHAUSTED), recomputed fresh at every use
  and rendered as a `<constraints>` block ("Evaluation budget: n/N evals used",
  "Wall-clock budget: x/y used (p%)"; "unspecified" when unset). Injected (a) on
  every turn of every node (`_constraint_refresh` in `_compose_messages`), (b) on
  every delegation report, (c) in critic GATE and FEEDBACK task messages.
  `as_dict()` persists the same numbers to DelegationLog at dispatch and
  completion. evals_used = ledger rows over all namespaces, else the sum of the
  delegation log. Advisory only: never stops.
- **Why:** replaced four partial, drifting computations; a frozen snapshot baked
  into the pinned first user turn had contradicted the live ones (module
  docstring; `_constraint_refresh` docstring).
- **Where:** `runtime/constraint_snapshot.py` (`compute_constraint_snapshot`,
  `snapshot_for_node`); `nodes/orchestration.py`; `nodes/tools/routing/{delegation,feedback}.py`;
  `nodes/critic_gate.py`; `infra/delegation_log.py`.
- **Config:** `eval_budget`, `budget`. **Status:** done (`tests/test_constraint_snapshot.py`).

### LLM-call telemetry (`debug/telemetry/`)
- **What:** one JSON row per LLM call (role, model, phase, delegation_id, ts,
  token fields, the SDK `total_cost_usd` [None under ollama, never faked],
  `cost_usd_computed`) written to `calls.<pid>.jsonl`. At close,
  `Telemetry.merge` unions them into `summary.json`: totals + by_role / by_phase
  / by_model (tokens, wall_time_s, cost, computed cost / computed_cost_calls).
- **Why:** additive and off the decision path, for post-hoc ablation ("where did
  the budget go"); a telemetry write never breaks a run (module docstring).
- **Where:** `infra/telemetry.py` (`Telemetry.record_call/merge`,
  `compute_cost_usd`); an instance per orchestrating node
  (`nodes/orchestration.py`); merged in `AgenticRun._finalize_run`; read by
  `studies/run_ledger.py`.
- **Config:** none (always on); prices in `infra/model_prices.yaml`. **Status:** done.

### Retrospective exit interview
- **What:** every node ends with a first-person `### Retrospective` about the
  SYSTEM, not the science. Strategizer, after the critic accepts: a separate
  post-Done turn (`_EXIT_INTERVIEW`) with CONSISTENCY (ok|flagged, quote both
  sides), DECISION, FRICTION (including recovered errors), BLOCKED (capability
  gaps). UNGATED/FAILED closes: `_FAILED_RETROSPECTIVE` (BLOCKED, BLOCKER,
  FRICTION). Stop/crash closes add a TIME bullet (`nodes/stop.py`). Workers: the
  same block is part of their report contract. `_record_retrospective` appends
  to `debug/retrospectives.jsonl` (16000-char cap; parse_failed rows kept).
  "CONSISTENCY: flagged" writes a `CONSISTENCY_FLAG` diagnostic and a
  notification. Missing replies are synthesized (see **Fallback retrospective**).
- **Why:** the highest-signal first-person friction record (cap raised from
  2000 after a truncation in run 20260624T021359; `recording.py`). Asked as a
  separate turn so it never pollutes the working context (`feedback.py` comment).
- **Where:** `nodes/tools/routing/feedback.py`; `nodes/recording.py`;
  `nodes/stop.py`; viewer `/api/runs/<id>/retrospectives`.
- **Config:** none. **Status:** done.

### Run architecture diagram
- **What:** `AgenticRun.render_architecture(out_path=None)` renders THIS run's
  actual agent graph — every node, its role, its description, its RESOLVED
  backend/model (each node can override either independently, matching
  `runtime/agent_runtime.py` — shown
  per card, never as one run-wide banner; `resolve_node_identity`, the same
  helper that builds each adapter), its full tool surface, and the
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
- **Where:** `runtime/run_diagram.py` `render_architecture_svg`;
  `AgenticRun.render_architecture` (default out: `run_dir/debug/architecture.svg`
  once a run has started, else `study_dir/architecture.svg`). Tests:
  `tests/test_run_diagram.py`. **Status:** done.

### Live run viewer
- **What:** `python -m adda.viewer <study-dir> [--host --port --allow-network]`
  (or `AgenticRun.serve_viewer`) serves one study's runs, live, to a browser.
  Two front ends, both current:
  - `/` → `/runs/<latest>` (`templates/graph.html`; Alpine + marked + KaTeX
    from CDN; IBM Plex bundled; Catppuccin Mocha/Latte by OS): tabs Brief,
    Overview (vertical concurrency timeline, gate rules, hypothesis hover-link),
    Graph (agent network, `run_diagram._bfs_layers` layout, SSE status dots;
    click a node for its transcript as chat bubbles, tool calls collapsed,
    adda notices tagged by kind via `app.py::_notice_kind`, compaction
    markers, science-monitor injections), Oracle (best-so-far over every
    store), Chat, Result. The only per-node transcript view.
  - `/ui` (spec 15; `static/ui/{index.html,ui.css,ui.js}`; no CDN, KaTeX
    vendored, Instrument Sans/Geist Mono, light/dark tokens): views Timeline,
    Hypotheses, Data, Deliverable, Logs, Setup; state in the URL
    (`?run=&view=&sel=`); polls only while the tab is visible and the run open.
- **Watching (`/ui`):**
  - Title block: elapsed vs budget (warn >1.5x, bad >2x), cost as "≥$" when
    any call recorded none, delegation count, best row. Pending operator
    question = banner with answer field (10 s undo).
  - Timeline: one column per concurrent slot from true session start/end;
    queued wait hatched; idle >30 min collapses to a labelled 24 px band;
    gate chips laid out right to left.
  - Hypotheses: prior→posterior bar, linked delegations, filters
    all/open/closed/retracted; retraction (closed→OPEN) drawn as a back-step,
    downgrade to INCONCLUSIVE as a dashed "revision" (`read_hypotheses`).
  - Data: best-so-far of the declared objective across stores (x = eval # or
    hours, log-y only past two decades); reference lines only from
    `objective.lines`, scaling only from `objective.unit_label`; funnel drawn
    only when the study declares `funnel:`, else a "0/1 columns" table;
    virtualised sortable store table, column picker in localStorage.
  - Deliverable: notebook as prose, code folded, tables rebuilt from markup
    only; headline strip = the notebook's own `REPRODUCED:`/`CLAIMED_HEADLINE:`
    (`notebook_exec.parse_headline`), never the store's best row; Stored /
    Last re-execution switch; download links.
  - Logs: tail with Pause (byte cursor kept); sources run log, Monitor
    (diagnostics), watchdog, per-delegation stdout, Tool calls.
  - Docs sheet (`adda.explain.explain`).
- **Acting (write token; every action a `viewer_actions.jsonl` row in the
  study dir):**
  - Answer a pending question; note to the entry node or one running
    delegation (see **Operator channel**).
  - Stop popover: `Stop gracefully` (writes `debug/stop_request.json`, see
    **Graceful stop**); `Kill now` behind a second confirm, offered only while
    a viewer-started process is alive.
  - Start sheet: preflight checklist, then the local runner
    (`python -m adda.watchdog <study>`) or the study's `runtime.launch`
    command; one live run per study (`LIVE_WINDOW_S` = 600 s); PID + start
    time in `runs/_viewer/registry.json`; Kill = SIGTERM, SIGKILL survivors
    after 30 s (`run_control.py`).
  - Re-execute the deliverable against a throwaway copy of
    `experiment_data/` in a child process, timeout max(budget/10, 180 s);
    output saved as `debug/viewer_reexec/pipeline_<stamp>.ipynb` +
    `reexec_<stamp>.json`; the run's own notebook and ledger are never written
    (`notebook_replay.py`).
  - Setup: edit PROBLEM_STATEMENT.md / config.yaml with diff, validation and a
    required message; commits just that file as the operator, refuses stale
    `base`/other dirty study files/bad config, restores on failure, never
    pushes (`study_edit.py`). New / duplicate study commits exactly the two
    files (`--only`).
- **Data endpoints** (`/api/runs/{id}/…` unless noted; cursors `after`/`limit`,
  `next_cursor` always an int on live files, non-int → 400):
  `graph`, `delegations`, `ledger`, `stream` (SSE), `vitals` (+`study`,
  `model`, `budget_s` from config), `oracle` (per-store paging, newest first,
  `namespace=`), `trajectory` (+`inputs`, `text`), `figure_of_merit`,
  `funnel[?stages=]` (`read_funnel`; order = declared `funnel:` else store
  order), `monitor`, `diagnostics[?kind=]`, `log?name=&after=` +
  `log_sources`, `tool_calls`, `transcript/<key>` (raw records),
  `transcript/<key>/events` (backend-neutral events, `transcript_events.py`),
  `node/{name}/transcripts`, `notebook[?reexec=]`, `artifacts`/`artifact`,
  `problem_statement`, `retrospectives`, `critic_reviews`
  (`delegation_id_source` recorded|ordinal|null), `notes`, `evidence[/<D>]`,
  `literature`, `download?what=notebook|store|debug`, `operator`; study-level
  `/api/study/{preflight,history,file/{name}[/diff|/commit],commit}`,
  `/api/studies`, `/api/docs`, `/api/session`.
  Writes: `answer`, `note`, `stop`, `notebook/reexecute[/stream]` (NDJSON),
  `/api/study/{start,kill,launch,launch/stop}`, `/api/studies`, study-file
  `diff`/`commit`.
- **Security:** binds loopback; any other `--host` needs `--allow-network`
  (prints a warning). Reads open; writes need the per-launch token set as an
  HttpOnly SameSite=Strict cookie by the printed `/session?token=…` URL, plus
  same-origin `Origin`, no cross-site `Sec-Fetch-Site`, and
  `Content-Type: application/json`. `/api/session` → `can_write`; read-only
  sessions see a message, not a failure. All git via `safe_git.py` (fixed
  argv, no shell, `--git-dir`, sha `[0-9a-f]{7,40}`, timeout, read-only).
- **Known limit:** on-disk delegation status lacks the registry's
  `Working`/`FollowUp`/`Cancelled`; a delegation waiting on a human looks
  like one still running.
- **Known inconsistency (code):** `viewer/__main__.py`'s docstring and argparse
  description still say "read-only".
- **Where:** `src/adda/_src/viewer/` (`app.py`, `readers.py`,
  `transcript_events.py`, `run_control.py`, `notebook_replay.py`,
  `study_edit.py`, `safe_git.py`, `downloads.py`, `templates/graph.html`,
  `static/ui/`); `runtime/agent_runtime.py::serve_viewer`; `viewer` extra.
  Tests `tests/test_viewer_*.py` (`test_viewer_ui_tokens.py`: AA contrast,
  token-only colours); screenshots `internal/tools/viewer_shots.py`
  (`--banner --data --hypotheses --deliverable --logs [--live] --setup
  --timeline`). **Status:** done.

---

### Notice provenance — telling adda's voice from a tool's output
- **What:** every piece of text adda injects into an agent's context —
  nudges, science-monitor drift, budget warnings, operator notes, peer
  messages, delegation notifications — is wrapped in an `<adda-note>` marker
  at the point of injection. The viewer lifts marked blocks out of the tool
  result and renders them in their own band (`--surface0`, peach left rule)
  above the tool's own output (`--crust`).
- **Where:** `nodes/notices.py` (`wrap_notice` / `split_notices` and the
  marker); injection sites in `nodes/orchestration.py` and
  `nodes/tools/routing/delegation.py` (grep `wrap_notice(`);
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
  record rather than terminal scrollback): **answers** to a question the entry
  node asks (`SendMessage(to="human")`, or `FollowUp` when `peer_interaction` is
  off); **notes** queued for the entry node's next tool call; and a
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
- **Where:** `src/adda/_src/infra/operator_channel.py` (`ask_question`,
  `answer_question`, `queue_note`, `drain_note_rows`, `touch_watch`,
  `is_watched`); routing in `nodes/orchestration.py`'s `_drain_notifications`;
  HTTP surface in `viewer/app.py` (`/answer`, `/note`); composers in
  `viewer/templates/graph.html` and `viewer/static/ui/ui.js`. **Status:** done.

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
appends the action to `studies/<study>/viewer_actions.jsonl`. **Status:** done
(node, watchdog, endpoint, `/ui` Stop popover with Kill now for viewer-started
runs).

---

- **Thinking display (`runtime.thinking_display`):** `summarized` (default) or
  `omitted`, passed as `thinking={"type": "adaptive", "display": ...}` to
  `ClaudeAgentOptions` on models that support adaptive thinking (Opus/Sonnet
  4.6+, Fable/Mythos 5+); Haiku and others untouched. Newer models default to
  `omitted`, which returns every ThinkingBlock with empty text and only a
  signature, so transcripts and the viewer read as bare tool calls (run
  artifact: Sonnet 5.5 smoke 20260928T233115, 0 of 78 assistant records carried
  thinking, against 126 of 369 on Haiku). Billing is the same either way — the full thinking
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
  `viewer/transcript_events.py` (`_compaction_facts`), `viewer/app.py`
  (`_compaction_html`). **Status:** done.

### Prompt prose names tools the way the backend exposes them
- **What:** on the Claude backend the SDK exposes closure tools only as
  `mcp__f3dasm_agent_tools__<Tool>`, so `ClaudeAdapter._render_system_prompt`
  rewrites every unambiguous tool reference in the assembled prompt (prose and
  tool docstrings) to that name: `Name(`, `` `Name` `` and multi-word names
  such as `ReportEvals`. A lone capitalised word without call syntax
  (`Wait for...`) is left alone. Other backends are unchanged. Without it the
  model called the bare name: "No such tool" (`ReportEvals`, `SendMessage`) or
  the CLI's own disabled native `Write`.
- **Where:** `backends/claude.py` (`_CLOSURE_MCP_SERVER = "f3dasm_agent_tools"`,
  `_qualify_closure_names`, `_render_system_prompt`). **Status:** done.

### Store integrity guard (RunScratch / RunNotebook)
- **What:** both tools run against a sandbox copy, and a guard fingerprints the
  real store around the call. Only a change that destroys existing content
  (a rewritten row, a deletion) is reverted, and only when no delegation was
  running and the file is still exactly as the call left it. A change that
  keeps every old row/key and only adds rows or columns (a concurrent
  campaign's flush, including one that declares a new provenance column and so
  rewrites the header and domain.json) is an append: reported, never reverted.
- **Where:** `nodes/tools/routing/notebook.py` (`_canonical_integrity_guard`,
  `_integrity_error_message`). **Status:** done.

## Tools (agent-declared names are test-enforced to appear in this file; injected closures are not)

| Tool | Feature |
|---|---|
| `Delegate` | Delegation (dynamically injected) |
| `Wait` · `FollowUp` (entry node, only with `peer_interaction` off) · `ReportEvals` | Delegation + messaging + telemetry (`Wait(id, block=False)` is the status poll) |
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
| `SendMessage` | Peer/human messaging (spec 12; default on). `to="human"` entry node only |
| `RecallHistory` | Read this node's prior delegations (any node with a delegation log) |
| `AskForFeedback` | Mid-run critic audit (entry node, critic connected) |
| `CancelDelegation` | Opt-in only; no production agent declares it |
| `ConsultHandbook` | Curated project handbook (every node) |
| `ConsultF3dasm` · `ConsultAdda` | Installed-API lookup (f3dasm: strategizer, implementer, datagenerator; adda: registered, unattached) |
| `CorpusAdd` · `ConsultLiterature` · `SearchPapers` · `PaperDetails` · `CitationGraph` | Literature reviewer (`ConsultLiterature` read-only on every node) |
| `ConsultAbaqus` · `ConsultBasilisk` | Offline solver docs (abaqus_/basilisk_datagenerator) |

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
- **Why:** run 20260928T225501 had 32 retries across 14 delegations, none
  visible (93a8fc7). The row also carries `error_type: "LLM_RETRY"`.
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

### Runtime knobs to drop adda's own prompting: `budget_notes`, `delegation_contract`, `reprompt_unfinished`
- **What:** three ablation knobs, all default on, so a custom graph can run
  without adda's machinery. `budget_notes` removes the in-band budget text
  (per-turn constraint snapshot, warnings and wrap-up ladder, the snapshot on
  a delegation report and a worker task message). `delegation_contract`
  removes the `<delegation_contract>` block from the worker preamble.
  `reprompt_unfinished` removes the bounded re-prompt after an unaccepted
  close and the UNGATED banner. Budgets stay soft; the critic still gets its
  constraints; the oracle sentence in the preamble stays (run substrate).
- **Where:** `runtime/features.py` (registry, so each shows in `arms` and the
  `arm_*` ledger columns); `nodes/orchestration.py::_budget_warnings`,
  `_constraint_refresh`, `_reprompt_unfinished`, `_banner`;
  `routing/delegation.py::_append_budget_report`, `_budget_broadcast`, the
  worker task message; `prompts/agent_prompts.py` inline gate.
  **Status:** done.

### Epistemic ownership follows the tools a node holds
- **What:** a node with outgoing edges owns the hypothesis ledger only if it
  holds a hypothesis tool, the milestone ledger only if it holds a milestone
  tool, and the science monitor if it holds either. The notebook is required
  at close (`_missing_deliverables`) only of a node that holds the notebook
  tools. `pipeline_deliverable` and the reproduction gate stay one switch.
  Telemetry still follows topology. Default graph: strategizer owns all;
  datagenerator and implementer keep ledger and monitor (they hold
  `HypothesisList`) and lose a milestone ledger nothing on them used; the
  other two own nothing, as before.
- **Where:** `nodes/orchestration.py::_install_epistemics`,
  `nodes/reproduction_gate.py::_missing_deliverables`. **Status:** done.

### The environment sets no run knob; a stale export is an error
- **What:** `settings` resolves explicit argument, then `config.yaml`, then
  default. There is no `F3DASM_<KEY>` tier. `settings.reject_stale_env()` runs
  at `AgenticRun` construction and raises if the environment holds
  `F3DASM_<KNOWN_KEY>`, naming each variable. The variables adda itself hands
  to subprocesses (`F3DASM_NAMESPACE`, `F3DASM_DELEGATION_ID`,
  `F3DASM_RUN_CONFIG`, `F3DASM_CANONICAL_STORE`, `F3DASM_DEDUP_SCOPE`) are not
  knobs and are exempt. `F3DASM_MEM_CAP` is refused the same way: the hard
  memory cap is `mem_cap` in config.yaml, then the SLURM allocation, then the
  4 GiB default.
- **Where:** `runtime/settings.py::reject_stale_env`,
  `runtime/agent_runtime.py::AgenticRun.__init__`. **Status:** done.


### The endpoint and the top-level keys are config (2026-10-06)

`config.yaml` takes `base_url` at top level and `nodes.<node>.base_url`.
`AgenticRun._resolve_base_url` picks node, then top level, then the run's
SLURM-served endpoint, else the adapter's default. The environment never sets an endpoint: an exported
`VLLM_BASE_URL`, `OLLAMA_BASE_URL` or `OPENROUTER_BASE_URL` is a startup error
(`settings.reject_stale_env`), like the `F3DASM_<KNOB>` variables; API keys stay
in the environment. The container runner no longer forwards `OLLAMA_BASE_URL`.
A node URL on
a backend with no endpoint is an error; `base_url` with `llm_slurm.enabled` is
an error. The SLURM endpoint no longer goes through `os.environ`, which removes a
race between concurrent runs in one process. `runtime/study_config.py` lists the
top-level keys; an unknown one is a startup error (and a viewer error) with a
"did you mean" hint. `study:` is reserved for the study's own scripts; adda never
reads inside it. **Status:** core.
