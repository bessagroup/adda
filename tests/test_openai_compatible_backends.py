"""Unit tests for the OpenAI-compatible backends (OpenRouter, vLLM).

Fully mocked — no API keys, no network. They verify the per-backend endpoint
and auth conventions (explicit arg > env > class default) and that the shared
invoke/usage machinery works through the subclass. Cross-backend interface
parity is enforced separately in test_backend_parity.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from adda._src.backends.openrouter import OpenRouterAdapter
from adda._src.backends.vllm import VLLMAdapter

# --------------------------------------------------------------------------
# Endpoint + auth resolution: explicit > env > class default
# --------------------------------------------------------------------------

def _fake_agent() -> MagicMock:
    """A stand-in compiled graph.

    The adapter drives the graph with ``stream(stream_mode="values")`` — a
    compiled LangGraph's ``invoke`` IS that loop, returning the final yielded
    state — so tests configure ``.invoke`` as before and ``.stream`` replays
    it as a one-state stream.
    """
    fake = MagicMock()
    fake.stream.side_effect = (
        lambda state, config=None, stream_mode=None:
        iter([fake.invoke(state, config=config)]))
    return fake


def test_openrouter_defaults(monkeypatch):
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    a = OpenRouterAdapter(model="anthropic/claude-3.5-sonnet", system_prompt="s")
    assert a._base_url == "https://openrouter.ai/api/v1"
    assert a._api_key is None  # no key baked in — must be supplied


def test_openrouter_reads_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example/v1")
    a = OpenRouterAdapter(model="m", system_prompt="s")
    assert a._base_url == "https://proxy.example/v1"
    assert a._api_key == "or-secret"


def test_vllm_defaults(monkeypatch):
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    a = VLLMAdapter(model="meta-llama/Llama-3.1-8B-Instruct", system_prompt="s")
    assert a._base_url == "http://localhost:8000/v1"
    assert a._api_key == "EMPTY"  # vLLM usually needs no auth


def test_vllm_reads_env(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpu-box:8001/v1")
    monkeypatch.setenv("VLLM_API_KEY", "served-key")
    a = VLLMAdapter(model="m", system_prompt="s")
    assert a._base_url == "http://gpu-box:8001/v1"
    assert a._api_key == "served-key"


def test_explicit_args_beat_env_and_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://env/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    a = OpenRouterAdapter(
        model="m", system_prompt="s",
        base_url="https://explicit/v1", api_key="explicit-key")
    assert a._base_url == "https://explicit/v1"
    assert a._api_key == "explicit-key"


# --------------------------------------------------------------------------
# Wiring: _build_agent points ChatOpenAI at the resolved endpoint + key
# --------------------------------------------------------------------------

def test_build_agent_passes_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    a = OpenRouterAdapter(model="anthropic/claude-3.5-sonnet", system_prompt="s")
    with patch("langchain_openai.ChatOpenAI") as MockLLM, \
            patch("langgraph.prebuilt.create_react_agent") as mock_cra:
        a._build_agent()
        MockLLM.assert_called_once()
        kw = MockLLM.call_args.kwargs
        assert kw["model"] == "anthropic/claude-3.5-sonnet"
        assert kw["base_url"] == "https://openrouter.ai/api/v1"
        assert kw["api_key"] == "or-secret"
        mock_cra.assert_called_once()


# --------------------------------------------------------------------------
# Shared machinery works through the subclass (mock the agent — no network)
# --------------------------------------------------------------------------

def test_invoke_returns_content_and_populates_usage():
    from langchain_core.messages import AIMessage, HumanMessage

    # Realistic shape: the graph's returned "messages" is the input we sent
    # (lc_msgs) followed by what this call generated — the reducer appends,
    # it never replaces. Here that's the input HumanMessage plus one AI reply.
    msg = AIMessage(content="the answer")
    msg.usage_metadata = {"input_tokens": 11, "output_tokens": 7}
    fake_agent = _fake_agent()
    fake_agent.invoke.return_value = {
        "messages": [HumanMessage(content="go"), msg],
    }

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = fake_agent  # inject — no real server call
    out = a.invoke([{"role": "user", "content": "go"}])

    assert out == "the answer"
    assert a.last_usage["input_tokens"] == 11
    assert a.last_usage["output_tokens"] == 7
    assert a.last_usage["total_cost_usd"] is None


# ---------------------------------------------------------------------------
# The undercounting bug: usage must be SUMMED across every AI message
# generated this invocation, not read from the last one only. And it must
# stop at the input boundary — messages passed IN (lc_msgs) must never be
# summed, even when they carry usage_metadata from an earlier call, or the
# total would double-count history instead of just being short by it.
# ---------------------------------------------------------------------------

def _ai(content, input_tokens, output_tokens, cache_read=0, cache_creation=0):
    from langchain_core.messages import AIMessage
    m = AIMessage(content=content)
    m.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_token_details": {
            "cache_read": cache_read, "cache_creation": cache_creation,
        },
    }
    return m


def test_usage_sums_across_every_ai_message_this_invocation():
    """The regression test: several tool-calling round trips each carry
    their own usage_metadata; the total must be their sum, not the last
    message's value alone."""
    from langchain_core.messages import HumanMessage, ToolMessage

    fake_agent = _fake_agent()
    fake_agent.invoke.return_value = {"messages": [
        HumanMessage(content="go"),           # input, excluded
        _ai("call a tool", 10, 5),
        ToolMessage(content="result", tool_call_id="t1"),
        _ai("call another", 20, 8),
        ToolMessage(content="result2", tool_call_id="t2"),
        _ai("final answer", 30, 12),
    ]}

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = fake_agent
    out = a.invoke([{"role": "user", "content": "go"}])

    assert out == "final answer"
    assert a.last_usage["input_tokens"] == 10 + 20 + 30
    assert a.last_usage["output_tokens"] == 5 + 8 + 12


