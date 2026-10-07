# Features: turning a piece of the scaffolding off

Most customization changes *who runs* the work. A feature changes *what the agents
are given*. adda has accumulated machinery—a hypothesis ledger, milestone
gates, a science-drift monitor, an f3dasm documentation lookup, a method
playbook—and the only way to find out whether a piece of it earns its cost
is to run the same problem with it removed. `Feature` is what makes "removed"
mean removed.

## The problem it solves

A capability is never just one object. The hypothesis ledger is a JSON file
*and* three tools the agent can call *and* a block of the strategizer's system
prompt telling it that `hypotheses.json` is its canonical scientific record.
Wire those three independently and a flag that switches off the object leaves
the other two running: the agent is still commanded to use the ledger, still
sees all three tools published as AUTHORITATIVE, calls one, and gets back

```
ERROR: hypothesis ledger not available in this run.
```

That run does not measure an agent without a ledger. It measures an agent
confused by broken tools—and it inflates `ERROR_RETURN`, the one diagnostic
whose target is zero. Three separate backlog entries in this repo are
successive rounds of *"the flag didn't actually turn it off."*

## The declaration

A feature declares, in one place, its knob, and everything that exists only
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

These fields carry what a feature owns:

| field | what it owns |
|---|---|
| `key` | the runtime knob, for example `science_monitor: false` |
| `tools` | tool names that exist only because this feature does |
| `sections` | prompt sections `<tag>…</tag>` this feature owns outright |
| `behaviours` | runtime capabilities with no tool and no prompt surface |
| `pervasive` | the feature's idea runs through prompt text it does not own, so turning it off is a partial ablation; see below |
| `requires` | other features this one needs; a run that turns one off and leaves this one on refuses to start |

A richer one—the ledger owns tools *and* a prompt section, and carries a
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

## Turning one off

Nothing in Python. It is a `runtime:` key in the study's `config.yaml`:

```yaml
runtime:
  science_monitor: false
```

or pass it as `AgenticRun(runtime={"science_monitor": False})`.
Precedence is explicit argument, then `config.yaml`, then the feature's own
`default`; the environment sets no knob. The switches are listed together under
*Ablation switches* in the [runtime reference](runtime-reference.md#ablation-switches).

What that one line does, with no other change anywhere:

1. every tool name in `tools` is withheld from the agent—not left
   registered and returning an error, *withheld*, so the tool catalog the agent is
   handed does not mention it;
2. every `<tag>…</tag>` in `sections` is cut out of the assembled system
   prompt, so the agent is never told about a capability it does not have;
3. the backing object is not constructed, and any `behaviours` the runtime
   would consult are off.

The agent that runs is an agent that never had the feature, rather than an
agent that had it taken away mid-sentence.

## The honest caveat: `pervasive`

`hypothesis_ledger` in the second declaration is marked `pervasive=True`. The Popperian workflow
is not confined to the section the ledger owns—it is the strategizer's
entire operating model, argued in `<scientific_process>`, enforced in
`<operating_principles>` and resolved in `<exploration_verdicts>`. Switching
the feature off removes its tools and its own section, but it cannot remove
the *idea*, so that arm is a **partial** ablation and has to be reported as
one.

Stripping those three sections too would not be a cleaner ablation. It would
be a different agent, and the comparison would be meaningless.

## What this costs the codebase

One registry file, and three places that read it: the tool catalog calls
`disabled_tool_names()`, prompt assembly calls
`strip_disabled_sections()`, and a feature with a runtime object or behaviour
calls `features.enabled("key")` at the single point where that object is
constructed. Features are declared in adda's own `runtime/features.py`; a study cannot declare one from its own files. Adding a feature does not add a branch anywhere else, and a
reader who does not care about ablations never meets one—the declarations
sit in `runtime/features.py` and the rest of the code reads as if the feature
is simply present.

The registry is checked rather than trusted: `tests/test_features.py` fails
if a declared section tag does not exist in the prompt that claims to own it,
so renaming a tag cannot silently stop it from being stripped, and
`tests/test_settings_contract.py` fails if a feature's key is missing from
the documented knob table.

For the switches themselves, see the [runtime reference](runtime-reference.md#ablation-switches).
