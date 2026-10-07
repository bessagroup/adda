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
| `budget` | Soft wall-clock limit, as `"HH:MM:SS"` or seconds. It is a nudge, not a hard stop. | none |
| `eval_budget` | Soft cap on how many real evaluations the run may spend. | none |
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
