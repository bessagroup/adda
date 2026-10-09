"""ClaudeCodeAgent: the session is plain Claude Code, nothing from adda."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import adda
from adda._src.backends.base import DEFAULT_PROMPT, DEFAULT_TOOLS
from adda._src.infra import token_clock
from adda._src.nodes.node import Node
from tests.test_claude_adapter import (
    _AssistantMessage,
    _ResultMessage,
    _get_adapter,
    _install_fake_sdk,
    _TextBlock,
)

STATEMENT = "Minimise y = (x1-1)^2 + (x2+2)^2.\n\nReturn the optimum.  \n"
RENDER = Path(__file__).with_name("_claude_code_render.py")


class _Result(_ResultMessage):
    session_id = "sess-1"


def _recording_query(calls):
    async def _q(prompt, options):
        calls.append((prompt, options))
        yield _AssistantMessage([_TextBlock("ok")])
        yield _Result()
    return _q


def _agent_adapter(calls):
    _install_fake_sdk(query=_recording_query(calls))
    a = _get_adapter()("claude-haiku-4-5-20251001", "", None, [DEFAULT_TOOLS])
    a.base_prompt = DEFAULT_PROMPT
    return a


def test_the_class_declares_plain_claude_code():
    a = adda.ClaudeCodeAgent()
    assert a.tools == frozenset({DEFAULT_TOOLS})
    assert a.base_prompt == DEFAULT_PROMPT and a.system_prompt == ""


def test_a_first_prompt_is_the_statement_byte_for_byte():
    calls: list = []
    _agent_adapter(calls).invoke([{"role": "user", "content": STATEMENT}])
    assert calls[0][0] == STATEMENT
    assert calls[0][1].get("resume") is None


def test_b_system_prompt_is_the_stock_preset_without_append():
    calls: list = []
    _agent_adapter(calls).invoke([{"role": "user", "content": STATEMENT}])
    assert calls[0][1]["system_prompt"] == {
        "type": "preset", "preset": "claude_code"}


def test_c_turn_two_sends_only_the_new_text_in_the_same_session():
    calls: list = []
    a = _agent_adapter(calls)
    a.invoke([{"role": "user", "content": STATEMENT}])
    a.invoke([{"role": "user", "content": STATEMENT},
              {"role": "assistant", "content": "ok"},
              {"role": "user", "content": "more please"}])
    prompt, options = calls[1]
    assert prompt == "more please"
    assert options["resume"] == "sess-1"


_PLUMBING_ENV = {"PATH", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                 "ADDA_SESSION_TOKEN"}


def test_d_no_adda_tool_or_variable_reaches_the_session():
    calls: list = []
    _agent_adapter(calls).invoke([{"role": "user", "content": STATEMENT}])
    prompt, options = calls[0]
    assert "adda" not in prompt.lower()
    assert not options.get("mcp_servers")
    assert not options.get("hooks")
    assert not options.get("disallowed_tools")
    assert set(options["env"]) <= _PLUMBING_ENV
    assert "BASH_DEFAULT_TIMEOUT_MS" not in options["env"]


def test_e_the_node_source_does_not_name_the_agent_class():
    nodes = Path(adda.__file__).parent / "_src" / "nodes"
    for f in nodes.rglob("*.py"):
        assert "ClaudeCodeAgent" not in f.read_text(encoding="utf-8"), f


def test_f_the_rendered_command_differs_from_bare_claude_only_in_the_listed_flags():
    out = subprocess.run([sys.executable, str(RENDER)], capture_output=True,
                         text=True, check=True).stdout
    cmd = json.loads(out.strip().splitlines()[-1])["cmd"]
    bare = ["claude", "--output-format", "stream-json", "--verbose",
            "--input-format", "stream-json"]
    extra = ["--tools", "default", "--model", "claude-haiku-4-5-20251001",
             "--permission-mode", "bypassPermissions",
             "--include-partial-messages", "--setting-sources="]
    rest = list(cmd)
    for tok in bare:
        rest.remove(tok)
    assert rest == extra


def test_a_plain_node_gets_the_statement_and_no_notice_or_clock():
    from tests.test_route_aware_termination import (
        StubAdapter,
        _make_state,
        _minimal_spec,
    )
    spec = _minimal_spec()
    spec.nodes["strategizer"] = adda.ClaudeCodeAgent()
    node = Node(StubAdapter(response="hi"), name="strategizer",
                outgoing=[], spec=spec)
    assert node._silent
    assert node._time_rules_ensure() is None


def test_at_the_cap_the_stream_stop_ends_the_plain_session_with_no_wind_down(
        tmp_path):
    """Both budgets over, a wind-down would be due: the plain node is never
    re-prompted; the run ends at BUDGET_STOP."""
    from tests.test_route_aware_termination import (
        StubAdapter,
        _make_state,
        _minimal_spec,
    )

    from adda._src.runtime import terminal
    study = tmp_path / "study"
    study.mkdir()
    (study / "pipeline.ipynb").write_text("# t\n")
    token_clock.configure(100, wall=(time.time() - 1000, 10))
    token_clock.note_stop()
    spec = _minimal_spec()
    spec.nodes["strategizer"] = adda.ClaudeCodeAgent()
    adapter = StubAdapter(response="partial")
    node = Node(adapter, name="strategizer", outgoing=[], spec=spec)
    node._run_start = time.time() - 1000
    cmd = node(_make_state(study_dir=study))
    assert cmd.update["termination"] == terminal.BUDGET_STOP
    assert [m for m in cmd.update["messages"] if m.type == "human"] == []
    assert adapter.invoke_count == 1, "no re-prompt after the stream break"
    assert node._time_rules is None


def test_an_ordinary_node_under_the_same_budget_does_hold_a_wind_down_clock():
    from tests.test_route_aware_termination import StubAdapter, _minimal_spec
    token_clock.configure(100, wall=(time.time() - 1000, 10))
    node = Node(StubAdapter(), name="strategizer", outgoing=["implementer"],
                spec=_minimal_spec())
    node._run_start = time.time() - 1000
    node._budget_seconds = 10
    assert not node._silent and node._time_rules_ensure() is not None
