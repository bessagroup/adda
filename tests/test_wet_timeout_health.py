"""The wet docs smoke test passes a timeout only for a run that is alive."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_docs_tutorials_wet import _assert_timed_out_while_healthy

_TIMEOUT = "Failed: Timeout (>1500.0s) from pytest-timeout."


def _study(tmp_path: Path, *, reason=_TIMEOUT, store=True, done=True,
           log="INFO fine\n") -> Path:
    run = tmp_path / "study" / "runs" / "20261007T000000"
    debug = run / "debug"
    debug.mkdir(parents=True)
    if store:
        (run / "experiment_data").mkdir()
    (debug / "run_status.json").write_text(
        json.dumps({"status": "crashed", "reason": reason, "wall_s": 1500.0}))
    (debug / "delegation_log.jsonl").write_text(json.dumps(
        {"id": "D001", "status": "DONE" if done else "FAILED"}) + "\n")
    (debug / "run.log").write_text(log)
    return tmp_path / "study"


def test_a_live_run_passes_and_says_so(tmp_path, capsys):
    _assert_timed_out_while_healthy(_study(tmp_path))
    assert "timed out while healthy" in capsys.readouterr().out


@pytest.mark.parametrize("kwargs", [
    {"reason": "RuntimeError: boom"},
    {"store": False},
    {"done": False},
    {"log": "Traceback (most recent call last):\n"},
])
def test_any_other_state_fails(tmp_path, kwargs):
    with pytest.raises(AssertionError):
        _assert_timed_out_while_healthy(_study(tmp_path, **kwargs))
