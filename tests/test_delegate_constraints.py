"""Delegate(constraints=[{text, basis}]): each constraint reaches the worker
with its basis, and a `prior` one must be answered in the report.
"""
from __future__ import annotations

import json

import pytest

from adda._src.nodes.tools.routing.delegation import (
    _constraints_brief,
    _parse_constraints,
)
from tests.test_list_arg_schema import (  # noqa: F401  (fixture re-used)
    _isolate_from_milestone_gate,
    _node,
    _register_two_hypotheses,
    _structured_tools,
)

GOOD = [
    {"text": "t/L stays below 0.2", "basis": "evidence:D003"},
    {"text": "use a 6-fold cell", "basis": "literature:Smith2020"},
    {"text": "hub must be rigid", "basis": "prior"},
]


def test_parse_accepts_the_three_bases_as_list_or_json_string():
    for raw in (GOOD, json.dumps(GOOD)):
        out, err = _parse_constraints(raw)
        assert err is None and [c["basis"] for c in out] == [
            "evidence:D003", "literature:Smith2020", "prior"]


@pytest.mark.parametrize("raw", [None, [], ""])
def test_parse_empty_is_no_constraints(raw):
    assert _parse_constraints(raw) == ([], None)


@pytest.mark.parametrize("bad", [
    [{"text": "x", "basis": "hunch"}],
    [{"text": "x", "basis": "evidence:"}],
    [{"text": "x"}],
    [{"basis": "prior"}],
    [{"text": " ", "basis": "prior"}],
    ["just a string"],
    "not json",
])
def test_parse_refuses_malformed(bad):
    out, err = _parse_constraints(bad)
    assert out == [] and err.startswith("ERROR")


def test_brief_shows_each_basis_and_asks_only_about_priors():
    b = _constraints_brief(_parse_constraints(GOOD)[0])
    assert "t/L stays below 0.2 [basis: evidence:D003]" in b
    assert "hub must be rigid [basis: prior]" in b
    assert "contradicted" in b
    no_prior = _constraints_brief(_parse_constraints(GOOD[:2])[0])
    assert "contradicted" not in no_prior
    assert _constraints_brief([]) == ""


def test_delegate_puts_constraints_in_the_workers_brief(tmp_path):
    node, worker = _node(tmp_path)
    h1, _ = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())
    tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": [h1], "wait": True, "constraints": GOOD})
    sent = "\n".join(worker.seen)
    assert "[basis: literature:Smith2020]" in sent
    assert "whether the data you produced contradicted it" in sent


def test_delegate_refuses_a_bad_basis_before_dispatch(tmp_path):
    node, worker = _node(tmp_path)
    h1, _ = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())
    out = tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": [h1], "wait": True,
        "constraints": [{"text": "x", "basis": "hunch"}]})
    assert "ERROR" in out and "basis" in out and not worker.seen


def test_delegate_without_constraints_is_unchanged(tmp_path):
    node, worker = _node(tmp_path)
    h1, _ = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())
    tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": [h1], "wait": True})
    assert "[basis:" not in "\n".join(worker.seen)


def test_constraints_are_logged_and_survive_the_terminal_record(tmp_path):
    node, _ = _node(tmp_path)
    h1, _ = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())
    tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": [h1], "wait": True, "constraints": GOOD})
    rec = node._delegation_log._load_all()[-1]
    assert rec["task_constraints"] == _parse_constraints(GOOD)[0]
    assert rec["status"] != "RUNNING"