def test_input_history_usage_metadata_is_not_double_counted():
    """Construct a case where summing ALL of result['messages'] (instead of
    only what was generated this call) would inflate the total: the input
    HumanMessage/AIMessage pair here carries usage_metadata as if from a
    prior call. Only the new AI message's tokens may count."""
    from langchain_core.messages import HumanMessage

    stale_input_ai = _ai("previous turn's reply", 999, 999)
    fake_agent = _fake_agent()
    fake_agent.invoke.return_value = {"messages": [
        HumanMessage(content="earlier turn"),
        stale_input_ai,                      # part of the input, must be excluded
        HumanMessage(content="go"),
        _ai("final answer", 10, 5),
    ]}

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = fake_agent

    # _to_lc_messages(messages) reconstructs the exact prefix the graph was
    # given, so patch it to match the 3-message input prefix above (the two
    # HumanMessages plus the stale AI reply between them) — i.e. everything
    # in the fake result except the final new AI message.
    import adda._src.backends.openai_compatible as oc_mod
    prefix = fake_agent.invoke.return_value["messages"][:-1]
    with patch.object(oc_mod, "_to_lc_messages", return_value=prefix):
        out = a.invoke([{"role": "user", "content": "go"}])

    assert out == "final answer"
    assert a.last_usage["input_tokens"] == 10
    assert a.last_usage["output_tokens"] == 5


def test_tool_and_human_messages_are_skipped_without_usage_metadata():
    from langchain_core.messages import HumanMessage, ToolMessage

    fake_agent = _fake_agent()
    fake_agent.invoke.return_value = {"messages": [
        HumanMessage(content="go"),
        ToolMessage(content="result", tool_call_id="t1"),
        _ai("final answer", 10, 5),
    ]}

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = fake_agent
    a.invoke([{"role": "user", "content": "go"}])

    assert a.last_usage["input_tokens"] == 10
    assert a.last_usage["output_tokens"] == 5


def test_ai_message_with_no_usage_metadata_contributes_zero():
    from langchain_core.messages import AIMessage, HumanMessage

    no_meta_ai = AIMessage(content="call a tool")  # no usage_metadata set
    fake_agent = _fake_agent()
    fake_agent.invoke.return_value = {"messages": [
        HumanMessage(content="go"),
        no_meta_ai,
        _ai("final answer", 10, 5),
    ]}

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = fake_agent
    a.invoke([{"role": "user", "content": "go"}])

    assert a.last_usage["input_tokens"] == 10
    assert a.last_usage["output_tokens"] == 5
    assert a.last_usage["total_cost_usd"] is None


