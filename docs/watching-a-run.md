# Watching a run

A run is autonomous, not silent. While it works you can see what every agent
is doing, steer it, and answer the questions it asks. This page covers the
live viewer and the ways the run reaches you.

Neither is required. A run with nobody watching behaves the same, and a
question nobody answers times out, after which the agent proceeds on its own
judgment.

## Start the viewer

The viewer is a web page that reads a study's `runs/` directory. It ships as
an optional extra.

```bash
pip install "adda[viewer]"
python -m adda.viewer my_study
```

Open the `http://…/session?token=…` URL it prints. Anyone who can reach the
port can read the runs. Only a browser that opened that URL once (it sets a
cookie) can write: answer a question, send a note, start or stop a run, commit
a setup edit. Another web page cannot write on your behalf. Without the cookie
the page is read-only and says so.

The viewer binds the loopback interface only, because it can start and kill
runs. To serve it on a network, pass `--allow-network`. Do this only on a
network you trust.

```bash
python -m adda.viewer my_study --host 0.0.0.0 --port 9000 --allow-network
```

The viewer is a separate process from the run, so start it in its own
terminal. It reads each run's `debug/` directory. It works the same on a
finished run, without the streaming.

If you already have the `AgenticRun` object, serve it from Python:

```python
run = AgenticRun(study_dir="my_study")
run.serve_viewer()   # blocking; run it in a separate process from execute()
```

`serve_viewer()` passes the viewer this run's live `Graph` object. The command
line has no in-memory graph, so it recovers one from the study's own
`build_graph()` and falls back to the default graph.

## The page

The left rail lists the study's runs, newest first, each with its status:
running, gated, stopped, halted, crashed or unapproved. A dot on a run marks a
question that waits for you.

The header shows the vitals of the selected run:

- **Wall clock** against the budget in `config.yaml`. The bar spans twice the
  budget, with marks at 1×, 1.5× (no new delegations) and 2× (stop).
- **Cost**, summed over metered calls. A call with no price is counted
  separately ("+3 calls without cost data"), because an unknown price is not a
  free call.
