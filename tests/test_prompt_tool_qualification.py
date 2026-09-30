"""On the Claude backend the SDK exposes closure tools only as
``mcp__<server>__<tool>``; prose that names one bare sends the model to a tool
that does not exist (run 20260928T225501: ReportEvals/SendMessage "No such
tool", and a bare Write hits the CLI's own disabled native Write)."""
import re
from types import SimpleNamespace

import pytest

from adda._src.agents._graphs import _default_graph
from adda._src.backends.claude import ClaudeAdapter
from adda._src.infra.delegation_log import DelegationLog
from adda._src.nodes import Node
from adda._src.prompts.tool_catalog import (
    qualify_tool_mentions,
    system_prompt_with_catalog,
)

Q = "mcp__f3dasm_agent_tools__"


def _names(*n):
    return {x: Q + x for x in n}


def test_call_backtick_and_multiword_mentions_are_qualified():
    out = qualify_tool_mentions(
        "Call ReportEvals once; use `Write` then Write('a') and SendMessage.",
        _names("ReportEvals", "Write", "SendMessage"))
    assert out == (f"Call {Q}ReportEvals once; use `{Q}Write` then "
                   f"{Q}Write('a') and {Q}SendMessage.")


def test_english_uses_qualified_names_and_attributes_are_untouched():
    text = (f"Wait for the reviewer. Write the report. status Done. "
            f"{Q}Write( x.Wait( ) `{Q}Done`")
    assert qualify_tool_mentions(
        text, _names("Wait", "Write", "Done")) == text


def _role_prompts(tmp_path):
    g = _default_graph()
    (tmp_path / "debug").mkdir(exist_ok=True)

    class _S:
        def __init__(self):
            self.closure_tools = {}
            self.last_usage = {}
            self.model = "m"

        def invoke(self, messages):
            return ""

    for role, agent in g.nodes.items():
        out = [e.target for e in g.edges if e.source == role]
        n = Node(_S(), name=role, outgoing=out, spec=g, study_dir=tmp_path,
                 agent_tools=frozenset(agent.tools or ()),
                 worker_adapters={o: _S() for o in out}, notes_dir=None,
                 delegation_log=DelegationLog(tmp_path / "debug" / "dl.jsonl"))
        yield role, agent.system_prompt, dict(n.adapter.closure_tools)


def test_every_role_prompt_on_claude_names_only_callable_tools(tmp_path):
    for role, prompt, closures in _role_prompts(tmp_path):
        ns = SimpleNamespace(system_prompt=prompt, closure_tools=closures)
        rendered = ClaudeAdapter._render_system_prompt(ns)
        for name in closures:
            multi = re.search(r"[a-z][A-Z]", name)
            for m in re.finditer(rf"(?<![\w.]){name}(?!\w)", rendered):
                after = rendered[m.end():m.end() + 1]
                before = rendered[m.start() - 1]
                if after == "(" or before == "`" or multi:
                    pytest.fail(f"{role}: bare {name} at "
                                f"{rendered[max(0, m.start()-30):m.end()+10]!r}")
        assert closures and all(
            f"### {Q}{n}" in rendered for n in closures)


def test_other_backends_render_prose_unqualified(tmp_path):
    for _, prompt, closures in _role_prompts(tmp_path):
        assert "mcp__" not in system_prompt_with_catalog(prompt, closures)