def test_streaming_and_non_streaming_branches_agree_on_totals(monkeypatch):
    """debug: true drives the streaming branch instead of a plain invoke();
    both must land on identical usage totals for the same conversation."""
    from langchain_core.messages import HumanMessage

    msgs = [
        HumanMessage(content="go"),
        _ai("call a tool", 10, 5),
        _ai("final answer", 20, 8),
    ]

    # Non-streaming branch.
    fake_agent_a = _fake_agent()
    fake_agent_a.invoke.return_value = {"messages": list(msgs)}
    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = fake_agent_a
    a.invoke([{"role": "user", "content": "go"}])
    non_streaming_usage = dict(a.last_usage)

    # Streaming branch: stream() yields the whole state after each step,
    # ending on the same final state invoke() would have returned. The
    # streaming branch is picked by `.base.debug_enabled()`, imported fresh
    # (locally) inside `_invoke_once` on every call, so patching it on the
    # `.base` module is what actually takes effect.
    import adda._src.backends.base as base_mod
    monkeypatch.setattr(base_mod, "debug_enabled", lambda: True)

    class _StreamingAgent:
        def stream(self, state, config=None, stream_mode=None):
            acc = list(state["messages"])
            for m in msgs[len(state["messages"]):]:
                acc = acc + [m]
                yield {"messages": list(acc)}

    b = VLLMAdapter(model="m", system_prompt="s")
    b._agent = _StreamingAgent()
    b.invoke([{"role": "user", "content": "go"}])
    streaming_usage = dict(b.last_usage)

    assert streaming_usage == non_streaming_usage
    assert streaming_usage["input_tokens"] == 10 + 20
    assert streaming_usage["output_tokens"] == 5 + 8


def test_total_cost_usd_stays_none_never_zero():
    from langchain_core.messages import HumanMessage

    fake_agent = _fake_agent()
    fake_agent.invoke.return_value = {"messages": [
        HumanMessage(content="go"), _ai("final answer", 10, 5),
    ]}

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = fake_agent
    a.invoke([{"role": "user", "content": "go"}])

    assert a.last_usage["total_cost_usd"] is None


def test_copy_is_independent():
    a = VLLMAdapter(model="m", system_prompt="s")
    b = a.copy()
    assert b is not a and b._lock is not a._lock
    assert b._oracle_nudge is not a._oracle_nudge
    b.closure_tools["X"] = 1
    assert "X" not in a.closure_tools


def test_select_native_tools_excludes_closures():
    tools = ["Bash", "Read", "Done", "FollowUp", "WriteNote", "Write"]
    native = OpenRouterAdapter.select_native_tools(tools)
    assert native == ["Bash", "Read", "Write"]  # closures dropped


# ---------------------------------------------------------------------------
# A request with no user turn is refused here, not by the provider
#
# Ollama answers a user-less payload with an opaque 500 ("no user query found
# in messages") that names nothing and reads as intermittent. It is reachable:
# nodes.parsing._to_adapter_messages and _to_lc_messages BOTH keep only
# Human/AI messages and silently discard every other role, so a history of
# system/tool messages filters to an empty list, and thread_id is fresh per
# invoke so nothing server-side backfills the turn.
#
# Whether a real run reaches that shape is still unestablished — these pin the
# guard that will say so attributably when it next happens.
# ---------------------------------------------------------------------------

def test_a_userless_payload_is_refused_with_a_diagnosable_error():
    from adda._src.backends.openai_compatible import UserlessPayloadError

    a = VLLMAdapter(model="m", system_prompt="s")

    class _NeverCalled:
        def invoke(self, *args, **kwargs):
            raise AssertionError("must not reach the provider")

    a._agent = _NeverCalled()
    with pytest.raises(UserlessPayloadError) as exc:
        a.invoke([{"role": "system", "content": "rules"},
                  {"role": "tool", "content": "output"}])

    msg = str(exc.value)
    # The error must carry the evidence: what arrived, and what survived.
    assert "system" in msg and "tool" in msg
    assert "2 message(s) in" in msg
    assert "0 survived" in msg


def test_the_userless_guard_is_not_retried_as_transient():
    """Retrying an unanswerable payload five times with backoff burns wall
    budget and buries the cause deeper."""
    from adda._src.backends.base import is_transient_error
    from adda._src.backends.openai_compatible import UserlessPayloadError

    assert is_transient_error(UserlessPayloadError("no user turn")) is False


