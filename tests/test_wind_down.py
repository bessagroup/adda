"""The wind-down: what happens when the declared time budget is spent.

Nothing is killed. A gate refuses new work with an ERROR that names the rule,
the entry node is walked through wait, save and Done, and the close runs ONE
reproduction gate and ONE critic review. No live model: stub adapters.
"""
from __future__ import annotations

import asyncio
import json
import time
import types
from unittest import mock

import pytest

from adda._src.infra import wind_down as gate
from adda._src.nodes import Node
from adda._src.nodes import wind_down as wd_text
from adda._src.runtime import settings, terminal

from .test_claude_adapter import _get_adapter, _install_fake_sdk
from .test_route_aware_termination import (
    StubAdapter, _make_state, _minimal_spec, _run_to_the_end)


@pytest.fixture(autouse=True)
def _clean():
    settings.configure(None)
    yield
    settings.configure(None)


# -- the gate -----------------------------------------------------------------

def test_the_gate_is_off_until_the_wind_down_begins():
    assert gate.check("entry", "Delegate", can_call_done=True) is None
    assert gate.calls() == {}


def test_a_tool_that_starts_new_work_is_refused_with_the_rule():
    gate.begin(50)
    for tool in ("Delegate", "Bash", "RunNotebook", "RunScratch", "WebSearch"):
        out = gate.check("entry", tool, can_call_done=True)
        assert out.startswith("ERROR: wind-down rule:")
        assert f"{tool} is closed" in out and "Done()" in out
    assert gate.calls() == {}, "a refused call is not counted"


def test_a_tool_that_writes_up_what_exists_is_allowed_and_counted():
    gate.begin(50)
    for tool in ("Wait", "Read", "Write", "WriteNote", "WriteDeliverable",
                 "mcp__f3dasm_agent_tools__QueryStore"):
        assert gate.check("entry", tool, can_call_done=True) is None
    assert gate.calls() == {"entry": 6}


def test_past_the_tool_call_limit_only_done_is_allowed():
    gate.begin(3)
    for _ in range(3):
        assert gate.check("entry", "Read", can_call_done=True) is None
    out = gate.check("entry", "Read", can_call_done=True)
    assert "used 3 of your 3 tool calls" in out and "Only Done() is allowed" in out
    assert gate.check("entry", "Done", can_call_done=True) is None
    assert gate.check("D001", "Read", can_call_done=False) is None, (
        "the count is per node")


def test_a_worker_is_told_to_report_not_to_call_done():
    gate.begin(1)
    assert "your final report" in gate.check("D001", "Delegate", can_call_done=False)
    gate.check("D001", "Read", can_call_done=False)
    assert "Only your final report is allowed" in gate.check(
        "D001", "Read", can_call_done=False)


def test_the_close_out_review_is_not_gated():
    gate.begin(1)
    with gate.suspend():
        assert gate.check("entry", "Bash", can_call_done=True) is None
    assert gate.check("entry", "Bash", can_call_done=True) is not None


# -- every tool path passes the gate ------------------------------------------

def test_an_adda_tool_is_refused_at_the_closure_wrapper():
    n = Node(StubAdapter(), name="strategizer", outgoing=["implementer"],
             spec=_minimal_spec())
    ran = []
    gate.begin(50)
    out = n._wrap_closure(_named("RunNotebook", ran), "strategizer")()
    assert out.startswith("ERROR: wind-down rule:") and not ran


def _named(name, ran):
    def fn():
        ran.append(1)
        return "ok"
    fn.__name__ = name
    return fn


def test_an_openai_compatible_native_tool_is_refused_by_the_guard():
    from adda._src.backends.openai_compatible import _guard_native

    class T:
        name = "Bash"
        func = staticmethod(lambda **kw: "ran")

    guarded = _guard_native(T(), None, lambda: None)
    assert guarded.func(command="x") == "ran"
    gate.begin(50)
    out = guarded.func(command="x")
    assert out.startswith("ERROR: wind-down rule:") and "Bash is closed" in out


def _claude_hooks(*, plain=False):
    captured = {}

    async def _query(prompt, options):
        captured["options"] = options
        return
        yield  # pragma: no cover

    _install_fake_sdk(
        query=_query,
        HookMatcher=lambda hooks: types.SimpleNamespace(hooks=hooks))
    if plain:
        from adda._src.backends.base import DEFAULT_PROMPT, DEFAULT_TOOLS
        adapter = _get_adapter()("claude-3", "", None, [DEFAULT_TOOLS])
        adapter.base_prompt = DEFAULT_PROMPT
    else:
        adapter = _get_adapter()("claude-3", "sys", None, [])
    adapter.invoke([{"role": "user", "content": "hi"}])
    return captured["options"].get("hooks")


