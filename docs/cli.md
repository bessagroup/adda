# Command-line reference

adda installs four entry points. Each one takes a study directory, except
`adda-docs`.

| Command | What it does |
|---|---|
| `python -m adda` | Runs a study. |
| `python -m adda.watchdog` | Runs a study under an outside watchdog that kills a wedged run. |
| `python -m adda.viewer` | Serves the live viewer for a study's runs. |
| `adda-docs` | Looks up adda's own API, concepts, and rules. |

## Run a study

```bash
python -m adda studies/example_study
```

The study directory must contain `PROBLEM_STATEMENT.md`. The run writes to
`runs/<timestamp>/` inside it.

| Option | Meaning |
|---|---|
| `study-dir` | The study directory. |
| `--model MODEL` | The model identifier. Default: the `model:` key in `config.yaml`, else `claude-haiku-4-5-20251001`. |
| `--budget DURATION` | A wall-clock budget (the wind-down begins at 100%), in seconds or as `HH:MM:SS`. Default: unlimited. |
| `--set KEY=VALUE` | Overrides one `runtime:` knob for this run. You can repeat it. It outranks `config.yaml`. An unknown knob is an error. |

`python -m adda` always builds the built-in default graph. A study whose
`run.py` declares its own graph runs through that script instead, or through
the watchdog's `--entrypoint` option.

For example, to switch off two pieces of the scaffolding for one run:

```bash
python -m adda studies/example_study \
  --set hypothesis_ledger=false --set verdict_validator=false
```

The knobs are listed in the [Runtime reference](runtime-reference.md).

## Run a study under a watchdog

```bash
python -m adda.watchdog studies/example_study --budget 00:45:00
```

The study runs as a child process. If it wedges, a hard timer ends the run
and every process it started. For the reasoning, see
[Author a study](author-a-study.md#launching-under-a-watchdog).

| Option | Meaning |
|---|---|
| `study-dir` | The study directory. |
| `--model MODEL` | Passed to `python -m adda` unchanged. |
| `--budget DURATION` | The run's wall-clock budget. Required here, or as `budget:` in `config.yaml`. The deadline comes from it. |
| `--watchdog-multiple X` | The deadline is `X` times the budget. The default and the floor are both `2.0`. You can raise it but not lower it. |
| `--entrypoint SCRIPT` | Runs the study's own script, for example `run.py`, in place of `python -m adda`. Use it when the script declares a custom graph. It cannot combine with `--model` or `--budget`, so the deadline comes from `config.yaml`. |
| `--set KEY=VALUE` | The same as for `python -m adda`. |

Stopping the watchdog with Ctrl-C or `SIGTERM` also stops the run. The
watchdog exits with `130` after SIGINT and `143` after SIGTERM.

## Serve the viewer

```bash
pip install "adda[viewer]"
python -m adda.viewer studies/example_study
```

| Option | Meaning |
|---|---|
| `study-dir` | The study whose `runs/` directory the viewer serves. |
| `--host HOST` | The interface to bind. Default: `127.0.0.1`. |
| `--port PORT` | The port to bind. Default: `8765`. |
| `--allow-network` | Lets `--host` be a non-loopback interface. The viewer can start and stop runs, so this exposes that to the network. |

See [Watch and steer a run](watch-and-steer-a-run.md) for what the viewer
shows and what you can do in it.

## Look up adda from a shell

```bash
adda-docs "hypothesis ledger"
adda-docs --source Delegate
```

A phrase returns a menu of matches. Pass a name back to get its full entry.
`adda-docs` searches only the installed adda.

| Option | Meaning |
|---|---|
| `query` | A name or a phrase. |
| `--source` | Prints the source of an exactly named symbol. |
| `--overview` | Lists the public surface instead of searching. |
| `-n LIMIT`, `--limit LIMIT` | How many matches to list. Default: `8`. |
