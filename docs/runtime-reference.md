# Runtime reference

This page lists every key of the `runtime:` block in a study's
`config.yaml`. For the other keys, see the
[Configuration reference](config-reference.md).

The `runtime:` block holds everything that tunes how the run executes, as
opposed to what it is asked to do. Every knob has a working default, so the
block is optional. Set a knob only when you have a reason to.

```yaml
runtime:
  debug: true            # write transcripts and diagnostics under runs/<ts>/debug/
  recursion_limit: 200   # LangGraph step ceiling for one run
```

`config.yaml` is the source of truth for these knobs. The environment sets no
knob. An `F3DASM_<KEY>` variable left exported in a shell stops the run at
start with an error that names it, so no arm runs under a label it does not
have. A key that this page does not list is ignored with a warning at startup,
so a typo tells you instead of silently reverting to the default.

| key | meaning | default |
|---|---|---|
| `context_window` | Tokens the backend accepts. `0` asks the server (Ollama's served `num_ctx`, vLLM's `max_model_len`). Set it to pin the value or to sweep it. The run records the resolved number and its source. | `0` |
| `context_policy` | How the run keeps one agent turn inside the served context window. It applies to OpenAI-compatible backends only, because the Claude SDK manages its own context. `compact` summarizes the middle of the conversation, so a delegation's result survives even when its prose does not. `trim` drops those messages instead: it is free and deterministic, but blind to what it discards. You cannot turn it off, because an unchecked context is the crash this setting exists to stop. | `compact` |
| `max_output_tokens` | Tokens that one model reply may generate. `0` derives the cap from the context window (a quarter of it, capped at 65536), the same share the trim reserves for the reply. `-1` removes the cap. The cap bounds a looping turn that would otherwise generate for hours on a large-window server. | `0` |
| `debug` | Capture full transcripts, diagnostics, and per-delegation logs under `runs/<ts>/debug/`. The run-analysis workflow requires it. | `false` |
| `recursion_limit` | LangGraph step ceiling for one run. | `2000` |
| `max_awake_nodes` | How many nodes may be awake at once, the strategizer included. The strategizer always holds one reserved slot, and workers share the rest. A delegation over the cap is reported as `QUEUED: too many nodes working (N/N)` and starts, first come first served, when a slot frees. A node that blocks in `Wait` on its own queued child, or whose report awaits review, hands its slot back meanwhile. A critic call runs inside its caller's slot. Memory scales with this knob: 5 awake nodes need `--mem >= 32G` on Slurm (run 20260928T225501 peaked at 16.76 of 16 GB with three awake). | `5` |
| `max_consecutive_errors` | Consecutive failures to one target before the run halts. | `12` |
| `run_backstop_multiple` | Multiple of the wall budget after which the run is asked to wind down and closes as `backstop_time`. The run is force-closed only if the wind-down overruns. | `2.0` |
| `delegate_cutoff_multiple` | Multiple of the wall budget past which the run refuses new delegations. In-flight delegations are never touched. It must stay below `run_backstop_multiple`, or it can never fire. `0` disables it. | `1.5` |
| `followup_wait_s` | How long a `FollowUp` waits for a human answer. | `600` |
| `stop_grace_s` | Only with the watchdog launcher: the seconds before its deadline at which it asks the run to wind down (a stop request in `debug/`) instead of waiting to stop it, so agents hand over real retrospectives. It is on unless you change it: unset means a tenth of the deadline, at most 900 s. The deadline does not move. It must be shorter than the deadline. `0` disables it. | `min(900, deadline/10)` |
| `resume_close_with_retrospectives` | On a resume whose process was lost (crash, SIGKILL, or out-of-memory), wind the run down at once instead of continuing it, so the entry node writes the retrospective that the crash cost it, including the time bullet. The run closes as `crashed`. The run logs workers that died in flight as missing (`RETROSPECTIVES_MISSING`, reason `process lost`). adda invents nothing for them. | `false` |
| `allow_arm_drift` | A resume under different ablation arms than the run started with is refused, because a run measured under two arms belongs to neither. Set this to accept the mix. The arms that the run started with stay recorded as `arms_initial` in `run_config.json`. | `false` |
| `peer_message_wait_s` | How long `SendMessage(wait_for_reply=True)` waits for a peer's reply before it returns. It applies only while `peer_interaction` is on. | `300` |
| `llm_retry_max` | Retry attempts for a failed model call. | `5` |
| `llm_retry_base` | Base seconds for the delay between retries. | `2.0` |
| `llm_stream_idle_timeout` | Seconds of stream silence before a call is abandoned. `0` disables it. | `600.0` |
| `llm_tool_idle_timeout` | Seconds that a single tool call may stall. `0` disables it. | `0.0` |
| `bash_timeout_s` | Seconds that a shell command may run before the run moves it to the background instead of blocking the agent. The agent can still pass its own `timeout`, capped at 600 s. The default is the same on every backend. | `120.0` |
| `llm_max_buffer_mb` | Cap on a single response buffered in memory. | `30.0` |
| `thinking_display` | `summarized` or `omitted`: whether Claude's thinking comes back as a readable summary (visible in transcripts and the viewer) or empty. Newer models default to `omitted`. Billing is identical either way. It applies only to models that support adaptive thinking (Opus and Sonnet 4.6 and later, and the 5.x families). Other models are untouched. | `summarized` |
| `llm_metadata_fetch` | Look up model metadata (context window, pricing) at startup. | `true` |
| `llm_metadata_timeout_s` | Seconds to wait for that lookup. | `8.0` |
| `llm_quantization` | Quantization hint for a locally served model. | none |