def test_the_claude_pre_tool_hook_denies_a_native_tool_and_skips_adda_tools():
    hooks = _claude_hooks()
    hook = hooks["PreToolUse"][0].hooks[0]
    run = lambda name: asyncio.run(hook({"tool_name": name}, "t", None))  # noqa: E731
    assert run("Bash") == {}
    gate.begin(50)
    out = run("Bash")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "wind-down rule" in out["permissionDecisionReason"]
    assert run("mcp__f3dasm_agent_tools__Delegate") == {}, (
        "adda tools are gated once, in the closure wrapper")


def test_plain_claude_code_gets_no_hook_and_no_wind_down():
    """The vanilla arm stays vanilla: with the gate on, its adapter still
    builds no hook at all, so nothing in it can consult the gate."""
    gate.begin(50)
    assert not _claude_hooks(plain=True)
    assert _claude_hooks(plain=False)["PreToolUse"], (
        "control: the same call on an adda node does build the hook")


# -- the entry node's walk ----------------------------------------------------

def _entry(tmp_path, adapter=None):
    study = tmp_path / "study"
    study.mkdir()
    (study / "pipeline.ipynb").write_text("# t\n")
    run_dir = study / "runs" / "T"
    (run_dir / "debug").mkdir(parents=True)
    node = Node(adapter or StubAdapter(response="what exists"),
                name="strategizer", outgoing=["implementer"],
                spec=_minimal_spec())
    node._current_notes_dir = run_dir / "debug" / "strategizer_notes"
    state = _make_state(study_dir=study)
    state.update(budget_seconds=10, start_time=time.time() - 100,
                 run_dir=str(run_dir))
    return node, state, run_dir


def _walk(node, state):
    sent = []
    for _ in range(12):
        cmd = node(state)
        if cmd.goto != "strategizer" and cmd.update.get("done"):
            return cmd, sent
        new = list(cmd.update["messages"])
        sent += [m.content for m in new if m.type == "human"]
        state["messages"] = list(state["messages"]) + new
    raise AssertionError("no close")


def test_the_entry_node_is_walked_through_save_then_done_then_the_close(tmp_path):
    node, state, run_dir = _entry(tmp_path)
    cmd, sent = _walk(node, state)
    assert wd_text.SAVE_TURN in sent
    assert wd_text.DONE_TURN.format(n=1, k=2) in sent
    assert wd_text.DONE_TURN.format(n=2, k=2) in sent
    assert cmd.update["termination"] == terminal.BUDGET_WIND_DOWN
    rec = json.loads((run_dir / "debug" / "wind_down.json").read_text())
    assert rec["forced_turns"] == 3 and "critic_verdict" in rec


def test_the_close_runs_one_reproduction_gate_and_no_rework(tmp_path):
    node, state, run_dir = _entry(tmp_path)
    runs = []
    node._reproduction_gate = lambda: runs.append(1) or "notebook fails"
    cmd, _ = _walk(node, state)
    assert runs == [1]
    rec = json.loads((run_dir / "debug" / "wind_down.json").read_text())
    assert rec["reproduction_gate"].startswith("FAIL")
    assert cmd.update["outcome"] == terminal.UNGATED
    assert "BUDGET SPENT" in cmd.update["last_report"]


def test_a_running_delegation_is_waited_for_and_never_cancelled(tmp_path):
    """No loss of science: a delegation that does not report is waited for
    (its own call limit or the external watchdog ends it), never cancelled."""
    node, state, run_dir = _entry(tmp_path)
    sent = []
    with mock.patch.object(node, "_pending_delegations",
                           return_value=["D001"]):
        with mock.patch.object(node, "_stop_cancel_stragglers") as cancel:
            for _ in range(6):
                cmd = node(state)
                assert not cmd.update.get("done")
                new = list(cmd.update["messages"])
                sent += [m.content for m in new if m.type == "human"]
                state["messages"] = list(state["messages"]) + new
    assert cancel.call_count == 0
    assert wd_text.WAIT_TURN.format(n=1, ids=["D001"], tail="") in sent
    assert wd_text.WAIT_TURN.format(
        n=5, ids=["D001"], tail=wd_text.WAIT_TAIL_INTERRUPT) in sent
    rec = json.loads((run_dir / "debug" / "wind_down.json").read_text())
    assert "cancelled" not in rec


def _gated_walk(tmp_path, *, verdict, repro):
    node, state, run_dir = _entry(tmp_path)
    node._reproduction_gate = lambda: repro
    node._find_critic_name = lambda: "critic"
    node._invoke_critic = lambda msg: f"### Verdict\n\n**{verdict}**\n"
    cmd, _ = _walk(node, state)
    return cmd


def test_a_passing_review_and_gate_earn_gated_with_termination_time_budget(
        tmp_path):
    cmd = _gated_walk(tmp_path, verdict="PASS", repro=None)
    assert cmd.update["outcome"] == terminal.GATED
    assert cmd.update["termination"] == terminal.BUDGET_WIND_DOWN
    assert cmd.update["reviewed"] is True
    assert "it is GATED" in cmd.update["last_report"]


