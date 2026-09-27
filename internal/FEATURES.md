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
  agents can ask one clarifying question, send async messages, and report progress.
- **Where:** `nodes/tools/routing/`, `nodes/orchestration.py`.
- **Tools:** `Delegate`*, `Wait`, `FollowUp`, `Confer`, `ReportEvals`.
  (`Wait(id, block=False)` is the status poll that used to be `GetStatus`.)
  (*Delegate is injected dynamically, not in a static `tools` set.)
- **Fan-out harvesting:** `Wait()` takes an OPTIONAL delegation id. Bare
  `Wait()` blocks until whichever delegation finishes first and returns that
  one's report (labelled with its ID), marking it read so N in flight are
  drained by N calls; it refuses when nothing is in flight, and refuses rather
  than hanging when every open delegation is parked on a `FollowUp` or has
  already died without reporting (a blocking call ends no turn, so the run's
  time backstop cannot fire while inside it). `Cancelled` is never harvested
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
  reason to exist is this gate's store-row precondition. A study can now
  require a notebook without requiring it to reproduce, or vice versa.
  Recorded per run in `run_config.json`'s `runtime` block like every other
  knob, so a sweep's arms can be told apart after the fact.
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

### `SendMessage` — the peer/human messaging tool (spec 12, in progress)
- **What:** one tool for all peer and human messaging —
  `SendMessage(to, message, wait_for_reply=False, approve=False)` —
  eventually replacing `Confer`/`FollowUp`/`Reply` (`internal/specs/
  12-peer-interaction.md`, not yet built in full). This first commit adds
  the tool itself: a shared, node-level closure (used by every thread
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
- **Ablation, temporarily inverted:** gated behind `peer_interaction`
  (`runtime/features.py`), the one Feature in this codebase that
  currently defaults **False** rather than True — a deliberate, TEMPORARY
  exception while spec 12 is built across several commits without
  disturbing `Confer`/`FollowUp`/`Reply`, which stay the live surface
  until the final migration-sweep commit flips this default and retires
  them. `Wait` was also re-tightened to outgoing-edges-only in this same
  series (a separate commit, `1d7e14c`) — see BACKLOG's spec 12 entry.
- **Where:** `nodes/tools/routing/delegation.py` (`DelegationTools.
  SendMessage`, `_resolve_send_target`, the `parent`/`to_worker`/
  `to_delegator`/`worker_cond` fields `_register_dispatch` now stamps on
  every registry entry), `nodes/orchestration.py`
  (`_get_delegator_cond`, the per-delegator-identity `Condition` registry
  this all synchronizes through), `nodes/tools/routing/__init__.py`
  (`build_routing_tools`'s feature gate), `runtime/features.py`
  (`peer_interaction`). **Status:** in progress — see `internal/specs/
  12-peer-interaction.md` for what remains (session-resumption report
  review, the pending-for-you notice mechanism, `to="human"`'s
  entry-node-only gating already lands here but the migration sweep that
  retires the old tools does not).

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
  being a separate tool), `CitationGraph` (citing / references / similar,
  OpenAlex first with Semantic Scholar as fallback) and `PaperDetails`.
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

### Per-delegation resource telemetry
- **What:** `Wait(id, block=False)` shows a delegation's eval count, current RSS, and **peak
  RSS** (the high-water across the watcher's ticks), so the strategizer can see a
  fat or fattening campaign (and `Confer` the implementer).
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
  the strategizer auto-receives (Wait/Confer/Done) — per-eval wall-time
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
  HTML template. Binds to `127.0.0.1`, no auth. No longer read-only: see
  **Operator channel** below for the write path (answering a `FollowUp`,
  queueing a note, nudging a running delegation).
- **Where:** `src/adda/_src/viewer/` (`readers.py` pure data functions,
  `app.py` the Starlette app, `templates/graph.html` the UI);
  `agent_runtime.py`'s `AgenticRun.serve_viewer`; `pyproject.toml`'s `viewer`
  optional-dependency group. **Status:** done (v1, read-only).

---

### Notice provenance — telling adda's voice from a tool's output
- **What:** every piece of text adda injects into an agent's context —
  nudges, science-monitor drift, budget warnings, operator notes, Confer
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
  and prefixed onto its next tool result — the same path `Confer` and the
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

## Tools (every one must be documented above; the test enforces it)

| Tool | Feature |
|---|---|
| `Delegate` | Delegation (dynamically injected) |
| `Wait` · `FollowUp` · `Confer` · `ReportEvals` | Delegation + messaging + telemetry (`Wait(id, block=False)` is the status poll) |
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
