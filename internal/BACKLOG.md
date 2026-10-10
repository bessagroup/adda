# Agentic system — backlog

**Open work only.** Resolved items keep their full write-up in
[`BACKLOG_RESOLVED.md`](BACKLOG_RESOLVED.md) — the record survives, it just stops
crowding out what still needs doing.

Item numbers are stable references, never queue positions. Detailed,
evidence-grounded specs live in [`specs/`](specs/README.md); the entries here are
the short version.

## How to read this

Three groups, because they need three different things from you:

| Group | What it means |
|---|---|
| **Awaiting your decision (§4)** | Already escalated. The work is understood; the *call* is yours. Not a new discovery — do not re-surface these as findings. |
| **Actionable** | Someone can pick this up and do it without asking anything first. |
| **Parked** | Deliberately deferred. No action expected; here so it is not rediscovered. |

---

## Awaiting your decision — CLAUDE.md §4

These are blocked on a judgment call, not on effort. Each has been surfaced
before. Until one is answered or closed, treat it as known, not news.

- [ ] **#10** Strategizer delegates the optimization as one monolithic un-budgeted campaign — *open, §4 user-owned* (**current binding constraint** — watchdog-kills runs)
- [ ] **#17** Closure + budget-severity model (Memory>Time>Eval; dynamic constraints) — *open, §4 user-owned* — Done() prompt iterated (`0400a653`); runtime nudge + severity model deferred
- [ ] **#19** Scientific adequacy is enforced as vibes while reproduction is enforced hard — *§4, partially addressed (`8ae9a402`, `93e6f0f2`); the load-bearing question is OPEN and empirical* — see §19 below
- [ ] **#18** Critic should flag an infeasible-extremum headline on a constrained study — *open, §4 user-owned* — grounding moved to the critic (`6494489b`) but it only checks the value is real, not feasible
- [ ] **#42** Same study, two Haiku runs, opposite gate outcomes (RunScratch vs. a token Delegate()-registered oracle) — *open, §4 user-owned* — see §42 below


## Actionable

- [ ] **#1** Reconcile cancelled-but-completed delegations — *open, highest priority* (recurring UNGATED root cause; partially mitigated 2026-06-15)
- [ ] **#6** Detect a delegation running but making zero ledger progress — *open*
- [ ] **#22** Stall watchdog: liveness = "file written", not "progress made" — *open, medium (a backstop, not a primary control)*. `seconds_since_last_activity()` (`infra/watchdog_cleanup.py`) is implemented exactly as this item describes and its docstring defends the choice; whether a busy-but-unproductive run should be caught is still unanswered — see §22 below. Note: `python -m adda.watchdog` (#41, resolved — see `internal/BACKLOG_RESOLVED.md`) is a flat 2×-budget hard deadline and deliberately does NOT consult this liveness signal, so this item is still genuinely open and independent of #41's fix.
- [ ] **#16** ABAQUS subprocess can't import workspace modules (PYTHONPATH) — *open, abaqus2py-owned* (recommendation only; not an f3dasm fix)
- [ ] **#43** promptmap.html citations are file:line, so any unrelated src edit that shifts lines makes it stale — *open, design smell in a working safety net* — see §43 below
- [ ] **#44** `pip install` inside an "activated" uv venv can silently target the wrong Python — *open, local-dev-only, not a fix, a documented gotcha* — see §44 below
- [ ] **#51** Rows from a superseded oracle revision still count in `evals_used` and in best-row ranking — *open, separate step after real mixed-revision stores exist* — see §51 below
- [ ] **#52** An in-band harness notice that guards data integrity travels inside a tool output that can be truncated — *open, investigate first, report before fixing* — see §52 below
- [ ] **#54** Per-call input-token size on small studies — *open, investigate first, no work yet* — see §54 below
- [ ] **#55** Derive the non-Default built-in floor from the CLI init record — *future-proofing, no work yet* — see §55 below
- [ ] **#59** `run_arm.sh` cannot run one repeat of an arm — *open, benchmarks repo, after the 2026-10-08 campaign* — see §59 below
- [ ] **#47** Computed cost sits 0-8% below the SDK's `total_cost_usd` on validator/critic calls — *open, low (telemetry column, not correctness)* — see §47 below

## Parked — deferred on purpose

- [ ] **#2** Richer delegator↔worker comms (typed blocker/escalation) — *deferred; superseded by spec [12](specs/12-peer-interaction.md) if built (Delegate/SendMessage/Wait replaces Confer/FollowUp/Reply rather than extending them — do not build both)*
- [ ] **#3** ProblemDefinerAgent pre-strategizer intake stage — *deferred*
- [ ] **#20** Open design-space discovery (agent invents new low-D parametrizations) — *spec approved, §4 user-owned, awaiting 2D experiment* — see [`OPEN_DESIGN_SPACE_FRAMEWORK.md`](OPEN_DESIGN_SPACE_FRAMEWORK.md); branch `exp/open-design-space`
- [ ] **#23** Rename `literature_reviewer` → `consultant` + give it live-web tools so it answers tech-stack/API/doc questions, not only academic literature — *spec, not built (user decision 2026-06-30)* — see §23 below
- [ ] **#45** `SummarizeDelegations` — a git-timeline-derived delegation summary tool — *idea, not an approved design* — see §45 below
- [ ] **#50** Two delegations edit the registered oracle file concurrently — *decided 2026-10-06: NO rule for now* — rely on the oracle-revision visibility (`1b356e4`); reopen only if a new run shows harm — see §50 below
- [ ] **#53** User-defined Features (`features.register()` / config-declared features) — *decided 2026-10-06: not built (YAGNI until a real user needs it)* — see §53 below
- [ ] **#46** Cross-run hypothesis ledger, study-scoped by default — *idea, not an approved design* — see §46 below

---

## 10. Strategizer delegates the optimization as ONE monolithic, un-budgeted campaign
**Status:** PARTIALLY MITIGATED 2026-06-22 (commit pending) — added a "scope each
delegation to one hypothesis" principle to the strategizer planning prompt
(`<scientific_process>`), grounded in Charter §3 (bundling confounds the test) with
a fail-fast/self-correct rationale. This NUDGES toward targeted campaigns but is not
enforced — the strategizer already ignored a sharper signal (H1's explicit 300-eval
criterion), so it likely needs the #9-family check to truly bind, and the wall-clock/
algorithm half (uncapped GP × 700 iters, non-resumable re-run) is untouched. Stays open.
**Status (orig):** raised 2026-06-22 (run 20260622T165943 watchdog post-mortem). **§4 — budget/decomposition, user-owned.** The recurring binding constraint once upstream phases are clean.