def test_a_normal_payload_still_passes_the_guard():
    """The guard must not fire on the ordinary case."""
    a = VLLMAdapter(model="m", system_prompt="s")

    class _Fake:
        def stream(self, state, config=None, stream_mode=None):
            from langchain_core.messages import AIMessage
            yield {"messages": [AIMessage(content="fine")]}

    a._agent = _Fake()
    assert a.invoke([{"role": "user", "content": "go"}]) == "fine"


def test_both_converters_drop_unrecognised_roles():
    """Pins the shape that makes the guard necessary, in both stages. This is
    the defect the guard instruments; fixing it means preserving these roles,
    which changes what EVERY backend sees and is a separate change."""
    from langchain_core.messages import SystemMessage, ToolMessage

    from adda._src.backends.openai_compatible import _to_lc_messages
    from adda._src.nodes.parsing import _to_adapter_messages

    state = [SystemMessage(content="rules"),
             ToolMessage(content="out", tool_call_id="t1")]
    assert _to_adapter_messages(state) == []
    assert _to_lc_messages([{"role": "system", "content": "x"},
                            {"role": "tool", "content": "y"}]) == []


# ---------------------------------------------------------------------------
# Block content was DESTROYED, not merely flattened
#
# Both converters joined block lists on ``c.get("text", "")``, so every block
# that was not a text block contributed "". A message carrying only
# tool-result blocks therefore arrived as ``content=""`` — the payload gone,
# and a user turn left holding nothing. That is silent data loss on its own,
# and it is also how a request acquires an empty user turn.
# ---------------------------------------------------------------------------

def test_a_tool_result_block_keeps_its_payload():
    """The bug: this returned '' and the tool output vanished."""
    from adda._src.backends.openai_compatible import _flatten_content

    out = _flatten_content([
        {"type": "tool_result", "tool_use_id": "x", "content": "42 rows"},
    ])
    assert out == "42 rows"


def test_a_text_block_still_wins_over_content():
    from adda._src.backends.openai_compatible import _flatten_content

    assert _flatten_content(
        [{"type": "text", "text": "hello", "content": "ignored"}]) == "hello"


def test_mixed_blocks_are_all_kept_in_order():
    from adda._src.backends.openai_compatible import _flatten_content

    out = _flatten_content([
        {"type": "text", "text": "ran the sampler"},
        {"type": "tool_result", "content": "8 rows"},
    ])
    assert out == "ran the sampler 8 rows"


def test_a_plain_string_is_untouched():
    from adda._src.backends.openai_compatible import _flatten_content

    assert _flatten_content("just text") == "just text"


def test_the_graph_converter_keeps_tool_result_payloads():
    from langchain_core.messages import HumanMessage

    from adda._src.nodes.parsing import _to_adapter_messages

    out = _to_adapter_messages([
        HumanMessage(content=[{"type": "tool_result", "content": "8 rows"}]),
    ])
    assert out == [{"role": "user", "content": "8 rows"}]


# ---------------------------------------------------------------------------
# An empty turn is the same thing as a missing one
# ---------------------------------------------------------------------------

def test_an_empty_turn_is_dropped_rather_than_sent():
    """It carries nothing, and a provider that counts non-empty user turns
    rejects the WHOLE request over it — so forwarding it can only convert a
    no-op into a 500."""
    from adda._src.backends.openai_compatible import _to_lc_messages

    assert _to_lc_messages([{"role": "user", "content": ""}]) == []
    assert _to_lc_messages([{"role": "user", "content": "   \n"}]) == []
    assert len(_to_lc_messages([{"role": "user", "content": "real"}])) == 1


def test_an_empty_user_turn_does_not_satisfy_the_guard():
    """The gap that let the reported 500 through: the guard tested for a
    HumanMessage, and ``HumanMessage(content="")`` is one. The provider does
    not care about the type, only about whether a user turn says anything."""
    from adda._src.backends.openai_compatible import UserlessPayloadError

    a = VLLMAdapter(model="m", system_prompt="s")

    class _NeverCalled:
        def invoke(self, *args, **kwargs):
            raise AssertionError("must not reach the provider")

    a._agent = _NeverCalled()
    with pytest.raises(UserlessPayloadError) as exc:
        a.invoke([{"role": "user", "content": "   "},
                  {"role": "ai", "content": "sure"}])

    # the ai turn survives; the empty user turn does not, which is the point
    assert "1 survived" in str(exc.value)
    assert "empty after flattening" in str(exc.value)


