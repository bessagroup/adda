"""Schema-level regression for model-facing list-typed tool parameters.

``test_list_arg_decoding.py`` covers ``decode_list_arg`` itself, called
directly. It never caught the real-world failure, because the decoder is
only reachable AFTER pydantic has already validated the raw tool-call
argument against the function's declared annotation. A backend that routes
tool calls through ``langchain_core.tools.StructuredTool.from_function``
(the ``openai_compatible`` backend used for local/vLLM models, e.g.
qwen3.8-27B) validates BEFORE the function body runs — so an annotation of
``list | None`` rejects a JSON/Python-repr-encoded string at the schema
boundary, and the decoder inside the body is never reached.

Reproduced directly with ``StructuredTool.from_function``: a real list, or
``[]``/``None``, passes; a JSON string (``'["H3"]'``) is REJECTED with
``hypothesis_ids: Input should be a valid list`` even though the decoder
handles that exact string correctly. Widening the annotation to
``list | str | None`` is the fix — this module builds the actual
``StructuredTool`` the backend builds (via
``OpenAICompatibleAdapter._build_tools``) and exercises the schema, not
just the decoder.
"""
from __future__ import annotations

import json

import pytest

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.backends.openai_compatible import OpenAICompatibleAdapter
from adda._src.infra.delegation_log import DelegationLog
from adda._src.nodes import Node


@pytest.fixture(autouse=True)
def _isolate_from_milestone_gate():
    """These tests exercise delegation mechanics, not the process backlog
    (covered in test_milestones.py)."""
    from adda._src.runtime import settings
    settings.configure({"milestones_enabled": False})
    yield
    settings.configure({})


class _Worker:
    """Minimal worker adapter: returns a canned Report synchronously and
    records every message it was sent (for the task-message regression
    below)."""

    def __init__(self) -> None:
        self.closure_tools: dict = {}
        self.seen: list[str] = []

    def copy(self):
        return self

    def invoke(self, messages):
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, str):
                self.seen.append(content)
        return "## Report\n\n### Conclusions\ndone\n"


def _node(tmp_path, with_critic=False):
    class S(Agent):
        role = "strategizer"
        tools = frozenset({"HypothesisList", "QueryStore"})
        description = "s"

    class W(Agent):
        description = "w"

    nodes = {"strategizer": S(), "implementer": W()}
    edges = [Edge("strategizer", "implementer")]
    worker = _Worker()
    worker_adapters = {"implementer": worker}
    if with_critic:
        class C(Agent):
            role = "critic"
            description = "c"

        nodes["critic"] = C()
        edges.append(Edge("strategizer", "critic"))
        worker_adapters["critic"] = _Worker()

    spec = Graph(nodes=nodes, edges=tuple(edges), entry="strategizer")
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    node = Node(
        _Worker(), name="strategizer",
        outgoing=list(worker_adapters),
        spec=spec, worker_adapters=worker_adapters, notes_dir=notes,
        delegation_log=DelegationLog(
            tmp_path / "debug" / "delegation_log.jsonl"),
        study_dir=tmp_path,
    )
    return node, worker


def _register_two_hypotheses(node):
    kw = dict(falsification_criterion="fc", prediction="pred", prior=0.5,
              proposed_by="test")
    h1 = node._ledger.propose(statement="one", **kw).split()[0]
    h2 = node._ledger.propose(statement="two", **kw).split()[0]
    return h1, h2


def _structured_tools(closure_tools):
    """Build the exact StructuredTools the openai_compatible backend builds
    for a vLLM/qwen-style model — the schema a model call is actually
    validated against."""
    adapter = OpenAICompatibleAdapter(
        model="test-model", system_prompt="", closure_tools=closure_tools,
    )
    return {t.name: t for t in adapter._build_tools()}


# ---------------------------------------------------------------------------
# Delegate.hypothesis_ids
# ---------------------------------------------------------------------------

def _encodings(ids):
    """Every shape a model may emit for a list-valued argument."""
    return {
        "real_list": list(ids),
        "json_string": json.dumps(ids),
        "python_repr_string": repr(ids),
        "bare_scalar": ids[0],
    }


@pytest.mark.parametrize(
    "label", ["real_list", "json_string", "python_repr_string"])
def test_delegate_hypothesis_ids_schema_accepts_string_encodings(
        tmp_path, label):
    """Delegate's schema (as built for the model) must accept a real list
    AND a string encoding of one, and decode it to the same ids."""
    node, worker = _node(tmp_path)
    h1, h2 = _register_two_hypotheses(node)
    raw = _encodings([h1, h2])[label]
    tools = _structured_tools(node._build_routing_closures())

    result = tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": raw, "wait": True,
    })

    assert not str(result).startswith("ERROR"), (
        f"Delegate schema rejected/refused encoding {raw!r}: {result}"
    )
    with node._registry_lock:
        entries = list(node._registry.values())
    assert entries, "no delegation was registered"
    assert entries[-1]["hypothesis_ids"] == [h1, h2], (
        f"encoding {raw!r} decoded to {entries[-1]['hypothesis_ids']!r}"
    )


