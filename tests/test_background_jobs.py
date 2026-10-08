"""Jobs still running when a delegation's session ends are made visible."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from adda._src.infra.background_jobs import BackgroundJobWatch

pytest.importorskip("psutil")

_SLEEP = "import time; time.sleep(60)"


def _spawn(delegation: str, code: str = _SLEEP) -> subprocess.Popen:
    env = {**os.environ, "F3DASM_DELEGATION_ID": delegation}
    return subprocess.Popen([sys.executable, "-c", code], env=env)


@pytest.fixture
def procs():
    made: list[subprocess.Popen] = []
    yield made
    for p in made:
        p.kill()
        p.wait()


def test_a_job_that_outlives_the_session_is_reported_dead_after_a_kill(procs):
    watch = BackgroundJobWatch("D001")
    watch.start()
    job = _spawn("D001")
    procs.append(job)
    time.sleep(0.3)
    watch.end(min_depth=1)
    job.kill()
    job.wait()
    [(found, state, text)] = watch.report_lines()
    assert found.pid == job.pid and state == "dead"
    assert text.startswith(f"background job `{found.command}` (pid {job.pid})")
    assert "was still running when this delegation ended at" in text
    assert text.endswith("it is no longer running. Results it had not saved "
                         "by then are not in the store.")


def test_a_job_still_running_at_delivery_is_reported_alive(procs):
    watch = BackgroundJobWatch("D001")
    watch.start()
    job = _spawn("D001")
    procs.append(job)
    time.sleep(0.3)
    watch.end(min_depth=1)
    [(_, state, text)] = watch.report_lines()
    assert state == "alive"
    assert text.endswith("and it still is. It may write to the store after "
                         "this report.")


def test_jobs_of_other_delegations_and_baseline_processes_are_not_reported(
        procs):
    before = _spawn("D001")
    other = _spawn("D002")
    procs.extend([before, other])
    time.sleep(0.3)
    watch = BackgroundJobWatch("D001")
    watch.start()
    watch.end(min_depth=1)
    assert watch.report_lines() == []


def test_only_the_root_of_a_job_subtree_is_reported(procs):
    code = ("import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {_SLEEP!r}]); "
            "time.sleep(60)")
    watch = BackgroundJobWatch("D001")
    watch.start()
    job = _spawn("D001", code)
    procs.append(job)
    time.sleep(1.0)
    watch.end(min_depth=1)
    assert [j.pid for j, _, _ in watch.report_lines()] == [job.pid]


def test_the_session_process_itself_is_not_a_job(procs):
    """min_depth=2 skips a direct child (the CLI), keeps its child (the job)."""
    code = ("import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {_SLEEP!r}]); "
            "time.sleep(60)")
    watch = BackgroundJobWatch("D001")
    watch.start()
    cli = _spawn("D001", code)
    procs.append(cli)
    time.sleep(1.0)
    watch.end(min_depth=2)
    reported = [j.pid for j, _, _ in watch.report_lines()]
    assert cli.pid not in reported and len(reported) == 1


def test_an_mcp_server_command_is_ignored(procs):
    watch = BackgroundJobWatch("D001", ignore=("time.sleep(60)",))
    watch.start()
    procs.append(_spawn("D001"))
    time.sleep(0.3)
    watch.end(min_depth=1)
    assert watch.report_lines() == []


def test_the_report_leads_with_one_line_per_job_and_logs_a_row(procs):
    from adda._src.nodes.tools.routing.delegation import WorkerSession

    rows: list[tuple] = []

    class _Node:
        def _record_intervention(self, kind, target, message, **extra):
            rows.append((kind, extra))

    watch = BackgroundJobWatch("D001")
    watch.start()
    procs.append(_spawn("D001"))
    time.sleep(0.3)
    watch.end(min_depth=1)
    ws = object.__new__(WorkerSession)
    ws.node, ws.target, ws.delegation_id = _Node(), "implementer", "D001"
    ws._background_watch = watch
    notes = ws._background_job_notes()
    assert notes.startswith("background job `")
    [(kind, extra)] = rows
    assert kind == "BACKGROUND_JOB_AT_END"
    assert extra["fault"] == "observation"
    assert extra["state_at_delivery"] == "alive"
    assert extra["delegation_id"] == "D001" and extra["pid"] == procs[0].pid


def test_the_openai_bash_tool_carries_the_delegation_id_on_a_langgraph_thread(
        tmp_path):
    """LangGraph runs tools on its own threads, so the id is bound by callable."""
    import threading

    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    from adda._src.backends.openai_compatible import _native_tool_map

    bash = _native_tool_map(tmp_path, delegation_id=lambda: "D007")["Bash"]
    msg = AIMessage(content="", tool_calls=[{
        "name": "Bash", "id": "c1",
        "args": {"command": "echo id=$F3DASM_DELEGATION_ID"}}])
    seen: dict = {}

    def run():
        seen["thread"] = threading.current_thread()
        g = StateGraph(MessagesState)
        g.add_node("tools", ToolNode([bash]))
        g.add_edge(START, "tools")
        g.add_edge("tools", END)
        seen["out"] = g.compile().invoke({"messages": [msg]})

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert "id=D007" in seen["out"]["messages"][-1].content