# ---------------------------------------------------------------------------
# A turn that RAISES has still spent tokens
#
# Observed on the qwen drop-rebound baseline (run 20260922T211717): three of
# five delegations died inside a tool (NotImplementedError from Glob, an NFS
# temp-dir cleanup OSError, a FileNotFoundError from download_paper). Every
# token the server generated for those turns was invisible to telemetry,
# because usage was only ever read on the success path. On a run whose
# delegations fail, that is most of the run.
# ---------------------------------------------------------------------------

def test_usage_is_captured_when_the_turn_raises():
    from langchain_core.messages import HumanMessage

    generated = [
        HumanMessage(content="go"),
        _ai("call a tool", 10, 5),
        _ai("call another", 20, 8),
    ]

    class _DyingAgent:
        def stream(self, state, config=None, stream_mode=None):
            acc = list(state["messages"])
            for m in generated[len(state["messages"]):]:
                acc = acc + [m]
                yield {"messages": list(acc)}
            raise NotImplementedError("Non-relative patterns are unsupported")

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = _DyingAgent()

    with pytest.raises(NotImplementedError):
        a.invoke([{"role": "user", "content": "go"}])

    assert a.last_usage["input_tokens"] == 10 + 20
    assert a.last_usage["output_tokens"] == 5 + 8


def test_a_turn_that_dies_before_any_model_call_reports_zero_not_stale():
    """The failure path must not leave the PREVIOUS turn's numbers standing —
    that would double-count them on the next record."""
    from langchain_core.messages import HumanMessage

    a = VLLMAdapter(model="m", system_prompt="s")
    a._agent = _fake_agent()
    a._agent.invoke.return_value = {"messages": [
        HumanMessage(content="go"), _ai("final answer", 10, 5),
    ]}
    a.invoke([{"role": "user", "content": "go"}])
    assert a.last_usage["output_tokens"] == 5

    class _InstantlyDyingAgent:
        def stream(self, state, config=None, stream_mode=None):
            raise RuntimeError("connection refused")
            yield  # pragma: no cover - makes this a generator

    a._agent = _InstantlyDyingAgent()
    with pytest.raises(RuntimeError):
        a.invoke([{"role": "user", "content": "go"}])

    assert a.last_usage["input_tokens"] == 0
    assert a.last_usage["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Glob: a model-supplied pattern must never end the run
#
# Run 20260922T211717, D001: the model passed an absolute pattern, Path.glob
# raised NotImplementedError("Non-relative patterns are unsupported"), and the
# whole literature_reviewer delegation died. A tool given a bad argument owes
# its caller an error it can read, not a crash — which the sibling Grep tool
# already did and this one did not.
# ---------------------------------------------------------------------------

def test_glob_accepts_an_absolute_pattern(tmp_path):
    from adda._src.backends.openai_compatible import _make_glob_tool

    (tmp_path / "a.pdf").write_text("x")
    tool = _make_glob_tool(tmp_path)

    out = tool.invoke({"pattern": str(tmp_path / "*.pdf")})
    assert str(tmp_path / "a.pdf") in out


def test_glob_finds_relative_patterns_under_the_workspace(tmp_path):
    from adda._src.backends.openai_compatible import _make_glob_tool

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("x")
    tool = _make_glob_tool(tmp_path)

    assert "b.txt" in tool.invoke({"pattern": "sub/*.txt"})
    assert tool.invoke({"pattern": "sub/*.md"}) == "(no matches)"


def test_glob_returns_an_error_string_it_never_raises(tmp_path):
    from adda._src.backends.openai_compatible import _make_glob_tool

    tool = _make_glob_tool(tmp_path)
    out = tool.invoke({"pattern": ""})  # Path("") -> ValueError inside glob
    assert out.startswith("ERROR:") or out == "(no matches)"


# ---------------------------------------------------------------------------
# read_paper: the PDF handle is closed before the temp dir goes away
#
# Run 20260922T211717, D002 died with OSError [Errno 39] Directory not empty
# on the TemporaryDirectory teardown — after the paper's text had already
# been extracted. fitz.open() left the file open; on NFS, unlinking an open
# file silly-renames it to .nfsXXXX, so the following rmdir finds the
# directory non-empty.
# ---------------------------------------------------------------------------

def _arxiv_closures(monkeypatch, fake_fitz):
    """Build the arxiv tool closures with a stubbed fitz and no network."""
    import sys

    from adda._src.backends import openai_compatible as oc

    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)
    closures = oc._build_arxiv_closures()
    if not closures:
        pytest.skip("arxiv not installed")
    return closures


