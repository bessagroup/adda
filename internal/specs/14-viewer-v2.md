# 14 — Viewer v2: a readable run, then editable study inputs

Status: SPEC, not built. Author: adda-boss-whopper with Elvis, 2026-10-01.
Grounded in the live viewer served on run 20260928T225501 (s55r1, 43 delegations,
22 h 38 m). Screenshots were taken headless; findings below cite what they showed.

The order of the phases matters. Phase 0 removes the defects that make the current page
read wrong. Phase 1 makes a long run readable. Phase 2 adds information. Phase 3 edits a
study's committed inputs, and it does not ship before Phase 0's security item.

---

## Phase 0 — defects found in the current viewer (fix first)

| # | Defect | Evidence (s55r1) | Fix |
|---|---|---|---|
| 0.1 | **No auth and no CSRF guard on the write endpoints.** `POST /api/runs/{id}/note` and `/answer` accept any JSON body from anyone who can reach the port. `request.json()` ignores Content-Type, so a cross-origin `text/plain` POST (a CORS "simple request", no preflight) from any web page Elvis visits can inject an operator note into a live run. The viewer binds to the ZeroTier IP, so every peer on that network can as well. | `viewer/app.py` has no token, auth or Origin check | A per-launch random token (printed once, kept in a cookie). Reject a write whose `Origin` isn't the viewer's own. Require `Content-Type: application/json` on writes. Reads stay open on the bound interface. **A precondition for Phase 3.** |
| 0.2 | **The hypothesis panel sorts lexicographically**: H1, H10, H11, … H2. | Overview screenshot: H1 → H10 → H11 | Natural sort by the numeric id. |
| 0.3 | **"Nothing has been evaluated" when the store is merely unreachable.** The Oracle tab and the header's EVALS show 0 when the store path doesn't exist on the viewer's host, which is absence of data presented as a zero. | The mirrored run had no `experiment_data/`: "An oracle is registered, but nothing has been evaluated", EVALS 0, while the run did 1,255 | Distinguish *store not found at <path>* from *store empty*. The header shows "—" with a tooltip, never 0. |
| 0.4 | **Timeline lane labels are overdrawn by bars once the timeline scrolls.** | Selected-delegation screenshot: "datagenerator" and "implementer" are covered by bars at 840 m+ | Superseded by Phase 1.1. If the horizontal timeline survives anywhere, the label column is sticky with an opaque background and a z-index above the bars. |
| 0.5 | **Bar labels truncate to nothing**: "D00", "D01e", "FB17", "revi". | Overview screenshot | Superseded by 1.1, where an id always has room. |
| 0.6 | **Same-role concurrency is invisible.** Parallel nodes (f626353) let two implementers run at once; one lane per role draws them on top of each other. | s55r1 already had D021/D022 overlapping in one lane | Phase 1.1 gives concurrency its own columns. |
| 0.7 | **The notebook render mangles math**: `P_max*1000/(pi*D1^2/4…` is eaten as markdown emphasis ("P_max1000/(piD1^2"). | Result screenshot | Render the notebook's markdown with math support (KaTeX from cdnjs, or escape `*` inside `$…$` and code spans). Same for Brief. |

---

## Phase 1 — make a long run readable

### 1.1 Vertical delegation timeline, left of the ledger (Elvis's proposal 1: VALIDATED, extended)

**Why it's right, beyond looks.** The run's real axis is time; a 22 h run in a horizontal
strip forces horizontal scrolling and truncates every id (0.4, 0.5). Vertical, time
scrolls the way pages do, and each row has room for the id, the intent and the outcome.

The stronger reason: parallel nodes made **concurrency** a first-class fact. Columns are
the natural place for it.

