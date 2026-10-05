"""Tests for the viewer's Starlette app — TestClient against fixture study
dirs, no real LLM/network involved.

One exception: the SSE `/stream` tests use a REAL live uvicorn server, not
TestClient. Verified directly (see the commit this file was added in) that
httpx's ASGI test transport — under both the sync `TestClient` wrapper and a
plain async `httpx.AsyncClient(transport=ASGITransport(...))` — buffers an
async generator's ENTIRE output until it fully completes before releasing
ANY of it to `iter_lines()`/`aiter_lines()`, even across an internal
`asyncio.sleep`. That makes it structurally incapable of testing a stream
that is deliberately infinite (real SSE never "completes"). A real uvicorn
server + a real socket-based `httpx.stream()` call delivers each chunk as
it's actually written, confirmed by the same minimal repro timing correctly
(0.03s / 0.33s) against the buffered version's (0.31s / 0.31s, i.e. never
incremental)."""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

from adda._src.viewer.app import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveServer:
    """A real uvicorn server on a real port, for tests that need genuine
    incremental streaming (see module docstring)."""

    def __init__(self, app):
        self.port = _free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(50):
            try:
                httpx.get(f"{self.url}/api/runs", timeout=0.2)
                return self
            except httpx.TransportError:
                time.sleep(0.1)
        raise RuntimeError("live server did not start in time")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _make_study(tmp_path: Path) -> Path:
    study = tmp_path / "study"
    study.mkdir()
    (study / "run.py").write_text(
        "from adda._src.backends.base import Agent, Edge, Graph\n"
        "class _S(Agent):\n"
        "    role = 'strategizer'\n"
        "    description = 'hub'\n"
        "    tools = frozenset()\n"
        "class _C(Agent):\n"
        "    role = 'critic'\n"
        "    description = 'gate'\n"
        "    tools = frozenset()\n"
        "def build_graph():\n"
        "    return Graph(nodes={'strategizer': _S(), 'critic': _C()}, "
        "edges=(Edge('strategizer', 'critic'),), entry='strategizer')\n",
        encoding="utf-8",
    )
    return study


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _make_run(study: Path, run_id: str) -> Path:
    run_dir = study / "runs" / run_id
    (run_dir / "debug").mkdir(parents=True)
    return run_dir


# ---------------------------------------------------------------------------
# /api/runs
# ---------------------------------------------------------------------------

def test_list_runs_empty(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_runs_lists_fixture_run(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    assert [r["run_id"] for r in resp.json()] == ["20260904T120000"]


# ---------------------------------------------------------------------------
# /api/runs/{id}/graph
# ---------------------------------------------------------------------------

def test_get_graph_returns_fixture_topology(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/graph")
    assert resp.status_code == 200
    data = resp.json()
    names = {n["name"] for n in data["nodes"]}
    assert names == {"strategizer", "critic"}
    assert data["edges"] == [{"source": "strategizer", "target": "critic"}]
    assert data["entry"] == "strategizer"


def test_get_graph_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/nonexistent/graph")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/runs/{id}/delegations
# ---------------------------------------------------------------------------

def test_get_delegations_matches_reader(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "delegation_log.jsonl", [
        {"id": "D001", "status": "RUNNING", "from_node": "strategizer",
         "to_node": "critic"},
        {"id": "D001", "status": "DONE", "from_node": "strategizer",
         "to_node": "critic"},
    ])
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/delegations")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "DONE"


def test_get_delegations_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/nonexistent/delegations")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/runs/{id}/problem_statement
# ---------------------------------------------------------------------------

def test_get_problem_statement_returns_snapshot_text(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    (run_dir / "debug" / "PROBLEM_STATEMENT_snapshot.md").write_text(
        "# Do the thing\n", encoding="utf-8")
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/problem_statement")
    assert resp.status_code == 200
    assert resp.json() == {"text": "# Do the thing\n"}


def test_get_problem_statement_404_when_snapshot_missing(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/problem_statement")
    assert resp.status_code == 404


def test_get_problem_statement_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/nonexistent/problem_statement")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/runs/{id}/node/{name}/transcripts — real, disk-verified keys
# ---------------------------------------------------------------------------

def test_get_node_transcripts_worker(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "delegation_log.jsonl", [
        {"id": "D001", "status": "DONE", "from_node": "strategizer",
         "to_node": "critic"},
    ])
    # The endpoint only offers keys that resolve to a real file on disk.
    _write_jsonl(run_dir / "debug" / "transcripts" / "D001.jsonl",
                 [{"type": "assistant", "text": "x"}])
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/node/critic/transcripts")
    assert resp.status_code == 200
    assert resp.json() == ["D001"]


def test_get_node_transcripts_entry_lists_real_turn_files(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    st_dir = run_dir / "debug" / "transcripts" / "strategizer"
    st_dir.mkdir(parents=True)
    (st_dir / "turn_001.jsonl").write_text("")
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/node/strategizer/transcripts")
    assert resp.status_code == 200
    assert resp.json() == ["strategizer/turn_001"]


def test_get_node_transcripts_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/nonexistent/node/critic/transcripts")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/runs/{id}/transcript/{key} — both 404 flavors
# ---------------------------------------------------------------------------

def test_get_transcript_200_with_events(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "hi"},
    ])
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/transcript/D007")
    assert resp.status_code == 200
    assert resp.json() == {
        "events": [{"ts": "t1", "type": "assistant", "text": "hi"}],
        "next_cursor": 1, "total": 1}


def test_get_transcript_pages_raw_records_by_cursor(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": f"t{i}", "type": "assistant", "text": str(i)} for i in range(5)])
    client = TestClient(create_app(study))
    body = client.get(
        "/api/runs/20260904T120000/transcript/D007?after=2&limit=2").json()
    assert [e["text"] for e in body["events"]] == ["2", "3"]
    assert body["next_cursor"] == 4 and body["total"] == 5
    bad = client.get("/api/runs/20260904T120000/transcript/D007?limit=x")
    assert bad.status_code == 400


def test_get_oracle_pages_by_cursor_and_namespace(tmp_path):
    from tests.test_viewer_readers import _oracle_run
    study = _make_study(tmp_path)
    _oracle_run(study, 5, ns_rows=3)
    client = TestClient(create_app(study))
    base = "/api/runs/R1/oracle"
    stores = client.get(f"{base}?limit=2").json()["stores"]
    assert [s["next_cursor"] for s in stores] == [2, 2]
    (canon,) = client.get(f"{base}?namespace=&after=2&limit=9").json()["stores"]
    assert [e["index"] for e in canon["evals"]] == [2, 1, 0]
    assert canon["next_cursor"] is None
    assert client.get(f"{base}?after=x").status_code == 400


def test_get_transcript_404_debug_flag_off(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")  # no transcripts/ dir at all
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/transcript/D007")
    assert resp.status_code == 404
    assert "debug flag" in resp.json()["error"]


def test_get_transcript_404_unknown_key(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    (run_dir / "debug" / "transcripts").mkdir(parents=True)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/transcript/D999")
    assert resp.status_code == 404
    assert "no such transcript" in resp.json()["error"]


def test_get_transcript_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/nonexistent/transcript/D007")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/runs/{id}/transcript/{key}/fragment — incremental append contract
# ---------------------------------------------------------------------------

def test_transcript_fragment_returns_only_events_after_index(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "first"},
        {"ts": "t2", "type": "assistant", "text": "second"},
        {"ts": "t3", "type": "assistant", "text": "third"},
    ])
    client = TestClient(create_app(study))
    resp = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=1")
    assert resp.status_code == 200
    assert "first" not in resp.text
    assert "second" in resp.text
    assert "third" in resp.text


def test_transcript_fragment_event_count_header_counts_raw_events_not_bubbles(
    tmp_path,
):
    """The client's next `after` cursor must count RAW events consumed,
    not rendered bubbles — most real events (stream_evt/partial/result)
    render to no bubble at all. A cursor counting bubbles falls behind the
    raw list and re-matches already-shown events on every subsequent poll,
    duplicating content forever even once the underlying file has stopped
    growing (confirmed for real: a genuine transcript had 527 raw events,
    only 47 rendered as bubbles — polling with a bubble-counted cursor grew
    the panel from 22KB to 74KB over three 2s polls on an already-finished
    run). X-Event-Count must reflect ALL events consumed, bubble or not."""
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "hello"},  # 1 bubble
        {"ts": "t2", "type": "stream_evt", "evt": "ping"},   # 0 bubbles
        {"ts": "t3", "type": "partial", "text": "..."},      # 0 bubbles
        {"ts": "t4", "type": "result", "usage": {}},         # 0 bubbles
    ])
    client = TestClient(create_app(study))
    resp = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=0")
    assert resp.status_code == 200
    assert resp.headers["X-Event-Count"] == "4"  # not "1" (the bubble count)

    # A second poll using that cursor must return NOTHING new — the file
    # hasn't grown, so a correct cursor is already past every event.
    resp2 = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=4")
    assert resp2.text == ""


def test_transcript_fragment_resolves_tool_result_name_by_position(tmp_path):
    """A tool_result event only carries `tool_use_id`, never the tool's
    name -- the fragment renderer must resolve it positionally against the
    full in-order tool-call queue (see `_render_fragment`'s docstring), not
    render a generic unlabeled "tool result" the reader can't attribute to
    anything (the exact defect the user's live screenshot showed)."""
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "",
         "tools": [{"name": "mcp__f3dasm_agent_tools__Delegate", "input": {}}]},
        {"ts": "t2", "type": "tool_result",
         "results": [{"tool_use_id": "x", "content": "ok"}]},
    ])
    client = TestClient(create_app(study))
    resp = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=0")
    assert resp.status_code == 200
    # The result is attributed to the tool it answers, with the mcp__
    # registration prefix stripped for readability. The attribution rides
    # on the turnstile glyph rather than a repeated visible name: the row
    # is nested under the named call it answers, and printing the name a
    # second time made the output read as another call instead of the
    # tail of the first.
    assert "<span class='result-glyph' title='Delegate'>" in resp.text
    # ...the raw registered name still preserved for precision on the call...
    assert "mcp__f3dasm_agent_tools__Delegate" in resp.text
    # ...and the output itself shown inline, not hidden behind a disclosure.
    assert "ok" in resp.text