The strategizer delegates the WHOLE optimization to one implementer call. Verbatim
intent (D004): "Execute a comprehensive 1000-eval black-box minimization campaign …
PHASE 1 LHS 250 … PHASE 2 BO loop ~700 evals … PHASE 3 multi-start 50". Its thinking:
"Now I'll delegate to the implementer to execute the full campaign … fairly
open-ended." No wall-clock budgeting, no chunking (it does NOT delegate Phase 1,
check the budget, then Phase 2). The implementer runs it as a monolithic background
script (optimize_1000eval.py) whose GP-surrogate BO loop is O(n³) per refit over 700
iterations — inherently slow. Result: ~41 min, 673/1000 evals, never finished, watchdog.

**KPI contrast (the tell):** run 20260622T043904 GATED with ~996 evals in **22 min**;
run 20260622T050137 GATED with 2570 evals in 49 min. So a ~1000-eval campaign CAN fit
and gate — the failure is per-eval SLOWNESS (heavy GP-BO refits) + a no-chunk, no-time-
budget delegation, not the eval count itself.

**Classification:** JUDGMENT CALL, not a bug — a reasonable agent following the spec
(pipeline = LHS→GP→BO→optimization; implementer is "the only agent that evaluates")
would delegate "the campaign". But the system gives the strategizer no wall-clock
signal and no incentive to chunk. The implementer's own retrospective even claims an
"~18-min allocation" while the campaign needed 41 — a budget-estimate mismatch.

**Resume-cold options (user's call):** (a) strategizer chunks the campaign (delegate a
bounded slice, check budget/ledger, delegate the next) so it can gate mid-way; (b) give
the implementer a wall-clock-aware budget that caps the campaign and returns partial
results; (c) cheaper default surrogate/acquisition (the slowness is GP-refit cost); (d)
raise the 3600s watchdog. Do NOT pick without the user — budget is soft by charter.

## 17. Closure decoupled from success-criteria + budget; budget-severity model — §4

**Status: open, §4 user-owned. Partially addressed.** Run `20260624T021359` closed
at ~22% of a 12h budget with its PRIMARY success criterion (Stage-2 Riks
`max_strain ≥ 0.90`) left INCONCLUSIVE and the affordable settling experiment (a
refined Riks re-run) never delegated. Root: a three-way gap — the strategizer prompt
made Done() eligible on "best design + one falsification attempt" (no criteria-met
notion), budget reached the agent only as a 95%/100% *brake* (never as runway,
`strategizer.py` budget warnings), Done() is budget-blind (`routing.py`), and the
critic is *forbidden* to weigh budget ("RESOURCE BOOKKEEPING IS NOT VALIDITY",
`agents/critic.py`) and is not scoped to closure timing.

**Done (`0400a653`):** the strategizer PREMATURE CONVERGENCE rule now requires
primary criteria MET (not merely tested), frames budget as runway, and asks for a
recorded reason when closing early; mirrored in the Done() docstring.

