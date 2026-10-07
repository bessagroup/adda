"""Wet smoke test: the docs tutorials actually run end to end, for real.

Runs the exact problem shape taught in two docs tutorials — the Quickstart's
Branin example and Authoring a study's worked example (studies/example_study)
— through a real AgenticRun, on a free-tier OpenRouter model so this costs
nothing to run.

Marked `integration` and skipped cleanly without `OPENROUTER_API_KEY` set, so
a normal `pytest -m integration` run (no key configured) doesn't spuriously
fail. Intended trigger is the scheduled `wet_docs_smoke.yml` workflow (once
daily + manual dispatch), not the regular push/PR `Tests` workflow.

The bar this enforces is deliberately narrow: does the tutorial, followed
literally, crash? Whether the run closes GATED or UNGATED is NOT asserted —
a free/weaker model may legitimately not clear the full science gate, and
that is a model-capability signal, not a docs or runtime bug. Likewise a
run still working when the clock ends passes only if its records show it
alive (see _assert_timed_out_while_healthy); the output says so. Run this
manually with:
    OPENROUTER_API_KEY=... uv run pytest tests/test_docs_tutorials_wet.py \
        -v -s --no-cov -m integration
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# OpenRouter's own free-tier meta-router: it auto-selects among whichever
# free models are currently healthy, rather than pinning to one specific
# `:free` model that can have a bad day (a single pinned model, gpt-oss-20b:
# free, failed 2 of 3 scheduled runs here for infra reasons — rate limits/
# outages — attributable to that one model, not this test or the docs).
# Override via WET_TEST_MODEL for a specific model (e.g. to reproduce a
# failure against one exact model), confirmed live against
# https://openrouter.ai/api/v1/models at the time this was written.
_FREE_MODEL = os.environ.get("WET_TEST_MODEL", "openrouter/free")

# Wall-clock and eval caps sized for a free-tier model on a trivial problem —
# tight enough that a stuck/looping run doesn't burn the whole scheduled slot.
_EVAL_BUDGET = 60
_WALLCLOCK_BUDGET_S = 20 * 60

# The run's own wall-clock budget decides, not the repo-wide 120 s unit-test
# bound; the margin covers startup and teardown.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(_WALLCLOCK_BUDGET_S + 5 * 60),
]


def _require_openrouter_key() -> None:
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — wet docs-tutorial smoke test skipped")


def _assert_timed_out_while_healthy(study_dir: Path) -> None:
    """A free model may still be working when the clock ends. That is not a
    crash, but only a run the records show alive may pass this way: the stop
    reason is the timeout itself, the store exists, at least one delegation
    finished, and nothing in the run's own log is a traceback."""
    import json

    run_dirs = sorted((study_dir / "runs").iterdir())
    assert run_dirs, "timed out and no runs/<timestamp>/ directory exists"
    run_dir = run_dirs[-1]
    debug = run_dir / "debug"
    status = json.loads((debug / "run_status.json").read_text())
    reason = str(status.get("reason", ""))
    assert reason.startswith("Failed: Timeout"), (
        f"the run stopped for a reason other than the clock: {reason!r}")
    assert (run_dir / "experiment_data").exists(), (
        "timed out and the run has no experiment store")
    rows: dict[str, dict] = {}
    for line in (debug / "delegation_log.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row
    done = [i for i, r in rows.items() if r.get("status") == "DONE"]
    assert done, f"timed out with no delegation DONE (rows: {sorted(rows)})"
    log = (debug / "run.log").read_text(encoding="utf-8", errors="replace")
    assert "Traceback (most recent call last)" not in log, (
        "timed out, but the run log holds a traceback")
    print(f"\n[wet docs smoke] {study_dir.name}: timed out while healthy "
          f"after {status.get('wall_s')}s; the run did not close "
          f"({len(done)} delegation(s) DONE: {', '.join(sorted(done))})")


def _run_and_check(study_dir: Path) -> None:
    """Execute study_dir through a real AgenticRun and assert it didn't crash.

    backend is config.yaml-only (AgenticRun has no backend= constructor
    kwarg — only model=), so the caller must have written/overwritten
    study_dir/config.yaml's `backend:` key to "openrouter" before calling
    this.
    """
    from adda import AgenticRun

    run = AgenticRun(
        study_dir=study_dir,
        model=_FREE_MODEL,
        eval_budget=_EVAL_BUDGET,
        budget=_WALLCLOCK_BUDGET_S,
        interactive=False,
    )
    try:
        report = run.execute()
    except pytest.fail.Exception as exc:
        if "Timeout" not in str(exc):
            raise
        _assert_timed_out_while_healthy(study_dir)
        return

    assert report, "AgenticRun.execute() returned an empty report"
    assert (study_dir / "pipeline.ipynb").exists(), (
        "tutorial did not produce pipeline.ipynb — the deliverable is missing"
    )

    runs_dir = study_dir / "runs"
    run_dirs = sorted(runs_dir.iterdir()) if runs_dir.exists() else []
    assert run_dirs, "no runs/<timestamp>/ directory was written"
    status_path = run_dirs[-1] / "run_status.json"
    assert status_path.exists(), f"run_status.json missing under {run_dirs[-1]}"

    # Informational only — NOT a pass/fail condition (see module docstring).
    import json

    status = json.loads(status_path.read_text()).get("status", "UNKNOWN")
    print(f"\n[wet docs smoke] {study_dir.name}: run closed as {status} "
          f"(GATED/UNGATED is informational here, not a failure condition)")


def test_quickstart_branin_runs_without_crashing(tmp_path):
    """Mirrors docs/notebooks/quickstart.ipynb's Branin example exactly."""
    _require_openrouter_key()

    study_dir = tmp_path / "quickstart_branin"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text(
        "Minimise the 2D Branin function over its standard domain.\n"
        "Report the best design found and the objective value there.\n"
    )
    (study_dir / "config.yaml").write_text("backend: openrouter\n")
    _run_and_check(study_dir)


def test_authoring_a_study_worked_example_runs_without_crashing(tmp_path):
    """Mirrors docs/author-a-study.md's worked example (studies/example_study),
    routed through OpenRouter instead of its own config.yaml's Claude default —
    the study's own PROBLEM_STATEMENT.md/evaluator.py are used unmodified."""
    _require_openrouter_key()

    src = Path(__file__).resolve().parent.parent / "studies" / "example_study"
    study_dir = tmp_path / "example_study"
    shutil.copytree(
        src, study_dir,
        ignore=shutil.ignore_patterns("runs", "__pycache__", "pipeline.ipynb"),
    )
    # The repo's config.yaml pins backend: claude / model: claude-haiku-...;
    # backend has no constructor override, so overwrite the COPY's
    # config.yaml (never the repo's own) to route this run through
    # OpenRouter instead. eval_budget/evaluator are unmodified.
    (study_dir / "config.yaml").write_text(
        "backend: openrouter\n"
        "eval_budget: 200\n"
        "evaluator:\n"
        '  entrypoint: "workspace/evaluator.py:evaluate"\n'
        "  output_names: [y]\n"
    )
    _run_and_check(study_dir)