@pytest.mark.parametrize("verdict,repro", [
    ("PASS", "notebook fails"), ("REVISE", None), ("REJECT", None)])
def test_a_failed_gate_or_a_non_pass_review_stays_ungated(
        tmp_path, verdict, repro):
    cmd = _gated_walk(tmp_path, verdict=verdict, repro=repro)
    assert cmd.update["outcome"] == terminal.UNGATED
    assert cmd.update["termination"] == terminal.BUDGET_WIND_DOWN


def test_the_review_is_counted_against_the_same_call_limit():
    gate.begin(2)
    with gate.suspend():
        assert gate.check("critic", "Bash", can_call_done=False) is None
        assert gate.check("critic", "Read", can_call_done=False) is None
        out = gate.check("critic", "Read", can_call_done=False)
    assert out.startswith("ERROR: wind-down rule: you have used 2 of your 2")
    assert gate.calls() == {gate.REVIEW_KEY: 2}


def test_a_time_budget_close_that_is_gated_is_not_marked_halted(tmp_path):
    status = _run_status(tmp_path, outcome="GATED")
    assert status["status"] == "GATED"
    assert status["termination"] == terminal.TIME_BUDGET
    assert status["overrun_s"] == 70.0


def _run_status(tmp_path, outcome="UNGATED"):
    import logging

    from adda._src.runtime.agent_runtime import AgenticRun, _RunContext

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("x\n", encoding="utf-8")
    run = AgenticRun(tmp_path)
    run._budget = 60.0
    run_dir = tmp_path / "runs" / "T"
    debug_dir = run_dir / "debug"
    debug_dir.mkdir(parents=True)
    (debug_dir / "wind_down.json").write_text(json.dumps({
        "forced_turns": 3, "deliverables_missing": ["pipeline.ipynb"],
        "reproduction_gate": "PASS", "critic_verdict": "FEEDBACK",
        "interrupted": [], "closed_at": 130.0}))
    ctx = _RunContext(
        ts="T", run_dir=run_dir, debug_dir=debug_dir,
        notes_dir=debug_dir / "strategizer_notes",
        workspace_dir=run_dir / "workspace",
        problem="x", problem_sha256="a", live_problem_sha256="a",
        resume_from=None, start_time=0.0, thread_id="th",
        log=logging.getLogger("t"), log_handler=logging.NullHandler(),
        delegation_log=mock.MagicMock(),
        canonical_cfg={}, study_cfg={}, initial_state={}, graph_config={},
    )
    run._finalize_run(ctx, {
        "last_report": "x", "outcome": outcome,
        "termination": terminal.TIME_BUDGET, "reviewed": True,
        "token_totals": {}})
    return json.loads((debug_dir / "run_status.json").read_text())


def test_run_status_carries_the_wind_down_facts(tmp_path):
    status = _run_status(tmp_path)
    assert status["status"] == "halted"
    assert status["termination"] == terminal.TIME_BUDGET
    assert status["overrun_s"] == 70.0 and status["wind_down_turns"] == 3
    assert status["deliverables_missing"] == ["pipeline.ipynb"]
    assert status["critic_verdict"] == "FEEDBACK"


# -- SIGINT reaches a Bash-tool shell's work and nothing else -----------------

def test_sigint_spares_the_cli_and_an_mcp_server_that_share_the_token():
    import os
    import subprocess
    import sys
    import textwrap

    from adda._src.infra import interrupt

    mcp_args = ["-c", "import time; time.sleep(60)"]
    cfg = {"srv": {"command": sys.executable, "args": mcp_args}}
    token = interrupt.new_token()
    cli_src = textwrap.dedent(f"""
        import subprocess, sys, time
        mcp = subprocess.Popen([sys.executable, *{mcp_args!r}])
        sh = subprocess.Popen(["bash", "-c", "sleep 60; true"])
        print(mcp.pid, sh.pid, flush=True)
        time.sleep(60)
    """)
    cli = subprocess.Popen(
        [sys.executable, "-c", cli_src], stdout=subprocess.PIPE, text=True,
        env={**os.environ, interrupt.SESSION_TOKEN_ENV: token})
    try:
        mcp_pid, sh_pid = map(int, cli.stdout.readline().split())
        import psutil
        deadline = time.time() + 10
        while time.time() < deadline and not psutil.Process(sh_pid).children():
            time.sleep(0.1)
        hit = interrupt.interrupt_session(token, cfg)
        pids = {h["pid"] for h in hit}
        assert sh_pid in pids and mcp_pid not in pids and cli.pid not in pids
        time.sleep(1)
        assert psutil.pid_exists(mcp_pid) and cli.poll() is None
        assert psutil.Process(mcp_pid).status() != psutil.STATUS_ZOMBIE
    finally:
        import signal
        try:
            for c in __import__("psutil").Process(cli.pid).children(
                    recursive=True):
                c.send_signal(signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass
        cli.kill()
        cli.wait()
