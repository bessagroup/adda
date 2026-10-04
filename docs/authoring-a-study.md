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
  deliver (e.g. "maximise the normalised buckling load; report the design and its
  value").
- **Design space**: every input variable, with its bounds, type (continuous,
  integer, or categorical), and **units**.
- **Deliverables**: anything the run must produce beyond the notebook (a plot, a
  mechanism explanation).
- **What "valid" means**: feasibility limits, regimes of validity, noise
  thresholds. State these explicitly; they're what keep the agents honest.

## `config.yaml` (optional)

Every setting has a default, so you can omit this file entirely. The settings you
can set:

| key | meaning | default |
|---|---|---|
| `model` | which language model to use | the backend's default |
| `backend` | `claude`, `ollama`, `openrouter`, or `vllm` | `claude` |
| `budget` | soft wall-clock limit, `"HH:MM:SS"` or seconds (a nudge, not a hard stop) | none |
| `eval_budget` | soft cap on how many real evaluations the run may spend | none |
| `budget_usd` | **hard** cost ceiling — halts the run when spend reaches it (resumable: raise it and resume). Inactive on a backend with no per-call cost data (e.g. Ollama) | none |
| `required_deliverables` | extra files that must exist before the run can finish | none |
| `evaluator` | how a design gets scored, see below | honor-system |

| `runtime` | run knobs — debug capture, timeouts, retry, limits; see below | all defaulted |

See [Customizing a run](customizing-a-run.md#reference-the-available-backends) for the
`backend`/`model` details, including setting them per agent instead of for
the whole run.

### `runtime:` — the run knobs

Everything that tunes *how the run executes* rather than *what it is asked to
do* lives in one nested block. Every knob has a working default, so the block
is optional; set one only when you have a reason to.

```yaml
runtime:
  debug: true            # write transcripts/diagnostics under runs/<ts>/debug/
  recursion_limit: 200   # LangGraph step ceiling for one run
```

`config.yaml` is the source of truth for these. An `F3DASM_<KEY>` environment
variable overrides the configured value, but that channel is for secrets and
one-off overrides — it is not where a study's settings belong. A key this
table does not list is ignored with a warning at startup, so a typo tells you
rather than silently reverting to the default.

| key | meaning | default |
|---|---|---|
| `context_window` | tokens the backend will accept. `0` asks the server (Ollama's served `num_ctx`, vLLM's `max_model_len`); set it to pin the value or to sweep it. The resolved number AND its source are recorded | `0` |
| `context_policy` | how one agent turn is kept inside the served context window (OpenAI-compatible backends only — the Claude SDK does its own and does it better). `compact` summarises the middle of the conversation, so a delegation's RESULT survives even when its prose does not; `trim` drops those messages instead — free and deterministic, but blind to what it discards. There is no "off": an unmanaged context is the crash this exists to stop | `compact` |
| `max_output_tokens` | tokens ONE model reply may generate. `0` derives it from the context window (a quarter of it, capped at 65536) — the same share the trim reserves for the reply; `-1` removes the cap. Bounds a looping turn that would otherwise generate for hours on a large-window server | `0` |
| `debug` | capture full transcripts, diagnostics and per-delegation logs under `runs/<ts>/debug/`. Required for the run-analysis workflow | `false` |
| `recursion_limit` | LangGraph step ceiling for one run | `2000` |
| `max_awake_nodes` | how many nodes may be awake at once, the strategizer included (it always holds one reserved slot; workers share the rest). A delegation over the cap is reported `QUEUED: too many nodes working (N/N)` and starts, first come first served, when a slot frees. A node that blocks in `Wait` on its own queued child, or whose report awaits review, hands its slot back meanwhile. A critic call runs inside its caller's slot. **Memory scales with it: 5 awake nodes needs `--mem >= 32G` on Slurm** (run 20260928T225501 peaked at 16.76 of 16 GB with three awake) | `5` |
| `max_consecutive_errors` | consecutive failures to one target before the run halts | `12` |
| `run_backstop_multiple` | multiple of the wall budget after which the run is asked to wind down and closes `backstop_time` (force-closed only if the wind-down overruns) | `2.0` |
| `delegate_cutoff_multiple` | multiple of the wall budget past which NEW delegations are refused (in-flight ones are never touched); must stay below `run_backstop_multiple` or it can never fire. `0` disables | `1.5` |
| `followup_wait_s` | how long a `FollowUp` waits for a human answer | `600` |
| `stop_grace_s` | only with the watchdog launcher: seconds BEFORE its deadline at which it asks the run to wind down (a stop request in `debug/`) instead of waiting to kill it, so agents hand over real retrospectives. On unless you change it: unset means a tenth of the deadline, at most 900 s. The deadline does not move. Must be shorter than the deadline; `0` disables | `min(900, deadline/10)` |
| `resume_close_with_retrospectives` | on a resume whose process was lost (crash, SIGKILL, OOM), wind the run down at once instead of continuing it, so the entry node gives the retrospective the crash cost it (TIME bullet included); closes `crashed`. Workers that died in flight are logged as missing (`RETROSPECTIVES_MISSING`, "process lost"); nothing is invented for them | `false` |
| `allow_arm_drift` | a resume under different ablation arms than the run started with is refused, since a run measured under two arms belongs to neither. Set this to accept the mix; the arms the run started with stay recorded as `arms_initial` in `run_config.json` | `false` |
| `peer_message_wait_s` | how long `SendMessage(wait_for_reply=True)` waits for a peer's reply before returning (spec 12, behind `peer_interaction`) | `300` |
| `llm_retry_max` | retry attempts for a failed model call | `5` |
| `llm_retry_base` | base seconds for retry backoff | `2.0` |
| `llm_stream_idle_timeout` | seconds of stream silence before a call is abandoned (`0` disables) | `600.0` |
| `llm_tool_idle_timeout` | seconds a single tool call may stall (`0` disables) | `0.0` |
| `llm_max_buffer_mb` | cap on a single response buffered in memory | `30.0` |
| `thinking_display` | `summarized` or `omitted`: whether Claude's thinking comes back as a readable summary (visible in transcripts and the viewer) or empty. Newer models default to `omitted`. Billing is identical either way. Applies only to models that support adaptive thinking (Opus/Sonnet 4.6+ and the 5.x families); others are untouched | `summarized` |
| `llm_metadata_fetch` | look up model metadata (context window, pricing) at startup | `true` |
| `llm_metadata_timeout_s` | seconds to wait for that lookup | `8.0` |
| `llm_quantization` | quantization hint for a locally-served model | none |

**Literature retrieval.** Only relevant when the graph has a literature
reviewer.

| key | meaning | default |
|---|---|---|
| `retrieval_mode` | corpus ranking strategy: `auto` (RRF when BM25+dense are available, else BM25, else substring), `hybrid`, `bm25`, `substring`. Only `auto` degrades — an explicitly requested mode that cannot be satisfied errors rather than silently falling back to a different one | `auto` |
| `citation_weighting` | multiply BM25 scores by `1 + log10(citations+1)` before rank fusion. A popularity prior on the lexical side only; untested | `true` |
| `semantic_scholar_api_key` | Semantic Scholar key; raises the literature rate limit | none |

#### Ablation switches — leave these alone unless you are running an experiment

These are the switchable parts of the scaffolding, and they are here to be
*measured*, not tuned: every one defaults to on, and turning one off makes a
normal run worse by construction. They are the arms of an ablation — the
question "does this machinery earn its cost" — so an experiment sweeps them
and a study author leaves them alone.

To run an arm without editing the committed study, pass it on the command line: `python -m adda studies/example_study --set hypothesis_ledger=false` (repeatable; `python -m adda.watchdog` takes the same flag and forwards it). A `--set` outranks the environment and `config.yaml`, an unknown knob is an error, and the value lands in `run_config.json` like any explicit knob. For a replicate sweep, launch each replicate from a clean copy of the study (no `runs/`, no `workspace/`, no archived `pipeline_*.ipynb`), because the literature notes under `runs/lit_reviewer_notes` and the archives are study-scoped and would otherwise carry one arm's work into the next.

Each one withholds everything it owns at once: its runtime object, the tools
that exist only because of it, and the prompt section that tells the agent to
use them. An agent in an arm is never left calling a tool that is gone. How
that is wired — and how to declare a new one — is in
[Customizing a run](customizing-a-run.md#turning-a-piece-of-the-scaffolding-off-feature).

(`pipeline_deliverable` is the exception that is also an ordinary study
choice: a study with no notebook deliverable legitimately turns it off.)

| key | meaning | default |
|---|---|---|
| `hypothesis_ledger` | the run's falsifiable-hypothesis record. Off withholds its five tools and its prompt section too, so the agent is never told to use a tool that is gone. PARTIAL: the Popperian workflow is argued throughout the strategizer's method, which stays | `true` |
| `milestones_enabled` | run the process-milestone gate | `true` |
| `science_monitor` | the runtime drift monitor that flags unledgered evals and unstamped rows, and escalates repeats to the critic | `true` |
| `f3dasm_api` | let the implementer and datagenerator look up the INSTALLED f3dasm's API (`ConsultF3dasm`): signatures, docstrings and source, read off the package the run actually executes against, so it cannot go stale. Off withholds the tool and the one prompt section that instructs its use; the CI-verified `<f3dasm_api>` excerpt stays, so the arm is "excerpt only" — the state before the tool existed | `true` |
| `verdict_validator` | an independent judge reviews each `HypothesisUpdate` verdict against the falsification charter and appends a concern when it disagrees. Advisory — it annotates, never blocks. Off, `HypothesisUpdate` behaves as it did before the judge existed: no judge call, no note, no diagnostics | `true` |
| `doe_playbook` | the implementer's DoE method prior: the space-filling recipe, the eval-budget arithmetic and the surrogate-guided exploit loop. Off leaves the f3dasm API and the oracle contract intact and makes the agent choose its own method | `true` |
| `pipeline_deliverable` | require `pipeline.ipynb` as the deliverable; turn off for a study with no notebook | `true` |
| `reproduction_gate` | enforce Done()'s reproduction gate: before a run can close GATED, the deliverable must reproduce lazily against the canonical store (zero new evals, no modified rows). Independent of `pipeline_deliverable` — that knob decides whether a notebook is required at all; this one decides whether an authored notebook must additionally prove it reproduces | `true` |
| `peer_interaction` | the `SendMessage` peer/human messaging tool (spec 12): every delegation report opens for the delegator's review instead of finalizing on delivery, and `Confer`/`Reply`/the peer-facing `FollowUp`/`ReportProgress` retire in its favour. Off restores that old surface exactly, as an ablation arm | `true` |

Every run records the arms it ran under, defaults included, in `run_config.json` (`arms`), in `run_status.json` and in the `arm_*` columns of `studies/run_ledger.csv`; `runtime` there lists only the knobs somebody set, so read `arms` to tell an all-defaults baseline from a run nobody labelled.

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

## What the run produces

At the study root you get **`pipeline.ipynb`**, the deliverable. Its opening
cells are the write-up; its code cells reproduce the headline result. Each run
also writes a timestamped folder under `runs/` with the evaluation record, logs,
and a status file. See [Understanding a run's output](reading-a-run.md) for what's
in there and how to read it.

Everything above is `config.yaml` and `PROBLEM_STATEMENT.md`; the graph itself
(which agents exist, how they delegate), each agent's system prompt, and each
agent's backend/model are Python-level extension points instead. See
[Customizing a run](customizing-a-run.md) if the built-in strategizer and
four specialists aren't the shape your problem needs.

## Before a long run, check

- `PROBLEM_STATEMENT.md` states explicit success criteria, the design space
  (bounds, types, units), and any deliverables.
- `config.yaml` parses, and its `evaluator` points at a real file/attribute or a
  real lookup pool.
- Your evaluator imports and runs on one sample without error.
- Your backend is reachable (for the default Claude backend, you're logged in;
  see [Installation](installation.md)).

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

`budget` and `run_backstop_multiple` (above) are both checked from *inside*
the run, so neither can help if the run genuinely wedges — a hung model call,
a stuck simulation — and never reaches its own next check. For that, launch
the study as a child process under an external watchdog instead: it owns the
wall-clock deadline from outside, and force-kills the whole run (every
process it spawned, not just the top one) if the deadline passes.

```bash
python -m adda.watchdog studies/example_study --budget 00:45:00
```

The deadline is twice whatever budget you give it (or `config.yaml`'s own
`budget:` if you don't pass `--budget`) — plenty of headroom, since this is a
last-resort kill switch for a hang, not a way to police a slow run. `python -m
adda <study-dir>` on its own still works exactly as before; this is an
additional, safer way to launch the same run when you want a hard outer
backstop.

Stopping the watchdog (Ctrl-C, or `kill` on its PID) takes the run down with
it: the whole process tree is reaped and the watchdog exits `130` (SIGINT) or
`143` (SIGTERM). A run is never left running with nothing watching it.