def test_transcript_fragment_resolution_correct_when_after_splits_events(
    tmp_path,
):
    """The name-resolution queue is recomputed over the FULL event list
    every call, specifically so a tool_result landing in a LATER poll (its
    matching tool-call assistant event already past the `after` cursor)
    still resolves to the right name."""
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "",
         "tools": [{"name": "Read", "input": {}}]},
        {"ts": "t2", "type": "tool_result",
         "results": [{"tool_use_id": "x", "content": "file contents"}]},
    ])
    client = TestClient(create_app(study))
    # The client already consumed event 0 (the assistant/tool-call event)
    # on a prior poll; this call only asks for event 1 onward.
    resp = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=1")
    assert resp.status_code == 200
    assert "<span class='result-glyph' title='Read'>" in resp.text
    assert "file contents" in resp.text


def test_a_result_is_nested_under_the_call_it_answers(tmp_path):
    """The result must share the call's indent context, not sit outside it.

    It used to be emitted as a bare top-level sibling while calls sat
    inside ``turn-cont``'s 42px indent, so every tool's output rendered
    OUTDENTED from — and visually detached from — the command that
    produced it. With 58 Bash calls in one delegation that left no way to
    see where one call ended and the next began, which is exactly the
    complaint. Claude Code, Codex and opencode all nest the result under
    the call; the shared wrapper is what encodes that.
    """
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "",
         "tools": [{"name": "Bash", "input": {"command": "ls"}}]},
        {"ts": "t2", "type": "tool_result",
         "results": [{"tool_use_id": "x", "content": "out"}]},
    ])
    client = TestClient(create_app(study))
    body = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=0").text
    # Same indent wrapper as a tool call, flagged as the continuation row
    # so it stays tucked against its call instead of opening a new unit.
    assert "<div class='turn turn-cont turn-res'><div class='turn-body'>" in body
    assert "class='tool-result" in body
    # And nothing renders at the old detached top level any more.
    assert "tool-result-row" not in body


def test_a_long_result_is_not_emitted_twice(tmp_path):
    """The disclosure holds the REMAINDER, not a second full copy.

    It used to hold the entire text again, so expanding a result
    re-printed the lines already on screen and the DOM carried the whole
    payload twice — on this run's largest result (834 lines) that is not
    a rounding error.
    """
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    body_lines = [f"line{i}" for i in range(20)]
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "",
         "tools": [{"name": "Bash", "input": {"command": "ls"}}]},
        {"ts": "t2", "type": "tool_result",
         "results": [{"tool_use_id": "x", "content": "\n".join(body_lines)}]},
    ])
    client = TestClient(create_app(study))
    body = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=0").text
    # The first line is in the preview and NOWHERE else; the last is only
    # in the disclosure. A duplicating renderer shows line0 twice.
    assert body.count("line0<") + body.count("line0\n") == 1
    assert "+8 lines" in body
    assert "line19" in body


