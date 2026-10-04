"""Run-scoped state must not collide between runs.

Run ids are second-resolution timestamps, so two runs started in the same
second shared a directory outright — each overwriting the other's debug
output, ledger and status. Rare by hand; routine under a sweep, where it would
silently merge two arms into one set of files.

The study's config.yaml is also rewritten from inside a run (a guardrail that
keeps its declared output_names honest). That write is study-scope, so a
concurrently starting run can be reading it mid-truncate.
"""
from __future__ import annotations

import json

import pytest

from adda._src.runtime import settings
from adda._src.runtime.agent_runtime import AgenticRun


@pytest.fixture(autouse=True)
def _clean():
    settings.configure(None)
    yield
    settings.configure(None)


def _study(tmp_path, name="s"):
    d = tmp_path / name
    d.mkdir()
    (d / "PROBLEM_STATEMENT.md").write_text("# test\n")
    (d / "config.yaml").write_text("model: claude-haiku-4-5-20251001\n")
    return d


def test_two_runs_in_the_same_second_get_different_directories(
        tmp_path, monkeypatch):
    import datetime as _dt

    from adda._src.runtime import agent_runtime

    # Freeze the clock: with the real one, a second boundary between the two
    # calls gives the second run a plain new timestamp, not a collision suffix.
    class _Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(agent_runtime, "datetime", _Frozen)
    study = _study(tmp_path)
    run = AgenticRun(study_dir=study, interactive=False)

    first_ts, first_dir, _ = run._resolve_run_dir()
    first_dir.mkdir(parents=True)
    second_ts, second_dir, _ = run._resolve_run_dir()

    assert second_dir != first_dir
    assert second_ts != first_ts
    # the timestamp stays the prefix, so run ordering still sorts correctly
    assert second_ts.startswith(first_ts[:15])


def test_an_uncontended_run_keeps_its_plain_timestamp_name(tmp_path):
    """Sequential runs must keep their historical naming exactly."""
    run = AgenticRun(study_dir=_study(tmp_path), interactive=False)

    ts, _, _ = run._resolve_run_dir()

    assert "-" not in ts and len(ts) == 15


def test_the_study_config_is_rewritten_atomically(tmp_path, monkeypatch):
    """A partial or empty config.yaml must never be observable: this is a
    study-scope write made from inside a run, and a run starting concurrently
    reads that same file. write_text truncates first, leaving a window."""
    import os as _os

    from adda._src.runtime.run_setup import _sync_config_output_names

    cfg = tmp_path / "config.yaml"
    cfg.write_text("evaluator:\n  output_names: [old]\n")

    renames = []
    real_replace = _os.replace
    monkeypatch.setattr(
        _os, "replace",
        lambda a, b: (renames.append((str(a), str(b))), real_replace(a, b))[1])

    assert _sync_config_output_names(cfg, ["y"]) is True

    assert renames, "the target was written in place, not renamed into place"
    src, dst = renames[-1]
    assert dst == str(cfg)
    assert src != str(cfg)
    assert "[y]" in cfg.read_text()
    assert not list(tmp_path.glob("*.tmp")), "tmp file left behind"


def test_the_run_records_the_knobs_it_actually_ran_with(tmp_path):
    """The condition read off the artifact rather than trusted from the
    sweep's label."""
    from adda._src.runtime.run_setup import _init_canonical_store

    settings.configure({"recursion_limit": 42}, {"milestones_enabled": False})
    run_dir = tmp_path / "runs" / "T"
    (run_dir / "debug").mkdir(parents=True)

    _init_canonical_store(
        study_dir=_study(tmp_path), run_dir=run_dir, evaluator_config={})

    cfg = json.loads(
        (run_dir / "debug" / "run_config.json").read_text(encoding="utf-8"))
    assert cfg["runtime"]["milestones_enabled"] is False
    assert cfg["runtime"]["recursion_limit"] == 42