**Deferred (the open §4 design question):** a *runtime* closure nudge (soft — a
two-shot reconsider when closing with large budget unused and a criterion
unmet/INCONCLUSIVE, mirroring the 95% wind-down nudge), and a **budget-severity
model**. The user's framing: budgets differ in severity — roughly **Memory > Time >
Eval** — and *accidental* constraints (e.g. a Riks `max_strain` gate) are
**dynamically assigned**, so they should receive *differentiated* treatment rather
than one uniform nudge. How to represent constraint severity/type and key the nudge
on it is unresolved. The benchmark `PROBLEM_STATEMENT.md` framing (objective excludes
strain; Riks demoted to a separate "Validation requirement"; criterion #4 buried)
contributed and is owned by the user (handled in the benchmark repo, not f3dasm —
robustness must not depend on a perfectly-framed problem statement).

## 19. Scientific adequacy is enforced as vibes while reproduction is enforced hard — §4

**Status: partially addressed (`8ae9a402`, `93e6f0f2`); the load-bearing question
is OPEN and empirical.** Diagnosis from run `20260625T014520` (supercompressible
14D). Numbers (faithful): 257 evals, 47 coilable (~18% feasible), exactly **1**
above the Bessa baseline (66.04 vs 65.3, a 1.1% edge), **0** above the +15% floor
(75.1). Closed voluntarily at 3 h 36 m wall (not watchdog-killed). The deliverable
declared "66.04 … represents the effective performance ceiling in this design
space" (H6 cell) and H4 FALSIFIED.

**The failure (established):** a synthesis-level over-generalisation. The run
converted "my underpowered search did not clear the floor" into "the 14D space
cannot." Its OWN deliverable documents the search as weak — ml cell: "CV R² ≈ 0.51
on 27 coilable points"; doe cell cites Loeppky 2009 "≥10×d" (=140) then uses 50;
D005 retrospective: GP length-scale pinned at bound 1000, "poorly tuned." The BO's
feasible hit-rate (3/37 ≈ 8%) was *worse* than the LHS (18%). The whole campaign
was pre-committed to a narrow box (`ratio_Ixx ∈ [5e-7,1.4e-6]`, `phase2_plan.md`)
derived from a 1-D scaling formula **before** the first LHS returned — so 14D was
never broadly explored; a physics-intuition ray was.

**Mechanism (established by reading the code):**
- The Charter's §2 severity rule IS present and IS applied — but only
  **per-hypothesis**. H4's registered prediction was scoped to "this search finds
  ≥75.1," so FALSIFIED is charter-legal. The over-reach lives in the **synthesised
  prose**, which no registered hypothesis covers.
- The live `verdict_validator.py` shares ONLY the Charter (not the critic's
  checklist) and judges ONE hypothesis verdict at a time — it is **structurally
  blind to the synthesis**.
- Both critic calls (`call_001` REVISE, `call_002` PASS) spent 100% on
  reproduction mechanics (composable BO, lazy guard, IN_PROGRESS jobs); neither
  engaged criteria 2/3/4 against the headline. The critic prompt’s criterion 6 was
  marked "binding" and its prose equated "scientific integrity" with "provenance +
  replicability", steering attention there. The gate proved the notebook
  *reproduces*, never that the science was *sound* (CLAUDE.md §4.5).
- Budget cost-prior: planned ~72 sims for 14D on an unverified ~600 s/sim
  assumption; measured median was 16.4 s (36.5x off), never recalibrated.

**Done this session (general principles, not patches — each passes the parsimony
test):**
- `8ae9a402` — Charter §2 extended so an achievement/absence claim ("some/no
  design reaches X") is adequately tested only if the search had the POWER to find
  the instance (coverage + a surrogate above chance); a stalled search →
  INCONCLUSIVE. Lives in the Charter, so the gate critic AND the live validator
  inherit it (the validator can now flag a premature FALSIFIED **on the fly at
  HypothesisUpdate**). Critic criterion 4 made CRITICAL-eligible for whole-space
  headlines; criterion-6 "integrity = reproduction" sentence corrected to
  "necessary but not sufficient." Strategizer PREMATURE CONVERGENCE: a stalled
  optimizer/plateau is NOT a valid "budget can't settle it" reason.
- `93e6f0f2` — per-delegation wall-time KPIs auto-appended to the report (attacks
  the cost-prior; interpretation-free to avoid overfitting).

**OPEN — the empirical question these edits do NOT answer.** Whether the failure
is *scaffold* (the harness graded reproduction, so the model optimized it) or
*model disposition* (commercial LLMs fine-tuned to terminate with a confident
answer, antagonistic to staying INCONCLUSIVE) is **unresolved and untested**. The
edits are an intervention, not a proven fix. Decisive test (one run): re-run this
study with `8ae9a402`+`93e6f0f2` live — if the agent stays INCONCLUSIVE / keeps
exploring, or the validator/critic flags the ceiling claim → scaffold; if it still
manufactures a confident ceiling → disposition. Do NOT record these edits as
"fixed" until that run exists. Note in favour of scaffold (not proof): the same
agent marked H5 INCONCLUSIVE correctly, so the capability is present.

**OPEN — structural gap (§4, user-owned).** The synthesis-level over-claim is
reachable only by the critic (criterion 4); the validator cannot see it because it
judges one hypothesis verdict at a time. Widening the validator's scope to the
synthesised headline — so it can catch this on the fly rather than at Done() — is
an architectural change the user owns. Related: #18 (critic blind to
infeasible-extremum headlines) and #17 (budget-severity model) are the same class
— the critic/validator scrutinise mechanics and atomic verdicts, not the headline
as a communicated scientific claim.

---

## 18. Critic should flag an infeasible-extremum headline on a constrained study — §4

**Status: open, §4 user-owned.** Commit `6494489b` removed the runtime
`REPRODUCED` extremum machine-check (it forced constrained studies to headline
their infeasible unconstrained extremum) and shifted headline grounding onto the
critic. But the critic's HEADLINE PROVENANCE mandate (`agents/critic.py:31-41`)
only verifies the headline value is **real** (traces to a ledger row) — it does
NOT verify the headline is **feasible**. So nothing currently flags the specific
failure of run 20260624T021359: a headline equal to the unconstrained extremum
(λ_cr_nd = 0.90709, a NON-coilable design) on a study whose objective is
`maximize λ_cr_nd subject to coilable = True`.

**Proposed (one clause, §4):** add to the critic's headline check that, when the
study declares a feasibility constraint (a constraint output column), the headline
must be the best **feasible** design; a headline equal to an infeasible extremum
is a MAJOR finding. The constraint identity could come from
run_config (an explicit constraint column) or be inferred from the deliverable's
stated objective. Deferred pending the user's call on how to represent the
constraint to the critic (it is the epistemic-contract owner's decision).

**Why not the runtime instead:** the runtime can't judge which ledger column is
the constraint or what counts as feasible generically without more config; the
critic already reads the deliverable's stated objective, so it is the better
place to judge feasibility-of-headline. See `6494489b` and `agents/critic.py`.

## 42. Same study, two Haiku runs, opposite gate outcomes — RunScratch vs. a token Delegate()-registered oracle

**Status: open, §4 user-owned.** Raised by run 20260927T034538
(`studies/lcp_matlab_regression`, SHA `26039fa`, the final wet Haiku
self-consistency re-run before the planned Sonnet/Oscar run) — a genuine
self-consistency finding, not a structural gate defect (corrected from an
earlier draft of this entry, which wrongly concluded the gate could never
pass for this study; it demonstrably can and has, twice).

**Flag for anyone reading `studies/run_ledger.csv` later: run
`f48da54`/`20260926T214835`'s `GATED` status is NOT a clean pass.** It
passed only because its strategizer delegated D004, a token oracle whose
sole job was populating one canonical-store row to satisfy the gate's
row-count check — not because it ran a real evaluation campaign the study
needed. Do not cite it as evidence the gate works cleanly for this study
class; see the perverse-incentive discussion below.

`RunNotebook(gate=True)`/`Done()`'s pre-critic reproduction gate refuses
outright with "canonical store has no evaluations yet. Run at least one
evaluation campaign before checking the gate" (`nodes/reproduction_gate.py`)
whenever the canonical store holds zero oracle rows. `lcp_matlab_regression`
is a direct MATLAB-vs-MATLAB comparison, so nothing about the SCIENCE
requires a canonical-store row — but **two earlier runs of this exact same
study, same graph, same config (`6689734`/20260926T124841 and
`f48da54`/20260926T214835) both reached GATED**, because their strategizer
delegated a step explicitly to satisfy this precondition: D004 in the
`20260926T214835` delegation log reads "Create and register a minimal
oracle for store population... wraps the KM baseline MATLAB model...
Register via `register_evaluator_entrypoint()`" — a token, one-purpose
oracle whose only job is populating one canonical-store row so the gate
will look. This run's strategizer (`26039fa`) instead did everything
itself via `RunScratch` — never called `Delegate()` a single time in the
whole run — got numerically identical, equally conclusive results (5 test
cases, max error 0.0 against pre-registered tolerances) but with no
canonical-store row to show for it, bounced off the gate 6 times exactly as
told, and closed UNGATED/FAILED. Same study, same difficulty, opposite
formal outcome, purely as a function of which of two equally-reasonable
strategies the strategizer happened to pick this run. `run_status.json`:
`"status": "FAILED", "reviewed": false`.

The agent self-diagnosed accurately and honestly either way — it did not
hallucinate a PASS, and its retrospective named the exact mismatch (ledger/
gate tooling assumes every study is a `Delegate()`-based evaluator loop;
this one measures success by re-running MATLAB directly). That accuracy is
the good news. The bad news is the inconsistency itself: whether a
zero-eval validation study gates or not currently depends on the
strategizer happening to think of the token-oracle workaround, not on
anything about the science.

**Proposed (not built, needs the user's call):** (a) prompt-level — tell
the strategizer explicitly, for zero-eval studies, to register a minimal
oracle purely for gate compliance (codifies the workaround `20260926T214835`
found on its own, but is exactly the kind of overfit-to-one-run rule §2
warns against unless stated as a general principle); (b) gate-level — let
the gate accept zero oracle rows as a valid terminal state when a study
declares itself evaluator-free (e.g. in `config.yaml`), checking only that
the notebook executes cleanly and its claims are internally consistent;
(c) leave it as-is and accept that a token oracle is simply how any
non-optimization study is expected to close, documenting the pattern
rather than changing anything. This touches the epistemic reproduction
contract (what "reproduced" means), not formatting, so it is not this
agent's call to make unilaterally.

**A related honesty gap, same root cause:** the run's own final closing
report (`run.py`'s printed `AgenticRun.execute()` return, the text a human
running this study actually reads) states flatly "**Hypothesis Status**
(all SUPPORTED via test_comparison.m): H1 ... SUPPORTED, H2 ... SUPPORTED"
for all five. The formal ledger (`debug/strategizer_notes/hypotheses.json`)
never recorded any of them past `OPEN` — every `status_log` entry's own
comment says so explicitly ("Would close SUPPORTED if a delegation ID were
available; evidence supports hypothesis at 99% confidence" — posterior
raised to 0.99, status left `OPEN`). The prose the strategizer chose for
its human-facing summary overstates what its own formal record shows,
even though the underlying evidence is genuinely strong (max error 0.0). A
reader trusting only the printed report would not know these hypotheses
never formally closed.

**Instrumentation checked clean, narrower than intended:** the plumbing
this run was meant to validate (SystemMessage transcript recording, the
entry-node run-context binding from `822dfdc`) behaved correctly wherever
it was exercised — 126 `"system"`-typed transcript records confirmed
present (`init`x2, `status`x124), zero spurious
`STREAM_ENDED_WITHOUT_RESULT`, zero `REPORT_RETRY` fired. But because this
run never delegated at all, the critic gate's and verdict-validator's own
run-context binding (also added in `822dfdc`) were never exercised —
confirmed clean only where the run's own shape happened to reach that
code path, not a full confirmation of all four binding sites.

## 1. Reconcile cancelled-but-completed delegations
**Status:** partially mitigated 2026-06-15 (cancel hardened + eval-count now
from ledger); the core inconsistency remains. **Highest priority** — it is the
recurring root cause of UNGATED / hypotheses-left-OPEN outcomes across runs.

`CancelDelegation` detaches a worker and tells the strategizer "result will be
ignored," but the **detached worker keeps running**, finishes, stamps real evals
into the canonical ledger, and writes its report to disk. The run then holds two
contradictory truths: the official record (delegation_log / registry / what the
strategizer is told) says *ignored / not executed*, while the ledger + on-disk
report say *done*. Primary evidence (run 20260615T192313): D005/D006 were
cancelled-detached, absent from delegation_log, yet have 145/32 ledgered evals +
`D006/REPORT_SUMMARY.txt` ("H1 SUPPORTED"). The strategizer then claimed "the
falsification was not executed" (per the runtime's "ignore it") while the critic
read the disk report — a **non-converging gate loop** rooted in inconsistent
state, NOT agent hallucination or prompt friction. Re-confirmed 2026-06-16: the
8d e2e left H1/H2 OPEN citing "D004 cancelled post-completion."

**Done so far:** (a) `evals_used` now counts from the ledger so those evals
aren't dropped from the run total (`agent_runtime.py`); (b) cancel is hardened
against impatience — two-shot for delegations already producing ledgered evals;
docstring + poll/premature nudges reframed (`routing.py`).

**Still to design:** when a detached worker completes with stamped ledger rows,
**reconcile** it — record its completion in the delegation_log (so the
strategizer sees it finished, not "ignored"), or stop the worker BEFORE it
stamps. Pick one source of truth so strategizer and critic never see
contradictory delegation state. Related: the stuck-delegation detection in #6.

---

## 6. Detect a delegation that is running but making zero ledger progress
**Status:** raised 2026-06-16. Cross-links #1 (reconcile) and #2 (steering levers).

A delegation can be **alive but unproductive**: the worker is still `Working`,
wall-time is climbing, but it is stamping **zero new rows** into the canonical
ledger. Today the strategizer cannot tell "slow but progressing" from "stuck /
spinning," so it polls, then either waits until the time budget dies or cancels
on impatience (feeding #1). Primary evidence (run 20260616T004655): the
implementer (D004) was alive ~228s+ yet produced **0 ledgered evals**; the run
ended UNGATED with an empty ledger.

**The signal:** status == Working AND wall-time since last ledger row > threshold
AND ledger delta == 0. That is detectable from the same `RunStateSummary` /
delegation timing the runtime already tracks. Surface it to the strategizer as a
distinct notice ("D004 has run 200s with no ledger progress") rather than letting
it guess from poll counts.

**Then it needs a lever, not just a notice** — which is exactly the typed
delegator decisions in #2 (`grant_budget` if it's genuinely close, `reroute` or
`abort` if it's stuck). Without #2 the only response is still the blunt
`CancelDelegation`. So #6 is the *detector*; #2 supplies the *actuators*; #1
ensures whatever the worker already stamped is reconciled rather than orphaned.

**Open question:** the threshold — fixed seconds, a fraction of the time budget,
or adaptive to the worker's own first-row latency? A cheap robust default: warn
once past max(120s, 15% of budget) with no row, escalate past 2× that.

---

## 22. Stall watchdog: liveness = "file written", not "progress made" (backstop defect)

**Severity:** medium (a backstop, not a primary control — the real cure for the
hang it failed to bound is the per-call validator timeout, shipped in `9d58b2a3`).

**What happened.** Run `20260627T211310` (watchdog_killed, 2h20m). A verdict-validator
LLM call hung at 22:03:29 (see `9d58b2a3` for the root cause). The study's stall
watchdog (`studies/agentic_namespace_ring/run.py` `_watchdog`, using
`watchdog_cleanup.seconds_since_last_activity`) is supposed to force-exit a hung run
after `STALL_SECONDS` (1200s here). It did fire — but only at **idle 5378s (~89 min)**,
~4.5× its own threshold. The watchdog post-mortem records `force-killed at 5378s`.

**Suspected cause (UNCONFIRMED — snapshot mtimes corrupted by the batch's non-`-p`
cp, so not provable from preserved artifacts):** `seconds_since_last_activity` =
most-recent mtime of ANY file under the run dir. Liveness so defined is satisfied by
a hung-but-still-twitching CLI subprocess (partial transcript flushes, checkpoint WAL,
telemetry) — i.e. *activity ≠ progress*. A run can write bytes while making zero
scientific progress, resetting the idle clock. (Alternative: daemon-thread starvation
under a GIL-holding loop — also unproven.)

**Proposed fix (deferred — the user flagged the watchdog as a SYMPTOM; do not
re-prioritise it over root causes):** define "stall" as *no PROGRESS* — no new ledgered
evaluations and no delegation state-transition for the window — rather than *no file
written*. Catches both a true hang and a grind-without-progress, and never kills a run
that is still producing evals (honours "never penalise parallel/slow-but-live work").
Needs a progress signal the watchdog can read cheaply (e.g. max over ledger row count +
delegation_log completed count). Validate headless before trusting it.

**Why not now:** with the validator call bounded (`9d58b2a3`), the specific hang that
exposed this can no longer run 89 min — it aborts in ~2 min. The watchdog defect only
matters for a *different*, not-yet-observed hang that the per-call timeouts don't cover.
Fix it when such a case appears, or as deliberate hardening — not as symptom-chasing.

---

## 16. ABAQUS subprocess can't import workspace modules (PYTHONPATH) — abaqus2py-owned

**Status: open — recommendation only (not an f3dasm fix).** During run
`20260624T021359`, all 51 ABAQUS runs of the first D003 attempt failed with
`ImportError: No module named 'supercompressible_lin_buckle_param'`: the workspace
dir was not on `PYTHONPATH` when the ABAQUS subprocess ran (it worked in validation
only because a validate script did `sys.path.insert(0, WORKSPACE)`). This also
contaminated the canonical store with 51 NEW-status rows that had to be cleared
before D004 (meta_errors.md Bug 1).

**Owner: the external `abaqus2py` package**, not f3dasm — `F3DASMAbaqusSimulator`
(which generates the `preprocess.py` wrapper and launches ABAQUS) is imported from
`abaqus2py` (`studies/fragile_becomes_supercompressible/main.py`), which is not in
this repo. **Recommended fix (in abaqus2py):** inject the workspace dir into the
ABAQUS subprocess environment (`PYTHONPATH`) or emit `sys.path.insert(0, WORKSPACE)`
into the generated `preprocess.py`, so worker-authored param modules resolve without
relying on the parent process's cwd/sys.path.

## 43. promptmap.html citations are file:line, not symbol -- any unrelated line shift makes it stale

**Status: open — design smell in a working safety net, not a bug.**
`internal/tools/promptmap.py` cites where every prompt section, tool
docstring, and gate lives as an exact `{"file": ..., "line": N, "line_end":
M}` span, and `check_promptmap` (CI) regenerates the map and diffs it
against the committed copy on every push that could have moved one. This
is correct and caught a real staleness on its own (CI run for 96b90f7,
2026-09-28): a QueryStore fix (`store.py`) inserted code above an existing
function, shifting every line number below it, which changed nothing
about what the prompt or tools actually say but still failed the diff.

The safety net did its job — nothing was silently wrong — but the cost is
real: ANY edit to a cited file that adds or removes a line, anywhere
above the cited span, however unrelated to prompts/tools, forces a
regenerate-and-recommit cycle and burns a CI cycle if missed locally.

**Recommended fix:** cite by symbol (module path + qualified function/class
name — the AST node's own identity) instead of by line number, resolving
to a line only at render time (already how `resolve_symbol`/`_class_line`
find things in the source — the citation payload just needs to stop
freezing the resolved line into the committed artifact). The map would
then only go stale when a cited prompt/tool/gate's own TEXT changes, or
when a symbol is renamed/moved/deleted — signal worth a CI failure —
never when an unrelated edit elsewhere in the same file shifts everything
below it.

**Interim mitigation (in effect until this lands):** run promptmap's own
freshness diff locally before any push that touches `src/`:
```
python internal/tools/promptmap.py -o /tmp/promptmap.html
diff -q internal/promptmap.html /tmp/promptmap.html
```
Regenerate and commit `internal/promptmap.html` in the same commit if it
differs, rather than relying on `pytest -m promptmap` alone (its own test
suite checks the map's internal consistency and provenance — it does not
re-run this specific stale-vs-committed diff check, which is a
shell-script step in `check_promptmap`'s CI job, not a pytest test).

## 44. `pip install` inside an "activated" uv venv can silently target the wrong Python

**Status: open — a documented local-dev gotcha, not something to fix in
code.** `pip` in an activated `.venv` runs whichever `pip` binary is FIRST on
`PATH` — that is not guaranteed to be the venv's own `pip`, and `uv`-managed
venvs frequently have none on `PATH` at all (uv's own workflow is `uv add`/
`uv pip install`, not bare `pip`). A shell where a system or Conda Python's
`pip` shadows the venv's own silently installs into that OTHER environment,
leaving the project's actual `.venv` unchanged and package-less, with no
error — `pip` reports success because it did succeed, just not where the
caller believed.

**Evidence.** Run `20260927T012131` (Oscar, accidental 24h run): a
retrospective blamed a `sklearn` import failure on "NumPy incompatibility."
Verified false — the raw Bash transcript for that delegation showed
`sklearn` was never actually importable in the running venv because an
earlier `pip install scikit-learn` in that shell had targeted a different
Anaconda environment's `pip`, not the project's `.venv`. The NumPy story was
the agent's own plausible-sounding but wrong self-diagnosis (see CLAUDE.md
§1's retrospective-mechanism caveat — this is the same class of finding it
warns about).

**Not fixing this in code**: this is a local/Oscar shell-environment
property, not an adda behavior — there is no adda code path that shells out
to bare `pip`. Mitigation is procedural: prefer `uv add`/`uv pip install`
(never bare `pip`) inside this repo's venv, and if a package "installed"
without error still fails to import, check `which pip` / `python -c "import
sys; print(sys.executable)"` before assuming the package itself is broken.

## 2. Richer delegator↔worker comms — typed blocker/escalation
**Status:** deferred (prefer benchmarking the current system first). Design explored 2026-06-15.

**Today:** the protocol is near single-shot — `Delegate(task, expected_report, …)`
down, the worker's structured report up, and exactly **one blocking
`FollowUp(question)` clarification** mid-task (≤1 per delegation; routes to the
delegating agent — or the human for the entry node). When no operator/TTY is
present FollowUp now returns an autonomous-proceed notice instead of blocking
(headless `input()` EOFError fixed 2026-06-16). The delegator can only
`CancelDelegation` a running worker — it **cannot steer** one.

**Principle:** a *blocker* differs from a *clarification* on **who must act** —
a clarification needs information back; a blocker needs the delegator to take an
**action** the worker can't take itself. So the fix is not "let the worker ask
more," it's "give the delegator the right levers." The real levers a blocked
worker needs: **provide** a missing input/capability, **grant** more eval
budget, **revise** scope, **reroute** to another specialist, **abort** cleanly.

**Chosen direction (Approach A + C's logging):** a worker tool `Escalate(blocker,
kind)` distinct from `FollowUp` — `kind ∈ {missing_input, over_budget,
capability_gap, scope_conflict, unrecoverable}` — that *blocks* (reuse the
FollowUp wait/Reply plumbing) and the delegator answers with a **typed
decision** (`provide / grant_budget(n) / revise_scope / reroute(target) /
abort`) that the runtime *applies* (budget bump, clean abort, re-delegation).
Log the escalation + its resolution to the delegation log (auditable, fits the
science-integrity ethos). These same `abort`/`grant_budget`/`reroute` levers are
what the strategizer needs to act on the stuck-delegation signal in #6.

**Open question to settle first:** which of the 5 delegator actions earn their
place vs YAGNI?

---

## 3. ProblemDefinerAgent — a pre-strategizer intake stage
**Status:** deferred. Raised 2026-06-16.

A new agent that sits **between the human and the strategizer**, running once at
the very start of a run, before the strategizer takes over. Its job is to turn a
raw human problem statement into a high-signal, airtight brief so the strategizer
spends its budget on science, not on plumbing/ambiguity.

**Responsibilities:**
- **(a) Airtight problem statement** — resolve ambiguity, pin the objective
  (minimise/maximise), constraints, success criterion, and what "the result"
  is. Today this is partly covered by the advisory `_review_problem_statement`
  pre-run pass (`agent_runtime.py:680`); the ProblemDefiner would *own* and
  extend it (interactive with the human, not just advisory).
- **(b) Tech stack + ExperimentData schema** — decide/confirm the f3dasm Domain
  (input variables + bounds + types) and the **output columns** of the canonical
  ExperimentData (objective col name, feasibility cols, units), so the ledger
  schema is fixed before any delegation runs.
- **(c) Hard-to-automate plumbing** — the evaluator entrypoint, eval/wall
  budgets, output_names, any study-specific config that today lives in
  `config.yaml` / `PROBLEM_STATEMENT.md` and is easy to get subtly wrong.

**Why:** it **offloads the strategizer** (which currently has to infer schema,
reconcile config vs problem statement, and self-review well-posedness) and hands
it a higher-quality signal. Net effect: fewer SCIENCE_DRIFT / MILESTONE_BLOCK
diagnostics traceable to an under-specified brief, and a fixed ledger schema from
turn one.

**To design when picked up:** is it a graph node (entry before strategizer) or a
runtime pre-pass like the current problem-statement review? How interactive with
the human (blocking Q&A vs one-shot)? Does it *write* `config.yaml` + an enriched
`PROBLEM_STATEMENT.md` as its output artifacts (so the brief is itself a
reproducible deliverable)? Relationship to the existing
`_review_problem_statement` advisory pass (replace vs wrap).

---

## 23. `consultant` — broaden the literature_reviewer into a research+docs consultant

**Status:** spec only; NOT built (user decision 2026-06-30: "one agent, general
but sharp … spec it and put it on the backlog"). Name decided: **`consultant`**.

**Motivation (evidence).** Run `20260629T191754` (supercompressible-material-
creative) shows the datagenerator/implementer repeatedly brute-forcing live
tech-stack gotchas with no doc-lookup channel: `.fil` vs `.odb` for Abaqus
`*IMPERFECTION` (D007), `max_waiting_time=60` too short for Riks preprocessing
(D007), `except RuntimeError` not catching `CalledProcessError`/`TimeoutError`
(D003), `data.add()` not existing on `ExperimentData` (D003), store ordering by
completion time (D004). Each is a documentation question the agents could not
ask anyone — the literature_reviewer can only search *academic papers*
(Corpus/Semantic Scholar/OpenAlex/arXiv), not Abaqus or Python docs.

**Current state (the channel already exists — this is mostly capability+prompt,
not topology).**
- `agents/_graphs.py` already wires `datagenerator → literature_reviewer` and
  `implementer → literature_reviewer`, and `agents/datagenerator.py` already
  instructs `Delegate(target="literature_reviewer", …)`.
- BUT `agents/datagenerator.py:48` says *"Delegate for methodology, not for
  Python syntax"* — the exact opposite of consulting for an API gotcha.
- AND `agents/literature.py` has **no** general-web tool (no WebSearch/WebFetch);
  its toolset is academic-paper search only.

**Proposed changes (one agent, two modes — "general but sharp").**
1. **Add `WebSearch` + `WebFetch`** to the agent (general web covers Abaqus,
   Python, any tech stack — no per-tool MCP needed). FEATURES.md entry required
   in the same commit (tool catalog is enforced by
   `tests/test_features_documented.py`).
2. **Flip the guidance** in `agents/datagenerator.py` (and the implementer) so
   workers may consult for tooling/API/doc questions, not just methodology.
3. **Rename** `literature_reviewer` → `consultant` everywhere: the agent class
   `role`, the node name + edges in `_graphs.py`, every prompt reference
   (strategizer/datagenerator/implementer/critic), the KB-menu audience filter,
   and the milestone/gate text that special-cases `literature_reviewer` (e.g.
   `milestones.py` "literature_reviewer is never gated" and its tests). This is
   the bulk of the mechanical churn — grep `literature_reviewer` across `src/`
   and `tests/` first; ~dozens of sites.
4. **Two-mode prompt (the one real risk).** The current prompt is science-
   citation-heavy (corpus, falsification support). A doc lookup needs *different*
   rigor — the right answer + a source URL, fast — not a literature synthesis.
   The prompt must explicitly distinguish: (a) *literature mode* (academic claim
   → cite a paper from the corpus) vs (b) *docs mode* (API/tooling question →
   authoritative doc/source URL, concise). Without this the agent will
   over-academicize a one-line API question.

**Scope guard.** One agent, not a split — the web tools serve both modes and a
second node is churn without evidence the roles conflict. Revisit only if a run
shows the two modes degrading each other.

**Not §4.** This is agent capability/tooling + prompt, not science epistemics —
no science_monitor / charter / critic-criteria / budget change. Build under the
normal contract (headless test first: assert the renamed node + edges resolve,
the new tools appear in the catalog, and both preamble/guidance render; e2e
behavior-only last).

## 45. `SummarizeDelegations` — a git-timeline-derived delegation summary tool

**Status: idea, not an approved design.**

Every delegation is already git-tracked (the workspace repo under
`debug/delegations/`, one commit per delegation). A tool, working name
`SummarizeDelegations`, would return a `tree`-like view of what each
delegation/turn did — id, target, task one-liner, outcome/status, key
result, and a pointer to its own report — capped in size.

**Motivation.** Summarizing prior work, both within a run (a strategizer
catching up mid-run) and at a warm start (a new run picking up an existing
study's `runs/` history), currently relies on free-text notes
(`final_results.md`, delegation reports) an agent has to read individually.
A structural, git-timeline-derived summary would be cheaper and more
reliable than re-deriving this from prose every time.

**Not designed yet** — open questions before this becomes a spec: what
exactly counts as "key result" per delegation (there is no single typed
field for it today — evals, headline value, and hypothesis outcome are
each tracked separately); how the cap is chosen (row count? char budget?
most-recent-N?); whether it reads `delegation_log.jsonl` (already
structured, no git needed) instead of walking git history at all, since
the log already carries `id`/`to_node`/`task`/`status`/`evals` — git may
only add the workspace-diff angle the log doesn't have. Needs a spec
before implementation.

## 46. Cross-run hypothesis ledger, study-scoped by default

**Status: idea, not an approved design.**

Make the hypothesis ledger study-scoped across runs by default, the way
`runs/lit_reviewer_notes/` already is. `HypothesisList` keeps its current
per-run behaviour by default, and gains an optional `previous=True` that
also returns prior runs' hypotheses: status, posterior, run id, and the
oracle/problem-statement version each was judged under — because an old
verdict may be stale once the oracle or the problem statement changes.
The answer must be capped (ranked by relevance/recency) so it can't bloat
context.

**Warm-start isolation stays manual**, consistent with every other piece of
run-scoped state in this system: the ledger must live under `runs/`, so
wiping `runs/` wipes it — no separate isolation mechanism for this one
piece of state.

**Not designed yet** — open questions: what "oracle/problem-statement
version" means concretely (a hash of `PROBLEM_STATEMENT.md` + the
evaluator entrypoint's source? something coarser?); how relevance is
scored for the ranked cap; whether a hypothesis that was SUPPORTED under a
now-changed oracle should be flagged distinctly from one still under the
current oracle, or just carry its judged-under version and leave the
distinction to the reading agent. Needs a spec before implementation.

## 47. Computed cost undershoots the SDK cost by 0-8% on validator/critic calls

`cost_usd_computed` (from `infra/model_prices.yaml`) matches the SDK's
`total_cost_usd` exactly in every live probe, but sits below it on real
validator/critic calls of run 20260928T141126 (per-call extra SDK cost:
validators ~$0.0034-0.0037, critic-1 $0.0047, critic-2/3 $0.0058, D001-D003
~$0.001).

Ruled out: cache TTL and per-TTL rates (every call was all-1h, one iteration,
no server tools); a per-token rate mismatch (linear fit fails); a plain call, a
call with an MCP tool, and a reviewer-like call (26k-token system prompt, no
tools) all reproduce the SDK cost to the cent, with a single model in
`model_usage`.

Unexplained: what the extra SDK spend is. The transcripts of that run have no
`model_usage`; the transcript `result` record now records it, so the next real
run shows whether a second model entry (a CLI auxiliary call) carries the gap.
Read it from a `result` record's `model_usage` vs `usage`.

## 48. A stall timeout mid-tool restarts the delegation from the top

`retry_on_transient` re-runs the whole `ainvoke` after a `TimeoutError`, so a
delegation that trips `llm_stream_idle_timeout` or `llm_tool_idle_timeout`
(default 0, uncapped) while a tool runs is re-sent its original task as a fresh
CLI session, not resumed. Evidence, run 20260928T225501 D035: 8 fresh sessions
(`F3DASM_LLM_RETRY_MAX=8` in the run's orchestrator.sbatch, one retry layer),
each ~30-40 s of state rediscovery, ~10.7 min apart, then a false FAILED. The
background campaign survived every teardown (no orphan, no second campaign), so
the cost was tokens and the status, not compute. 0630d5e removes the trigger
that fired here (trailing stream events re-arming the window during a tool); a
genuine stall past a configured `llm_tool_idle_timeout` still repeats the full
restart. Open: resume the CLI session (`resume=`) instead of re-sending the
task, or do not retry a timeout once a tool is known running. Judgment call, not
changed.

## 49. Monitor tool stays disabled in headless per-turn queries

The SDK's native Bash description advertises a `Monitor` tool that this context
does not enable; agents called it ~4x per run (D018/D029/D034/D042) and recovered
from the CLI's clear error in one call. Left disabled (judgment call, no prompt
line: that would be a workaround, not a principle). Option (b), enabling it, is
gated on a live test that Monitor's async event notifications reach a headless
per-turn query.

## 50. Two delegations edit the registered oracle file concurrently — parked

Run `truss-iscso2015-open` / `20261007T002015`: the registered oracle is
`workspace/data_generator.py`, a plain file. D002 (builder) and D001 (DoE
runner) both rewrote it between 00:23 and 00:30 UTC. Oracle revisions now make
the change visible in the store (`_oracle_rev`), but nothing says who may edit
the file. Whether to forbid it, serialize it or leave it was the user's call. Decided
2026-10-06: no rule for now; reopen only if a new run shows harm.

## 51. Superseded-revision rows still count

`_oracle_rev` separates rows, but `evals_used`, the best-row table and the
viewer's best-so-far still count every row. Risk: a stale row from a broken
oracle with an attractive objective can win `n_best`. Decide after real
mixed-revision stores exist.

## 52. A harness notice rides inside a truncatable tool output

D001 got "[EVAL NOT STORED ... NEW output differs from STORED]" eight times,
inside a 185 KB tool output cut to a 2 KB preview, so the warning never arrived.
Find where the preview cut happens and whether harness notices can travel in a
channel that is never truncated (prepended, or injected like the science
monitor's notices). Report before fixing.

## 53. User-defined Features — parked idea

Not built (decided 2026-10-06, YAGNI until a real user needs it). Sketch:
`features.register(Feature(...))` appends to `FEATURES`, and
`settings.KNOWN_KEYS` is derived from `FEATURES` instead of listed by hand, so
a registered key is a valid `runtime:` knob and an arm switch. A Feature
covers prompt sections (`<tag>` blocks, `[[if key]]` gates) and tool names
only. It cannot carry behaviour: code that must change when the knob flips
(object construction, a gate, a validator) still needs an `enabled(key)` check
written in the package.

## 54. Per-call input-token size on small studies

One datagenerator delegation (D001) on the trivial `quickstart_branin` study
recorded 1,242,472 input tokens and 10,430 output tokens in a single telemetry
row (`debug/telemetry/calls.<pid>.jsonl`, from the 2026-10-06 wet docs test,
`openrouter/free`, so no money was spent). On a paid backend this would be real
cost for a toy study. Investigate: is the input figure cumulative context across
the delegation's turns, or repeated tool output? Check whether the row sums
turns or counts one call. No work until then.

## 55. Derive the non-Default built-in floor from the CLI init record

`backends/claude.py` (`_base_disallowed`, `NATIVE_TOOLS`) disallows the
sub-agent built-in by the literal name `Task`. The headless init records of the
pinned bundled CLI (2.1.209) and of 2.1.292 both list `Task`, so the floor is
correct today. A future CLI that renames it would leave every non-Default node
able to spawn sub-agents that bypass `Delegate()`, with no signal. Fix when it
matters: build the floor from the built-ins the first init record lists (the
`TOOLS_RESOLVED` row already reads that list for Default nodes) instead of a
fixed name list, with a test that a renamed built-in is still disallowed. No
work until a CLI rename is seen. Evidence note: the init records were read by a
probe that Elvis had not approved (see the 2026-10-07 exchange with the PM).

## 56. A datagenerator read adda's evaluator source to learn how to register

On the 2026-10-06 wet docs test (`openrouter/free`), D001 spent about 280 s
reading the source of `get_evaluator` to learn how to register its evaluator,
although it had already read the registration handbook entry. Read as friction:
either the entry did not answer the question D001 had, or the model did not
trust it. The model was an unpinned router, so the run cannot say which. Only a
note. Revisit if runs on the pinned wet-test model show it again; then read the
transcript to see which question the entry failed to answer.

## 57. A format check for required deliverables — dropped
Run 20261007T154633: a required `design.json` was written as a status report,
not a design. A declared schema per deliverable would catch it. Dropped: n=1,
and a schema needs content specific to each problem. Revisit if a second run
shows the same failure.

## 58. A close-time scan for unmetered oracle calls
Run 20261008T152224 (truss-iscso2015 baseline): 21 of 22 feasible designs came from
unmetered solver calls (662 solver-ledger rows against 601 metered; D001 made 2 calls
and D006 made 59 outside `get_evaluator()`). The just-in-time nudge missed them
(fixed: it now reads the registered generator's own imports). A second layer: at
delegation close, scan the delegation's scripts statically for the wrapped module's
imports and write one diagnostic. Not now (boss, 2026-10-08). Revisit if the nudge
still lets a headline rest on unmetered calls.

## 59. `run_arm.sh` cannot run one repeat of an arm (benchmarks repo)
`ablations/scripts/run_arm.sh` in adda-benchmarks has only `--repeat K`: K sequential
runs of one arm, indexed 1..K. Every new invocation is run 1, so it re-locks adda to the
main tip and re-syncs the study venv. Arms cannot be interleaved by repeat on one pin
without a re-sync per call. Evidence: the 2026-10-08 truss-10bar campaign ran arm by arm
for this reason. Wanted: `--start-index` (or `--repeat-index`) so a call can run repeat
N only and reuse the pinned lock. Do after the campaign (freeze); the change belongs in
adda-benchmarks, not here.

## 61. Real Claude Code on qwen through ANTHROPIC_BASE_URL (vLLM /v1/messages)
Wanted: a claude-backend node with `base_url` (per-node env ANTHROPIC_BASE_URL plus a token placeholder) so Q-off is real Claude Code on qwen. Evidence (2026-10-08, Oscar live test): vLLM 0.19.1 `/v1/messages` rejects Claude Code's first request with HTTP 400, a message with role "system" at messages[1]; vLLM accepts only user and assistant. Blocked on the server side, so the local adapter change was not pushed. Revisit with a newer vLLM or a translating proxy.

## 64. Next campaign needs a metered solo arm and one hard eval cap for every arm
The solo arm is unmetered: its self-reported num_evaluations is unreliable. Audit of
12 valid solo runs (Haiku r2-r6, r102-r106; qwen r2, r4) found true simulator calls of
about 1,300 to 77,000 or more, against a self-report of 119 to 1,037 (e.g. r2 119 vs
>=65,000; r103 1000 vs ~77,300). 0 of 12 comply with the 1,000-simulation limit; both
qwen arms also exceeded 30 min. Wanted: wrap the study simulator so every call is
counted outside the agent's control, and enforce one hard eval cap the same way for
every arm, so quality compares at equal budget. A hard cap needs Elvis's approval.
Audit scripts: scratchpad `audit/`. Local note only.

## 65. Two leftovers from the SIGINT flake work (2026-10-09)
1. `viewer/run_control.py` keeps a persisted epoch `create_time` and compares it with a 1.0 s tolerance. A wall-clock step larger than that makes the viewer treat a live run process as a different process. `resource_backend.proc_start_time` already uses psutil's monotonic identity (af9a31e); run_control should use the same key.
2. `Node` arms wind-down timers but only `agent_runtime._finalize_run` cancels them. A caller that runs a node outside `agent_runtime` leaks the timers into the process. Tests must cancel by hand (245aa7d).

## 66. QueryStore friction: no S column, float equality, the 1e3 sentinel
Three separate gaps, all seen in the lattice baseline r2 run 20261010T182423 (D010, D011, D012 retrospectives and critic-1/2). (1) The store holds only `neg_S`; `QueryStore(columns=["S"])` is rejected, so the agent derives S by hand. (2) `where=` with float equality (`x1 == 0.05`) returns 0 rows; the hint appears only after that first empty result. (3) `where=` cannot filter the 1e3 solver-None sentinel as "not stable", so agents cross-check with a script. Judgment call: none is a bug in the spec; each costs a tool call or a script.

## 67. A deliberate off-ledger verification call versus the ORACLE ACCESS hook
Run 20261010T182423 (D014 retrospective, strategizer and critic-3 retros). The delegation said "do NOT call get_evaluator" for a `design.json` check; the PostToolUse hook then said the same direct solver call "must be re-run through get_evaluator()". The agent followed the delegation, and the hook gave no way to mark the call as a deliberate off-ledger verification. Result: 397 ledger rows plus 1 off-ledger call; the critic saw 397 vs 398 and accepted the labelled call. Wanted: a way to declare a verification call so the hook stays quiet and the call stays visible.

## 68. A registered hypothesis statement cannot be edited
Run 20261010T182423 (critic-3 and critic-4 retros, strategizer DONE retro). H1 was registered as "at most 6 joints"; the tested family became K=3, d<=27. The statement is immutable, so the narrowing lives only in the status comment and `HypothesisList` shows a statement that contradicts the tested family. Judgment call: immutability protects the pre-registered prediction (Charter §4). Wanted: an append-only "narrowed to" field that the ledger shows next to the original text, without rewriting it. Elvis owns the ledger contract.
