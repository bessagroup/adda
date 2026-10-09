"""F3DASM_STUDY_ROOT in every agent session env is THIS run's study dir, never
a value the launcher's shell leaked in."""
from __future__ import annotations

import json
import sys

import pytest

from adda._src.backends import base
from adda._src.backends.base import bind_run_context


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    base._study_root_overrides_logged.clear()
    yield
    base._study_root_overrides_logged.clear()


def _run(tmp_path):
    study = tmp_path / "study"
    debug = study / "runs" / "T" / "debug"
    debug.mkdir(parents=True)
    rc = debug / "run_config.json"
    rc.write_text(json.dumps({"study_dir": str(study), "store_dir": str(debug)}))
    return study, debug, rc


def test_claude_session_env_overrides_a_leaked_value_and_logs_it(
        tmp_path, monkeypatch):
    from adda._src.backends.claude import _build_session_env

    study, debug, rc = _run(tmp_path)
    monkeypatch.setenv("F3DASM_STUDY_ROOT", "/some/other/study")
    with bind_run_context("strategizer", str(rc)):
        env = _build_session_env()
    assert env["F3DASM_STUDY_ROOT"] == str(study)
    rows = [json.loads(x) for x in
            (debug / "diagnostics.jsonl").read_text().splitlines()]
    row = [r for r in rows if r["tool"] == "STUDY_ROOT_OVERRIDDEN"]
    assert len(row) == 1 and row[0]["inherited"] == "/some/other/study"


def test_claude_session_env_sets_it_without_a_leak_and_logs_nothing(
        tmp_path, monkeypatch):
    from adda._src.backends.claude import _build_session_env

    study, debug, rc = _run(tmp_path)
    monkeypatch.delenv("F3DASM_STUDY_ROOT", raising=False)
    with bind_run_context("strategizer", str(rc)):
        env = _build_session_env()
    assert env["F3DASM_STUDY_ROOT"] == str(study)
    assert not (debug / "diagnostics.jsonl").exists()


def test_openai_shell_env_pins_it_from_a_thread_without_run_context(
        tmp_path, monkeypatch):
    from adda._src.backends.openai_compatible import _shell_env

    study, debug, rc = _run(tmp_path)
    monkeypatch.setenv("F3DASM_STUDY_ROOT", "/some/other/study")
    env = _shell_env("D001", str(rc))
    assert env["F3DASM_STUDY_ROOT"] == str(study)
    assert sys.executable  # PATH handling unchanged
    assert (debug / "diagnostics.jsonl").exists()


def test_no_run_context_leaves_the_env_alone(monkeypatch):
    from adda._src.backends.openai_compatible import _shell_env

    monkeypatch.setenv("F3DASM_STUDY_ROOT", "/kept")
    assert _shell_env("D001")["F3DASM_STUDY_ROOT"] == "/kept"
