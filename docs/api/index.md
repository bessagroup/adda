# Python API reference

Most users never call this API. A study is a directory with
`PROBLEM_STATEMENT.md` and an optional `config.yaml`, and
[`python -m adda`](../cli.md) runs it. Use the Python API when you build a
custom graph, serve the viewer from a script, or subclass a backend.

| Page | What it covers |
|---|---|
| [Running a study](run.md) | `AgenticRun`, the entry point, and its use as an f3dasm optimizer. |
| [Agents](agents.md) | The specialist roles the strategizer delegates to. |
| [Graph and state](graph.md) | `Graph`, `Edge`, `Agent`, and `Node`: who may delegate to whom. |
| [Oracle and ledger](evaluator.md) | The metered evaluator and the evaluation ledger. |
| [Model backends](backends.md) | The adapters that connect an agent to a model. |
| [Symbolic derivations](math.md) | `Workspace`, for derivations that SymPy checks. |

For the concepts behind the graph, see [The permission graph](../permission-graph.md).
