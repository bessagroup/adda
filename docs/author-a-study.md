# Authoring a study

A **study** is the single input to a run. It's a folder you prepare; adda reads
it, does the work, and writes its results back into the same folder:

```python
from adda import AgenticRun
AgenticRun(study_dir="my_study").execute()
```

This page shows what to put in that folder.

## What the folder contains

```
my_study/
  PROBLEM_STATEMENT.md   # required: the brief the agents work from
  config.yaml            # optional: the model, budgets, and how to evaluate a design
  workspace/             # optional: your evaluator, if you ship one
    evaluator.py
```

Everything else you'll see later (`pipeline.ipynb`, a `runs/` directory) is
**produced by the run**: you don't write it.

## `PROBLEM_STATEMENT.md` (required)

This is the whole task definition. The agents work from it and the critic checks
the result against it, so write it precisely: a vague brief produces a vague,
unverifiable answer. Cover:

- **Objective and success criteria**: the headline number or claim the run must
  deliver (for example, "maximise the normalised buckling load; report the design and its
  value").
- **Design space**: every input variable, with its bounds, type (continuous,
  integer, or categorical), and **units**.
- **Deliverables**: anything the run must produce beyond the notebook (a plot, a
  mechanism explanation).
- **What "valid" means**: feasibility limits, regimes of validity, noise
  thresholds. State these explicitly; they're what keep the agents honest.

## `config.yaml` (optional)

Every setting has a default, so you can omit this file entirely. The
[config reference](config-reference.md) lists every key, its meaning, and its
default. The keys you set most often are:

```yaml
model: claude-haiku-4-5-20251001   # which language model to use
eval_budget: 200                   # soft cap on real evaluations
budget: "01:00:00"                 # wall-clock limit; the run hard-stops at 100%
```

Put settings for your own scripts under `study:`, which adda never reads.

## How designs get evaluated (the evaluator)

The evaluator is your ground truth: the function that scores a design. It's the
one metered path: the only calls counted against `eval_budget`. (Surrogates and
optimisers the agents build on top are their own business and aren't metered.)
Declare it one of these ways:

**A function you ship**, one argument per input variable:

```yaml
evaluator:
  entrypoint: "workspace/evaluator.py:evaluate"   # path within the study folder
  output_names: [y]                               # names of what it returns
```

**A precomputed table**, each query resolves to the nearest row (results are
approximate, since a query may land between rows):

```yaml
evaluator:
  lookup:
    pool: "experiment_data"     # path within the study folder
    input_columns: [x1, x2]
    output_columns: [y]
```

**Described in the brief**: omit the `evaluator` block and describe the scoring
oracle (a binary, a dataset, a physics model) in `PROBLEM_STATEMENT.md`; the
agents write the evaluator themselves during the run.

**Nothing**: no evaluator and none described falls back to the honor system, and the
agents self-report. Fine for exploring, not for a result you want verified.

## Declaring the objective

Say what "best" and "feasible" mean, once, and the run ledger and the viewer's
figure of merit both use it. Nothing is inferred from column names.

```yaml
objective:
  column: score
  direction: max          # max | min
  feasible: feasible      # optional: a 0/1 output column
  lines:                  # optional: labelled reference lines on the Data chart
    - {value: 2.0,  label: "reference"}
    - {value: 20.0, label: "goal"}
  unit_label: {divide_by: 2.0, label: "x reference"}   # optional: display scaling
```

A design counts only if its `column` value is finite and, when `feasible` is
given, that column is 1; a finite value on an infeasible design does not count.
`column` and `feasible` must be outputs the evaluator declares (`output_names`
or the `output_columns` of the lookup); an unknown name refuses the run at start.
When the evaluator is written during the run, the start check is skipped; each
oracle later registered without a declared column is reported to the delegating
agent and recorded as an `OBJECTIVE_COLUMN_MISSING` diagnostics event.
`lines` are drawn on the viewer's best-so-far chart as dashed, labelled rules;
`value` is in the objective column's own units, `label` is any non-empty text,
and no line is drawn unless you declare it. `unit_label` only changes how the
viewer displays the axis, lines and values (raw value divided by `divide_by`,
shown with `label`); the store and the ledger stay in raw units.
The objective is scored on every store that records both declared columns: the
canonical store and each namespace. Results are therefore comparable across
oracle families, and the best row names the store it came from. A store missing
a declared column is listed as "not scored" rather than dropped.

Without the block the ledger records `objective: undeclared`, judges rows by
the finite rule alone, and reports the running min and max instead of a best.

The viewer's stage funnel counts how many designs survive each 0/1 output
column in turn. Declare the stages, in order, at the top level of config.yaml:

```yaml
funnel: [valid, simulated, converged]
```

Each must be an output the evaluator declares. With no `funnel:` the viewer
draws no funnel: the 0/1 columns are listed as independent flags (count of ones
per column) in a compact table, because they are not ordered stages unless you say so.

## What the run produces

At the study root you get **`pipeline.ipynb`**, the deliverable. Its opening
cells are the write-up; its code cells reproduce the headline result. Each run
also writes a timestamped folder under `runs/` with the evaluation record, logs,
and a status file. See [Understanding a run's output](read-a-runs-output.md) for what's
in there and how to read it.

Everything on this page so far is `config.yaml` and `PROBLEM_STATEMENT.md`; the graph itself
(which agents exist, how they delegate), each agent's system prompt, and each
agent's backend/model are Python-level extension points instead. See
[Customize agents and tools](customize-agents-and-tools.md) if the built-in strategizer and
four specialists aren't the shape your problem needs.

## Before a long run, check

- `PROBLEM_STATEMENT.md` states explicit success criteria, the design space
  (bounds, types, units), and any deliverables.
- `config.yaml` parses, and its `evaluator` points at a real file/attribute or a
  real lookup pool.
- Your evaluator imports and runs on one sample without error.
- Your backend is reachable (for the default Claude backend, you're logged in;
  see [Installation](install.md)).

## A minimal worked example

A runnable copy of this folder ships in the repository at `studies/example_study`
(it's exercised by the test suite, so it can't drift from what the runtime
actually expects).

`config.yaml`:

```yaml
model: claude-haiku-4-5-20251001
backend: claude
eval_budget: 200  # soft cap on real calls to evaluate(), not a hard stop
evaluator:
  entrypoint: "workspace/evaluator.py:evaluate"  # module:function, relative to the study
  output_names: [y]  # names the one value evaluate() returns
```

`workspace/evaluator.py`:

```python
def evaluate(x1: float, x2: float) -> float:
    """One argument per input; returns the output named in output_names."""
    return (x1 - 1.0) ** 2 + (x2 + 2.0) ** 2  # the ground truth being scored
```

`PROBLEM_STATEMENT.md`:

```markdown
# Minimise a 2-D quadratic
Objective: minimise y = (x1-1)^2 + (x2+2)^2.
Success: report the argmin (x1*, x2*) and the value y*, reproduced in
pipeline.ipynb from the run's evaluation record.
Design space: x1, x2, continuous, in [-5, 5], dimensionless.
```

Then run it:

```python
from adda import AgenticRun
AgenticRun(study_dir="studies/example_study").execute()
```

## Launching under a watchdog

`budget` and its wind-down (`wind_down_at`, described earlier) are both checked from *inside*
the run, so neither can help if the run genuinely wedges—a hung model call,
a stuck simulation—and never reaches its own next check. For that, launch
the study as a child process under an external watchdog instead: it owns the
wall-clock deadline from outside, and force-kills the whole run (every
process it spawned, not just the top one) if the deadline passes.

```bash
python -m adda.watchdog studies/example_study --budget 00:45:00
```

The deadline is twice whatever budget you give it (or `config.yaml`'s own
`budget:` if you don't pass `--budget`)—plenty of headroom, since this is a
last-resort cutoff for a hang, not a way to police a slow run. The run's own wind-down at 100% of the budget comes first; the watchdog acts only if the run is still alive at 200%. `python -m
adda <study-dir>` on its own still works exactly as before; this is an
additional, safer way to launch the same run when you want a hard outer
backstop.

Stopping the watchdog (Ctrl-C, or `kill` on its PID) takes the run down with
it: the whole process tree is reaped and the watchdog exits `130` (SIGINT) or
`143` (SIGTERM). A run is never left running with nothing watching it.