- **Delegations**, done and running.
- **Best row**, the best counted row by the study's declared
  [objective](authoring-a-study.md#declaring-the-objective). It is not the
  run's headline claim. Click it to open the row. Without a declared
  objective the field shows a dash.

Below the header, six views share one pane. Selecting a delegation,
hypothesis or data row opens an inspector beside the pane. **Esc** closes it.
The address bar follows your selection, so you can share a link to a view or
an item.

### Timeline

The timeline draws the run from top to bottom.

- Each **card** is one delegation, on a column for its worker slot. The number
  of columns is the run's real peak concurrency. A card shows the role, the
  duration and, when it is tall enough, the task, a `falsify` chip for a
  falsification attempt, the hypotheses it carries and its evaluation count.
- A **hatched strip** beside a card is the time the delegation waited for a
  free slot.
- A **gate mark** is an acceptance review of the deliverable, with its
  verdict. A mid-run critic audit appears as a card.
- A **band** replaces any stretch of 30 minutes or more with no delegation
  running.
- **Follow live** keeps the view at the newest card while the run is open.

The inspector for a delegation shows its cost, output tokens and evaluations,
the task, the report it returned, the hypotheses it carries, the critic's
review and the files it changed.

### Hypotheses

The view lists each hypothesis the strategizer stated, with its verdict, its
belief bar and the delegations that tested it. Filter by all, open, closed or
retracted. A retraction is a verdict the strategizer later withdrew.

The inspector shows the statement, the falsification criterion, the
prediction and the status history. Each entry in the history carries its
belief, its evidence and, when a validator weakened or rejected a verdict, the
validator's note.

### Data

The view shows the oracle's store: every real evaluation, as it is recorded.

- **Best so far** plots the best counted value against the evaluation number
  or against elapsed time. You choose the x axis. The y axis is linear. When
  every counted value is positive and they span two or more decades, a log
  option appears, and it is the default when the best-so-far line spans two
  decades. A dot is filled when its row counts and hollow when it does not.
  Points outside the range are ticks at the edge. Hover a dot for its values
  and click it to open the row.
- The chart needs an `objective:` block in `config.yaml`. Without one, the
  view says so and draws nothing, because the viewer does not choose a
  direction for you. Reference lines and the display unit come from the same
  block.
- **Stage funnel** counts the designs that survive each 0/1 output you list
  under `funnel:`. Without that list the view shows the 0/1 columns as
  independent flags.
- **Store** is a sortable table of the rows. The column picker hides or shows
  columns, and the choice persists in your browser. When the run has several
  namespaces, buttons switch the store in focus.

The inspector for a row shows its objective value, why it does or does not
count, the delegation that produced it, and its inputs, outputs and notes.

### Deliverable

The view renders `pipeline.ipynb` as a reader sees it, with the headline
result first. **Show code** reveals the cells' code. The row of buttons
downloads the notebook, the store tables (as a zip) or the whole `debug/`
folder (as a zip).

**Re-execute** runs the notebook again against a copy of the run's ledger and
streams the log into a drawer, then marks the result passed or failed. After a
re-execution, a switch shows either the stored notebook or the last
re-execution. A run that never wrote a notebook shows nothing here, and that
is itself a finding about the run.

### Logs

The view tails one file at a time, live while the run is open. **Pause** stops
the tail. The sources are:

- **Run log**, the run's own log.
- **Monitor**, the science monitor's diagnostics.
- **Watchdog**, only for a run this viewer started.
- **Tool calls**, one line per tool call with its agent and outcome. It needs
  the run's transcripts, so it is empty on a run that recorded none.
- **Delegation output**, a menu of each delegation's stdout log.

### Setup

The view edits the study's two input files, `PROBLEM_STATEMENT.md` and
`config.yaml`, in a text buffer. It shows a diff against the committed file,
and for `config.yaml` it validates the file as you type. A run sees only what
is committed, so **Commit** writes the buffer to git with the message you
give, and it commits that one file and nothing else. The history below lists
each commit that touched the file, and you can open one for its diff.

The viewer refuses a commit when `config.yaml` has validation errors, when the
message is empty, when the committed file changed since you opened the editor,
or when other files in the study have uncommitted changes.

## Start, stop and steer a run

All of these need the `/session?token=…` cookie.

### Start a run

For a closed run, **Re-run study** opens the start sheet. The sheet shows the
model and the budget, read from the committed `config.yaml`, and warns when
either file has edits that are not committed. It then runs the pre-flight
checks. Each check is *pass*, *blocked* or *your call*: a blocked check stops
the start, and a check that needs a judgment, such as whether the problem
statement states the success criteria, is left to you. **Start run** launches
the run under the watchdog, in the same way as `python -m adda.watchdog`.

A study with a `launch:` block in `config.yaml` starts through its own
launcher instead, and the sheet shows the launcher's output and the captured
job id.

### Stop a run

**Stop** opens a popover. **Stop gracefully** asks the run to finish its
current step, write its retrospectives and close. **Kill now** ends the run's
processes at once, writes no retrospectives and leaves the run unclosed. Kill
is offered only for a run this viewer started.

### Send a note

**Note to run** queues a note: a correction, a constraint you forgot to write
down, a "stop chasing that branch". By default it goes to the entry node. You
can address it to one running delegation instead. The agent reads the note on
its next tool call, marked as coming from the operator rather than from a
tool. Notes are one way and asynchronous: you cannot know when the agent reads
one, only that it will, and the note lands in the run record.

### Answer a question

The entry node can ask the operator one clarifying question per delegation,
with `FollowUp`. When it does, the run **blocks** until it gets an answer or
times out. The timeout is `runtime: followup_wait_s`, ten minutes by default.
After the timeout the agent proceeds with its best judgment, and an unanswered
question is not a failure.

A banner at the top of the viewer shows the question and who asked it. When you
send an answer, the viewer holds it for 10 seconds with an **Undo** button, then
posts it. You can answer from either place, whichever gets there first:

- **the terminal**, if the run is attached to a TTY: type the answer;
- **the viewer**, through the banner.

Both write to the same on-disk channel in the run's `debug/` directory, so what
was asked, what was answered and what went unanswered all end up in the run
record.

A headless run is never deaf, because the viewer still reaches it. With no TTY
and no viewer, every `FollowUp` waits out its timeout. To turn the prompting off
for an unattended run, set `interactive=False` on `AgenticRun`.

The viewer also sends a heartbeat that says a human is looking, and the run uses
it to tell "someone is about to answer" from "this is a stall".

## Other controls

- **New study** creates a study next to this one, blank or copied from an
  existing study, and commits it with the message you give. It prints the
  command that opens the new study in a viewer.
- **Docs** asks adda's documentation a question, the same lookup the
  `adda-docs` command runs. **Source** adds the source of the match.
- **Keys.** Press **?** for the list: `j` and `k` move through delegations,
  `g` then `t`, `h`, `d`, `l` or `s` switches view, and **Esc** closes the
  inspector.

## Agents talking to each other

You will see this in transcripts, so it helps to know. Agents also message each
other mid-flight with `SendMessage`. The strategizer uses it to steer a
delegation that is already running, instead of waiting for a wrong result and
re-delegating. A running delegation gets the message prefixed onto its next
tool result. An idle node's message waits until that node next checks in.

You do not drive this. It explains messages in a transcript that the agent
never asked for.

## Next

- [Understanding a run's output](reading-a-run.md): what to read once a run
  finishes.
- [How a run is kept honest](how-a-run-is-kept-honest.md): the checks a run has
  to pass before it can close.
