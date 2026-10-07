# Run an ablation

An ablation switches off one piece of adda's scaffolding and compares the run
with a baseline. This page shows the smallest case: the same study twice, once
with the drift monitor on and once with it off.

The switches are listed in the
[Runtime reference](runtime-reference.md#ablation-switches). Each one is a
`runtime:` knob, so you set it in `config.yaml` or with `--set`.

## Run the baseline and the arm

Use `studies/example_study`. It is cheap, and its problem is a 2-D quadratic.

```bash
python -m adda studies/example_study
python -m adda studies/example_study --set science_monitor=false
```

Each command writes its own `runs/<timestamp>/`. Nothing is wiped between
them, so the second run warm-starts from the first. If you want the arm to
begin without the baseline's knowledge, move the baseline's `runs/` directory
aside by hand first. adda does not isolate runs for you.

## What changes in the records

Open `debug/run_config.json` in each run. Two entries matter.

- `arms` lists every ablation switch with its value, defaults included, and
  `max_awake_nodes`. The baseline shows `"science_monitor": true`. The arm shows
  `"science_monitor": false`. Every other switch matches.
- `runtime` lists only the knobs that somebody set. The baseline has no
  `science_monitor` entry there. The arm has `science_monitor: false`.

Read `arms`, not `runtime`, to label a run. An empty `runtime` can mean either
an unlabelled baseline or a run that nobody configured.

`debug/node_tools.json` does not change for this arm. That file records a
`nodes:` override of an agent's tools. A switch that withholds a tool, such as
`hypothesis_ledger: false`, removes the tool at run time, and `node_tools.json`
does not show it. `science_monitor` owns no tools, so the monitor's absence
shows only in `arms` and in a missing `diagnostics.jsonl` stream of monitor
events, such as `UNSTAMPED_ROWS`.

## Compare two runs

Compare the same fields in both runs.

1. Check that `arms` differs in exactly the switch you meant to change. A
   second difference means the comparison measures two things.
2. Read the outcome from `run_status.json`: `status`, and the arms recorded
   beside it.
3. Compare the rows that `studies/run_ledger.csv` keeps for each run. The
   `arm_<switch>` columns hold the arms. `evals_used`, `wall_s`, and `best_f`
   hold the outcome.

One run per arm shows that a difference can occur. It does not show how large
the difference is, because a language-model run varies between repeats.

A resume under different arms is refused, because a run measured under two arms
belongs to neither. See `allow_arm_drift` in the
[Runtime reference](runtime-reference.md).

## Run a full ablation study

The [adda-benchmarks](https://github.com/bessagroup/adda-benchmarks) repository
holds ablation studies on research problems. Use it when you need repeats per
arm and a report across them.