def test_read_paper_closes_the_pdf_before_cleanup(monkeypatch):
    import types

    closed = []

    class _Page:
        def get_text(self):
            return "body text"

    class _Doc:
        def __iter__(self):
            return iter([_Page()])

        def close(self):
            closed.append(True)

    fake_fitz = types.SimpleNamespace(open=lambda path: _Doc())
    closures = _arxiv_closures(monkeypatch, fake_fitz)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(b"%PDF-1.4"),
    )

    assert closures["arxiv_read_paper"]("2505.00902") == "body text"
    assert closed == [True], "PDF handle left open at temp-dir teardown"


def test_read_paper_closes_the_pdf_even_when_extraction_raises(monkeypatch):
    import types

    closed = []

    class _Doc:
        def __iter__(self):
            raise RuntimeError("corrupt page tree")

        def close(self):
            closed.append(True)

    fake_fitz = types.SimpleNamespace(open=lambda path: _Doc())
    closures = _arxiv_closures(monkeypatch, fake_fitz)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(b"%PDF-1.4"),
    )

    with pytest.raises(RuntimeError):
        closures["arxiv_read_paper"]("2505.00902")
    assert closed == [True]


def test_read_paper_survives_a_temp_dir_that_will_not_delete(monkeypatch):
    """The NFS failure mode itself: cleanup fails, the text still comes back."""
    import os
    import shutil
    import sys
    import types

    class _Page:
        def get_text(self):
            return "body text"

    class _Doc:
        def __iter__(self):
            return iter([_Page()])

        def close(self):
            pass

    fake_fitz = types.SimpleNamespace(open=lambda path: _Doc())
    closures = _arxiv_closures(monkeypatch, fake_fitz)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(b"%PDF-1.4"),
    )

    # Faithful to the real path: shutil.rmtree REPORTS the failure through
    # the hook tempfile installs, and it is that hook which honours
    # ignore_cleanup_errors. Raising straight out of rmtree instead would
    # test a mechanism the failure never went through.
    #
    # tempfile passes onerror= up to 3.11 and onexc= from 3.12 on, with
    # different third arguments (an exc_info triple vs the exception). Accept
    # whichever this interpreter installs: a stub written to one spelling
    # calls None on the other, which is a failure of the test and says
    # nothing about the code under it.
    def _boom(path, onexc=None, onerror=None, **k):
        try:
            raise OSError(39, "Directory not empty")
        except OSError as exc:
            if onexc is not None:
                onexc(os.rmdir, path, exc)
            else:
                onerror(os.rmdir, path, sys.exc_info())

    monkeypatch.setattr(shutil, "rmtree", _boom)
    assert closures["arxiv_read_paper"]("2505.00902") == "body text"


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# download_paper: the output directory is created, not assumed
#
# Run 20260922T211717, D003: the model asked for the paper under "D003/",
# its own delegation namespace, which did not exist relative to the process
# cwd. open() then raised FileNotFoundError naming the PDF, so the failure
# read as a bad download rather than a missing directory.
# ---------------------------------------------------------------------------

def test_download_paper_creates_a_missing_output_dir(monkeypatch, tmp_path):
    import types

    closures = _arxiv_closures(monkeypatch, types.SimpleNamespace(open=None))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(b"%PDF-1.4"),
    )

    dest_dir = tmp_path / "D003" / "papers"
    assert not dest_dir.exists()

    out = closures["arxiv_download_paper"]("2505.00902", str(dest_dir))

    written = dest_dir / "2505.00902.pdf"
    assert written.exists()
    assert written.read_bytes() == b"%PDF-1.4"
    assert str(written) in out
