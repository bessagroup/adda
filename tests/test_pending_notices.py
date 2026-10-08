"""A campaign's notice reaches the agent outside the tool result.

The CLI keeps only the first 2 KB of a large tool result, so a warning printed
by the oracle wrapper after a long campaign log is lost. The wrapper also
queues it under ``<run>/debug/pending_notices/``; each backend's post-tool hook
drains the queue and hands it to the model as extra context.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest
from f3dasm._src.core import DataGenerator
from f3dasm._src.experimentsample import ExperimentSample, JobStatus

from adda._src.infra import pending_notices
from adda._src.runtime import settings

from .test_claude_adapter import _get_adapter, _install_fake_sdk
from .test_instrumented import _make_sample

PREVIEW_BYTES = 2048


@pytest.fixture(autouse=True)
def _clean_settings():
    settings.configure(None)
    yield
    settings.configure(None)


class _Gen(DataGenerator):
    def execute(self, experiment_sample: ExperimentSample, **kwargs):
        experiment_sample._output_data["f"] = (
            -999.0 if kwargs.get("override") == "K1" else 42.0)
        experiment_sample.job_status = JobStatus.FINISHED
        return experiment_sample


def _campaign_with_long_log(tmp_path, capsys):
    """10 KB of the agent's own output, then a 150-design batch whose
    corrected evaluations are all dropped. Returns (stdout, debug_dir)."""
    from adda._src.evaluation.instrumented import InstrumentedDataGenerator

    debug = tmp_path / "debug"
    debug.mkdir()
    gen = InstrumentedDataGenerator(
        inner=_Gen(), store_dir=tmp_path / "experiment_data",
        delegation_id="D001", flush_every=1000)
    for i in range(150):
        gen.execute(_make_sample(i / 1000), override="K1")
    gen.flush()
    capsys.readouterr()
    print("campaign log line\n" * 600, end="")
    for i in range(150):
        gen.execute(_make_sample(i / 1000), override="K2")
    gen.flush()
    return capsys.readouterr().out, debug


def test_the_notice_is_outside_the_preview_on_stdout_but_in_the_hook_context(
        tmp_path, capsys):
    out, debug = _campaign_with_long_log(tmp_path, capsys)
    assert "EVAL NOT STORED" in out
    assert "EVAL NOT STORED" not in out[:PREVIEW_BYTES]

    ctx = pending_notices.post_tool_context(None, "Bash", {}, debug, "D001")
    assert ctx is not None
    assert "<adda-note>" in ctx
    assert "[EVAL NOT STORED — D001] 150 evaluation(s)" in ctx
    assert "differs from the STORED row for 150 of the 150" in ctx
    # drained: the next call has nothing
    assert pending_notices.post_tool_context(None, "Bash", {}, debug, "D001") is None


def test_a_notice_is_delivered_only_to_its_own_delegation(tmp_path):
    pending_notices.post(tmp_path, "D001", "for one")
    assert pending_notices.drain(tmp_path, "D002") == []
    assert pending_notices.drain(tmp_path, "D001") == ["for one"]


def test_the_claude_post_tool_hook_hands_the_notice_to_the_model(
        tmp_path, capsys):
    from adda._src.backends.base import set_delegation_id, set_run_config_path

    out, debug = _campaign_with_long_log(tmp_path, capsys)
    captured = {}

    async def _query(prompt, options):
        captured["options"] = options
        return
        yield  # pragma: no cover

    _install_fake_sdk(
        query=_query,
        HookMatcher=lambda hooks: types.SimpleNamespace(hooks=hooks))
    adapter = _get_adapter()("claude-3", "sys", None, [])
    set_delegation_id("D001")
    set_run_config_path(str(debug / "run_config.json"))
    try:
        adapter.invoke([{"role": "user", "content": "hi"}])
    finally:
        set_delegation_id(None)
        set_run_config_path(None)

    hook = captured["options"]["hooks"]["PostToolUse"][0].hooks[0]
    result = asyncio.run(hook({"tool_name": "Bash", "tool_input": {}}, "t", None))
    extra = result["hookSpecificOutput"]["additionalContext"]
    assert "[EVAL NOT STORED — D001] 150 evaluation(s)" in extra


def test_the_openai_compatible_backend_appends_the_notice_to_a_bash_result(
        tmp_path, capsys):
    from adda._src.backends.base import set_delegation_id, set_run_config_path
    from adda._src.backends.openai_compatible import OpenAICompatibleAdapter

    out, debug = _campaign_with_long_log(tmp_path, capsys)
    adapter = OpenAICompatibleAdapter(
        model="m", system_prompt="", closure_tools={}, native_tools=["Bash"],
        study_dir=Path(tmp_path))
    set_delegation_id("D001")
    set_run_config_path(str(debug / "run_config.json"))
    try:
        adapter._notice_ctx = (debug, "D001")  # what _invoke_once binds
    finally:
        set_delegation_id(None)
        set_run_config_path(None)
    bash = {t.name: t for t in adapter._build_tools()}["Bash"]
    result = bash.func(command="echo hi")
    assert "hi" in result
    assert "[EVAL NOT STORED — D001] 150 evaluation(s)" in result


def test_with_science_monitor_off_the_claude_hook_keeps_store_notices(
        tmp_path, capsys):
    from adda._src.backends.base import set_delegation_id, set_run_config_path

    out, debug = _campaign_with_long_log(tmp_path, capsys)
    seen = {}

    async def _query(prompt, options):
        seen["options"] = options
        return
        yield  # pragma: no cover

    _install_fake_sdk(
        query=_query,
        HookMatcher=lambda hooks: types.SimpleNamespace(hooks=hooks))
    settings.configure({"science_monitor": False})
    try:
        adapter = _get_adapter()("claude-3", "sys", None, [])
        set_delegation_id("D001")
        set_run_config_path(str(debug / "run_config.json"))
        try:
            adapter.invoke([{"role": "user", "content": "hi"}])
        finally:
            set_delegation_id(None)
            set_run_config_path(None)
    finally:
        settings.configure(None)
    assert adapter._oracle_nudge.enabled is False
    hook = seen["options"]["hooks"]["PostToolUse"][0].hooks[0]
    result = asyncio.run(hook({"tool_name": "Bash", "tool_input": {}}, "t", None))
    assert "[EVAL NOT STORED" in result["hookSpecificOutput"]["additionalContext"]


def test_with_science_monitor_off_the_openai_backend_drops_only_the_nudge(
        tmp_path, capsys):
    from adda._src.backends.openai_compatible import OpenAICompatibleAdapter

    out, debug = _campaign_with_long_log(tmp_path, capsys)
    adapter = OpenAICompatibleAdapter(
        model="m", system_prompt="", closure_tools={}, native_tools=["Bash"],
        study_dir=Path(tmp_path))
    adapter._notice_ctx = (debug, "D001")
    consulted = []
    adapter._oracle_nudge = types.SimpleNamespace(
        check=lambda *a: consulted.append(a) or "ORACLE ACCESS")
    raw = {"command": "python3 -c 'from evaluator import evaluate'"}
    settings.configure({"science_monitor": False})
    try:
        out = adapter._post_tool_context("Bash", raw)
    finally:
        settings.configure(None)
    assert "[EVAL NOT STORED" in out and "ORACLE ACCESS" not in out
    assert consulted == []
