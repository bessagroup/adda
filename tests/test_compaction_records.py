"""A context compaction must leave the same run-level trace on every backend.

The Claude backend always wrote a CONTEXT_COMPACTED diagnostic; the local
(OpenAI-compatible) hook wrote only a debug-gated transcript record, so a
non-debug local run that compacted left nothing for analysis.
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from adda._src.backends import base
from adda._src.runtime import settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.configure(None)
    settings.configure(None)
    yield
    settings.configure(None)
    base.set_delegation_id(None)
    base.set_run_config_path(None)
    base.set_transcript_sink(None)


def _bind(tmp_path):
    debug = tmp_path / "debug"
    debug.mkdir()
    rc = debug / "run_config.json"
    rc.write_text(json.dumps({"store_dir": str(tmp_path), "study_dir": "x"}))
    base.set_delegation_id("D003")
    base.set_run_config_path(str(rc))
    return debug / "diagnostics.jsonl"


def _convo(n, size=400):
    msgs = [HumanMessage(content="THE ORIGINAL TASK")]
    for i in range(n):
        msgs.append(AIMessage(content=f"step {i} " + "x" * size))
    return msgs


def _hook(policy, **knobs):
    from adda._src.backends.vllm import VLLMAdapter

    settings.configure({"context_policy": policy, **knobs})
    a = VLLMAdapter(model="m", system_prompt="s")
    a._ctx_window = (2048, "setting")
    a._summarize = lambda prompt: "THE SUMMARY"
    return a._context_hook("SYSTEM")


def _diag(path):
    return [json.loads(x) for x in path.read_text().splitlines()
            if json.loads(x).get("error_type") == "CONTEXT_COMPACTED"]


@pytest.mark.parametrize("policy", ["trim", "compact"])
def test_local_compaction_writes_a_diagnostic_with_debug_off(tmp_path, policy):
    diag = _bind(tmp_path)
    _hook(policy)({"messages": _convo(200)})
    rows = _diag(diag)
    assert len(rows) == 1
    data = rows[0]["compaction_data"]
    assert data["policy"] == policy
    assert data["window"] == 2048
    assert data["dropped"] > 0
    assert data["tokens_before"] > data["tokens_after"]
    assert rows[0]["node"] == "D003"


def test_an_unchanged_compaction_is_not_re_recorded_every_turn(tmp_path):
    diag = _bind(tmp_path)
    hook = _hook("trim")
    msgs = _convo(200)
    hook({"messages": msgs})
    hook({"messages": msgs})
    assert len(_diag(diag)) == 1
    hook({"messages": _convo(260)})
    assert len(_diag(diag)) == 2


def test_no_diagnostic_when_nothing_was_compacted(tmp_path):
    diag = _bind(tmp_path)
    _hook("trim")({"messages": _convo(2)})
    assert not diag.exists() or not _diag(diag)


def test_compact_transcript_record_carries_the_summary(tmp_path, monkeypatch):
    _bind(tmp_path)
    sink = tmp_path / "D003.jsonl"
    base.set_transcript_sink(str(sink))
    _hook("compact", debug=True)({"messages": _convo(200)})
    recs = [json.loads(x) for x in sink.read_text().splitlines()]
    rec = [r for r in recs if r["type"] == "ContextCompaction"][0]
    assert rec["policy"] == "compact"
    assert rec["summary"] == "THE SUMMARY"
    assert rec["trim"]["dropped"] > 0
