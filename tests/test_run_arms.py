"""A run's ablation arm is read off the run, not off whoever launched it.

`settings.resolved()` lists only knobs somebody set, so an all-defaults
baseline used to record `{}` and its arm was recoverable only from the code
version. These tests pin the three places the full arm is written and the
resume check that stops one run being measured under two arms.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from adda._src.runtime import features, settings
from adda._src.runtime.run_setup import _init_canonical_store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "studies"))
import run_ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_settings():
    settings.configure(None)
    yield
    settings.configure(None)


def _run_dir(tmp_path):
    run_dir = tmp_path / "studies" / "s" / "runs" / "20260101T000000"
    (run_dir / "debug").mkdir(parents=True)
    return run_dir


def _init(run_dir, tmp_path):
    return _init_canonical_store(run_dir, tmp_path / "studies" / "s")


def test_an_all_defaults_run_records_every_arm(tmp_path):
    run_dir = _run_dir(tmp_path)
    _init(run_dir, tmp_path)
    cfg = json.loads((run_dir / "debug" / "run_config.json").read_text())

    assert cfg["runtime"] == {}
    assert set(features.FEATURE_KEYS) <= set(cfg["arms"])
    assert all(cfg["arms"][k] is True for k in features.FEATURE_KEYS)
    assert cfg["arms"]["max_awake_nodes"] == features.max_awake_nodes()


def test_a_disabled_arm_is_recorded_false(tmp_path):
    settings.configure({"hypothesis_ledger": False})
    run_dir = _run_dir(tmp_path)
    _init(run_dir, tmp_path)
    cfg = json.loads((run_dir / "debug" / "run_config.json").read_text())
    assert cfg["arms"]["hypothesis_ledger"] is False


def test_resume_under_different_arms_is_refused(tmp_path):
    run_dir = _run_dir(tmp_path)
    _init(run_dir, tmp_path)
    settings.configure({"science_monitor": False})
    with pytest.raises(RuntimeError, match="science_monitor"):
        _init(run_dir, tmp_path)


def test_arm_drift_can_be_accepted_and_keeps_the_first_arms(tmp_path):
    run_dir = _run_dir(tmp_path)
    _init(run_dir, tmp_path)
    settings.configure({"science_monitor": False, "allow_arm_drift": True})
    _init(run_dir, tmp_path)
    cfg = json.loads((run_dir / "debug" / "run_config.json").read_text())
    assert cfg["arms"]["science_monitor"] is False
    assert cfg["arms_initial"]["science_monitor"] is True


def test_resume_with_the_same_arms_is_silent(tmp_path):
    run_dir = _run_dir(tmp_path)
    _init(run_dir, tmp_path)
    _init(run_dir, tmp_path)


def test_run_status_carries_the_arms(tmp_path):
    from adda._src.runtime.agent_runtime import AgenticRun

    d = tmp_path / "debug"
    d.mkdir()
    AgenticRun._write_run_status(d, status="GATED")
    status = json.loads((d / "run_status.json").read_text())
    assert status["arms"] == features.arm_config()


def test_the_ledger_row_carries_the_arms(tmp_path):
    settings.configure({"doe_playbook": False})
    run_dir = _run_dir(tmp_path)
    _init(run_dir, tmp_path)
    row = run_ledger.extract(run_dir)
    assert row["arm_doe_playbook"] == "false"
    assert row["arm_hypothesis_ledger"] == "true"
    assert row["arm_max_awake_nodes"] == str(features.max_awake_nodes())
    assert {f"arm_{k}" for k in features.FEATURE_KEYS} <= set(
        run_ledger.COLUMNS)
