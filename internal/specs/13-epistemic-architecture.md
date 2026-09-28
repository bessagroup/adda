# Spec 13 — Epistemic architecture: typed claims, lineage, sequential evidence

**Status:** design notes, NOT approved for build. Recorded 2026-09-28 from a
design discussion between Elvis and adda-boss-whopper, so the reasoning can be
picked up later without reconstruction. **Priority:** to be set. **Branch:**
any build happens on a feature branch, not `main`; small independent patches
may still land on `main`.

Paper interpretations cited here are recorded in Zotero (the ground truth for
paper interpretation in this project), in each item's "Relevance to a3dasm"
note: POPPER `XUPPQ4SV`, Hypothesis Evolution Protocol `SC573Z2A`
(duplicate item `PK8WT5ES`), "AI scientists produce results without reasoning
scientifically" `TZH5WRR6`. Where this document and a Zotero note disagree,
the Zotero note governs.

## 1. Question

Whether the hypothesis ledger (`epistemics/hypothesis_ledger.py`: a flat JSON
map of free-text hypotheses, each with a falsification criterion, prediction,
scalar prior and a status log carrying agent-stated posteriors, `MAX_OPEN = 3`)
has enough formal structure to (a) scale to many hypotheses, including across
runs, (b) raise inconsistencies programmatically, and (c) make priors
meaningful rather than subjective.

## 2. Measured baseline (2026-09-28)

Source: all `debug/strategizer_notes/hypotheses.json` files of the
`supercompressible-material` study on Oscar (47 runs, 277 hypotheses), parsed
as JSON (not grepped).

| Measure | Value |
|---|---|
| Hypotheses per run | mean ≈ 6, max 22 |
| Final status | INCONCLUSIVE 94, FALSIFIED 93, SUPPORTED 70, OPEN 20 |
| Statements referencing another hypothesis by ID | 8 of 277 |
| Prior 0.2–0.4 → share resolved SUPPORTED | 0.12 (n = 52) |
| Prior 0.4–0.6 → SUPPORTED | 0.40 (n = 50) |
| Prior 0.6–0.8 → SUPPORTED | 0.61 (n = 38) |
| Prior 0.8–1.0 → SUPPORTED | 1.00 (n = 20) |
| Brier score of priors (resolved, n = 163) | 0.182 (constant base-rate predictor: 0.245) |
| Hypotheses with ≥ 4 posterior updates that reversed direction at least once | 51 of 62 |

Readings, with their caveats:

- Scale exists across runs, not within a run. Each run's ledger is isolated.
- Relations between hypotheses are almost never explicit, although runs
  re-test the same mechanism families across runs and some cross-run verdicts
  conflict (e.g. an outward radial bow FALSIFIED in 20260907T212358; the bowed
  family was the best design in the accidental run 20260927T012131).
- Priors are informative on this corpus. Two caveats: outcomes are graded by
  the same agents that set the priors, and the 0.8–1.0 bin is dominated by
  sanity-check hypotheses (e.g. "the oracle wiring is faithful").
- Posteriors oscillate; this is the verdict-oscillation tension already listed
  in CLAUDE.md §4 as a charter question.
- Old hypotheses were judged under earlier oracle versions and problem
  statements (e.g. before the deterministic Riks fix), so their verdicts are
  not ground truth for current conditions (Elvis).

## 3. Prior art read in full (details in Zotero)

- **POPPER** (Huang et al., ICML 2025). Sequential falsification with e-values:
  each test's p-value is converted to an e-value, the running product is a
  super-martingale under the null, and rejecting when the product reaches 1/α
  bounds the false-validation rate by α under optional stopping. The guarantee
  requires tests chosen without seeing their data and sub-hypotheses logically
  implied by the main one (enforced by a relevance checker; removing it raised
  Type-I error from 0.082 to 0.340). The Zotero note records an adversarial
  read: the implementation lets the execution agent choose tests after seeing
  the data, so the guarantee is nominal. App. C: Type-I control is not
  false-discovery control; FDR over many hypotheses needs e-BH.
