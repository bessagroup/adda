# Customizing a run

The [Quickstart](notebooks/quickstart.ipynb) ran the Branin problem with
adda's own defaults: the built-in graph, the shipped agents' own prompts,
one backend for everything. This page reuses that same problem to show what
changes, and how, as you reach for something different.

## The starting point

```python
from pathlib import Path
from adda import AgenticRun

study_dir = Path("studies/branin")
study_dir.mkdir(parents=True, exist_ok=True)
(study_dir / "PROBLEM_STATEMENT.md").write_text(
    "Minimise the 2D Branin function over its standard domain.\n"
    "Report the best design found and the objective value there.\n"
)

AgenticRun(study_dir=study_dir, model="claude-haiku-4-5-20251001").execute()
```

No `config.yaml` here; `model` is the only thing set, and there's no
`backend` at all (it defaults to Claude). This runs the built-in graph
(strategizer plus four specialists), every agent on the same backend, every
agent using its own shipped prompt.

## A different backend for the whole run: `config.yaml`

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
`agent.backend or self._backend` (in `agent_runtime.py`'s `_make_adapter`).

## A different backend for one agent: this needs Python, not YAML

`config.yaml`'s `backend`/`model` are run-wide; there's no YAML key for
"just the implementer." Getting that means setting `backend`/`model` on one
node, which means that node is your own `Agent` subclass, which means
building the graph yourself: swapping a hand-rolled agent into the shipped
graph while keeping the rest isn't a pattern this project tests. So a custom
graph, a custom prompt, and a different backend for one node usually arrive
together, in one `Graph`:

```python
from adda import Agent, Edge, Graph, AgenticRun


class Strategist(Agent):
    role = "strategizer"  # the hub; other roles default to "worker"
    description = "Decides what to try next."  # required, or Graph() raises
    system_prompt = "You are the strategizer. Delegate to the implementer."
    # backend/model unset: this node falls back to the run's own default


class Implementer(Agent):
    description = "Writes and runs the evaluation code."
    backend = "ollama"  # a class attribute override, only for this node


graph = Graph(
    nodes={
        "strategizer": Strategist(),
        "implementer": Implementer(model="qwen2.5:7b"),  # model is a
        # constructor arg, not a class attribute: Agent.__init__ always
        # does self.model = model, which would silently shadow a
        # class-level override
    },
    edges=(Edge("strategizer", "implementer"),),  # who may delegate to whom
    entry="strategizer",  # who gets the initial briefing
)

AgenticRun(study_dir=study_dir, graph=graph).execute()
```

Run this with no `config.yaml` (or one that just says `backend: claude`) and
the strategizer runs on Claude, the run's default, while the implementer
alone runs on Ollama with `qwen2.5:7b`. Nothing here reads `config.yaml` for
the implementer's backend at all; `Implementer(model=...)` and
`backend = "ollama"` are the only source of truth for that one node.

A bare `Agent` subclass starts from zero tools (`Agent.tools` defaults to
`frozenset()`) and zero epistemic machinery. The shipped agents wire up the
hypothesis ledger, the reproduction gate, and each other's delegation tools
already; a hand-rolled one, like `Strategist`/`Implementer` above, does not.
This is also why no shipped agent sets a per-agent `backend`, and no test in
this project exercises a graph that mixes backends: it's a real, working
lever (as shown above), but running most of a graph on Claude and one node
on a local model is a combination you'd be the first to try.

## Changing one agent's tools: `nodes:` in `config.yaml`

Each agent comes with a set of tools. To change that set for one agent
without writing Python, name the agent in a `nodes:` block of `config.yaml`:

```yaml
nodes:
  implementer:
    tools: [Default, ReadNote]
```

The list **replaces** the agent's own set. It is not added to it, so a tool
you leave out is gone. Agents you do not name keep their usual tools. `tools`
is the only setting a node accepts here. A node name the graph does not have,
or a `tools` that is not a list of names, stops the run at startup with a
message that names the problem. The study editor in the viewer checks the same
rules before it saves.

Every agent also gets five tools that its class does not declare:
`ConsultHandbook`, `ConsultLiterature`, `ReportEvals`, `RecallHistory` and the
sandboxed `Write`. A `tools` list in `config.yaml` is the agent's whole set, so
it drops each of these five that it does not name. Name one in the list to keep
it. An agent with no `nodes:` entry keeps all five. A `Default` agent keeps the
sandboxed `Write`. At startup, adda logs the tools a list adds, removes or
withholds (diagnostics row `TOOLS_CONFIG_DIFFERS`).

### `Default`: the backend's full set of built-in tools

`Default` is a tool name that means "every built-in tool this backend has".

- **Claude (the default backend):** the agent gets the full tool set of the
  Claude CLI, including web search, web fetch and sub-agents. Other agents
  never get these: adda switches them off. `Default` removes that restriction
  for the agent that holds it. One block stays: if the agent also has an adda
  tool with the same name as a built-in one (for example the sandboxed
  `Write`), the built-in one is switched off, so the sandbox still applies.
- **Other backends (Ollama, OpenAI-compatible, vLLM):** `Default` means that
  backend's own built-in tools: `Bash`, `BashOutput`, `KillShell`, `Read`,
  `Write`, `Edit`, `Glob` and `Grep`.

`Default` is never added for you. You can also put it in the `tools` set of
your own agent class.

A node with `Default` can step around parts of adda. Its built-in tools do not
go through the delegation tools, the literature rate limiter and cache, the
reproduction gate or the human follow-up channel. adda does not block this.
It tells you.

### What you will see

adda never stops a run over a tool choice. It records notices in the run's
diagnostics feed, the run log and the viewer:

- **`DEFAULT_TOOLS_BYPASS`**, once per node that holds `Default`. It lists the
  adda paths that node can bypass.
- **`TOOLS_CONFIG_DIFFERS`**, for every node whose list in `config.yaml` is not
  the list its class declares. It states which tools were added and which were
  removed.
- **`TOOLS_RESOLVED`**, once per `Default` node on the Claude backend, after
  the Claude CLI starts. It lists the built-in tools the node really received
  and marks any adda has not reviewed.

On another backend, a `Default` node also gets **`DEFAULT_TOOLS_EXPANDED`**: it
names the tools `Default` turned into there.

The run folder keeps the result: `debug/node_tools.json` has, for every node,
where its tools came from (class or `config.yaml`), the class set, the final
set and the difference.

## Seeing what you built

A custom graph is easy to get subtly wrong — a node with a typo'd role, an
edge to the wrong target, an override that silently didn't take. Render it:

```python
run = AgenticRun(study_dir=study_dir, graph=graph)
run.render_architecture()  # writes study_dir/architecture.svg
```

The SVG shows every node's role, description, and full tool surface (its
declared `Agent.tools` plus what it actually gets injected at runtime), the
delegation edges between nodes, and — the thing worth checking after the
example above — each node's *resolved* backend/model, exactly as
`agent_runtime.py` resolves it (`agent.model or self._model`, `agent.backend
or self._backend`). For the graph above, that means the diagram should show
the strategizer on the run's default and the implementer on `ollama ·
qwen2.5:7b`, on its own card — not a single run-wide banner claiming one
backend for everything. Works before or after `execute()`; call it any time
you want to check a graph rather than trust it.

## Turning a piece of the scaffolding off: `Feature`

Everything above changes *who runs* the work. This changes *what the agents
are given*. adda has accumulated machinery — a hypothesis ledger, milestone
gates, a science-drift monitor, an f3dasm documentation lookup, a method
playbook — and the only way to find out whether a piece of it earns its cost
is to run the same problem with it removed. `Feature` is what makes "removed"
mean removed.

### The problem it solves

A capability is never just one object. The hypothesis ledger is a JSON file
*and* five tools the agent can call *and* a block of the strategizer's system
prompt telling it that `hypotheses.json` is its canonical scientific record.
Wire those three independently and a flag that switches off the object leaves
the other two running: the agent is still commanded to use the ledger, still
sees all five tools published as AUTHORITATIVE, calls one, and gets back

```
ERROR: hypothesis ledger not available in this run.
```

That run does not measure an agent without a ledger. It measures an agent
confused by broken tools — and it inflates `ERROR_RETURN`, the one diagnostic
whose target is zero. Three separate backlog entries in this repo are
successive rounds of *"the flag didn't actually turn it off."*

### The declaration

A feature declares, in one place, its knob and everything that exists only
because of it. Here is the whole of the science monitor, verbatim from
`runtime/features.py`:

```python
Feature(
    key="science_monitor",
    default=True,
    # No tools: the monitor speaks by injecting notices. Its prompt block
    # is self-contained, which makes this the cleanest arm of the set.
    sections=("science_monitor",),
)
```

Four fields carry everything a feature can own:

| field | what it owns |
|---|---|
| `key` | the runtime knob, e.g. `science_monitor: false` |
| `tools` | tool names that exist only because this feature does |
| `sections` | prompt sections `<tag>…</tag>` this feature owns outright |
| `behaviours` | runtime capabilities with no tool and no prompt surface |

A richer one — the ledger owns tools *and* a prompt section, and carries a
caveat explained below:

```python
Feature(
    key="hypothesis_ledger",
    default=True,
    tools=frozenset({
        "HypothesisPropose", "HypothesisUpdate", "HypothesisList",
    }),
    sections=("hypothesis_ledger",),
    pervasive=True,
)
```

### Turning one off

Nothing in Python. It is a `runtime:` key in the study's `config.yaml`:

```yaml
runtime:
  science_monitor: false
