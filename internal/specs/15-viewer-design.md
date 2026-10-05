# 15: Viewer design, the feel and UX (owned by adda-boss-whopper)

Status: DESIGN SPEC, 2026-10-04. It supersedes spec 14's Phase 4 ("aesthetics audit") entirely, and it governs every pixel of Phases 2, 3 and 5. Spec 14 says WHAT the viewer shows and does; this spec says HOW it looks, feels and behaves.

**Elvis's direction, verbatim:** "ZERO anchor bias on the UI, i am actually not that happy with the feel and the UX". This is a redesign from first principles: no layout, colour, type or component carries over from `templates/graph.html` because it already exists. The data endpoints stay.

**The reference implementation** is `15-viewer-design/run.html`, a working prototype on real s55r2 data (`s55r2_sample.json`, produced by the viewer's own API). Serve the folder and open it:
```
cd internal/specs/15-viewer-design && python3 -m http.server 8765
```
Where this text and the prototype disagree, the text wins. The prototype hard-codes its sample study's baseline and target lines and its axis unit for illustration only. That is exactly what §6a forbids in the real viewer. Where the text is silent, match the prototype.

---

## 1. What the viewer is

**One sentence:** an instrument for watching, steering and operating an autonomous research run, for people who don't use a terminal (spec 14, Phase 5).

**Its users:**
- researchers;
- people who leave a run going for 12–24 h and check in, sometimes from a phone;
- people answering an agent that is blocked on them.

**The jobs, in priority order:**
1. **Is it healthy, and is the science progressing?** Answered in under 3 seconds, from the title block, without scrolling.
2. **An agent is asking me something.** It must be impossible to miss, and answerable in place.
3. **What happened, when, and why?** The timeline plus the inspector.
4. **What did it find, and does it hold?** Data, Hypotheses, Deliverable.
5. **Operate:** start, stop, note, edit inputs, re-run (spec 14, Phases 3 and 5).

**The metaphor:** a materials-testing instrument and its test record. Calm, exact, dense where data is dense. The title block borrows from an engineering drawing's title block: the run's identity and vitals live in one ruled strip.

## 2. Principles (each decides a real trade-off)

- **P1 One selection, everywhere.** Exactly one thing is selected: a delegation, hypothesis, gate, data row or file. It's highlighted in every view that shows it, it drives the inspector, and it's in the URL (`?view=timeline&sel=D012`), so any state can be shared as a link.
- **P2 Summary before detail.** Every view opens on its answer; detail is one click away in the inspector. The inspector never replaces the main view, it sits beside it.
- **P3 State is shown by shape plus colour plus word.** Never colour alone (colour-blind safe, greyscale-print safe).
- **P4 No dead ends.** Every id you can read (D012, H4, a row number, a file) is a link that selects it.
- **P5 Honest numbers.**
  - Absent data is "—" with a tooltip saying why, never 0.
  - Every number that carries a comparison shows its unit and its reference (a study-declared unit label such as "× baseline", or "of 12 h budget").
  - Cost that's partly unknown says so ("$83 +1 unknown").
  - A "best" says what it's the best of (see §7, open questions).
- **P6 Jargon stays.** Elvis: "It doesnt have to pay down the jargon". We use the run's own words (delegation, gate, falsification, the study's own column names) and never invent friendlier synonyms. We do make every term discoverable: a hover or long-press on a term gives its one-line definition, taken from the docs.
- **P7 Live, not twitchy.** New events arrive with a 160 ms fade-and-rise of 6 px. Nothing else moves. The only looping animation is the pulse on a live status dot. `prefers-reduced-motion` turns everything off.

## 3. Visual language

### 3.1 Colour tokens

