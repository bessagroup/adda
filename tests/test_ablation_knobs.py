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


_ALL_TOOLS = frozenset({"Read", "Write", "Bash", "WriteNote", "Delegate"})


def test_the_delegation_contract_leaves_the_preamble_when_off():
    settings.configure({})
    assert "<delegation_contract>" in features.resolve_gates(
        WORKSPACE_PREAMBLE_TEMPLATE, holds=_ALL_TOOLS)
    _off("delegation_contract")
    text = features.resolve_gates(
        WORKSPACE_PREAMBLE_TEMPLATE, holds=_ALL_TOOLS)
    assert "<delegation_contract>" not in text
    assert "get_evaluator" in text


def test_budget_notes_off_silences_warnings_and_snapshot():
    state = {"eval_budget": 1, "evals_used": 5}
    node = SimpleNamespace(_ledgered_eval_total=lambda n: n,
                           _time_rules_tick=lambda: None)
    settings.configure({})
    _off("budget_notes")
    assert orchestration.OrchestrationMixin._budget_warnings(node, state) == []
    assert orchestration.OrchestrationMixin._constraint_refresh(node) == []


def test_reprompt_unfinished_off_lets_the_run_end_and_drops_the_banner():
    node = SimpleNamespace(_finish_attempts=0, _silent=False)
    settings.configure({})
    held = orchestration.OrchestrationMixin._reprompt_unfinished
    _off("reprompt_unfinished")
    assert held(node, None, False, []) is None
    assert node._finish_attempts == 0
    assert orchestration.OrchestrationMixin._banner(node, "s", False, ["x"]) == "s"


def test_an_unaccepted_close_is_still_recorded_ungated_with_reprompts_off():
    import threading

    _off("reprompt_unfinished")
    node = SimpleNamespace(
        _registry_lock=threading.Lock(), _delegation_seq=0, _seq_at_turn_start=0,
        _registry={}, _route={}, _token_totals={}, _error_counts={},
        _banner=lambda *a, **k: orchestration.OrchestrationMixin._banner(node, *a, **k),
        _flush_ghost_delegations=lambda: None,
        _ledgered_eval_total=lambda n: n)
    state = {"total_delegations": 0, "evals_used": 0}
    cmd = orchestration.OrchestrationMixin._terminate_run(
        node, state, SimpleNamespace(content="done?"), False, [])
    assert cmd.update["outcome"] == "UNGATED"
    assert cmd.update["reviewed"] is False
