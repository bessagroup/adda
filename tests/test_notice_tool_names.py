"""No text adda writes to a node names a tool that node does not hold."""
from __future__ import annotations

import re
from unittest import mock

import pytest
from langchain_core.messages import AIMessage

from adda._src.nodes import wind_down as wd_text
from adda._src.runtime import settings
from adda._src.runtime import time_rules as rules

from .test_wind_down import _entry, _walk

ADDA_TOOLS = re.compile(
    r"\b(Done|Wait|Delegate|WriteDeliverable|WriteNote|ReadNote|WriteCell|"
    r"RunNotebook|FollowUp|HypothesisUpdate|HypothesisList|MilestoneSet|"
    r"QueryStore)\b")


@pytest.fixture(autouse=True)
def _clean():
    settings.configure(None)
    yield
    settings.configure(None)


def _texts(node, tmp_path) -> list[str]:
    """Every runtime text a turn-taking node can be sent, rendered live."""
    ai = AIMessage(content="stopping")
    out: list[str] = []

    def _grab(cmd):
        out.extend(m.content for m in cmd.update["messages"]
                   if m.type == "human")

    with mock.patch.object(node, "_working_delegations", return_value=[]):
        node._finish_attempts = 0
        _grab(node._reprompt_unfinished(ai, False, ["report.md"]))
    with mock.patch.object(node, "_working_delegations",
                           return_value=["D001"]):
        node._finish_attempts = 0
        _grab(node._reprompt_unfinished(ai, False, []))
        _grab(node._reprompt_while_working(ai, False, ["report.md"]))
    r = rules.TimeRules.from_settings(1800, tokens=1000, evals=50)
    done = node._holds("Done")
    for phase, frac in r.schedule():
        out.append(r.notice(phase, r.progress(frac * 1800, 0),
                            can_call_done=done))
    out.append(r.cutoff_refusal(r.progress(0.92 * 1800, 0),
                                can_call_done=done))
    return out


def test_a_node_without_the_routing_tools_is_told_to_use_none(tmp_path):
    node, state, run_dir = _entry(tmp_path)
    node.adapter.closure_tools.clear()
    texts = _texts(node, tmp_path)
    _, sent = _walk(node, state)
    texts += sent + list(node._notifications)
    bad = [str(t) for t in texts if ADDA_TOOLS.search(str(t))]
    assert not bad, f"texts name tools the node lacks: {bad}"


def test_a_node_that_holds_the_tools_is_still_told_to_use_them(tmp_path):
    node, state, run_dir = _entry(tmp_path)
    assert node._holds("Done")
    named = {m.group(1) for t in _texts(node, tmp_path)
             for m in ADDA_TOOLS.finditer(t)}
    assert {"Done", "Wait", "WriteDeliverable"} <= named
    assert wd_text.HUB_NOTICE