def test_a_call_still_waiting_on_its_result_is_marked(tmp_path):
    """The live-run case: a call whose output has not been written yet.

    Without this a call awaiting its result and a call that returned
    nothing render identically, so a reader watching a run cannot tell
    when a command is still going — the "hard to know when it ends"
    complaint. The mark is the glyph's own animation; there is no label.
    """
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "assistant", "text": "",
         "tools": [{"name": "Bash", "input": {"command": "first"}}]},
        {"ts": "t2", "type": "tool_result",
         "results": [{"tool_use_id": "x", "content": "done"}]},
        {"ts": "t3", "type": "assistant", "text": "",
         "tools": [{"name": "Bash", "input": {"command": "second"}}]},
    ])
    client = TestClient(create_app(study))
    body = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=0").text
    # Exactly one call is outstanding — the second. The first came back.
    assert body.count("data-pending") == 1
    assert body.index("first") < body.index("data-pending")
    # data-pending is the handle the frontend uses to clear a stale mark
    # once the result arrives in a later append-only fragment.
    assert "class='tool-call pending' data-pending" in body


def test_pending_marks_are_suppressed_for_langchain_transcripts(tmp_path):
    """Pending is inferred by counting `tool_result` events against calls,
    which only holds for the Claude backend's strictly-ordered shape. The
    OpenAI-compatible backend records a LangChain ToolMessage per result,
    paired by name rather than position, so the comparison is meaningless
    there — and applying it anyway would brand every one of that
    backend's calls as unfinished forever.
    """
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D007.jsonl", [
        {"ts": "t1", "type": "AIMessage", "text": "",
         "tools": [{"name": "Bash", "args": {"command": "ls"}}]},
        {"ts": "t2", "type": "ToolMessage", "name": "Bash", "text": "out"},
    ])
    client = TestClient(create_app(study))
    body = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=0").text
    assert "data-pending" not in body
    # ...while still rendering, which is the whole reason that shape is
    # handled at all.
    assert "Bash" in body and "out" in body


def test_transcript_fragment_404_when_debug_off(tmp_path):
    """404 (not 200): the frontend's error path relies on !ok to stop
    polling and show the message — a 200 with an "empty-looking" body was
    indistinguishable from a legitimate empty poll, so the panel silently
    stayed blank with no explanation and kept polling forever."""
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get(
        "/api/runs/20260904T120000/transcript/D007/fragment?after=0")
    assert resp.status_code == 404
    assert "debug flag" in resp.text


def test_transcript_fragment_404_for_key_with_no_transcript_file(tmp_path):
    """A real delegation id with no captured conversation at all (e.g. a
    Done()-gate-check bookkeeping record like "GATE190739", confirmed for
    real to have a delegation_log.jsonl row but no transcripts/*.jsonl file)
    must 404 with an honest explanation, not silently return 200 with an
    empty body — the panel otherwise goes blank with zero explanation."""
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    (run_dir / "debug" / "transcripts").mkdir(parents=True)  # debug ON
    client = TestClient(create_app(study))
    resp = client.get(
        "/api/runs/20260904T120000/transcript/GATE190739/fragment?after=0")
    assert resp.status_code == 404
    assert "No transcript recorded" in resp.text


# ---------------------------------------------------------------------------
# /runs/{id} — the HTML page
# ---------------------------------------------------------------------------