**Layout.**
- **Left:** time runs downward, with a sticky hour ruler.
- **Columns:** one per *awake slot*, at most `runtime.max_awake_nodes − 1` worker columns, plus a thin strategizer gutter. A delegation is a card occupying its column from start to end. A QUEUED span is a hatched segment *before* the card starts, labelled with its queue reason.
- **Card:** the id, the role colour (as today), a one-line intent (the first clause of the task), the status chip and its duration. The outcome (DONE / FAILED / OPEN_FOR_REVIEW) is shown as a chip, not as colour alone.
- **Right:** the hypothesis ledger (1.2). **Each card draws a faint connector to the hypotheses it carries** (`hypothesis_ids`), and hovering a hypothesis highlights its delegations. This is the view the run's logic actually needs: which experiments served which claim.
- **Below a card, on select:** the existing detail panel (task, report, nudge box, Open chat).
- **Gate reviews** (critic gate calls) are full-width horizontal rules across the columns, labelled with the verdict (REJECT / REVISE / PASS). They're run-level events, not one worker's work.

### 1.2 Hypothesis ledger, made a ledger

Today it shows a statement and a status word. It should show, per hypothesis:
- the status chip;
- **prior → posterior** as a small bar;
- the registered falsification criterion, collapsed;
- the **status history**: every transition with its timestamp, its delegation and the validator note. Retractions matter. s55r2's H4 went SUPPORTED → OPEN on a validator note within a minute, and today's panel can't show that.
- Filters: open / closed / retracted.

### 1.3 Header vitals that answer "is it healthy?"

- Wall clock against the budget, with the 1.5× (no new delegations) and 2× (stop) marks.
- Evals.
- Cost so far, labelled "known" vs "unknown" for attempts that died without a ResultMessage. Per 6ed4fce, cost can be None; never print 0.
- **Awake slots N / max** and the queue length.
- Orchestrator RSS against the job's memory, if readable.

---

## Phase 2 — information the run already records but the viewer doesn't show (Elvis's question 3)

Ordered by value, each sourced from a record that already exists:

1. **Best-so-far trajectory**: σ (in ×reference) against wall time, per namespace, feasible vs salvaged marked differently, with the target line. *Source:* the store's output.csv plus `_ts`. This is the single chart that says whether the science is progressing.
2. **Feasibility funnel per namespace**: n → coilable → solved → feasible, and which criterion binds. *Source:* the store. It mirrors the deck's Stats line, so the viewer and the deck speak one language.
3. **Thinking**, collapsed, in Chat. Summarized thinking is now on by default (98af9ef) and visible in s55r2. Count of omitted blocks shown.
4. **Diagnostics timeline**: ERROR_RETURN, LLM_RETRY (93a8fc7), CONTEXT_COMPACTED, CONSISTENCY_FLAG, VERDICT_SUBSTANCE_FLAG, SCIENCE_DRIFT as ticks on the 1.1 time axis, filterable. Today they're only a count in the science-monitor drawer.
5. **Critic gate reviews**: each `critic_reviews/call_NNN.md` rendered, with the verdict and the criterion-5 finding, linked from its gate rule in 1.1.
6. **Per-delegation evidence**: the files it touched and its report, from the workspace git history and `debug/evidence_index.md` (6f33688). That's the same index the critic gets.
7. **Slurm jobs per delegation**: job ids, state, wall. Shown only if the study records them; don't scrape squeue from the viewer.
8. **Strategizer notes**: `strategizer_notes/strategy_*.md`, rendered with timestamps. In s55r1 these were the only reasoning trace.
9. **Literature corpus**: papers added (`CorpusAdd`), and source failures (Semantic Scholar 403s, paywalls) as a count per source.
10. **Retrospectives**, after close, grouped by CONSISTENCY / DECISION / FRICTION / BLOCKED, each linked to its delegation. These are the goldmines; reading them should be one click.
11. **Run comparison**: two runs side by side on 1.3's vitals and on 2.1's trajectory, e.g. s55r1 vs s55r2. *Source:* both runs' records; no new data.

Not proposed: anything needing a new recorder in adda. Phase 2 reads only what's already on disk.

---

## Phase 3 — editing a study's committed inputs (Elvis's proposal 2: VALIDATED in direction, with conditions)

The direction is right: the viewer already is the operator console (notes, answers), and
editing the inputs while watching the run is one loop.