Defined once on `:root`, light by default. Dark redefines only the tokens, under `@media (prefers-color-scheme: dark) :root:not([data-theme=light])` and again under `:root[data-theme=dark]`. Components never contain a literal colour.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--ground` | `#F2F3F0` | `#0E1114` | page |
| `--surface` | `#FFFFFF` | `#151A1F` | panels, cards, title block |
| `--surface-2` | `#F8F9F7` | `#1A2026` | nav rail |
| `--sunken` | `#E9EBE6` | `#0B0E11` | tracks, chips, hover |
| `--ink` / `--ink-2` / `--ink-3` | `#14181D` / `#48505A` / `#6B737D` | `#E7EAEC` / `#AAB2BB` / `#87909A` | text: primary / secondary / meta |
| `--rule` / `--rule-strong` | `#D9DCD5` / `#BFC4BB` | `#262D35` / `#36404A` | hairlines / control borders |
| `--select` | `#2B4ACB` | `#7E95FF` | selection, focus, live; the ONLY accent |
| `--ok` `--warn` `--bad` | `#2E7D4F` `#A06410` `#C2402A` | `#5DBB86` `#E0A54B` `#F07560` | semantic state only |

**Role identities** are used as a 3 px edge, plus the role name in that colour. They're never used as fills:

| Role | Light | Dark |
|---|---|---|
| strategizer | `#2B2F36` | `#C9CED4` |
| literature_reviewer | `#7A5AA6` | `#B79BE0` |
| datagenerator | `#B0601E` | `#E39457` |
| implementer | `#1F7F86` | `#4FC1C9` |
| critic | `#B23A6A` | `#EC79A4` |

**Contrast:** every text/ground pair must reach WCAG AA (4.5:1 under 18 px). The `--ink-3` values above were chosen to reach it on `--surface` in both themes. Verify with a script in CI (§8), not by eye.

### 3.2 Type

