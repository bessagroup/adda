"""The output-token budget clock: the run budget counted in generated tokens.

No live model: usage events are fed by hand. The thresholds themselves are the
wall clock's (``test_time_rules.py``); here the unit changes, not the rules.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra import token_clock, wind_down
from adda._src.nodes import Node
from adda._src.runtime import settings
from adda._src.runtime import time_rules as rules
from adda._src.runtime.constraint_snapshot import compute_constraint_snapshot

BUDGET = 10_000


# -- parse ------------------------------------------------------------------

def test_the_wall_clock_is_the_default():
    assert token_clock.parse({}) is None
    assert token_clock.parse({"budget": "01:00:00"}) is None


def test_the_token_clock_returns_its_budget():
    assert token_clock.parse(
        {"budget_clock": "output_tokens", "token_budget": 5000}) == 5000


@pytest.mark.parametrize("cfg, words", [
    ({"budget_clock": "cpu"}, "budget_clock must be one of"),
    ({"budget_clock": "output_tokens"}, "needs token_budget"),
    ({"budget_clock": "output_tokens", "token_budget": 0}, "positive integer"),
    ({"budget_clock": "output_tokens", "token_budget": 1.5}, "positive integer"),
    ({"budget_clock": "output_tokens", "token_budget": True}, "positive integer"),
    ({"token_budget": 100}, "budget_clock is 'wall'"),
    ({"budget_clock": "output_tokens", "token_budget": 100, "budget": 60},
     "two limits"),
])
def test_a_bad_clock_config_is_rejected_by_name(cfg, words):
    with pytest.raises(ValueError, match=words):
        token_clock.parse(cfg)


# -- the counter ------------------------------------------------------------

def test_the_counter_is_off_until_configured():
    assert not token_clock.enabled()
    assert token_clock.record("k", 50) == 0
    assert token_clock.used() == 0


def test_a_call_counts_once_and_only_grows():
    token_clock.configure(BUDGET)
    assert token_clock.record("a", 10) == 10
    assert token_clock.record("a", 10) == 0
    assert token_clock.record("a", 4) == 0
    assert token_clock.record("a", 25) == 15
    assert token_clock.record("b", 5) == 5
    assert token_clock.used() == 30


def test_a_resume_starts_from_the_seed():
    token_clock.configure(BUDGET, seed=4000)
    token_clock.record("a", 100)
    assert token_clock.used() == 4100


def test_a_call_with_no_count_fails_loudly_and_is_never_estimated():
    token_clock.configure(BUDGET)
    with pytest.raises(token_clock.TokenUsageMissing, match="reported none"):
        token_clock.record("a", None, "the model server's response")
    assert token_clock.used() == 0


def test_a_listener_hears_each_new_count_and_can_be_removed():
    token_clock.configure(BUDGET)
    heard = []
    cb = lambda: heard.append(token_clock.used())  # noqa: E731
    token_clock.on_change(cb)
    token_clock.record("a", 7)
    token_clock.record("a", 7)
    token_clock.remove_listener(cb)
    token_clock.record("b", 3)
    assert heard == [7]


# -- the text ---------------------------------------------------------------

def test_the_notices_state_output_tokens_left():
    r = rules.TimeRules.from_settings(BUDGET, tokens=True)
    text = r.notice("notice:0.75", 7500, can_call_done=True)
    assert text == (
        "Output tokens: 75% of the budget used; 2,500 output tokens left "
        "before the wind-down at 10,000 output tokens. Plan so you can call "
        "Done().")
    assert "token budget of 10,000 output tokens" == r.budget_phrase()
    assert "500 output tokens over" in r.cutoff_refusal(10_500)


# -- the node clock follows the counter -------------------------------------

class _Stub:
    def __init__(self) -> None:
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"

    def invoke(self, messages):
        return "ok"

    def copy(self):
        return _Stub()

    def interrupt(self) -> list:
        return []


@pytest.fixture
def entry(tmp_path):
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "Delegate", "Wait"})
        description = "s"

    class B(Agent):
        role = "implementer"
        description = "i"

    spec = Graph(nodes={"strategizer": A(), "implementer": B()},
                 edges=(Edge("strategizer", "implementer"),),
                 entry="strategizer")
    run_dir = tmp_path / "study" / "runs" / "T"
    (run_dir / "debug").mkdir(parents=True)
    (run_dir / "debug" / "run_config.json").write_text("{}")
    n = Node(_Stub(), name="strategizer", outgoing=["implementer"],
             spec=spec, worker_adapters={"implementer": _Stub()})
    n._current_notes_dir = run_dir / "debug" / "strategizer_notes"
    n._run_start = 1_000_000.0
    settings.configure({})
    token_clock.configure(BUDGET)
    return n


def _diag(node) -> list[str]:
    p = node._current_run_dir / "debug" / "diagnostics.jsonl"
    if not p.exists():
        return []
    return [json.loads(x).get("error_type") for x in p.read_text().splitlines()]


def test_thresholds_fire_on_tokens_not_on_seconds(entry):
    entry._time_rules_tick()
    assert _diag(entry) == []
    token_clock.record("a", 7500)
    entry._time_rules_tick()
    entry._time_rules_tick()
    assert _diag(entry) == ["TIME_NOTICE"]
    assert entry._time_elapsed() == 7500
    token_clock.record("b", 1500)
    assert entry._time_cutoff_refusal().startswith("ERROR: No new delegations")


def test_the_wind_down_begins_at_the_budget_and_publishes_the_stop_epoch(entry):
    token_clock.record("a", BUDGET)
    entry._time_rules_tick()
    assert "TIME_WIND_DOWN" in _diag(entry)
    assert wind_down.active()
    cfg = json.loads(
        (entry._current_run_dir / "debug" / "run_config.json").read_text())
    assert cfg["eval_stop_epoch"] == pytest.approx(entry._wd_started_at)
    rec = json.loads(
        (entry._current_run_dir / "debug" / "wind_down.json").read_text())
    assert (rec["budget"], rec["budget_unit"]) == (BUDGET, "output_tokens")
    entry._time_rules_cancel()


def test_a_model_call_that_crosses_a_threshold_fires_it_on_its_own_thread(entry):
    entry._time_rules_start()
    token_clock.record("a", 7600)
    import time
    for _ in range(100):
        if "TIME_NOTICE" in _diag(entry):
            break
        time.sleep(0.02)
    assert _diag(entry) == ["TIME_NOTICE"]
    entry._time_rules_cancel()


# -- the backends feed the counter -----------------------------------------

def test_the_claude_stream_counts_each_call_at_its_message_delta():
    from adda._src.backends.claude import _count_output_tokens
    token_clock.configure(BUDGET)
    cur = ["m1"]
    assert _count_output_tokens(
        {"type": "message_start", "message": {"usage": {"output_tokens": 1}}},
        cur) == 0
    assert _count_output_tokens(
        {"type": "message_delta", "usage": {"output_tokens": 40}}, cur) == 40
    cur = ["m2"]
    _count_output_tokens(
        {"type": "message_delta", "usage": {"output_tokens": 2}}, cur)
    assert token_clock.used() == 42


def test_a_claude_stream_without_usage_fails():
    from adda._src.backends.claude import _count_output_tokens
    token_clock.configure(BUDGET)
    with pytest.raises(token_clock.TokenUsageMissing):
        _count_output_tokens({"type": "message_delta", "usage": {}}, ["m1"])


def _claude():
    from tests.test_claude_adapter import _get_adapter, _install_fake_sdk
    _install_fake_sdk(query=None)
    return _get_adapter()("claude-3", "sys", None, [])


def test_claude_settles_the_uncounted_remainder_of_an_attempt():
    token_clock.configure(BUDGET)
    a = _claude()
    result = SimpleNamespace(
        usage={"output_tokens": 100}, total_cost_usd=None, session_id="s")
    a._settle_usage(result, {}, None, counted=60)
    assert token_clock.used() == 40


def test_claude_fails_a_finished_attempt_that_reported_no_output():
    token_clock.configure(BUDGET)
    a = _claude()
    result = SimpleNamespace(usage={}, total_cost_usd=None, session_id="s")
    with pytest.raises(token_clock.TokenUsageMissing):
        a._settle_usage(result, {}, None)


def test_the_openai_compatible_backend_counts_each_ai_message_once():
    from adda._src.backends.openai_compatible import OpenAICompatibleAdapter
    token_clock.configure(BUDGET)
    seed = SimpleNamespace(type="human", usage_metadata=None)
    ai1 = SimpleNamespace(type="ai", usage_metadata={"output_tokens": 30})
    ai2 = SimpleNamespace(type="ai", usage_metadata={"output_tokens": 12})
    count = OpenAICompatibleAdapter._count_output_tokens
    count({"messages": [seed, ai1]}, 1, "t")
    count({"messages": [seed, ai1, ai2]}, 1, "t")
    assert token_clock.used() == 42


def test_the_openai_compatible_backend_fails_without_usage():
    from adda._src.backends.openai_compatible import OpenAICompatibleAdapter
    token_clock.configure(BUDGET)
    ai = SimpleNamespace(type="ai", usage_metadata=None)
    with pytest.raises(token_clock.TokenUsageMissing):
        OpenAICompatibleAdapter._count_output_tokens({"messages": [ai]}, 0, "t")


# -- the constraint snapshot and the watchdog -------------------------------

def test_the_constraint_snapshot_states_the_token_budget():
    token_clock.configure(BUDGET)
    token_clock.record("a", 2500)
    s = compute_constraint_snapshot(
        eval_budget=None, budget_seconds=None, run_start=None,
        experiment_data_dir=None)
    assert (s.token_budget, s.tokens_used) == (BUDGET, 2500)
    assert "Output-token budget: 2,500/10,000" in s.as_text()


def _study(tmp_path, cfg: str):
    d = tmp_path / "study"
    d.mkdir()
    (d / "PROBLEM_STATEMENT.md").write_text("x")
    (d / "config.yaml").write_text(cfg)
    return d


def test_the_watchdog_deadline_is_the_explicit_watchdog_wall_s(
        tmp_path, monkeypatch):
    import adda._src.infra.watchdog_launcher as wl
    d = _study(tmp_path,
               "budget_clock: output_tokens\ntoken_budget: 5000\n"
               "watchdog_wall_s: 7200\n")
    seen = {}

    def fake(cmd, *, deadline_s, **kw):
        seen.update(cmd=cmd, deadline_s=deadline_s)
        return wl.WatchdogResult(timed_out=False, returncode=0, pgid=1)

    monkeypatch.setattr(wl, "run_under_watchdog", fake)
    assert wl.main([str(d)]) == 0
    assert seen["deadline_s"] == 7200.0
    assert "--budget" not in seen["cmd"]


def test_the_watchdog_refuses_a_token_run_with_no_wall_limit(tmp_path, capsys):
    import adda._src.infra.watchdog_launcher as wl
    d = _study(tmp_path, "budget_clock: output_tokens\ntoken_budget: 5000\n")
    assert wl.main([str(d)]) == 2
    assert "watchdog_wall_s" in capsys.readouterr().err


def test_the_watchdog_refuses_budget_next_to_the_token_clock(tmp_path, capsys):
    import adda._src.infra.watchdog_launcher as wl
    d = _study(tmp_path,
               "budget_clock: output_tokens\ntoken_budget: 5000\n"
               "watchdog_wall_s: 60\n")
    assert wl.main([str(d), "--budget", "60"]) == 2
    assert "drop --budget" in capsys.readouterr().err


def test_a_summary_call_counts_on_the_clock(monkeypatch):
    import langchain_openai

    from adda._src.backends.ollama import OllamaAdapter

    class _LLM:
        def __init__(self, **kw):
            pass

        def invoke(self, msgs):
            return SimpleNamespace(
                content="s", usage_metadata={"output_tokens": 9})

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _LLM)
    token_clock.configure(BUDGET)
    a = OllamaAdapter(model="m", system_prompt="s")
    assert a._summarize("p") == "s"
    assert token_clock.used() == 9


def test_the_evaluator_refuses_once_the_stop_epoch_appears_mid_run(tmp_path):
    import time

    from adda._src.evaluation.instrumented import InstrumentedDataGenerator
    from tests.test_pending_notices import _Gen, _make_sample

    stop = [None]
    gen = InstrumentedDataGenerator(
        inner=_Gen(), store_dir=tmp_path / "experiment_data",
        delegation_id="D001", stop_after=lambda: stop[0])
    gen.execute(_make_sample(0.1))
    stop[0] = time.time() - 1
    with pytest.raises(RuntimeError, match="no new evaluations"):
        gen.execute(_make_sample(0.2))


def test_a_wall_budget_next_to_the_token_clock_stops_the_run_at_construction(
        tmp_path):
    from adda._src.runtime.agent_runtime import AgenticRun
    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x")
    (tmp_path / "config.yaml").write_text(
        "budget_clock: output_tokens\ntoken_budget: 5000\n")
    with pytest.raises(ValueError, match="two limits"):
        AgenticRun(tmp_path, budget=60)


# -- plain Claude Code: the operator ends the session at the cap --------------

def _plain_stream(consumed):
    """A fake CLI stream: three API calls of 60 output tokens each."""
    from tests.test_claude_adapter import _StreamEvent

    def ev(**e):
        m = _StreamEvent()
        m.event = e
        return m

    async def _query(prompt, options):
        _query.options = options
        for i in range(3):
            consumed.append(i)
            yield ev(type="message_start", message={"id": f"m{i}", "usage": {}})
            yield ev(type="message_delta", usage={"output_tokens": 60})
    return _query


def _plain_adapter(query):
    from adda._src.backends.base import DEFAULT_PROMPT, DEFAULT_TOOLS
    from tests.test_claude_adapter import _get_adapter, _install_fake_sdk
    _install_fake_sdk(
        query=query, HookMatcher=lambda hooks: SimpleNamespace(hooks=hooks))
    a = _get_adapter()("claude-3", "", None, [DEFAULT_TOOLS])
    a.base_prompt = DEFAULT_PROMPT
    return a


def test_the_plain_arm_is_stopped_when_the_count_reaches_the_cap():
    token_clock.configure(100)
    consumed: list[int] = []
    q = _plain_stream(consumed)
    a = _plain_adapter(q)
    a.invoke([{"role": "user", "content": "go"}])
    assert consumed == [0, 1], "the third call is never read"
    stop = token_clock.stop_info()
    assert stop["output_tokens_used"] == 120 and stop["tokens_over"] == 20
    assert not q.options.get("hooks"), "the plain arm still builds no hook"


def test_an_adda_node_is_not_stopped_by_the_stream_at_the_cap():
    from tests.test_claude_adapter import _get_adapter, _install_fake_sdk
    token_clock.configure(100)
    consumed: list[int] = []
    _install_fake_sdk(query=_plain_stream(consumed))
    _get_adapter()("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "go"}])
    assert consumed == [0, 1, 2] and token_clock.stop_info() is None


def test_a_plain_stop_ends_the_run_with_no_notice_and_records_it(tmp_path):
    from tests.test_route_aware_termination import (
        StubAdapter, _make_state, _minimal_spec)

    from adda._src.runtime import terminal
    study = tmp_path / "study"
    study.mkdir()
    (study / "pipeline.ipynb").write_text("# t\n")
    token_clock.configure(100)
    token_clock.note_stop()
    node = Node(StubAdapter(response="partial"), name="strategizer",
                outgoing=["implementer"], spec=_minimal_spec())
    cmd = node(_make_state(study_dir=study))
    assert cmd.update["done"] and cmd.update["termination"] == (
        terminal.TOKEN_BUDGET)
    assert cmd.update["outcome"] == terminal.UNGATED
    assert [m for m in cmd.update["messages"] if m.type == "human"] == []
    stop = token_clock.stop_info()
    assert stop["deliverables_present"] == ["pipeline.ipynb"]
    assert stop["deliverables_missing"] == []
