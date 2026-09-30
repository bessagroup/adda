# adda

**A**gentic **D**ata-**D**riven **D**esign and **A**nalysis — the agentic layer
over [f3dasm](https://github.com/bessagroup/f3dasm) (*Framework for* Data-Driven
Design & Analysis of Structures & Materials), itself the framework for the
[3dasm](https://github.com/bessagroup/3dasm_course) course.

You write one file describing an engineering design or data problem — the
objective, the design space, what counts as valid. adda runs a team of LLM
agents that decide what to try, build the code to evaluate it, run real
experiments, review their own conclusions before accepting them, and hand you
back a notebook that reproduces the result end to end.

It builds on [f3dasm](https://github.com/bessagroup/f3dasm) for the
data-driven primitives (`ExperimentData`, `Domain`, `DataGenerator`, the
`Pipeline`); adda is the agentic layer on top and carries no copy of f3dasm
core.

## Install

```bash
pip install "adda @ git+https://github.com/bessagroup/adda.git"
```

You'll also need a model to drive the agents — by default, the
[Claude CLI](https://docs.claude.com/en/docs/claude-code):

```bash
npm install -g @anthropic-ai/claude-code
claude   # first run prompts you to log in
```

## Quick start

The only required input is a `PROBLEM_STATEMENT.md` in the study directory.

```python
from adda import AgenticRun

report = AgenticRun(
    study_dir="studies/my_study",
    model="claude-haiku-4-5-20251001",
).execute()
print(report)
```

See the [Quickstart](https://bessagroup.github.io/adda/notebooks/quickstart/)
for a worked example, start to finish.

## Documentation

<https://bessagroup.github.io/adda/> — or run `mkdocs serve` locally.

## License

BSD-3-Clause.
