# Configuration reference

This page lists every key you can set in a study's `config.yaml`. Every key
has a default, so you can omit the file entirely. For how to write a study, see
[Author a study](author-a-study.md).

adda reads exactly the keys on this page. An unknown top-level key stops the
run at start with an error that suggests the closest known key, so a typo
cannot pass silently. Put your own settings under `study:`, which adda never
reads.

## Top-level keys

| key | meaning | default |
|---|---|---|
| `model` | The language model to use. | The backend's default. |
| `backend` | `claude`, `ollama`, `openrouter`, or `vllm`. | `claude` |
| `budget` | Wall-clock limit, as `"HH:MM:SS"` or seconds. | none |
| `token_budget` | A positive integer of generated tokens, summed over every agent, the critic and the verdict checks among them. Input tokens do not count. A backend that reports no output tokens stops the run with an error; adda never estimates. Use it to give two models the same budget regardless of GPU speed or queue time. | none |
| `eval_budget` | How many real evaluations the run should spend, counted as the rows in the canonical store. It only sends notices; it never stops the run or refuses an evaluation. | none |
| `watchdog_wall_s` | The wall-clock limit for `python -m adda.watchdog`, as `"HH:MM:SS"` or seconds. Use it when the run has no `budget`, because there is then no wall budget to take a multiple of. Setting it next to a `budget` is an error. | none |

The wall and token budgets (`budget` and `token_budget`) are enforced together.
Each has a progress fraction, and the schedule follows the one that is
furthest along: notices from 75%, no new delegations at 90%, and the wind-down
at 100%, when the first of them reaches it (`runtime:` keys in the
[runtime reference](runtime-reference.md)). `run_status.json` records which
budget set the stop in `budget_trigger` (`wall` or `tokens`). The evaluation
budget only sends notices, from 75% and then every 5%, with the evaluations
left. It does not count toward the cutoff or the wind-down. The old
`budget_clock` key was removed; a config that has it is rejected.
| `budget_usd` | **Hard** cost ceiling. The run halts when spend reaches it, and you can raise it and resume. Inactive on a backend with no per-call cost data, such as Ollama. | none |
| `required_deliverables` | Extra files that must exist before the run can finish. | none |
| `evaluator` | How a design gets scored. See [How designs get evaluated](author-a-study.md#how-designs-get-evaluated-the-evaluator). | Honor system. |
| `objective` | Which output is optimized, in which direction, and which 0/1 column marks a design feasible. See [Declaring the objective](author-a-study.md#declaring-the-objective). | Undeclared. |
| `funnel` | The 0/1 outputs that the viewer's stage funnel counts, in order. See [Declaring the objective](author-a-study.md#declaring-the-objective). | none |
| `training_data` | A precomputed pool used only as training data, with no live oracle. Use it for a surrogate-only study. | none |
| `base_url` | The model server's endpoint, on a backend that has one. See [Use a different model or backend](use-a-different-model-or-backend.md#openai-compatible-endpoints-openrouter-vllm-others). | The backend's default. |
| `nodes` | Per-agent `tools`, `model`, `backend`, and `base_url`. See [Customize agents and tools](customize-agents-and-tools.md#changing-one-agents-tools-nodes-in-configyaml). | Each agent's own. |
| `llm_slurm` | Serve the model on a Slurm GPU allocation. See [Use a different model or backend](use-a-different-model-or-backend.md#a-local-model-on-a-slurm-gpu-node-vllm). | Off. |
| `mem_cap` | Hard memory cap per delegation, in bytes. When absent, the cap is the Slurm job's memory allocation, else a built-in default. | See meaning. |
| `review_statement` | Review `PROBLEM_STATEMENT.md` before the run. The review is advisory only. `false` skips it. | `true` |
| `runtime` | Run knobs: debug capture, timeouts, retry, and limits. See the [Runtime reference](runtime-reference.md). | All defaulted. |
| `study` | Settings for your own scripts. adda never reads inside it. | none |

For the `backend` and `model` details, including how to set them per agent
instead of for the whole run, see
[Use a different model or backend](use-a-different-model-or-backend.md#the-available-backends).

The `runtime:` block has its own page: the [Runtime reference](runtime-reference.md).

## Required deliverables

`pipeline.ipynb` is always required. List any other file the study must hand
back under `required_deliverables`, by bare filename:

```yaml
required_deliverables:
  - design.json
```

adda refuses `Done()` until each listed file exists in the study directory. The
agent writes it with `WriteDeliverable`, which takes a bare filename and writes
into the study directory. A path with a directory, such as `temp/submission.json`,
cannot be declared. If a study's `PROBLEM_STATEMENT.md` asks for a file by name,
list that file here. A file that you do not list is not checked, and the run can
finish without it.
