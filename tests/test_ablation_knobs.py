"""budget_notes, delegation_contract and reprompt_unfinished: on by default, off means gone."""

from __future__ import annotations

from types import SimpleNamespace

from adda._src.nodes import orchestration
from adda._src.prompts.agent_prompts import WORKSPACE_PREAMBLE_TEMPLATE
from adda._src.runtime import features, settings

KNOBS = ("budget_notes", "delegation_contract", "reprompt_unfinished")


def _off(*keys):
    settings.configure({k: False for k in keys})


def teardown_function():
    settings.configure({})


def test_the_knobs_are_registered_and_default_on():
    settings.configure({})
    assert all(features.enabled(k) for k in KNOBS)
    assert set(KNOBS) <= features.FEATURE_KEYS


def test_the_delegation_contract_leaves_the_preamble_when_off():
    settings.configure({})
    assert "<delegation_contract>" in features.resolve_gates(WORKSPACE_PREAMBLE_TEMPLATE)
    _off("delegation_contract")
    text = features.resolve_gates(WORKSPACE_PREAMBLE_TEMPLATE)
    assert "<delegation_contract>" not in text
    assert "get_evaluator" in text


def _node(**kw):
    return SimpleNamespace(_budget_seconds=60.0, _run_start=0.0,
                           _budget_bands_fired=set(), **kw)


def test_budget_notes_off_silences_warnings_and_snapshot():
    state = {"eval_budget": 1, "evals_used": 5}
    node = SimpleNamespace(_ledgered_eval_total=lambda n: n,
                           _budget_seconds=None, _run_start=None)
    settings.configure({})
    assert orchestration.OrchestrationMixin._budget_warnings(node, state)
    _off("budget_notes")
    assert orchestration.OrchestrationMixin._budget_warnings(node, state) == []
    assert orchestration.OrchestrationMixin._constraint_refresh(node) == []


def test_reprompt_unfinished_off_lets_the_run_end_and_drops_the_banner():
    node = SimpleNamespace(_finish_attempts=0)
    settings.configure({})
    held = orchestration.OrchestrationMixin._reprompt_unfinished
    _off("reprompt_unfinished")
    assert held(node, None, False, []) is None
    assert node._finish_attempts == 0
    assert orchestration.OrchestrationMixin._banner(node, "s", False, ["x"]) == "s"
