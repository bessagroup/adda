"""After a run is abandoned, no backend makes another model or tool call."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from adda._src.infra import run_abandon
from adda._src.infra.run_abandon import RunAbandoned


def _openai_adapter():
    from adda._src.backends.ollama import OllamaAdapter
    return OllamaAdapter(model="llama3.2", system_prompt="sys")


def _stream_agent(pulled: list, n: int = 5, after: int | None = None):
    """A graph whose stream yields n states; counts how many were pulled."""
    agent = MagicMock()

    def _stream(state, config=None, stream_mode=None):
        for i in range(n):
            pulled.append(i)
            if after is not None and i == after:
                run_abandon.request_stop()
            yield {"messages": [MagicMock(content=f"step {i}")]}

    agent.stream.side_effect = _stream
    return agent


def test_openai_backend_makes_no_model_call_once_stopped():
    pulled: list = []
    adapter = _openai_adapter()
    adapter._agent = _stream_agent(pulled)
    run_abandon.request_stop()
    with pytest.raises(RunAbandoned):
        adapter.invoke([{"role": "user", "content": "hi"}])
    assert pulled == []
    adapter._agent.stream.assert_not_called()
    assert run_abandon.blocked_calls() == 1


def test_openai_backend_stops_before_the_next_step_of_a_running_turn():
    pulled: list = []
    adapter = _openai_adapter()
    adapter._agent = _stream_agent(pulled, after=1)
    with pytest.raises(RunAbandoned):
        adapter.invoke([{"role": "user", "content": "hi"}])
    assert pulled == [0, 1]
    assert run_abandon.blocked_calls() == 1


def test_openai_native_tool_is_refused_once_stopped():
    from adda._src.backends.openai_compatible import _guard_native

    ran: list = []
    tool = SimpleNamespace(name="Bash", func=lambda **k: ran.append(k) or "ok")
    _guard_native(tool)
    assert tool.func(cmd="ls") == "ok"
    run_abandon.request_stop()
    with pytest.raises(RunAbandoned):
        tool.func(cmd="ls")
    assert len(ran) == 1


def _claude_adapter(query):
    from tests.test_claude_adapter import _get_adapter, _install_fake_sdk
    _install_fake_sdk(query=query)
    return _get_adapter()("claude-3", "sys", None, [])


def test_claude_backend_makes_no_model_call_once_stopped():
    from tests.test_claude_adapter import _AssistantMessage, _TextBlock
    started: list = []

    async def _query(prompt, options):
        started.append(1)
        yield _AssistantMessage([_TextBlock("x")])

    adapter = _claude_adapter(_query)
    run_abandon.request_stop()
    with pytest.raises(RunAbandoned):
        adapter.invoke([{"role": "user", "content": "hi"}])
    assert started == []


def test_claude_backend_stops_reading_the_stream_once_stopped():
    from tests.test_claude_adapter import _AssistantMessage, _TextBlock
    pulled: list = []

    async def _query(prompt, options):
        for i in range(5):
            pulled.append(i)
            if i == 1:
                run_abandon.request_stop()
            yield _AssistantMessage([_TextBlock(str(i))])

    adapter = _claude_adapter(_query)
    with pytest.raises(RunAbandoned):
        adapter.invoke([{"role": "user", "content": "hi"}])
    assert pulled == [0, 1]
    assert run_abandon.blocked_calls() >= 1


def test_run_abandoned_row_records_how_many_calls_were_blocked(
        tmp_path, monkeypatch):
    from adda._src.runtime.agent_runtime import AgenticRun

    monkeypatch.setattr("adda._src.runtime.agent_runtime._ABANDON_GRACE_S", 0.1)

    study = tmp_path / "study"
    study.mkdir()
    (study / "PROBLEM_STATEMENT.md").write_text("# t\n")
    run = AgenticRun(study_dir=study, interactive=False)
    run_abandon.reset_stop()
    run_abandon.request_stop()
    for _ in range(2):
        with pytest.raises(RunAbandoned):
            run_abandon.raise_if_stopped("n")
    never = threading.Event()
    ctx = SimpleNamespace(debug_dir=tmp_path)
    run._abandon_graph(never, ctx)
    row = json.loads((tmp_path / "diagnostics.jsonl").read_text()
                     .splitlines()[-1])
    assert row["error_type"] == "RUN_ABANDONED"
    assert row["detail"]["calls_blocked"] == 2
    assert "calls blocked by the stop signal: 2" in row["message"]