def test_delegate_hypothesis_ids_schema_accepts_bare_scalar_string(tmp_path):
    node, worker = _node(tmp_path)
    h1, _h2 = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())

    result = tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": h1, "wait": True,
    })

    assert not str(result).startswith("ERROR"), result
    with node._registry_lock:
        entries = list(node._registry.values())
    assert entries[-1]["hypothesis_ids"] == [h1]


@pytest.mark.parametrize("raw", [None, []])
def test_delegate_hypothesis_ids_schema_accepts_none_and_empty(
        tmp_path, raw):
    """None and [] must not be schema errors either way — Delegate's own
    domain policy (refuse when the ledger has entries but none were cited)
    is unrelated to schema validation and is exercised elsewhere; this only
    pins that neither value raises at the schema boundary."""
    node, worker = _node(tmp_path)
    _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())

    # Must not raise (a pydantic ValidationError would propagate from
    # .invoke() as an exception, not a string return).
    tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": raw, "wait": True,
    })


def test_delegate_task_message_does_not_explode_string_hypothesis_ids(
        tmp_path):
    """Regression for a second, related bug found while widening the
    schema: Delegate forwards its RAW ``hypothesis_ids`` (not the already
    -decoded ``h_ids``) into ``_compose_task_message`` ->
    ``_hypothesis_brief``, which does ``for hid in hypothesis_ids``. That
    was harmless while pydantic guaranteed a real list; once the schema
    accepts a string, an undecoded JSON string would iterate character by
    character into the worker's task message."""
    node, worker = _node(tmp_path)
    h1, h2 = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())

    tools["Delegate"].invoke({
        "target": "implementer", "intent": "do work", "expected_report": "",
        "hypothesis_ids": json.dumps([h1, h2]),
        "is_falsification_attempt": True, "wait": True,
    })

    sent = "\n".join(worker.seen)
    assert f"**{h1}**" in sent, sent[:1500]
    assert f"**{h2}**" in sent, sent[:1500]
    # A char-exploded id would show up as a bare single-character bullet.
    assert "**[**" not in sent and "**\"**" not in sent, sent[:1500]


# ---------------------------------------------------------------------------
# AskForFeedback.hypothesis_ids
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label", ["real_list", "json_string", "python_repr_string"])
def test_ask_for_feedback_hypothesis_ids_schema_accepts_string_encodings(
        tmp_path, label):
    node, worker = _node(tmp_path, with_critic=True)
    h1, h2 = _register_two_hypotheses(node)
    raw = _encodings([h1, h2])[label]
    tools = _structured_tools(node._build_routing_closures())

    result = tools["AskForFeedback"].invoke({"hypothesis_ids": raw})
    assert result, "AskForFeedback returned nothing"

    last_line = (
        (tmp_path / "debug" / "delegation_log.jsonl")
        .read_text().strip().splitlines()[-1]
    )
    rec = json.loads(last_line)
    assert rec["hypothesis_ids"] == [h1, h2], (
        f"encoding {raw!r} decoded to {rec['hypothesis_ids']!r}"
    )


def test_ask_for_feedback_hypothesis_ids_schema_accepts_bare_scalar(
        tmp_path):
    node, worker = _node(tmp_path, with_critic=True)
    h1, _h2 = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())

    tools["AskForFeedback"].invoke({"hypothesis_ids": h1})

    last_line = (
        (tmp_path / "debug" / "delegation_log.jsonl")
        .read_text().strip().splitlines()[-1]
    )
    rec = json.loads(last_line)
    assert rec["hypothesis_ids"] == [h1]


@pytest.mark.parametrize("raw", [None, []])
def test_ask_for_feedback_hypothesis_ids_schema_accepts_none_and_empty(
        tmp_path, raw):
    node, worker = _node(tmp_path, with_critic=True)
    tools = _structured_tools(node._build_routing_closures())
    # Must not raise.
    tools["AskForFeedback"].invoke({"hypothesis_ids": raw})


# ---------------------------------------------------------------------------
# HypothesisList.hypothesis_ids (accepted-and-ignored)
# ---------------------------------------------------------------------------

def test_hypothesis_list_schema_accepts_string_encoded_ids(tmp_path):
    """A JSON-string list is decoded, and the ids it names are the ones
    returned in full — HypothesisList absorbed HypothesisGet."""
    node, worker = _node(tmp_path)
    h1, h2 = _register_two_hypotheses(node)
    tools = _structured_tools(node._build_routing_closures())

    result = tools["HypothesisList"].invoke(
        {"hypothesis_ids": json.dumps([h1])})

    assert f'"id": "{h1}"' in result and h2 not in result, result


# ---------------------------------------------------------------------------
# QueryStore.delegation_ids / .columns — already str | list | None; pinned
# here so the annotation can never be narrowed back without a red test.
# ---------------------------------------------------------------------------

def test_query_store_schema_already_accepts_string_encodings(tmp_path):
    node, worker = _node(tmp_path)
    tools = _structured_tools(node._build_routing_closures())

    # No experiment store exists in this tmp_path — the point is only that
    # the SCHEMA accepts these encodings without raising.
    result = tools["QueryStore"].invoke({
        "delegation_ids": "['D001', 'D002']",
        "columns": '["f", "feasible"]',
    })
    assert isinstance(result, str)