- **UI face:** Instrument Sans, 400/500/600. Numbers use `font-variant-numeric: tabular-nums`.
- **Mono face:** Geist Mono, 400/500. Used ONLY for identifiers and code: run ids, D/H ids, file paths, column names, chips, axis ticks. Vitals and facts use the UI face (the prototype's first pass used mono for vitals and it read like a typewriter).
- **Fonts are vendored** as woff2 under `viewer/static/fonts/` with their OFL licences. No CDN at runtime: the viewer must work offline on a cluster node.
- **Scale, the only sizes allowed:** 12 meta · 13 data · 14 body · 16 section · 20 page title · 26 vital.
- **Uppercase is for 11 px field labels only**, with letter-spacing .05em. Hierarchy comes from size and weight, not caps.
- **Prose** (task text, reports, the notebook) is capped at 62–75 ch.

### 3.3 Space, shape, depth

- **Spacing:** 4 / 8 / 12 / 16 / 24 / 32 only. Sibling groups use `gap`, never margins that collapse.
- **Radius:** 6 px for controls and cards, 8 px for panels, 999 px for status pills. Nothing else.
- **Depth:** hairline rules first. One shadow token for cards (a 1 px drop). No other elevation, except the dialog scrim.
- **Icons:** none, except ✕ close and ◐ theme. Words beat glyphs for this audience.

### 3.4 State vocabulary

The same everywhere, viewer and decks alike (P3):

| State | Mark | Colour |
|---|---|---|
| supported / done / pass | filled circle | `--ok` |
| inconclusive / revise | hollow square | `--warn` |
| falsified / failed / reject | filled triangle | `--bad` |
| open / queued | hollow circle | `--ink-3` |
| running / live | filled circle, pulsing | `--select` |
| queued span (timeline) | hatched fill | `--hatch` |

## 4. Layout and screens

### 4.1 App shell (≥ 1200 px)

- **Nav rail, 220 px:** brand + host name, studies, the current study's runs (each with its status mark), and at the foot "New study" and the theme toggle. The rail can collapse to 56 px.
- **Main:** title block, then view tabs, then body.
- **Body:** the work area (fluid), plus the inspector (440 px, resizable 360–640 px, closable). Esc closes the inspector; selecting reopens it.

### 4.2 Title block, the 3-second answer

The fields, left to right:
1. study · model;
2. run id (mono, 20 px) plus the status pill;
3. **wall clock:** "12.6 h / 12 h", with a track carrying ticks at 1×, 1.5× (no new delegations) and 2× (stop);
4. **the run's figure of merit:** the best counted row's value, with the study-declared unit label and its store, e.g. "19.8 × baseline · store B row 217". Which column, and its reference, come from the study's declared objective, not a hard-coded σ. Clicking it selects that row;
5. **evaluations;**
6. **cost**, with an unknown count;
7. **actions:** Note to run · Stop (or Start, when idle) · Re-run study. Primary styling only on the single most likely action for the state: Re-run when closed, Note when live.

When an agent is waiting on a human, a question banner (§4.4) spans the full width directly under the title block. It outranks everything.

### 4.3 Views (tabs)

- **Timeline (default).**
  - **Layout:** vertical time going down; one column per awake slot, numbered "slot 1…n"; a sticky header row.
  - **Ruler:** shows elapsed time ("+3 h"), not clock time. Clock time is in the inspector and in hover text. Runs are compared by elapsed time.
  - **Cards:** a role edge; id (mono); role name; duration (right); the first clause of the task (2-line clamp, once the card is tall enough); chips for falsification, hypotheses and evals (once taller still).
  - **Gates:** full-width rules in their verdict colour, with a chip at the right edge ("gate · revise").
  - **Queued spans:** hatched segments above the card they precede.
  - **Live runs:** a "now" line in `--select`, and a "Follow live" toggle that keeps it in view.
  - **Keyboard:** `j`/`k` step through delegations in start order.
  - **Not yet in the prototype, required:** connectors from a card to its hypotheses' rows (spec 14 1.1) appear only for the selected card, as one faint line per hypothesis, never all at once.
- **Hypotheses.**
  - **Rows:** id; statement (clamped to four lines with a more/less toggle in the list; the inspector always has the full text); status mark plus word; a posterior bar with prior → posterior; linked delegations as id links.
  - **Selecting a row:** the inspector shows the falsification criterion, the prediction, and the full status history with validator notes. A retraction is drawn as a back-step in the history, not hidden.
  - **Filter chips:** all · open · closed · retracted, each with a count. *Open* means no verdict now; *closed* means a verdict now (supported, falsified or inconclusive); *retracted* means the status log ever returned a closed verdict to open, whatever the status is now. A retracted row carries a "retracted" tag.
  - **Revision:** a downgrade (supported or falsified to inconclusive) is a revision, not a retraction: the history marks it "revised" in its own dashed style, and the retracted filter leaves it out.
  - **Linked delegations:** those that carry the hypothesis plus those its evidence cites.
  - **Empty states:** no hypothesis yet says what will appear; a filter with no match says what the filter means.
- **Data.**
  - **Progress chart (revised 2026-10-06, after Elvis: "98% white space, I would hardly call it useful").**
    - **Default x is the evaluation number:** counted plus uncounted rows across every scored store, in time order. A toggle switches to elapsed time. Best-so-far against evaluations is the standard convergence view. It also separates evaluations that a single delegation ran in one burst, which elapsed time stacks into one column.
    - **The domain fits the data:**
      - y spans the counted values, padded by 8%;
      - x spans the first to the last evaluation, padded by 2%;
      - no axis is forced to start at 0.
    - **Values outside the domain:**
      - a reference line outside the y-domain is drawn as an edge tag ("label ↑ above range"), not by stretching the axis;
      - uncounted values outside the domain become edge ticks, with their count in the caption.
    - **A log-y toggle** appears when the counted values span more than two decades.
    - **Height:** 260 px at desktop, 200 px at phone width. The plot area takes at least 85% of the panel. No empty bands.
    - **Best-so-far:** a step line, with its current value labelled at the right end (value, unit label, store/row) and linked.
    - **Dots:** every scored store's dots are drawn, so the best-so-far line is never left without the points that set it. The focus store is at full opacity, the others at 30%. Counted dots are filled; uncounted ones are hollow at 40%. Store identity is a mark shape; role is not encoded here.
  - **Below the chart:**
    - **the stage funnel** (spec 14 2.2), stages side by side, each with its own pass count, labelled with column names. A funnel is drawn ONLY when the study declares its stages (`funnel: [col, …]` in config.yaml). Without a declaration the 0/1 columns are independent flags, not ordered stages, so a cumulative funnel over them would be meaningless. The default is instead one compact, sortable table titled "0/1 columns": column, count of 1s, n, and a small bar. No column name is ever built into the viewer;
    - **the store** as a virtualised table with sortable columns, the column picker remembered per viewer. Columns are sized to their content (numbers right-aligned); the table may be narrower than its panel and is left-aligned, never stretched to fill it.
  - **Store switcher:** it highlights. The focus store's dots are at full opacity, and its flags/funnel and table are shown. It never hides a store's dots or the best-so-far line, which always span every scored store. Selecting a row focuses its store.
- **Deliverable.** The notebook rendered at a 75 ch prose measure, with figures and tables allowed to 1000 px. Math via KaTeX, vendored locally (no CDN at runtime). A "Re-execute" button (spec 14, 5.8) streams progress (`POST …/notebook/reexecute/stream`, NDJSON) into a log drawer and ends with a pass/fail mark.
  - **Headline:** the run's own, as the notebook prints it (`REPRODUCED:` / `CLAIMED_HEADLINE:`), unconverted and never mixed with the store's best row (Q1). A stored notebook that carries no outputs has none to show; the strip says so until a Re-execute passes. Each re-execution is saved as a new notebook plus a small JSON under `debug/viewer_reexec/` (the run's own notebook is never modified), and a "Stored · Last re-execution (time)" switch picks which is shown, defaulting to the last passing one; the headline is read from the notebook shown and labelled "From re-execution at <time>". Tables and figures take their own measure (up to 1000 px, scrolling inside the block if wider); header cells never break inside a word.
- **Logs.** A live tail in mono, with a source switch (Run log = `run.log`, Monitor = `diagnostics.jsonl`; other files join by adding a name to `readers._LOG_FILES`) and a pause button. Lines that carry an id link it (P4).
- **Setup.** The problem statement and config editors (spec 14, 3.1/3.2). Side-by-side diff before commit, a commit message field, and the study's history list (spec 14, 5.10).

### 4.4 Question banner

The style: `--select` border with a washed fill, and three parts:
- "Asked by strategizer · 4 min ago";
- the question in 14/500;
- an answer field with a Send button.

It shows on every view of that run, and as a dot on the run in the nav rail.

**Heartbeat coupling, a hard rule.** Polling `GET /api/runs/{rid}/operator` IS the human heartbeat: a run only waits for an answer while that endpoint is being polled. So:
- the front end polls it only while the page is visible (Page Visibility API);
- it stops when the tab is hidden or the run is closed;
- it never polls from a background or hidden tab.
A banner must not claim "waiting for you" when nobody is looking. Sending collapses it into a "Answered · undo for 10 s" toast. It's never a modal: the user may need to read the timeline to answer.

### 4.5 Inspector

Contextual to the selection:
- **Header:** id (20 px mono), role, status mark; a meta line with clock times, the source and output tokens.
- **Facts row:** 3 cells (duration · evals · cost) in the UI face.
- **Then:** Task (full) → Hypotheses it carries (linked) → Report (rendered markdown with math) → Files touched → actions (Open transcript · Files touched · Nudge).
- **Other selections:** a gate shows its review; a data row shows its inputs and outputs; a hypothesis shows its history.

### 4.6 Study home and dialogs

- **Study home:** a table of studies, not a card grid. Columns: name, last run (status mark plus time ago), figure of merit, runs, and a Start button. A row click opens the study.
- **Study page:** problem statement (rendered), the runs table, and a Start run button.
- **Start run:** a side sheet, not a modal over everything. It holds the pre-flight checklist (spec 14, 5.3) with a mark per check, the budget and model read from the committed config, and Start. Blocked checks say why and link to Setup.
- **Stop:** a confirm popover anchored to the button: "Stop gracefully (retrospectives run)" as primary, and "Kill now" behind a second confirm.

### 4.7 Phone (< 820 px): monitor first

- The title block stacks into a 2×2 vitals grid plus actions.
- Tabs scroll horizontally.
- The timeline becomes a single chronological list of cards (no slot columns, no ruler).
- The inspector opens full-screen with a back control.
- The question banner and the note box are reachable within one scroll of the top.

## 5. Interaction details

- **Hover** gives a border shift only (no lift). **Selected:** `--select` border plus a 1 px ring. **Focus:** a 2 px `--select` outline offset by 2 px, on every interactive element.
- **Keyboard:**
  - `j`/`k` step;
  - `/` find (a command palette over ids, files and hypotheses);
  - `g` then `t`/`h`/`d`/`l`/`s` switch views;
  - `n` opens the note box;
  - Esc closes the inspector or palette.
  A `?` overlay lists them.
- **Toasts:** bottom-left, a 4 s auto-dismiss, an undo where the action allows. Copy names the result: "Note sent to run 20261001T001224".
- **Empty states** say what will appear and when: "No evaluations yet. The data generator registers the oracle first; points appear here as they're evaluated."
- **Errors** say what failed and what to do next, never "Something went wrong".
- **Loading:** skeleton rows in `--sunken`. Never a spinner over existing content.

## 6. Copy rules

- Sentence case for everything except the 11 px field labels.
- **Buttons name the action exactly:** "Stop gracefully", not "OK".
- **Times:** elapsed as "+3 h 12"; durations as "1 h 27" / "7 m"; clock times with a timezone, in the inspector only.
- **Numbers:** a thin space before units in prose; a declared unit label right after the value; money as "$4.05".

## 6a. Problem-agnostic, a hard rule

The viewer serves ANY study. No study's vocabulary may appear in viewer code, templates, defaults, copy or spec examples: no column names, no units, no baselines, no physics. Everything study-specific comes from the study's config (`objective:`, `funnel:`, unit labels) or from the run's own records. With no declaration, the viewer shows the raw column names and nothing invented. Acceptance: a test greps the shipped viewer for a denylist built from every study in `studies/` and the benchmarks (column names, namespaces, units) and fails on a hit. The screenshot set includes the trivial example study alongside a study that declares an objective, so the undeclared path is seen.

## 7. Open questions (boss decides; raise them, don't guess)

- **Q1. Figure of merit.** The store's max feasible row can be a variant of the nominal design rather than the design itself (one zero-shot run: 19.8× from a variant row, while the run's own headline was 17.4× nominal). Proposal: the title block shows the store max, labelled "best row", and links to it. The deliverable view shows the run's own headline. Never mix them silently.
- **Q2. Run comparison** (spec 14 2.11): a second run overlaid on the Data chart in `--ink-3`, selectable from a "Compare with…" menu.

## 8. Acceptance

1. **Screenshots** with Playwright for every view at 1600×1000 and 400×900, light and dark: 24 images per change, before and after, attached to the commit message's EVIDENCE or the PR comment. The boss reviews each set before the next screen starts.
2. **A contrast script** checks every token text/ground pair (AA), run in the headless tests.
3. **Visual-bug checks** on each screenshot set:
   - no horizontal page scroll at 400 px;
   - no text overlapping a mark;
   - no truncated id (ids never ellipsize; intents may);
   - no literal colour outside the token block (a test greps the template for `#[0-9a-f]{3,6}` outside `:root`).
4. **Every control** in spec 14 Phase 5 is reachable by keyboard, with a visible focus ring.
5. **Build order:**
   1. shell + title block + Timeline + inspector (the prototype's scope);
   2. question banner;
   3. Data;
   4. Hypotheses;
   5. Deliverable;
   6. Logs;
   7. Setup and dialogs;
   8. phone pass.

   Each step lands as its own commit with its screenshot set.
