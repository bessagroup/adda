"""GET /api/runs/{id}/transcript/{key}/events — both backends, one shape."""
from starlette.testclient import TestClient

from adda._src.viewer.app import create_app
from tests.test_viewer_app import _make_run, _make_study, _write_jsonl

RUN = "20260904T120000"


def _client(tmp_path, rows, key="D007"):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, RUN)
    _write_jsonl(run_dir / "debug" / "transcripts" / f"{key}.jsonl", rows)
    return TestClient(create_app(study))


def _events(client, qs="", key="D007"):
    r = client.get(f"/api/runs/{RUN}/transcript/{key}/events{qs}")
    assert r.status_code == 200, r.text
    return r.json()


CLAUDE = [
    {"ts": "t0", "type": "user", "text": "Minimise the drag."},
    {"ts": "t1", "type": "stream_evt", "evt": "message_start"},
    {"ts": "t2", "type": "partial", "text": "Al"},
    {"ts": "t3", "type": "assistant", "text": "plan", "thinking": ["hm"],
     "tools": [{"name": "mcp__x__Bash", "input": {"command": "ls"}},
               {"name": "mcp__x__Read", "input": {"file_path": "a"}}]},
    {"ts": "t4", "type": "tool_result", "results": [
        {"tool_use_id": "1", "content": [{"type": "text", "text": "a.py"}]}]},
    {"ts": "t5", "type": "result", "usage": {}},
]


def test_claude_transcript_is_normalised(tmp_path):
    body = _events(_client(tmp_path, CLAUDE))
    kinds = [e["kind"] for e in body["events"]]
    assert kinds == ["user", "thinking", "assistant", "tool_use", "tool_use",
                     "tool_result"]
    assert body["total"] == 6 and body["next_cursor"] == 6
    uses = [e for e in body["events"] if e["kind"] == "tool_use"]
    assert uses[0]["tool"] == {"name": "mcp__x__Bash", "input": {"command": "ls"}}
    # one call has a result, the second is still running
    assert [u["pending"] for u in uses] == [False, True]
    res = body["events"][-1]
    assert res["result"] == {"ok": True, "text": "a.py",
                             "name": "mcp__x__Bash", "event": ""}


def test_openai_shape_is_normalised(tmp_path):
    rows = [
        {"ts": "t0", "type": "HumanMessage", "text": "go"},
        {"ts": "t1", "type": "AIMessage", "text": "",
         "tools": [{"name": "Bash", "args": {"command": "ls"}}]},
        {"ts": "t2", "type": "ToolMessage", "name": "Bash", "text": "ERROR boom"},
    ]
    ev = _events(_client(tmp_path, rows))["events"]
    assert [e["kind"] for e in ev] == ["user", "tool_use", "tool_result"]
    assert ev[1]["tool"]["input"] == {"command": "ls"}
    assert ev[1]["pending"] is False
    assert ev[2]["result"]["ok"] is False and ev[2]["result"]["name"] == "Bash"


def test_cursor_counts_raw_events_and_pages_without_loss_or_repeat(tmp_path):
    client = _client(tmp_path, CLAUDE)
    seen, after = [], 0
    for _ in range(10):
        body = _events(client, f"?after={after}&limit=2")
        seen += [(e["kind"], e["ts"]) for e in body["events"]]
        if body["next_cursor"] == after:
            break
        after = body["next_cursor"]
        if after >= body["total"]:
            break
    whole = [(e["kind"], e["ts"]) for e in _events(client)["events"]]
    assert seen == whole


def test_tool_names_resolve_across_a_page_boundary(tmp_path):
    body = _events(_client(tmp_path, CLAUDE), "?after=4")
    assert [e["kind"] for e in body["events"]] == ["tool_result"]
    assert body["events"][0]["result"]["name"] == "mcp__x__Bash"


def test_drained_poll_returns_the_same_cursor(tmp_path):
    body = _events(_client(tmp_path, CLAUDE), "?after=6")
    assert body == {"events": [], "next_cursor": 6, "total": 6}


def test_notices_are_split_from_user_and_result_text(tmp_path):
    from adda._src.nodes.notices import wrap_notice
    rows = [{"ts": "t", "type": "user",
             "text": wrap_notice("[SCIENCE MONITOR] drift") + "real task"}]
    ev = _events(_client(tmp_path, rows))["events"][0]
    assert ev["text"] == "real task"
    assert ev["notices"] and ev["notices"][0].startswith("[SCIENCE MONITOR")


def test_compaction_is_a_system_event(tmp_path):
    rows = [{"ts": "t", "type": "system", "subtype": "compact_boundary",
             "data": {"compact_metadata": {"preTokens": 9, "postTokens": 3}}}]
    ev = _events(_client(tmp_path, rows))["events"][0]
    assert ev["kind"] == "system"
    assert ev["compaction"]["before"] == 9 and ev["compaction"]["after"] == 3


def test_errors(tmp_path):
    client = _client(tmp_path, CLAUDE)
    base = f"/api/runs/{RUN}/transcript"
    assert client.get(f"{base}/NOPE/events").status_code == 404
    assert client.get(f"{base}/D007/events?after=x").status_code == 400
    assert client.get("/api/runs/nope/transcript/D007/events").status_code == 404
    assert client.get(f"{base}/../../../x/events").status_code == 404
