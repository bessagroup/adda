# Model backends

A backend is how an agent talks to a model. It is a configuration choice:
the graph and the science do not change with it. See
[Use a different model or backend](../use-a-different-model-or-backend.md#the-available-backends)
for choosing one, and [Installation](../install.md) for what each needs.

The adapters below are exported for direct use and for subclassing; a normal
run selects one by name from `config.yaml` and never touches these classes.

::: adda.ClaudeAdapter

::: adda.OllamaAdapter