Starting and stopping a run is covered by Phase 5 (terminal parity, 2026-10-04). That phase
supersedes the earlier exclusion as follows. The viewer runs adda's OWN commands
(`python -m adda.watchdog <study>`) on the machine it runs on. For anything else, such as a
cluster launcher, it runs only the command the study declares in its config. adda itself
still knows nothing about Slurm. Inside a run, adda works with the permissions of the
session that launched it, and its agents submit and cancel their own compute jobs through
the study's broker. That is unchanged.

It turns a read-only page into something that edits the inputs science depends on. So:

**3.0 Preconditions (non-negotiable).**
- Phase 0.1's auth and Origin check.
- Every write action asks for confirmation inside the page. No `confirm()`.
- Every write is written to an audit log (`studies/<study>/viewer_actions.jsonl`: who, what, when, diff).

**3.1 Edit the problem statement — through git, not a text box over a file.**
The zero-shot launcher copies only the *committed* study tree (`git archive HEAD`), so an
unsaved or uncommitted edit silently never reaches the agents. That's exactly the failure
mode we hit on 2026-09-30, when Elvis "couldn't commit".

So the editor:
- shows the committed text;
- edits in a buffer;
- shows the **diff**;
- **commits** with a required message (authored as the logged-in user), and pushes only if asked;
- refuses to commit if the working tree has unrelated changes.

**3.2 Configure a run — the study's `config.yaml`, schema-validated.**
- **Fields:** model per node, budget, mem_cap, `runtime.*` knobs (max_awake_nodes, thinking_display, stream timeouts).
- **Validation:** against adda's settings schema, so a typo is refused, not silently ignored (Elvis's explicit-config rule).
- **Commit:** the same commit flow as 3.1.

**3.3 Out of scope:** editing prompts or the corpus (that's the prompt-corpus artifact's job), deleting runs, and moving or archiving scratch. Starting and stopping runs moved to Phase 5.

---

## Phase 5 — terminal parity (Elvis, 2026-10-04)

**The requirement, verbatim:** "non technical = anyone who doesnt use a terminal. It doesnt have to poay down the hargon, just allow them to do anything hou would do on a terminal command".

So the bar is parity with what a terminal user does with adda, not simplification. The jargon stays; every action gets a control.

**The inventory of terminal actions.** This is the acceptance list: each row needs a working control, and a headless test that it runs the same thing the command does.

| # | Terminal action today | Viewer control |
|---|---|---|
| 5.1 | `mkdir studies/<name>`, write `PROBLEM_STATEMENT.md` + `config.yaml` | **New study**: name, problem statement and config (schema-validated, as 3.2), from a blank or an existing study as template. Commit flow as 3.1. |
| 5.2 | Copy a study to vary it | **Duplicate study**, then edit it. |
| 5.3 | `python -m adda.watchdog <study> --budget …` | **Start run**: the budget and model come from the committed config. A pre-flight check shows what `docs/authoring-a-study.md` "Before a long run, check" asks for; failures block the start, with the reason. |
| 5.4 | Ctrl-C / kill the watchdog | **Stop run**: a graceful stop through the run's own shutdown path, so retrospectives and close run. The watchdog today SIGTERMs the process tree on its deadline (`infra/watchdog_launcher.py::run_under_watchdog`). Verify first whether a run closes gracefully on that signal. If it doesn't, adding a graceful stop is part of 5.4. A second, confirmed **Kill** sends the signal to the exact PID the viewer started. Never by name pattern. |
| 5.5 | A study-specific launcher (e.g. `launch_zeroshot.sh` on a cluster) | **Start via study launcher**: the viewer runs `config.yaml → runtime.launch.command` (explicit config, no env flags) with its declared arguments, and shows its output. If the study declares no launcher, the control is absent. Stop calls `runtime.launch.stop_command` with the id the launcher printed. The viewer never composes cluster commands itself. |
| 5.6 | `tail -f` logs, `ls runs/` | **Runs list and live logs** per run: orchestrator log, watchdog log, run status. |
| 5.7 | Notes / answers to a live run | Exists: Phase 0.1 and the current note/answer boxes. |
| 5.8 | `jupyter` on `pipeline.ipynb`; re-run it | **Open the deliverable** (rendered) and **Re-execute** it through adda's own notebook execution (`evaluation/notebook_exec.py`), showing pass/fail plus output. |
| 5.9 | `adda-docs "<question>"` | **Ask the docs**: a box that runs the same `adda.explain` entry point. |
| 5.10 | `git log` / `git diff` on the study | **History**: the study's commits, with the diff of each. |
| 5.11 | Copy files off the machine | **Download**: the notebook, the run's `debug/` as a zip, store CSVs. |