- **Hypothesis Evolution Protocol** (Takahara & Mizoguchi, 2026).
  Event-sourced, hash-chained registry; lineage via generation mechanisms
  (de-novo, inspired-by, refine, merge; refine/merge only from evidenced
  parents); five states including `dormant`; verdicts gated at belief ≥ 0.8 /
  ≤ 0.2; saturation-based stopping. Per the Zotero note it lacks a
  falsification requirement, adversarial validation and a live monitor, which
  a3dasm has; a3dasm lacks its immutable event log and lineage.
- **AI scientists produce results without reasoning scientifically**
  (Ríos-García et al., 2026). Over 25,000 runs of thin scaffolds (ReAct,
  tool-calling; multi-agent orchestration explicitly out of scope): evidence
  non-uptake 68%, beliefs never updated 71%, refutation-driven revision 26%.
  Contributes an instrument: reasoning traces as graphs of epistemic operations
  with named anti-pattern templates (untested claim, evidence non-uptake,
  contradiction without repair, premature commitment). The Zotero note records
  that the annotation agreement statistics are contested (κ ≈ 0.067).
- **AutoDiscovery** (Agarwal et al., 2025): Bayesian surprise as the reward
  for hypothesis search. Only the abstract was read (download failed); it is
  not in Zotero and nothing here depends on it.
- **Jev** (TypeSafe AI, released 2026-09-15): a "System One" decision model
  returning a choice from a fixed answer set with probabilities. Vendor
  benchmarks are on language tasks, not scientific judgement. Not a paper.

## 4. Proposal components

Each component is listed with its status in the discussion.

1. **Typed claims: the quantifier.** Each hypothesis declares a quantifier:
   universal ("no design in R does X"), existential ("some design in R does
   X"), tendency (sign or trend of an effect), or causal (intervening on A
   changes B). *Status: Elvis open to adding it. adda-boss-whopper's timing
   concern: without an evidence rule that reads it, the field is a label only;
   proposed to land together with the universal-versus-data rule (component 3),
   after the current 24 h run.*
2. **Further typed-claim fields: scope, prediction, conditions.** Scope and
   prediction as predicates over the study's declared input and output names,
   in QueryStore's existing `where=` syntax; conditions = oracle version or
   hash, problem-statement version, fidelity. *Status: backlogged; Elvis judged
   them possibly overfit and is 50/50 on executable hypotheses, noting agents
   must decide what is executable either way.*
3. **Quantifier-selected evidence rules.**
   - Existential: verified by one row satisfying scope and prediction.
   - Universal: refuted by one such row, including rows pooled from other runs
     under the same conditions; supportable only in bounded form (n uniform
     random samples in R with no exceedance ⇒ exceedance fraction below about
     3/n at 95%).
   - Tendency and causal: p-values (e.g. permutation tests over sampled
     designs, since a deterministic oracle's randomness is the design sampling)
     converted to e-values and accumulated POPPER-style, verdict at 1/α.
   To satisfy POPPER's Assumption 2, the test would be fixed at registration
   (a3dasm already pre-registers falsification criteria) rather than chosen by
   the executing agent. *Status: proposal. The transfer to deterministic
   oracles and the bounded universal rule are adda-boss-whopper's inference,
   not results from the papers.*
4. **Lineage (from HEP).** Parent plus generation mechanism; only tested
   parents may have children; a `dormant` state. *Status: proposal.*
5. **Research programmes (Lakatos).** A programme is a de-novo root with its
   descendants: a core idea plus auxiliary hypotheses. Children are tagged
   *novel prediction* or *rescue* (proposed after a parent failed, addressing
   the same observation). Standing is computed: progressive when novel
   predictions hold, degenerating when descendants are mostly rescues. Uses:
   a concrete target for the critic's graded time rule (criterion 5: substantial
   time left → move off a degenerating programme); the programme as the unit of
   cross-run memory, replacing by-hand tracking in the study slide deck.
   *Status: proposal; Elvis liked the idea and asked for the operational link,
   given here.*
