# adda

adda runs a team of LLM agents on a data-driven engineering problem. You
describe the problem in one file, and adda hands back a notebook that
reproduces the result end to end.

## What do you want to do?

**Get started**

- [Install adda](install.md) and connect a model.
- [Run the quickstart](notebooks/quickstart.ipynb): a real problem, a real
  answer, a couple of minutes.

**Do a task**

- [Write a study](author-a-study.md).
- [Watch a run and steer it](watch-and-steer-a-run.md).
- [Understand what a run produced](read-a-runs-output.md).
- [Use a different model or backend](use-a-different-model-or-backend.md).
- [Change an agent's tools or build your own agents](customize-agents-and-tools.md).
- [Fix a run that failed or stalled](troubleshoot.md).

**Understand how it works**

- [The permission graph](permission-graph.md): who may delegate to whom.
- [How a run is kept honest](how-a-run-is-kept-honest.md): the hypothesis
  ledger, the critic, and the reproduction gate.
- [Features](features.md): how a piece of the scaffolding is turned off for an
  experiment.

**Look something up**

- [Configuration reference](config-reference.md) and
  [Runtime reference](runtime-reference.md): every `config.yaml` key.
- [Command-line reference](cli.md).
- [Python API reference](api/index.md).

adda builds on [f3dasm](https://github.com/bessagroup/f3dasm) for the
data-driven primitives and adds the agentic orchestration on top.
