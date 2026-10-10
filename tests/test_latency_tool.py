"""internal/tools/latency.py on synthetic transcripts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "latency", Path(__file__).parents[1] / "internal/tools/latency.py")
latency = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(latency)

T0 = 1_000_000.0


def _ts(sec: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(T0 + sec, timezone.utc).isoformat(timespec="seconds")


def _call(at, stop, tool=None, tid="t1", end=None):
    rows = [{"ts": _ts(at), "type": "system", "subtype": "status",
             "data": {"status": "requesting"}},
            {"ts": _ts(at + 1), "type": "stream_evt", "evt": "message_start"}]
    if tool:
        rows.append({"ts": _ts(stop), "type": "assistant", "tools": [
            {"name": tool, "input": {}, "tool_use_id": tid}]})
    rows.append({"ts": _ts(stop), "type": "stream_evt",
                 "evt": "message_stop"})
    if tool:
        rows.append({"ts": _ts(end), "type": "tool_result",
                     "results": [{"tool_use_id": tid, "content": "ok"}]})
    return rows


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows))


def test_the_wall_is_split_by_rank_and_the_rest_is_idle(tmp_path):
    dbg = tmp_path / "run" / "r1" / "debug"
    _write(dbg / "transcripts/strategizer/turn_001.jsonl",
           _call(0, 10, "Bash", end=40) + _call(40, 50))
    _write(dbg / "transcripts/D001.jsonl", _call(20, 30))
    _write(dbg / "transcripts/critic/call_001.jsonl",
           _call(60, 70) + _call(70, 80))
    (dbg / "run_status.json").write_text(
        json.dumps({"status": "GATED", "wall_s": 100}))
    (dbg / "run_started_at").write_text(str(T0))
    r = latency.analyse(dbg)
    k = r["kinds"]
    assert k["model"] == 10 + 10 + 10   # call 0-10, call 20-30, call 40-50
    assert k["shell"] == 20             # Bash 10-40 minus the child's 20-30
    assert k["critic"] == 20            # 60-80
    assert k["idle"] == 100 - 30 - 20 - 20
    assert abs(sum(k.values()) - 100) < 1e-6


def test_tool_kinds():
    p = latency.ADDA_PREFIX
    assert latency.tool_kind("Bash") == "shell"
    assert latency.tool_kind(p + "CorpusAdd") == "lit"
    assert latency.tool_kind(p + "Delegate") == "wait"
    assert latency.tool_kind(p + "HypothesisUpdate") == "adda"


def test_a_blocking_tool_is_not_model_time(tmp_path):
    """The CLI logs message_stop only after a blocking tool returns."""
    dbg = tmp_path / "run" / "r1" / "debug"
    rows = _call(0, 100, "Bash", end=100)
    rows.insert(2, {"ts": _ts(2), "type": "assistant", "text": "go",
                    "tools": [{"name": "Bash", "input": {},
                               "tool_use_id": "t1"}]})
    rows = [r for r in rows if not (r["type"] == "assistant" and r["ts"] == _ts(100))]
    rows.insert(3, {"ts": _ts(2), "type": "stream_evt",
                    "evt": "content_block_stop"})
    _write(dbg / "transcripts/strategizer/turn_001.jsonl", rows)
    (dbg / "run_status.json").write_text(
        json.dumps({"status": "GATED", "wall_s": 100}))
    (dbg / "run_started_at").write_text(str(T0))
    k = latency.analyse(dbg)["kinds"]
    assert k["model"] == 2
    assert k["shell"] == 98