**Safety (on top of 3.0, non-negotiable):**
- Start, stop and launcher commands make the viewer a remote-execution surface. The default bind becomes `127.0.0.1`. Binding to any other interface needs an explicit flag, and prints a warning that start/stop is exposed to everyone on that network.
- One active run per study from the viewer. A second Start is refused while one is live (read from the run's status file, not from memory).
- Every start, stop, kill and launcher call goes in the audit log (3.0), with the exact command line run.
- The viewer stores the PIDs and launcher ids it started. Stop and Kill touch only those.

**Out of scope here:** deleting runs or studies (irreversible; stays a terminal action), editing the prompt corpus, scratch management.

**Order:** Phase 5 comes after Phase 2 and before Phase 4. 5.3, 5.4, 5.6 and 5.8 first: they're the loop a non-terminal user needs to run anything at all.

---

## Phase 4 — aesthetics audit (Elvis's point 4: AGREED it's "70% there"; what the last 30% is)

What already works and must be kept:
- the calm neutral ground;
- IBM Plex;
- the per-role identity colours, used consistently across header, lanes, chat and graph;
- the status pill with its dot;
- the restraint.

What holds it back, from the screenshots:

1. **Empty canvas where there should be information.**
   - Graph: ~70% of the viewport is blank below four boxes.
   - Chat: an empty right pane until a node is picked.
   - Oracle: a three-line page.
   - Result: the notebook capped at ~700 px on a 1,600 px screen.

   Fix: Graph becomes a compact strip on Overview, showing live slot occupancy per node, or gets per-node vitals (delegations, cost, last activity). Chat opens on the most recent active node. Oracle folds into Phase 2's trajectory and funnel. Result uses a wider measure for tables and figures, keeping prose at ~75 ch.
2. **Hierarchy is flat.** Labels are uppercase micro-type (TIMELINE, HYPOTHESES), and everything else is the same weight. Fix: define a type scale once, title / section / body / meta at four steps, and use weight and size for hierarchy rather than letter-spaced caps alone.
3. **Status is carried by colour words only.** "inconclusive" and "falsified" are coloured text. Fix: chips with shape plus colour, consistent with the deck's chip vocabulary so the viewer and the deck read the same way.
4. **Low contrast on meta text.** The grey-on-grey of the timeline range, the axis ticks and "Pick a delegation above" is too faint. Check WCAG AA (4.5:1 for text under 18 px) on every meta colour token.
5. **No dark theme.** The page is light-only. Define the palette as tokens with a `prefers-color-scheme: dark` set. It's long-run monitoring at night.
6. **The science-monitor drawer is a strip with an orange badge** that reads as a warning. Fold it into Phase 2.4's timeline ticks.
7. **Spacing.** Panels butt against each other (the timeline/ledger split, the header/tab rule). Use one spacing scale and consistent gutters.

Process for the bugfixer: the screenshots in this spec's review were produced headlessly with Playwright against a mirrored run. Keep that as the acceptance check. Before and after each Phase 1/4 change, screenshot every tab at 1600×1000 and 400×900 (phone width) and attach both.

---

## Decisions for Elvis

- **D1** Ship order: Phase 0 → 1 → 2 → 3 → 4 polish alongside 1, as written? (Recommended)
- **D2** The commit identity for viewer edits: Elvis's git identity, or a "viewer" author with Elvis as committer?