## Literature retrieval

These knobs matter only when the graph has a literature reviewer.

| key | meaning | default |
|---|---|---|
| `retrieval_mode` | Corpus ranking strategy: `auto` (RRF when BM25 and dense retrieval are available, else BM25, else substring), `hybrid`, `bm25`, or `substring`. Only `auto` degrades. An explicitly requested mode that cannot be satisfied raises an error instead of silently falling back to a different one. | `auto` |
| `citation_weighting` | Multiply BM25 scores by `1 + log10(citations+1)` before rank fusion. This is a popularity prior on the lexical side only, and it is untested. | `true` |
| `launch` | How the viewer starts a run for a study that has its own launcher, such as an `sbatch` wrapper. It is a mapping with `command` (argument list, run in the study directory), `id_pattern` (a regular expression with one group, matched against the launcher's output to capture the job id), `stop_command` (argument list that contains `{id}`, which needs `id_pattern`), and `timeout_s` (default 120). The viewer runs only these commands and stops only an id it captured. When absent, the viewer offers no launcher control. The viewer reads this key, not the run. | none |

## Ablation switches

Leave these alone unless you are running an experiment.

These are the switchable parts of the scaffolding. They exist to be measured,
not tuned. Every one defaults to on, and turning one off makes a normal run
worse by construction. They are the arms of an ablation, which asks whether a
piece of machinery earns its cost. An experiment sweeps them, and a study
author leaves them alone.

To run an arm without editing the committed study, pass it on the command line:

```bash
python -m adda studies/example_study --set hypothesis_ledger=false --set verdict_validator=false
```

You can repeat `--set`. `python -m adda.watchdog` takes the same flag and
forwards it. A `--set` outranks `config.yaml`, an unknown knob is an error, and
the value lands in `run_config.json` like any explicit knob. For a replicate
sweep, launch each replicate from a clean copy of the study: no `runs/`, no
`workspace/`, and no archived `pipeline_*.ipynb`. The literature notes under
`runs/lit_reviewer_notes` and the archives are study-scoped, and would
otherwise carry one arm's work into the next.

Each switch withholds everything it owns at once: its runtime object, the tools
that exist only because of it, and the prompt section that tells the agent to
use them. An agent in an arm is never left calling a tool that is gone. For how
that is wired, and how to declare a new switch, see
[Features](features.md).

`pipeline_deliverable` is the exception that is also an ordinary study choice.
A study with no notebook deliverable can legitimately turn it off.

| key | meaning | default |
|---|---|---|
| `hypothesis_ledger` | The run's falsifiable-hypothesis record. Off withholds its three tools and its prompt section, so the agent is never told to use a tool that is gone. The ablation is partial: the strategizer's method argues the Popperian workflow throughout, and that stays. | `true` |
| `milestones_enabled` | Run the process-milestone gate. | `true` |
| `science_monitor` | The runtime drift monitor. It flags evaluations that are missing from the ledger and rows without a stamp, and escalates repeats to the critic. | `true` |
| `f3dasm_api` | Let the implementer and the data generator look up the installed f3dasm's API (`ConsultF3dasm`): signatures, docstrings, and source, read off the package that the run executes against, so it cannot go stale. Off withholds the tool and the one prompt section that instructs its use. The CI-verified `<f3dasm_api>` excerpt stays, so the arm is the excerpt only, which is the state before the tool existed. | `true` |
| `verdict_validator` | An independent judge reviews each `HypothesisUpdate` verdict against the falsification charter and appends a concern when it disagrees. It is advisory: it annotates and never blocks. Off, `HypothesisUpdate` behaves as it did before the judge existed, with no judge call, no note, and no diagnostics. It judges nothing without the ledger, so `hypothesis_ledger: false` requires `verdict_validator: false` too. A run that leaves it on refuses to start. | `true` |
| `doe_playbook` | The implementer's design-of-experiments method prior: the space-filling recipe, the evaluation-budget arithmetic, and the surrogate-guided exploit loop. Off leaves the f3dasm API and the oracle contract intact, and the agent chooses its own method. | `true` |
| `pipeline_deliverable` | Require `pipeline.ipynb` as the deliverable. Turn it off for a study with no notebook. | `true` |
| `reproduction_gate` | Enforce the reproduction gate in `Done()`. Before a run can close as GATED, the deliverable must reproduce lazily against the canonical store. It must add zero evaluations and modify no rows. It requires `pipeline_deliverable`: that knob decides whether a notebook is required at all, and this one decides whether an authored notebook must also prove that it reproduces. A study with `pipeline_deliverable: false` must set `reproduction_gate: false` too, or the run refuses to start. | `true` |
| `peer_interaction` | The `SendMessage` peer and human messaging tool. Every delegation report opens for the delegator's review instead of finalizing on delivery, which replaces the old `Confer`, `Reply`, worker `FollowUp`, and `ReportProgress` surface. Off is the no-peer-messaging arm: there is no `SendMessage`, reports finalize on delivery, and only the entry node keeps a human channel (`FollowUp`). | `true` |
| `budget_notes` | The in-band budget text that an agent reads: the per-turn constraint snapshot, the budget warnings and wrap-up ladder, and the snapshot on a delegation report and on a worker's task message. Budgets stay soft, and the cost backstop and the critic's constraints are unchanged. | `true` |
| `delegation_contract` | The `<delegation_contract>` rules in every worker's preamble: numbers come from tool output, report failures, and do not extend the task. | `true` |
| `reprompt_unfinished` | The bounded re-prompt (up to 3) after a turn ends without an accepted `Done()`, and the UNGATED banner on the run summary. Off, the run ends at the first such turn. | `true` |

Every run records the arms it ran under, defaults included, in `run_config.json`
(`arms`), in `run_status.json`, and in the `arm_*` columns of
`studies/run_ledger.csv`. The `runtime` entry there lists only the knobs that
somebody set, so read `arms` to tell an all-defaults baseline from a run that
nobody labelled.