def test_graph_page_renders(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get("/runs/20260904T120000")
    assert resp.status_code == 200
    assert "20260904T120000" in resp.text


def test_ledger_endpoint_returns_hypotheses_and_milestones(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    notes = run / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    (notes / "hypotheses.json").write_text(json.dumps({
        "H1": {"statement": "s", "status_log": [
            {"status": "SUPPORTED", "ts": "t2"}]},
    }))
    (notes / "milestones.json").write_text(json.dumps({
        "M001": {"key": "k", "status": "SKIPPED", "note": "why"},
    }))

    client = TestClient(create_app(study))
    body = client.get("/api/runs/20260904T120000/ledger").json()
    assert [h["status"] for h in body["hypotheses"]] == ["SUPPORTED"]
    assert [m["note"] for m in body["milestones"]] == ["why"]


def test_ledger_endpoint_empty_for_a_run_with_no_notes(tmp_path):
    """A run with the debug flag off, or one that crashed early, answers
    with empty lists rather than a 404 or a 500 — the client's "nothing
    yet" path is the same path as "nothing on disk"."""
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/ledger")
    assert resp.status_code == 200
    assert resp.json() == {"hypotheses": [], "milestones": []}


def test_notebook_endpoint_reports_missing_rather_than_404(tmp_path):
    """A run with no deliverable is a real finding about that run, not a
    routing error — the client must be able to say so in words."""
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/20260904T120000/notebook")
    assert resp.status_code == 200
    assert resp.json() == {"cells": [], "missing": True}


def test_notebook_endpoint_serves_this_runs_archive(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    (study / "pipeline_20260904T120000.ipynb").write_text(json.dumps({
        "cells": [{"cell_type": "code", "source": "x = 1",
                   "execution_count": 1, "metadata": {}, "outputs": []}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }))
    client = TestClient(create_app(study))
    body = client.get("/api/runs/20260904T120000/notebook").json()
    assert [c["source"] for c in body["cells"]] == ["x = 1"]


def test_ledger_endpoint_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    assert client.get("/api/runs/nope/ledger").status_code == 404


def test_operator_endpoint_reports_pending_questions_and_beats_the_heartbeat(tmp_path):
    """Reading this endpoint IS the signal that a human is present.

    A run only waits for an answer while the heartbeat is fresh, so the
    poll that shows you the question must also be what tells the run you
    are there to answer it.
    """
    from adda._src.infra import operator_channel as oc

    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    qid = oc.ask_question(run, "strategizer", "Is the floor advisory?")
    assert oc.is_watched(run) is False

    client = TestClient(create_app(study))
    body = client.get("/api/runs/20260904T120000/operator").json()

    assert [q["id"] for q in body["questions"]] == [qid]
    assert oc.is_watched(run) is True



def _writer(study):
    """A client holding the session cookie the printed URL would set."""
    client = TestClient(create_app(study, token="t0k3n"))
    assert client.get("/session?token=t0k3n", follow_redirects=False).status_code == 303
    return client


def test_answering_reaches_the_run(tmp_path):
    from adda._src.infra import operator_channel as oc

    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    qid = oc.ask_question(run, "strategizer", "?")

    client = _writer(study)
    resp = client.post("/api/runs/20260904T120000/answer",
                       json={"id": qid, "answer": "advisory"})
    assert resp.status_code == 200
    assert oc.read_answer(run, qid) == "advisory"


def test_answering_a_question_the_run_gave_up_on_is_a_conflict(tmp_path):
    """Must not report success for an answer the agent will never see."""
    from adda._src.infra import operator_channel as oc

    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    qid = oc.ask_question(run, "strategizer", "?")
    oc.close_question(run, qid, "timeout")

    client = _writer(study)
    resp = client.post("/api/runs/20260904T120000/answer",
                       json={"id": qid, "answer": "too late"})
    assert resp.status_code == 409


def test_queueing_a_note_puts_it_where_the_node_drains_it(tmp_path):
    from adda._src.infra import operator_channel as oc

    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")

    client = _writer(study)
    resp = client.post("/api/runs/20260904T120000/note",
                       json={"text": "re-run shell_05 first"})
    assert resp.status_code == 200
    assert oc.drain_notes(run) == ["re-run shell_05 first"]


def test_an_empty_note_is_rejected_by_the_endpoint(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = _writer(study)
    assert client.post("/api/runs/20260904T120000/note",
                       json={"text": "   "}).status_code == 400


def test_stop_writes_the_request_the_run_reads_and_is_audited(tmp_path):
    from adda._src.infra.stop_request import read_stop_request

    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    resp = _writer(study).post("/api/runs/20260904T120000/stop", json={})
    assert resp.status_code == 200
    req = read_stop_request(run)
    assert req is not None and req["by"] == "viewer"
    rows = [json.loads(line) for line in
            (study / "viewer_actions.jsonl").read_text().splitlines()]
    assert rows[-1]["action"] == "stop"
    assert rows[-1]["run_id"] == "20260904T120000"
    assert rows[-1]["wrote"].endswith("debug/stop_request.json")


def test_a_second_stop_is_a_conflict_not_a_second_request(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = _writer(study)
    assert client.post("/api/runs/20260904T120000/stop", json={}).status_code == 200
    assert client.post("/api/runs/20260904T120000/stop", json={}).status_code == 409


def test_stopping_a_closed_run_is_refused(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "run_status.json").write_text('{"status": "GATED"}')
    resp = _writer(study).post("/api/runs/20260904T120000/stop", json={})
    assert resp.status_code == 409
    assert not (run / "debug" / "stop_request.json").exists()


def test_stop_needs_the_write_token(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.post("/api/runs/20260904T120000/stop", json={})
    assert resp.status_code == 403
    assert not (run / "debug" / "stop_request.json").exists()


def test_log_tail_follows_a_growing_file_without_gaps_or_repeats(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    log = run / "debug" / "run.log"
    log.write_text("one\ntwo\n")
    client = TestClient(create_app(study))
    url = "/api/runs/20260904T120000/log"
    first = client.get(url).json()
    assert first["text"] == "one\ntwo\n" and first["exists"] is True
    with open(log, "a") as fh:
        fh.write("three\n")
    second = client.get(url, params={"after": first["next_cursor"]}).json()
    assert second["text"] == "three\n" and second["reset"] is False
    assert client.get(
        url, params={"after": second["next_cursor"]}).json()["text"] == ""


def test_log_tail_without_after_returns_only_the_last_window(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "run.log").write_text("a" * 100 + "TAIL")
    out = TestClient(create_app(study)).get(
        "/api/runs/20260904T120000/log", params={"limit": 4}).json()
    assert out["text"] == "TAIL"


def test_log_tail_resets_when_the_file_shrank(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "run.log").write_text("fresh\n")
    out = TestClient(create_app(study)).get(
        "/api/runs/20260904T120000/log", params={"after": 9999}).json()
    assert out["reset"] is True and out["text"] == "fresh\n"


def test_log_tail_serves_the_monitor_diagnostics_as_a_second_named_source(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "diagnostics.jsonl").write_text('{"error_type": "ERROR_RETURN"}\n')
    out = TestClient(create_app(study)).get(
        "/api/runs/20260904T120000/log", params={"name": "diagnostics"}).json()
    assert out["exists"] is True and "ERROR_RETURN" in out["text"]


def test_log_tail_takes_a_name_never_a_path(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    url = "/api/runs/20260904T120000/log"
    assert client.get(url, params={"name": "../../PROBLEM_STATEMENT.md"}
                      ).status_code == 404
    assert client.get(url, params={"after": "x"}).status_code == 400
    assert client.get(url).json()["exists"] is False


def test_log_sources_list_watchdog_stdout_logs_and_tool_calls(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "run_started_at").write_text("1000.0")
    state = study / "runs" / "_viewer"
    state.mkdir(parents=True)
    (state / "watchdog_a.log").write_text("watchdog says hi\n")
    (state / "registry.json").write_text(json.dumps([
        {"kind": "watchdog", "started_at": 990.0,
         "log": "runs/_viewer/watchdog_a.log"},
        {"kind": "watchdog", "started_at": 100.0,
         "log": "runs/_viewer/watchdog_old.log"}]))
    d2 = run / "debug" / "delegations" / "D002"
    d2.mkdir(parents=True)
    (d2 / "campaign_stdout.log").write_text("evaluating\n")
    client = TestClient(create_app(study))
    base = "/api/runs/20260904T120000"
    names = {x["name"]: x for x in client.get(base + "/log_sources").json()["sources"]}
    assert {"run", "diagnostics", "watchdog", "tools",
            "out:D002/campaign_stdout.log"} <= set(names)
    assert names["out:D002/campaign_stdout.log"]["delegation"] == "D002"
    assert client.get(base + "/log", params={"name": "watchdog"}
                      ).json()["text"] == "watchdog says hi\n"
    assert client.get(base + "/log", params={"name": "out:D002/campaign_stdout.log"}
                      ).json()["text"] == "evaluating\n"
    assert client.get(base + "/log", params={"name": "out:../../x_stdout.log"}
                      ).status_code == 404


def test_watchdog_source_is_absent_for_a_run_the_viewer_did_not_start(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    names = [x["name"] for x in TestClient(create_app(study)).get(
        "/api/runs/20260904T120000/log_sources").json()["sources"]]
    assert "watchdog" not in names


def test_tool_calls_pair_each_call_with_its_result_across_transcripts(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    tdir = run / "debug" / "transcripts"
    (tdir / "strategizer").mkdir(parents=True)

    def rec(path, rows):
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    rec(tdir / "D001.jsonl", [
        {"type": "assistant", "ts": "2026-09-04T12:00:05+00:00",
         "tools": [{"name": "Bash"}, {"name": "Read"}]},
        {"type": "tool_result", "ts": "2026-09-04T12:00:06+00:00",
         "results": [{"is_error": False}, {"is_error": True}]},
        {"type": "assistant", "ts": "2026-09-04T12:00:09+00:00",
         "tools": [{"name": "Write"}]}])
    rec(tdir / "strategizer" / "turn_001.jsonl", [
        {"type": "assistant", "ts": "2026-09-04T12:00:01+00:00",
         "tools": [{"name": "mcp__f3dasm_agent_tools__Delegate"}]},
        {"type": "tool_result", "ts": "2026-09-04T12:00:02+00:00",
         "results": [{"is_error": False}]}])
    client = TestClient(create_app(study))
    out = client.get("/api/runs/20260904T120000/tool_calls").json()
    got = [(c["who"], c["tool"], c["ok"]) for c in out["calls"]]
    assert got == [("strategizer/turn_001", "Delegate", True),
                   ("D001", "Bash", True), ("D001", "Read", False),
                   ("D001", "Write", None)]
    since = out["calls"][2]["t"]
    later = client.get("/api/runs/20260904T120000/tool_calls",
                       params={"since": since}).json()["calls"]
    assert [c["tool"] for c in later] == ["Bash", "Read", "Write"]
    assert client.get("/api/runs/20260904T120000/tool_calls",
                      params={"since": "x"}).status_code == 400


def test_tool_calls_404_when_the_run_recorded_no_transcripts(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    assert TestClient(create_app(study)).get(
        "/api/runs/20260904T120000/tool_calls").status_code == 404


def test_transcript_key_cannot_escape_the_run_directory(tmp_path):
    """The severe one: {key:path} reaches read_transcript raw.

    Before containment, GET /transcript/%2e%2e%2f*5/secret returned 200 with
    the contents of any .jsonl on the host — and this server is routinely
    bound to a LAN/ZeroTier address, so it was remotely reachable. The
    percent-encoding matters: it survives the client-side normalisation that
    would collapse a literal "../".
    """
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "transcripts").mkdir(parents=True)
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"type": "assistant", "text": "TOP SECRET"}\n',
                      encoding="utf-8")

    client = TestClient(create_app(study))
    for depth in range(1, 9):
        esc = "%2e%2e%2f" * depth
        for route in (f"/api/runs/20260904T120000/transcript/{esc}secret",
                      f"/api/runs/20260904T120000/transcript/{esc}secret/fragment"):
            resp = client.get(route)
            assert "TOP SECRET" not in resp.text, f"leaked via {route}"


def test_a_tool_name_cannot_inject_an_event_handler(tmp_path):
    """Tool names are model output and land in a single-quoted attribute.

    _esc did not escape the apostrophe, so a name of
    ``x' onmouseover='alert(1)`` closed title='...' and added a live handler
    to the served page.
    """
    from adda._src.viewer.app import _bubble_html

    html = _bubble_html({
        "type": "assistant", "text": "hi",
        "tools": [{"name": "x' onmouseover='alert(1)", "input": {}}],
    })
    assert "onmouseover='alert(1)'" not in html
    assert "&#39;" in html


def test_claude_compact_boundary_renders_a_marker(tmp_path):
    from adda._src.viewer.app import _bubble_html

    html = _bubble_html({
        "type": "system",
        "subtype": "compact_boundary",
        "data": {"trigger": "auto", "preTokens": 123456},
    })
    assert "context compacted" in html
    assert "123456 tokens before" in html
    assert "policy: auto" in html


def test_claude_compact_boundary_reads_nested_metadata(tmp_path):
    from adda._src.viewer.app import _bubble_html

    html = _bubble_html({
        "type": "system", "subtype": "compact_boundary",
        "data": {"compact_metadata": {"trigger": "manual", "pre_tokens": 900,
                                      "post_tokens": 100}},
    })
    assert "900 \u2192 100 tokens" in html


def test_local_compaction_renders_the_same_marker_with_collapsed_summary(tmp_path):
    from adda._src.viewer.app import _bubble_html

    html = _bubble_html({
        "type": "ContextCompaction",
        "text": "policy=compact window=2048 source=setting",
        "policy": "compact",
        "trim": {"dropped": 41, "truncated": 0, "tokens_before": 9000,
                 "tokens_after": 1500, "budget": 1600},
        "summary": "the <b>earlier</b> work",
    })
    assert "context compacted" in html
    assert "9000 \u2192 1500 tokens" in html
    assert "41 message(s) dropped" in html
    assert "policy: compact" in html
    assert "<details class='compaction'>" in html
    assert "the &lt;b&gt;earlier&lt;/b&gt; work" in html


def test_local_trim_compaction_has_no_summary_disclosure(tmp_path):
    from adda._src.viewer.app import _bubble_html

    html = _bubble_html({
        "type": "ContextCompaction", "policy": "trim", "summary": "",
        "trim": {"dropped": 3, "tokens_before": 10, "tokens_after": 5},
    })
    assert "context compacted" in html
    assert "<details" not in html


def test_other_system_subtypes_still_render_nothing(tmp_path):
    from adda._src.viewer.app import _bubble_html

    assert _bubble_html({"type": "system", "subtype": "init", "data": {}}) == ""


def test_compaction_marker_appears_in_order_in_a_rendered_fragment(tmp_path):
    from adda._src.viewer.app import _render_fragment

    html = _render_fragment([
        {"type": "assistant", "text": "before", "tools": []},
        {"type": "ContextCompaction", "policy": "trim",
         "trim": {"dropped": 1, "tokens_before": 9, "tokens_after": 4}},
        {"type": "assistant", "text": "after", "tools": []},
    ], 0)
    assert html.index("before") < html.index("context compacted") < html.index("after")


def test_malformed_client_input_does_not_500(tmp_path):
    """Unauthenticated endpoints on a network-bound server."""
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    _write_jsonl(run / "debug" / "transcripts" / "D001.jsonl",
                 [{"type": "assistant", "text": "x"}])
    client = _writer(study)

    for bad in ("abc", "-5", "1e9999"):
        r = client.get(
            f"/api/runs/20260904T120000/transcript/D001/fragment?after={bad}")
        assert r.status_code < 500, f"after={bad} -> {r.status_code}"

    for route in ("note", "answer"):
        r = client.post(f"/api/runs/20260904T120000/{route}", content="notjson",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400


def test_a_torn_multibyte_append_does_not_500(tmp_path):
    """The readers' stated contract is that partial data degrades.

    A JSONL file caught mid-append can split a multibyte character, and
    read_text() raises UnicodeDecodeError before any line is parsed — so the
    per-line JSON guard never gets a chance.
    """
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "diagnostics.jsonl").write_bytes(
        b'{"ts": 1, "message": "caf\xc3')

    client = TestClient(create_app(study))
    assert client.get("/api/runs/20260904T120000/vitals").status_code == 200


def test_the_viewer_never_writes_into_the_study(tmp_path):
    """A read-only viewer must not create anything in the study it reads.

    graph_spec_json() calls each agent's build_closure_tools(), and those
    are not side-effect free: LiteratureReviewAgent's constructs a
    LiteratureCorpus whose __init__ mkdir()s runs/lit_reviewer_notes/ and
    papers/. Serving /graph therefore created directories inside the study
    — which read_artifacts then listed back as a "shared workspace" the
    viewer itself had produced.
    """
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    before = sorted(p.name for p in study.rglob("*"))

    client = TestClient(create_app(study))
    for _ in range(3):
        assert client.get("/api/runs/20260904T120000/graph").status_code == 200

    after = sorted(p.name for p in study.rglob("*"))
    assert before == after, f"viewer created: {sorted(set(after) - set(before))}"


def test_graph_page_shows_the_study_name_not_only_the_run_id(tmp_path):
    """The status bar identifies the study, not just an opaque timestamp.

    A run id like 20260904T120000 says nothing about which study produced
    it, and the viewer can be left open on one run for a long time.
    """
    study = _make_study(tmp_path)
    _make_run(study, "20260904T120000")
    client = TestClient(create_app(study))
    resp = client.get("/runs/20260904T120000")
    assert study.name in resp.text


def test_graph_page_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/runs/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/runs/{id}/stream — the append-while-streaming SSE test
# ---------------------------------------------------------------------------

def _read_sse_events(url, n, timeout=5.0):
    """Read *n* SSE "data:" lines off a REAL socket URL, bounded by
    *timeout* — run in a background thread so a stuck stream can't hang the
    test suite. Returns just the JSON payloads (event *type* discarded) —
    use `_read_typed_sse_events` when the test needs to disambiguate event
    types that share a JSON shape (e.g. `run_status` vs `delegation`, both
    of which carry a `status` key)."""
    return [data for _etype, data in _read_typed_sse_events(url, n, timeout)]


def _read_typed_sse_events(url, n, timeout=5.0):
    """Like `_read_sse_events`, but also captures each event's preceding
    "event: <type>" line, returning a list of (event_type, data) pairs.
    Needed because an SSE payload's JSON shape alone doesn't always say
    which event it is: a `run_status` event's `{"status": "GATED"}` is not
    structurally distinguishable from a `delegation` row's `status` field."""
    q: queue.Queue = queue.Queue()

    def _run():
        try:
            with httpx.stream("GET", url, timeout=timeout) as resp:
                collected = []
                current_event = "message"
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[len("data:"):].strip())
                        collected.append((current_event, data))
                        if len(collected) >= n:
                            break
                q.put(("ok", collected))
        except Exception as exc:  # noqa: BLE001
            q.put(("err", exc))

    threading.Thread(target=_run, daemon=True).start()
    try:
        kind, value = q.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"did not receive {n} SSE events within {timeout}s")
    if kind == "err":
        raise value
    return value


@pytest.mark.smoke
def test_stream_replays_then_reflects_status_change(tmp_path):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    log_path = run_dir / "debug" / "delegation_log.jsonl"
    _write_jsonl(log_path, [
        {"id": "D001", "status": "RUNNING", "from_node": "strategizer",
         "to_node": "critic"},
    ])

    def _append_done():
        time.sleep(0.5)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": "D001", "status": "DONE",
                "from_node": "strategizer", "to_node": "critic",
            }) + "\n")

    with _LiveServer(create_app(study)) as srv:
        threading.Thread(target=_append_done, daemon=True).start()
        events = _read_sse_events(
            f"{srv.url}/api/runs/20260904T120000/stream", n=2, timeout=8.0)

    assert events[0]["status"] == "RUNNING"
    assert events[1]["status"] == "DONE"


@pytest.mark.smoke
def test_stream_reports_a_queued_delegation_starting(tmp_path):
    """Starting a queued delegation is a patch row: status stays RUNNING and
    only session_started_at changes. The page must still hear about it, or a
    delegation that is working stays "queued" until it finishes."""
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    log_path = run_dir / "debug" / "delegation_log.jsonl"
    _write_jsonl(log_path, [
        {"id": "D002", "status": "RUNNING", "from_node": "strategizer",
         "to_node": "implementer", "session_started_at": None},
    ])

    def _append_patch():
        time.sleep(0.5)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": "D002", "patch": {"session_started_at": "2026-09-30T19:37:51+00:00"},
                "ts": "2026-09-30T19:37:51+00:00",
            }) + "\n")

    with _LiveServer(create_app(study)) as srv:
        threading.Thread(target=_append_patch, daemon=True).start()
        events = _read_sse_events(
            f"{srv.url}/api/runs/20260904T120000/stream", n=2, timeout=8.0)

    assert events[0]["session_started_at"] is None
    assert events[1]["session_started_at"] == "2026-09-30T19:37:51+00:00"


@pytest.mark.smoke
def test_stream_replays_run_status_when_already_present(tmp_path):
    """run_status.json written before the client ever connects (a run that
    already finished by the time someone opens the viewer) must replay
    immediately, not wait for a live filesystem event — it's write-once, so
    there is nothing to "tail"."""
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    (run_dir / "debug" / "run_status.json").write_text(
        json.dumps({"status": "GATED"}), encoding="utf-8")

    with _LiveServer(create_app(study)) as srv:
        events = _read_typed_sse_events(
            f"{srv.url}/api/runs/20260904T120000/stream", n=1, timeout=8.0)

    assert events[0] == ("run_status", {"status": "GATED"})


@pytest.mark.smoke
def test_stream_emits_run_status_once_it_appears_mid_stream(tmp_path):
    """A still-running run has no run_status.json yet at connect time; once
    the run closes and writes it, the already-open stream must emit it
    without the client needing to reconnect."""
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    status_path = run_dir / "debug" / "run_status.json"

    def _write_status():
        time.sleep(0.5)
        status_path.write_text(
            json.dumps({"status": "UNGATED"}), encoding="utf-8")

    with _LiveServer(create_app(study)) as srv:
        threading.Thread(target=_write_status, daemon=True).start()
        events = _read_typed_sse_events(
            f"{srv.url}/api/runs/20260904T120000/stream", n=1, timeout=8.0)

    assert events[0] == ("run_status", {"status": "UNGATED"})


def test_stream_404_for_missing_run(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    resp = client.get("/api/runs/nonexistent/stream")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The graph spec is cached PER RUN, not once per study
# ---------------------------------------------------------------------------

def test_graph_spec_cache_does_not_leak_across_runs(tmp_path):
    """Regression: _spec_cache held ONE spec for the whole study, so whichever
    run was viewed first served its models to every later run. A node's model
    is a property of the run, so the cache key has to be too."""
    import json as _json

    from adda._src.backends.base import Agent, Graph

    class _Hub(Agent):
        role = "strategizer"
        description = "hub"

    class _Math(Agent):
        role = "math_expert"
        description = "derivations"

    study = _make_study(tmp_path)
    graph = Graph(
        nodes={"strategizer": _Hub(), "math_expert": _Math()},
        edges=(), entry="strategizer",
    )

    for run_id, model in (("20260901T000000", "claude-haiku-4-5-20251001"),
                          ("20260912T142229", "qwen3.8-27b-256k")):
        run_dir = _make_run(study, run_id)
        (run_dir / "debug" / "node_models.json").write_text(_json.dumps({
            "strategizer": {"model": "claude-haiku-4-5-20251001",
                            "backend": "claude"},
            "math_expert": {"model": model, "backend": "claude"},
        }), encoding="utf-8")

    client = TestClient(create_app(study, graph=graph))

    def _math_model(run_id: str) -> str:
        resp = client.get(f"/api/runs/{run_id}/graph")
        assert resp.status_code == 200
        return next(n["model"] for n in resp.json()["nodes"]
                    if n["name"] == "math_expert")

    # Viewing the older run FIRST is what used to poison the cache.
    assert _math_model("20260901T000000") == "Claude Haiku 4.5"
    assert _math_model("20260912T142229") == "Qwen/Qwen3.8-27B (256k ctx)"
    # and the first run still reports its own model afterwards
    assert _math_model("20260901T000000") == "Claude Haiku 4.5"


# ---------------------------------------------------------------------------
# System nudges are visible, and attributed to the system
# ---------------------------------------------------------------------------

def test_injected_nudges_render_as_notices_not_as_the_humans_task(tmp_path):
    """Regression (user report, run 20260912T142229): "I wasn't able to see the
    nudges from the system in a different background color."

    Two defects, one symptom. Turn-level injections (budget warnings, the
    no-source nudge, the milestone backlog, pushed notifications) reach the
    agent on the SAME "user" role as the human's brief, and the viewer
    rendered only assistant turns and tool results — so they were not merely
    unstyled, they were absent. Marked at the injection site, lifted here.
    """
    from adda._src.nodes.notices import wrap_notice
    from adda._src.viewer.app import _bubble_html

    html = _bubble_html({
        "type": "HumanMessage",
        "text": wrap_notice("[EVAL BUDGET] 180/200 evals used.",
                            trailing="") + "\n\nPlease continue.",
    })
    assert "class='notice notice-adda'" in html, "a marked nudge must get the notice band"
    assert "EVAL BUDGET" in html
    # the human's own words stay outside the band
    assert "Please continue." in html
    assert "Please continue." not in html.split("notice-body")[1].split("</div>")[0]


def test_plain_human_turn_renders_without_a_notice_band(tmp_path):
    from adda._src.viewer.app import _bubble_html

    html = _bubble_html({"type": "HumanMessage", "text": "Minimise the drag."})
    assert "Minimise the drag." in html
    assert "class='notice" not in html


def test_empty_human_turn_renders_nothing(tmp_path):
    from adda._src.viewer.app import _bubble_html

    assert _bubble_html({"type": "HumanMessage", "text": "   "}) == ""


def test_notices_are_classified_and_monitor_is_collapsed():
    from adda._src.nodes.notices import wrap_notice
    from adda._src.viewer.app import _tool_result_html
    html = _tool_result_html(
        {"results": [{"tool_use_id": "t", "content":
            wrap_notice("[SCIENCE MONITOR — RULE_X] drifted")
            + wrap_notice("[OPERATOR NOTE — from the human] hi")
            + wrap_notice("[NUDGE] plain")
            + "out"}]}, ["Bash"])
    assert "<details class='notice notice-monitor'>" in html
    assert "RULE_X" in html
    assert "notice notice-operator" in html
    assert "notice notice-adda" in html


def test_critic_verdict_and_approval_results_carry_an_event_class():
    from adda._src.viewer.app import _tool_result_html
    v = _tool_result_html(
        {"results": [{"content": "### Verdict\n\n**REVISE**\nfix it"}]}, ["x"])
    assert "tool-result ev-verdict-revise" in v
    a = _tool_result_html(
        {"results": [{"content": "Approved. D004 finalized."}]}, ["x"])
    assert "tool-result ev-review" in a
    e = _tool_result_html(
        {"results": [{"content": "ERROR: ### Verdict REVISE"}]}, ["x"])
    assert " ev-" not in e and "is-error" in e


def test_monitor_endpoint_lists_injections_across_transcripts(tmp_path):
    from adda._src.nodes.notices import wrap_notice
    study = _make_study(tmp_path)
    run_dir = _make_run(study, "20260904T120000")
    _write_jsonl(run_dir / "debug" / "transcripts" / "D001.jsonl", [
        {"ts": "t1", "type": "tool_result", "results": [{"content": [
            {"type": "text", "text": wrap_notice(
                "[SCIENCE MONITOR — R] one") + "x"}]}]},
        {"ts": "t2", "type": "tool_result", "results": [{"content":
            wrap_notice("[NUDGE] not monitor") + "y"}]},
    ])
    _write_jsonl(
        run_dir / "debug" / "transcripts" / "strategizer" / "turn_001.jsonl",
        [{"ts": "t3", "type": "user",
          "text": wrap_notice("[SCIENCE MONITOR] +2 more: A(h1)")}])
    client = TestClient(create_app(study))
    rows = client.get("/api/runs/20260904T120000/monitor").json()
    assert sorted(r["source"] for r in rows) == [
        "D001", "strategizer/turn_001"]


def test_bundled_fonts_are_served_locally(tmp_path):
    study = _make_study(tmp_path)
    client = TestClient(create_app(study))
    r = client.get("/static/fonts/ibm-plex-sans-latin-400-normal.woff2")
    assert r.status_code == 200 and len(r.content) > 1000


# ---- write endpoints: only the page this viewer served may write ----------

_NOTE = "/api/runs/20260904T120000/note"


def _note_study(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    return study, run


def test_a_cross_origin_simple_request_cannot_inject_a_note(tmp_path):
    """text/plain is a CORS "simple request": no preflight, so a page the
    operator merely visits can send it. It must be refused on content type
    alone, even with a valid cookie."""
    from adda._src.infra import operator_channel as oc

    study, run = _note_study(tmp_path)
    client = _writer(study)
    r = client.post(_NOTE, content='{"text": "owned"}',
                    headers={"content-type": "text/plain",
                             "origin": "https://evil.example"})
    assert r.status_code == 415
    assert oc.drain_notes(run) == []


def test_a_json_write_from_another_origin_is_refused(tmp_path):
    from adda._src.infra import operator_channel as oc

    study, run = _note_study(tmp_path)
    client = _writer(study)
    r = client.post(_NOTE, json={"text": "owned"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert oc.drain_notes(run) == []


def test_a_write_from_the_viewers_own_origin_is_accepted(tmp_path):
    study, _ = _note_study(tmp_path)
    client = _writer(study)
    r = client.post(_NOTE, json={"text": "ok"},
                    headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_a_cross_site_fetch_is_refused_even_without_an_origin(tmp_path):
    study, _ = _note_study(tmp_path)
    client = _writer(study)
    r = client.post(_NOTE, json={"text": "x"},
                    headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_a_write_without_the_session_token_is_refused(tmp_path):
    from adda._src.infra import operator_channel as oc

    study, run = _note_study(tmp_path)
    client = TestClient(create_app(study, token="t0k3n"))
    r = client.post(_NOTE, json={"text": "x"})
    assert r.status_code == 403
    assert oc.drain_notes(run) == []


def test_a_wrong_token_does_not_start_a_session(tmp_path):
    study, _ = _note_study(tmp_path)
    client = TestClient(create_app(study, token="t0k3n"))
    client.get("/session?token=wrong", follow_redirects=False)
    assert client.get("/api/session").json() == {"can_write": False}
    assert client.post(_NOTE, json={"text": "x"}).status_code == 403


def test_the_session_url_grants_write_and_reads_stay_open(tmp_path):
    study, _ = _note_study(tmp_path)
    client = TestClient(create_app(study, token="t0k3n"))
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/session").json() == {"can_write": False}
    r = client.get("/session?token=t0k3n", follow_redirects=False)
    assert r.status_code == 303
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/api/session").json() == {"can_write": True}


def test_session_redirect_stays_on_this_site(tmp_path):
    study, _ = _note_study(tmp_path)
    client = TestClient(create_app(study, token="t0k3n"))
    r = client.get("/session?token=t0k3n&next=//evil.example/x",
                   follow_redirects=False)
    assert r.headers["location"].startswith("/")
    assert not r.headers["location"].startswith("//")


def test_funnel_route_serves_stages_and_404s_unknown_run(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    data = run / "experiment_data" / "experiment_data"
    data.mkdir(parents=True)
    (data / "domain.json").write_text('{"input_space": {"x": {}}}', encoding="utf-8")
    (data / "input.csv").write_text(",x\n0,1\n1,2\n", encoding="utf-8")
    (data / "output.csv").write_text(",feasible\n0,1\n1,0\n", encoding="utf-8")
    (data / "jobs.csv").write_text(",0\n0,FINISHED\n1,FINISHED\n", encoding="utf-8")
    client = TestClient(create_app(study))
    body = client.get("/api/runs/20260904T120000/funnel?stages=feasible").json()
    assert body["stores"][0]["stages"][0]["cumulative"] == 1
    assert client.get("/api/runs/nope/funnel").status_code == 404


def test_figure_of_merit_route_reads_the_declared_objective(tmp_path):
    study = _make_study(tmp_path)
    run = _make_run(study, "20260904T120000")
    (run / "debug").mkdir(exist_ok=True)
    (run / "debug" / "run_config.json").write_text(json.dumps(
        {"objective": {"column": "s", "direction": "max", "feasible": "ok"}}),
        encoding="utf-8")
    data = run / "experiment_data" / "experiment_data"
    data.mkdir(parents=True)
    (data / "output.csv").write_text(",s,ok\n0,9,0\n1,4,1\n", encoding="utf-8")
    client = TestClient(create_app(study))
    body = client.get("/api/runs/20260904T120000/figure_of_merit").json()
    assert (body["value"], body["row"]) == (4.0, 1)
    assert client.get("/api/runs/nope/figure_of_merit").status_code == 404


# ---------------------------------------------------------------------------
# /api/runs/{id}/download (spec 14 Phase 5.11)
# ---------------------------------------------------------------------------

def test_download_debug_and_store_are_zips_of_existing_files(tmp_path):
    import io
    import zipfile

    study = _make_study(tmp_path)
    run = _make_run(study, "R1")
    (run / "debug" / "run.log").write_text("hello")
    client = TestClient(create_app(study))

    r = client.get("/api/runs/R1/download?what=debug")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert names == ["debug/run.log"]
    assert "R1_debug.zip" in r.headers["content-disposition"]

    # nothing of that kind, unknown kind, unknown or escaping run
    assert client.get("/api/runs/R1/download?what=notebook").status_code == 404
    assert client.get("/api/runs/R1/download?what=store").status_code == 404
    assert client.get("/api/runs/R1/download?what=nope").status_code == 400
    assert client.get("/api/runs/ZZ/download?what=debug").status_code == 404
    assert client.get("/api/runs/../download?what=debug").status_code == 404


def test_download_notebook_serves_the_runs_file(tmp_path):
    study = _make_study(tmp_path)
    _make_run(study, "R1")
    (study / "pipeline_R1.ipynb").write_text('{"cells": []}')
    client = TestClient(create_app(study))
    r = client.get("/api/runs/R1/download?what=notebook")
    assert r.status_code == 200 and r.content == b'{"cells": []}'


def test_docs_endpoint_runs_the_same_lookup_as_adda_docs(tmp_path):
    from adda.explain import explain

    client = TestClient(create_app(_make_study(tmp_path)))
    r = client.get("/api/docs", params={"q": "AgenticRun"})
    assert r.status_code == 200
    assert r.json()["text"] == explain("AgenticRun")
    assert client.get("/api/docs", params={"q": " "}).status_code == 400