```

or, for a one-off, the environment channel `F3DASM_SCIENCE_MONITOR=false`.
Precedence is explicit argument, then environment, then `config.yaml`, then
the feature's own `default`. The switches are listed together under
*Ablation switches* in [Authoring a study](authoring-a-study.md).

What that one line does, with no other change anywhere:

1. every tool name in `tools` is withheld from the agent — not left
   registered and erroring, *withheld*, so the tool catalog the agent is
   handed does not mention it;
2. every `<tag>…</tag>` in `sections` is cut out of the assembled system
   prompt, so the agent is never told about a capability it does not have;
3. the backing object is not constructed, and any `behaviours` the runtime
   would consult are off.

The agent that runs is an agent that never had the feature, rather than an
agent that had it taken away mid-sentence.

### The honest caveat: `pervasive`

`hypothesis_ledger` above is marked `pervasive=True`. The Popperian workflow
is not confined to the section the ledger owns — it is the strategizer's
entire operating model, argued in `<scientific_process>`, enforced in
`<operating_principles>` and resolved in `<exploration_verdicts>`. Switching
the feature off removes its tools and its own section, but it cannot remove
the *idea*, so that arm is a **partial** ablation and has to be reported as
one.

Stripping those three sections too would not be a cleaner ablation. It would
be a different agent, and the comparison would be meaningless.

### What this costs the codebase

One registry file, and three places that read it: the tool catalog calls
`disabled_tool_names()`, prompt assembly calls
`strip_disabled_sections()`, and a feature with a runtime object or behaviour
calls `features.enabled("key")` at the single point where that object is
constructed. Adding a feature does not add a branch anywhere else, and a
reader who does not care about ablations never meets one — the declarations
sit in `runtime/features.py` and the rest of the code reads as if the feature
is simply present.

The registry is checked rather than trusted: `tests/test_features.py` fails
if a declared section tag does not exist in the prompt that claims to own it,
so renaming a tag cannot silently stop it from being stripped, and
`tests/test_settings_contract.py` fails if a feature's key is missing from
the documented knob table.

## Reference: the available backends

Whichever backend a run defaults to, whether set globally in `config.yaml`
or per agent above, all backends report token usage through the same
telemetry, so cost and throughput stay comparable.

### Claude CLI (default)

Uses the local `claude` CLI: see [Installation](installation.md) if you
haven't set it up yet. Once `claude` runs on its own in your terminal,
there's nothing else to configure beyond the model:

```yaml
backend: claude
model: claude-haiku-4-5-20251001
```

### Ollama

A local Ollama server. Point at it with `OLLAMA_BASE_URL` (defaults to
`http://localhost:11434`).

```yaml
backend: ollama
model: qwen2.5:7b
```

### OpenAI-compatible endpoints (OpenRouter, vLLM, others)

Any server that speaks the OpenAI API. The adapter resolves the base URL from an
explicit argument, then the relevant `*_BASE_URL` environment variable, then a
default.

```yaml
backend: openrouter        # or: vllm
model: meta-llama/llama-3.1-70b-instruct
```

```bash
export OPENROUTER_API_KEY=...        # or VLLM_BASE_URL=http://host:8000/v1
```

### A local model on a SLURM GPU node (vLLM)

adda can own a model served on a separate SLURM GPU allocation for the whole
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
queue and serve timeouts, and a `tensor_parallel` size for sharding a large model
across GPUs.
