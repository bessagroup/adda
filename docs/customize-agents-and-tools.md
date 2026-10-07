# Customize agents and tools

The [Quickstart](notebooks/quickstart.ipynb) ran the Branin problem with adda's
own defaults: the built-in graph and each shipped agent's own prompt and tools.
This page shows how to change who the agents are and which tools each one has.
To change the model or backend instead, see
[Use a different model or backend](use-a-different-model-or-backend.md).

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

## Your own agents: a custom graph in Python

If you need your own `Agent` subclass, for a custom prompt or a different set
of agents, build the graph yourself. Swapping a hand-written agent into the
shipped graph while keeping the rest is not a pattern this project tests, so a
custom graph and a custom prompt usually arrive together, in one `Graph`. In
that case you can also set `backend` and `model` on the node directly:

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
alone runs on Ollama with `qwen2.5:7b`. If the study's `config.yaml` also has
a `nodes:` entry for that agent, the config entry wins over the class.

A bare `Agent` subclass starts from zero tools (`Agent.tools` defaults to
`frozenset()`) and zero epistemic machinery. The shipped agents wire up the
hypothesis ledger, the reproduction gate, and each other's delegation tools
already; a hand-rolled one, like `Strategist`/`Implementer` in the example, does not.
No shipped agent sets its own `backend`. Tests check that each node resolves
its own backend, model and endpoint, but none runs a whole graph that mixes
backends. Running most of a graph on Claude and one node on a local model
works by design, but you may be the first to try it.

## Changing one agent's tools: `nodes:` in `config.yaml`

Each agent comes with a set of tools. To change that set for one agent
without writing Python, name the agent in a `nodes:` block of `config.yaml`:

```yaml
nodes:
  implementer:
    tools: [Default, ReadNote]
```

The list **replaces** the agent's own set. It is not added to it, so a tool
you leave out is gone. Agents you do not name keep their usual tools. A node
entry may also set `model`, `backend` and `base_url` for that agent alone, for
example `model: claude-haiku-4-5`. Each is a non-empty string; a `base_url` is
only valid on a backend that has an endpoint. Any other key is an error. A node name the graph does not have,
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

`Default` is a tool name that means "every built-in tool this backend has."

- **Claude (the default backend):** the agent gets the full tool set of the
  Claude command-line tool, including web search, web fetch and sub-agents. Other agents
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

### What adda reports

adda never stops a run over a tool choice. It records notices in the run's
diagnostics feed, the run log and the viewer:

- **`DEFAULT_TOOLS_BYPASS`**, once per node that holds `Default`. It lists the
  adda paths that node can bypass.
- **`TOOLS_CONFIG_DIFFERS`**, for every node whose list in `config.yaml` is not
  the list its class declares. It states which tools were added and which were
  removed.
- **`TOOLS_RESOLVED`**, once per `Default` node on the Claude backend, after
  the Claude command-line tool starts. It lists the built-in tools the node really received
  and marks any adda has not reviewed.

On another backend, a `Default` node also gets **`DEFAULT_TOOLS_EXPANDED`**: it
names the tools `Default` turned into there.

The run folder keeps the result: `debug/node_tools.json` has, for every node,
where its tools came from (class or `config.yaml`), the class set, the final
set and the difference.

## Seeing what you built

A custom graph is easy to get subtly wrong—a node with a mistyped role, an
edge to the wrong target, an override that silently didn't take. Render it:

```python
run = AgenticRun(study_dir=study_dir, graph=graph)
run.render_architecture()  # writes study_dir/architecture.svg
```

The SVG shows every node's role, description, and full tool surface (its
declared `Agent.tools` plus what it actually gets injected at runtime), the
delegation edges between nodes, and—the thing worth checking after the
example—each node's *resolved* backend/model, exactly as
`agent_runtime.py` resolves it (`agent.model or self._model`, `agent.backend
or self._backend`). For the example graph, that means the diagram should show
the strategizer on the run's default and the implementer on `ollama ·
qwen2.5:7b`, on its own card—not a single run-wide banner claiming one
backend for everything. Works before or after `execute()`; call it any time
you want to check a graph rather than trust it.