6. **Consistency checks as structural templates.** From the anti-pattern
   catalog: contradiction without repair (contradicting evidence attached to a
   hypothesis still SUPPORTED), untested claim, evidence non-uptake (a result
   linked to no hypothesis), premature commitment (verdict on one-sided
   evidence); plus, with component 2, universal-versus-data, sign conflicts
   between tendency claims on the same variable pair with overlapping scopes,
   duplicates, and stale verdicts after a change of conditions. For free-text
   claims: embed, cluster, and adjudicate only within clusters (consistent /
   contradictory / duplicate / unrelated), for which a typed decision model
   such as Jev is one candidate. *Status: proposal.*
7. **Consequential priors.**
   - Allocation: choose the next test by expected information gain × stakes ÷
     cost.
   - Scoring: per-role Brier or log score accumulated across runs and shown to
     agents.
   - Independent prior: evaluate a separate model's P(survives its test)
     against the 163 resolved hypotheses (agents' own Brier 0.182) before
     adopting it.
   *Status: proposal.*
8. **Keep** pre-registered falsification criteria, the independent critic and
   verdict validator, and code-enforced monitoring (science monitor, gates).

## 5. Related backlog items

- BACKLOG #45 — delegation timeline summary from the per-delegation git
  history (`SummarizeDelegations`, working name).
- BACKLOG #46 — cross-run, study-scoped hypothesis ledger; `HypothesisList`
  keeps per-run default and gains `previous=True`, capped output, tagged with
  the conditions each verdict was judged under; stored under `runs/` so manual
  isolation (wiping `runs/`) still works.

## 6. Points of disagreement or correction during the discussion

- adda-boss-whopper first claimed LLM-asserted edges are unreliable. Elvis
  objected (cooperative principle; capable, costly agents). The narrower
  position that survived: capable agents' assertions carry information, and
  the empirical concern is non-use of evidence, which argues for enforcement
  at tool boundaries rather than against assertion. The 95.7% human–LLM
  agreement figure from Ríos-García et al. was cited as support, then
  retracted on the strength of the Zotero note (κ ≈ 0.067), then re-checked
  against App. H.5: experts judged the large majority of LLM-drawn edges
  correct (87–99.8% by reviewer), and the near-zero κ reflects disagreement on
  which rare items are wrong. The evidence moderately supports reliability of
  edges drawn by a third-party annotator on 25 traces; it says nothing about
  edges an acting agent asserts about its own hypotheses. Zotero notes are
  treated as priors to verify, not as ground truth for the paper's content.
- The statement that e-value guarantees "hold" was an overstatement: they hold
  under the paper's assumptions, which its own implementation is argued (in
  the Zotero note) to violate.
- A proposed "retrospective audit" of the 277 old hypotheses was questioned by
  Elvis on the grounds that past hypotheses may reflect what past conditions
  required rather than good science. Any audit must therefore condition on the
  oracle and problem-statement version each hypothesis was judged under.

## 7. Open questions

1. α: a fixed default, or a per-study setting (a policy choice for Elvis).
2. Primary target: within-run reasoning or cross-run knowledge. The measured
   baseline points to cross-run; within-run counts are small.
3. The expected mix of executable (response-surface) versus mechanistic
   (causal-story) hypotheses; this decides how much of component 3 applies.

## 8. Validation before any build

1. Replay the component-6 checks over the 277-hypothesis corpus, counting
   findings only where the compared hypotheses share oracle and
   problem-statement conditions.
2. If the branch is built: A/B against `main` on the ablation suite, judged on
   the CLAUDE.md §1 KPIs (gate outcome, gate attempts, ERROR_RETURN, evals,
   wall clock, headline value) plus the anti-pattern rates.
