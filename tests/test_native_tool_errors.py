"""A native tool that fails returns an ERROR string; it never ends the delegation."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from adda._src.backends.ollama import OllamaAdapter


def _tools(tmp_path):
    adapter = OllamaAdapter(
        model="m", system_prompt="s", study_dir=tmp_path,
        native_tools=["Read", "Write", "Edit", "Glob", "Grep",
                      "Bash", "BashOutput", "KillShell"])
    debug = tmp_path / "debug"
    debug.mkdir()
    adapter._notice_ctx = (debug, "D002")
    return {t.name: t for t in adapter._build_tools()}, debug


def _rows(debug):
    path = debug / "diagnostics.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_read_on_a_directory_returns_an_error_string(tmp_path):
    tools, debug = _tools(tmp_path)
    (tmp_path / "sub").mkdir()
    out = tools["Read"].invoke({"path": str(tmp_path / "sub")})
    assert out.startswith("ERROR: IsADirectoryError")
    assert str(tmp_path / "sub") in out
    (row,) = _rows(debug)
    assert row["error_type"] == "ERROR_RETURN" and row["tool"] == "Read"
    assert row["node"] == "D002" and row["fault"] == "agent"
    assert row["args"] == {"path": str(tmp_path / "sub")}


def test_read_on_a_non_utf8_file_returns_an_error_string(tmp_path):
    tools, debug = _tools(tmp_path)
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00\x80")
    out = tools["Read"].invoke({"path": str(tmp_path / "b.bin")})
    assert out.startswith("ERROR: UnicodeDecodeError")
    assert _rows(debug)[0]["tool"] == "Read"


def test_write_to_a_directory_returns_an_error_string(tmp_path):
    tools, debug = _tools(tmp_path)
    (tmp_path / "sub").mkdir()
    out = tools["Write"].invoke(
        {"path": str(tmp_path / "sub"), "content": "x"})
    assert out.startswith("ERROR: IsADirectoryError")
    assert _rows(debug)[0]["tool"] == "Write"


def test_an_error_string_a_tool_already_returns_is_counted_too(tmp_path):
    tools, debug = _tools(tmp_path)
    out = tools["Read"].invoke({"path": str(tmp_path / "missing.txt")})
    assert out.startswith("ERROR:")
    assert _rows(debug)[0]["error_type"] == "ERROR_RETURN"


def test_a_good_call_is_unchanged_and_writes_no_row(tmp_path):
    tools, debug = _tools(tmp_path)
    (tmp_path / "ok.txt").write_text("hello")
    assert tools["Read"].invoke({"path": str(tmp_path / "ok.txt")}) == "hello"
    assert not (debug / "diagnostics.jsonl").exists()


def test_the_error_is_recorded_when_the_tool_runs_on_another_thread(tmp_path):
    tools, debug = _tools(tmp_path)
    (tmp_path / "sub").mkdir()
    with ThreadPoolExecutor(1) as pool:
        out = pool.submit(
            tools["Read"].invoke, {"path": str(tmp_path / "sub")}).result()
    assert out.startswith("ERROR: IsADirectoryError")
    assert len(_rows(debug)) == 1


def test_a_graph_tool_node_survives_a_failing_native_tool(tmp_path):
    from langchain_core.messages import AIMessage
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    tools, _ = _tools(tmp_path)
    (tmp_path / "sub").mkdir()
    msg = AIMessage(content="", tool_calls=[{
        "name": "Read", "args": {"path": str(tmp_path / "sub")}, "id": "c1"}])
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([tools["Read"]], handle_tool_errors=False))
    graph.add_edge(START, "tools")
    out = graph.compile().invoke({"messages": [msg]})
    assert out["messages"][-1].content.startswith("ERROR: IsADirectoryError")
