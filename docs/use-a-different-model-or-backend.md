# Use a different model or backend

By default every agent in a run uses the same backend and model. This page shows
how to change that for the whole run or for one agent, and how to set up each
backend.

## Change the backend for the whole run

Say the run should go through Ollama instead. Drop a `config.yaml` next to
`PROBLEM_STATEMENT.md`:

```yaml
backend: ollama
model: qwen2.5:7b
```

Nothing else changes. Same graph, same prompts, same
`AgenticRun(study_dir=study_dir).execute()` call (`model`/`backend` now come
from `config.yaml`, so the constructor doesn't need them). Every agent in
the built-in graph now runs on Ollama, because none of the shipped agents
(`StrategizerAgent`, `LiteratureReviewAgent`, `DataGeneratorAgent`,
`F3dasmImplementerAgent`, `AdversarialCritiqueAgent`) sets its own
`backend`, so each one falls back to the run's default:
`agent.backend or self._backend`.

## Change the backend or model for one agent

`backend` and `model` in `config.yaml` apply to the whole run. To set them for
one agent, name the agent in a `nodes:` block:

```yaml
backend: claude
nodes:
  implementer:
    backend: ollama
    model: qwen2.5:7b
    base_url: http://localhost:11434/v1
```

The strategizer and every other agent keep the run's backend. The other keys
of a `nodes:` entry, and the rules they follow, are in
[Customize agents and tools](customize-agents-and-tools.md#changing-one-agents-tools-nodes-in-configyaml).

To set a backend or model on an agent that you define in Python, see
[Customize agents and tools](customize-agents-and-tools.md#your-own-agents-a-custom-graph-in-python).

## The available backends

Whichever backend a run defaults to, whether set for the whole run
or per agent, all backends report token usage through the same
telemetry, so cost and throughput stay comparable.

### Claude command-line tool (default)

Uses the local `claude` command-line tool: see [Installation](install.md) if you
haven't set it up yet. Once `claude` runs on its own in your terminal,
there's nothing else to configure beyond the model:

```yaml
backend: claude
model: claude-haiku-4-5-20251001
```

### Ollama

A local Ollama server. It listens on `http://localhost:11434/v1` unless
you set `base_url`.

```yaml
backend: ollama
model: qwen2.5:7b
base_url: http://localhost:11434/v1
```

Inside the container runner, the host's server is at
`http://host.docker.internal:11434/v1`. Put that in `base_url`.

### OpenAI-compatible endpoints (OpenRouter, vLLM, others)

Any server that speaks the OpenAI API. The base URL comes from, in order:
`nodes.<node>.base_url`, the top-level `base_url`, the endpoint of a
Slurm-served model (`llm_slurm`), then the backend's default. The
environment never sets the endpoint: an exported `VLLM_BASE_URL`,
`OLLAMA_BASE_URL` or `OPENROUTER_BASE_URL` stops the run at start-up. Setting both `base_url` and
`llm_slurm.enabled` is an error.

```yaml
backend: openrouter        # or: vllm
model: meta-llama/llama-3.1-70b-instruct
base_url: http://host:8000/v1
```

```bash
export OPENROUTER_API_KEY=...        # the key stays in the environment
```

### Claude Code on an OpenAI-compatible model (Qwen on vLLM)

A node on the `claude` backend runs real Claude Code, which speaks the Anthropic
Messages API. vLLM's own `/v1/messages` route rejects part of what Claude Code
sends (an inline `system` message, HTTP 400 on vLLM 0.19.1), so run adda's
translating proxy next to the model server:

```bash
python -m adda._src.backends.anthropic_proxy \
    --upstream http://localhost:8000/v1 --port 8001
```

Then point the node at the proxy. Only a node's own `base_url` reaches the
`claude` backend; the top-level `base_url` and the `llm_slurm` endpoint do not,
because the proxy address is not the model server's address.

```yaml
nodes:
  solo:
    backend: claude
    model: Qwen/Qwen3-32B      # shown to Claude Code; the proxy serves its --model
    base_url: http://127.0.0.1:8001
```

The proxy sends every request, including Claude Code's side requests for a
small model, to one upstream model: `--model`, or the first model the server
lists. Output tokens come from the server's `usage`. `/v1/messages/count_tokens`
is an estimate that only Claude Code's own context display reads.

### A local model on a Slurm GPU node (vLLM)

adda can own a model served on a separate Slurm GPU allocation for the whole
run: it submits the `vllm serve` job, waits for the node and a ready server,
points the backend at it over the cluster network, and cancels the job on every
exit path. Enable it with an `llm_slurm` block; a config-time throughput estimate
warns if the model/GPU choice is likely to be painfully slow.

```yaml
backend: vllm
llm_slurm:
  enabled: true
  model: gemma-4-27b-it
  gpu_model: a100            # enables the config-time speed check
  cluster:
    partition: gpu
    account: my_acct
    runner: "uv run python"
    env_setup: ["module load cuda", "module load vllm"]
```

The `llm_slurm` block also accepts resource overrides (`gres`, `mem`, `time`),
queue and serve timeouts, and a `tensor_parallel` size for splitting a large model
across GPUs.
