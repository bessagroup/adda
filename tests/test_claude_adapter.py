"""Tests for ClaudeAdapter — stub out claude_agent_sdk.query."""
import types
import sys


# ---------------------------------------------------------------------------
# Helpers to build realistic fake SDK event streams
# ---------------------------------------------------------------------------

class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _AssistantMessage:
    def __init__(self, blocks) -> None:
        self.content = blocks


class _ResultMessage:
    usage = None
    total_cost_usd = None


class _StreamEvent:
    pass


class _SystemMessage:
    def __init__(self, subtype, data=None) -> None:
        self.subtype = subtype
        self.data = data or {}


class _UserMessage:
    def __init__(self, blocks=None) -> None:
        self.content = blocks or []


class _ToolUseBlockType:
    pass


def make_async_gen_with_messages(*text_contents):
    """Yield AssistantMessage-like events with TextBlock content, then ResultMessage."""
    async def _gen(prompt, options):
        yield _AssistantMessage([_TextBlock(t) for t in text_contents])
        yield _ResultMessage()
    return _gen


def _install_fake_sdk(**extra):
    """Install (or update) a fake claude_agent_sdk in sys.modules."""
    mod = sys.modules.get("claude_agent_sdk")
    if mod is None:
        mod = types.ModuleType("claude_agent_sdk")
        sys.modules["claude_agent_sdk"] = mod

    mod.AssistantMessage = _AssistantMessage
    mod.ResultMessage = _ResultMessage
    mod.TextBlock = _TextBlock
    mod.SdkMcpTool = object  # not used in these tests
    mod.StreamEvent = _StreamEvent
    mod.SystemMessage = _SystemMessage
    mod.UserMessage = _UserMessage
    mod.ToolUseBlock = _ToolUseBlockType
    mod.ClaudeAgentOptions = lambda **kw: kw
    mod.create_sdk_mcp_server = lambda name=None, tools=None: {"name": name}

    for k, v in extra.items():
        setattr(mod, k, v)

    return mod


def _get_adapter():
    """Return the (possibly reloaded) ClaudeAdapter with SDK pre-marked available."""
    import adda._src.backends.claude as cmod
    cmod._SDK_AVAILABLE = True
    return cmod.ClaudeAdapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_single_text_block():
    """Single TextBlock → returns that text exactly."""
    _install_fake_sdk(query=make_async_gen_with_messages("hello world"))
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    result = adapter.invoke([{"role": "user", "content": "hi"}])
    assert result == "hello world"


def test_transcript_captured_when_debug_on(tmp_path, monkeypatch):
    """With F3DASM_DEBUG on and a sink set, ainvoke streams assistant text +
    result records to the transcript JSONL."""
    import json
    from adda._src.backends.base import set_transcript_sink
    monkeypatch.setenv("F3DASM_DEBUG", "1")
    _install_fake_sdk(query=make_async_gen_with_messages("reasoning here"))
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    sink = tmp_path / "D001.jsonl"
    set_transcript_sink(str(sink))
    adapter.invoke([{"role": "user", "content": "hi"}])
    set_transcript_sink(None)

    recs = [json.loads(x) for x in sink.read_text().strip().splitlines()]
    types = [r["type"] for r in recs]
    assert "assistant" in types
    asst = next(r for r in recs if r["type"] == "assistant")
    assert asst["text"] == "reasoning here"


def test_partials_flushed_for_incomplete_turn(tmp_path, monkeypatch):
    """An agent that never completes a message (infinite thinking) emits only
    StreamEvents — the transcript must STILL disclose its partial output via
    periodic flushes, not stay empty."""
    import json

    from adda._src.backends.base import set_transcript_sink

    class _StreamEv:
        def __init__(self, text):
            self.event = {"delta": {"type": "text_delta", "text": text}}

    def _gen_never_completes(prompt, options):
        async def _g(prompt, options):
            # 60 stream events, NO AssistantMessage, NO ResultMessage.
            for i in range(60):
                yield _StreamEv(f"tok{i} ")
        return _g

    monkeypatch.setenv("F3DASM_DEBUG", "1")
    mod = _install_fake_sdk(query=_gen_never_completes(None, None))
    mod.StreamEvent = _StreamEv  # adapter isinstance-checks this
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    sink = tmp_path / "D001.jsonl"
    set_transcript_sink(str(sink))
    adapter.invoke([{"role": "user", "content": "hi"}])
    set_transcript_sink(None)

    recs = [json.loads(x) for x in sink.read_text().strip().splitlines()]
    partials = [r for r in recs if r["type"] == "partial"]
    assert partials, "incomplete turn disclosed nothing"
    # 60 events → flush at 25, 50, plus the teardown flush for the tail.
    assert any("tok49" in r["text"] for r in partials)


def _capture_options_gen(captured: dict):
    """A fake query that records the ClaudeAgentOptions dict it was given."""
    async def _gen(prompt, options):
        captured["options"] = options
        yield _AssistantMessage([_TextBlock("ok")])
        yield _ResultMessage()
    return _gen


def test_infer_schema_skips_underscore_closure_params():
    """Closure capture-args (_ws=..., _did=...) must NOT appear in the tool
    schema — else a model can pass them (e.g. _ws as a string) and crash a
    write tool with str / path. Real params are still exposed."""
    from adda._src.backends.claude import _infer_schema_from_callable

    def _write(path: str, body: str, _ws="x", _did="D001"):
        return ""

    schema = _infer_schema_from_callable(_write)
    props = schema["properties"]
    assert "path" in props and "body" in props
    assert "_ws" not in props and "_did" not in props
    assert set(schema.get("required", [])) == {"path", "body"}


def test_resume_option_sets_fork_session_false(monkeypatch):
    """Spec 12 item 3: resuming a worker's session for report review must
    continue the SAME session, not branch a copy of it -- passing
    resume= to ainvoke/invoke must set fork_session=False on the actual
    ClaudeAgentOptions built, and carry the session id through
    unchanged."""
    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap))
    ClaudeAdapter = _get_adapter()
    ClaudeAdapter("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "clarify this"}], resume="sess-1")
    assert cap["options"]["resume"] == "sess-1"
    assert cap["options"]["fork_session"] is False


def test_no_resume_option_when_resume_not_passed():
    """The ordinary (non-review) path must not carry a resume/fork_session
    key at all -- a normal fresh invocation is unaffected."""
    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap))
    ClaudeAdapter = _get_adapter()
    ClaudeAdapter("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "hi"}])
    assert "resume" not in cap["options"]
    assert "fork_session" not in cap["options"]


def test_max_buffer_size_is_set_and_tunable(monkeypatch):
    """The 1MB default crashed the lit reviewer on a >1MB PDF result; we set
    30MB (env-tunable) so realistic large tool results don't overflow."""
    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap))
    ClaudeAdapter = _get_adapter()
    ClaudeAdapter("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "hi"}])
    assert cap["options"]["max_buffer_size"] == 30 * 1024 * 1024
    cap.clear()
    monkeypatch.setenv("F3DASM_LLM_MAX_BUFFER_MB", "50")
    _install_fake_sdk(query=_capture_options_gen(cap))
    _get_adapter()("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "hi"}])
    assert cap["options"]["max_buffer_size"] == 50 * 1024 * 1024


def test_buffer_overflow_is_graceful_not_fatal(monkeypatch):
    """A buffer-overflow mid-stream must NOT crash the delegation — the turn
    ends with a clear marker so the agent can retry smaller (gracefully
    contour), instead of the old fatal non-retried FAILED."""
    def _overflow_gen(prompt, options):
        async def _g(prompt, options):
            yield _AssistantMessage([_TextBlock("partial work")])
            raise Exception(
                "Failed to decode JSON: JSON message exceeded maximum "
                "buffer size of 1048576 bytes")
        return _g
    _install_fake_sdk(query=_overflow_gen(None, None))
    ClaudeAdapter = _get_adapter()
    # Does NOT raise — returns gracefully with the marker appended.
    out = ClaudeAdapter("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "hi"}])
    assert "partial work" in out
    assert "overflowed the message buffer" in out


def test_non_buffer_stream_error_still_raises():
    """Only buffer-overflow is contoured; other stream errors still propagate
    (so retry_on_transient / FAILED handling stays intact)."""
    import pytest as _pytest

    def _err_gen(prompt, options):
        async def _g(prompt, options):
            yield _AssistantMessage([_TextBlock("x")])
            raise RuntimeError("some other fatal error")
        return _g
    _install_fake_sdk(query=_err_gen(None, None))
    ClaudeAdapter = _get_adapter()
    with _pytest.raises(RuntimeError):
        ClaudeAdapter("claude-3", "sys", None, []).invoke(
            [{"role": "user", "content": "hi"}])


def test_no_stale_computer_tool_in_native_tools_or_disallowed():
    """Regression (wet-test self-consistency finding #3, run 20260926T214835):
    'computer' was listed in NATIVE_TOOLS as if it were a real, selectable
    native SDK tool, and unconditionally denied in _base_disallowed for every
    agent — but no bare 'computer' tool exists in the bundled CLI (its real
    Computer Use surface is an MCP server, not a native tool type adda ever
    wires up), so the CLI printed "Permission deny rule 'computer' matches no
    known tool" on every single agent session in the run. Confirmed by
    `strings` on the bundled CLI binary: no bare 'computer' native tool type
    exists there either."""
    from adda._src.backends.claude import ClaudeAdapter
    assert "computer" not in ClaudeAdapter.NATIVE_TOOLS

    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap))
    adapter = _get_adapter()("claude-3", "sys", None, [])
    adapter.invoke([{"role": "user", "content": "hi"}])
    assert "computer" not in cap["options"]["disallowed_tools"]


def test_session_is_hermetic_setting_sources_empty():
    """#1 fresh hooks: sessions load NO filesystem settings, so worker/critic
    subprocesses don't inherit the developer's global ~/.claude hooks."""
    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap))
    ClaudeAdapter = _get_adapter()
    ClaudeAdapter("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "hi"}])
    assert cap["options"]["setting_sources"] == []


def test_delegation_id_injected_into_session_env(monkeypatch):
    """Finding 2: the bound delegation id reaches the session env as
    F3DASM_DELEGATION_ID so get_evaluator() resolves without a cd into D###."""
    from adda._src.backends.base import set_delegation_id
    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap))
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])

    set_delegation_id("D007")
    adapter.invoke([{"role": "user", "content": "hi"}])
    set_delegation_id(None)
    assert cap["options"]["env"].get("F3DASM_DELEGATION_ID") == "D007"

    # With no delegation bound, the key is absent (no stray injection).
    cap.clear()
    adapter.invoke([{"role": "user", "content": "hi"}])
    assert "F3DASM_DELEGATION_ID" not in cap["options"]["env"]


def test_auto_memory_disabled_in_built_options():
    """Regression (run 20260926T124841): setting_sources=[] does NOT stop the
    bundled CLI's auto-memory injection — only CLAUDE_CODE_DISABLE_AUTO_MEMORY
    does. Assert it on the ACTUAL ClaudeAgentOptions.env this adapter builds
    (not just _build_session_env's return value), so a second construction
    site bypassing _build_session_env would also be caught."""
    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap))
    ClaudeAdapter = _get_adapter()
    ClaudeAdapter("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "hi"}])
    assert cap["options"]["env"].get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_stream_event_types_captured_for_ping_measurement(tmp_path, monkeypatch):
    """Records each non-delta StreamEvent's type + inter-event gap, so a run
    reveals whether ping/lifecycle events arrive during silent phases (the
    data that settles whether 60s silence is a dead stream or slow prefill)."""
    import json
    from adda._src.backends.base import set_transcript_sink

    class _SE:
        def __init__(self, etype):
            self.event = {"type": etype}

    def _gen_factory(prompt, options):
        async def _g(prompt, options):
            yield _SE("message_start")
            yield _SE("ping")
            yield _SE("content_block_delta")  # delta, fast → not recorded
            yield _AssistantMessage([_TextBlock("hi")])
            yield _ResultMessage()
        return _g

    monkeypatch.setenv("F3DASM_DEBUG", "1")
    mod = _install_fake_sdk(query=_gen_factory(None, None))
    mod.StreamEvent = _SE
    ClaudeAdapter = _get_adapter()
    sink = tmp_path / "D001.jsonl"
    set_transcript_sink(str(sink))
    ClaudeAdapter("claude-3", "sys", None, []).invoke(
        [{"role": "user", "content": "hi"}])
    set_transcript_sink(None)

    recs = [json.loads(x) for x in sink.read_text().strip().splitlines()]
    evts = [r["evt"] for r in recs if r["type"] == "stream_evt"]
    assert "message_start" in evts and "ping" in evts
    assert all("gap_s" in r for r in recs if r["type"] == "stream_evt")


def test_no_transcript_when_debug_off(tmp_path, monkeypatch):
    from adda._src.backends.base import set_transcript_sink
    monkeypatch.delenv("F3DASM_DEBUG", raising=False)
    _install_fake_sdk(query=make_async_gen_with_messages("x"))
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    sink = tmp_path / "D001.jsonl"
    set_transcript_sink(str(sink))
    adapter.invoke([{"role": "user", "content": "hi"}])
    set_transcript_sink(None)
    assert not sink.exists()


def test_multiple_text_blocks_concatenated():
    """Multiple TextBlocks in one AssistantMessage → concatenated."""
    _install_fake_sdk(query=make_async_gen_with_messages("foo", "bar", "baz"))
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    result = adapter.invoke([{"role": "user", "content": "hi"}])
    assert result == "foobarbaz"


def test_non_text_blocks_are_ignored():
    """Non-TextBlock content blocks are skipped; only TextBlock.text is collected."""

    class _ToolUseBlock:
        """Simulates a tool_use content block — should be ignored."""
        name = "Bash"
        input = {"command": "ls"}

    async def _gen_mixed(prompt, options):
        # AssistantMessage with mixed block types
        yield _AssistantMessage([
            _TextBlock("kept"),
            _ToolUseBlock(),
            _TextBlock(" also kept"),
        ])
        yield _ResultMessage()

    _install_fake_sdk(query=_gen_mixed)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    result = adapter.invoke([{"role": "user", "content": "hi"}])
    assert result == "kept also kept"


# ---------------------------------------------------------------------------
# Blindspot 2: last_usage populated from ResultMessage
# ---------------------------------------------------------------------------


def test_last_usage_populated_from_result_message():
    """last_usage is populated with usage fields from ResultMessage after invoke."""

    class _ResultMessageWithUsage:
        usage = {"input_tokens": 50, "output_tokens": 20}
        total_cost_usd = 0.002

    async def _gen_with_usage(prompt, options):
        yield _AssistantMessage([_TextBlock("response text")])
        yield _ResultMessageWithUsage()

    _install_fake_sdk(
        query=_gen_with_usage,
        ResultMessage=_ResultMessageWithUsage,
    )
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    adapter.invoke([{"role": "user", "content": "hi"}])

    assert adapter.last_usage["input_tokens"] == 50
    assert adapter.last_usage["output_tokens"] == 20
    assert adapter.last_usage["total_cost_usd"] == 0.002


def test_last_usage_empty_when_no_result_message():
    """last_usage is empty dict when no ResultMessage is yielded."""

    async def _gen_no_result(prompt, options):
        yield _AssistantMessage([_TextBlock("response text")])
        # No ResultMessage yielded — generator just ends

    _install_fake_sdk(query=_gen_no_result)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    adapter.invoke([{"role": "user", "content": "hi"}])

    # last_usage should be empty (or all zeroes / None) — not a crash
    assert adapter.last_usage == {} or not any(
        v for v in adapter.last_usage.values() if v
    )


def test_last_usage_recovered_from_assistant_message_when_route_watcher_breaks_early():
    """route_watcher (set by Node on every run-closing Done() call,
    strategizer.py:200) breaks the stream on the AssistantMessage that
    triggered it, before the SDK's own ResultMessage/total_cost_usd ever
    arrives. Regression: this previously fell through to last_usage={},
    silently recording zero tokens/cost for every run-closing strategizer
    turn. The AssistantMessage we broke on already carries its own usage
    dict; total_cost_usd stays None (a session-level rollup a lone
    AssistantMessage doesn't carry), matching openai_compatible.py's
    existing "unknown, not zero" convention.
    """
    class _AssistantMessageWithUsage(_AssistantMessage):
        def __init__(self, blocks, usage):
            super().__init__(blocks)
            self.usage = usage

    async def _gen_route_watcher_breaks(prompt, options):
        yield _AssistantMessageWithUsage(
            [_TextBlock("Run complete.")],
            {"input_tokens": 1200, "output_tokens": 340},
        )
        # A real stream would still be producing more messages/the eventual
        # ResultMessage here — route_watcher breaks before any of that.

    _install_fake_sdk(query=_gen_route_watcher_breaks)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    adapter.route_watcher = lambda: True  # fires on the very first message
    adapter.invoke([{"role": "user", "content": "hi"}])

    assert adapter.last_usage["input_tokens"] == 1200
    assert adapter.last_usage["output_tokens"] == 340
    assert adapter.last_usage["total_cost_usd"] is None


def test_route_watcher_break_sums_every_api_call_of_the_stream():
    """The strategizer's whole run is ONE long stream of many API calls.
    Breaking on the closing AssistantMessage used to record only that last
    message's snapshot usage (output_tokens ~1 for the entire run, run
    20260928T141126). The per-call usage the stream reports (message_start
    input/cache, message_delta FINAL output) must be summed instead --
    AssistantMessage.usage is a pre-completion snapshot and is not used when
    stream events exist."""
    class _Ev(_StreamEvent):
        def __init__(self, event):
            self.event = event

    class _Asst(_AssistantMessage):
        def __init__(self, blocks, mid, usage):
            super().__init__(blocks)
            self.message_id = mid
            self.usage = usage

    def _start(mid, inp, cr, cc):
        return _Ev({"type": "message_start", "message": {
            "id": mid, "usage": {
                "input_tokens": inp, "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc, "output_tokens": 1}}})

    def _delta(out):
        return _Ev({"type": "message_delta", "usage": {"output_tokens": out}})

    stale = {"input_tokens": 0, "output_tokens": 1}

    async def _gen(prompt, options):
        yield _start("m1", 10, 1000, 200)
        yield _Asst([_TextBlock("a")], "m1", stale)
        yield _delta(300)
        yield _start("m2", 5, 1500, 0)
        yield _Asst([_TextBlock("Run complete.")], "m2", stale)
        # route_watcher breaks here, before m2's message_delta / any result

    _install_fake_sdk(query=_gen, StreamEvent=_Ev)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    seen = []
    adapter.route_watcher = lambda: len(seen.append(1) or seen) >= 2
    adapter.invoke([{"role": "user", "content": "hi"}])

    u = adapter.last_usage
    assert u["input_tokens"] == 15
    assert u["cache_read_input_tokens"] == 2500
    assert u["cache_creation_input_tokens"] == 200
    assert u["output_tokens"] == 301  # m1 final 300 + m2's start snapshot 1
    assert u["total_cost_usd"] is None


# ---------------------------------------------------------------------------
# STREAM_ENDED_WITHOUT_RESULT (report 7, run 20260830T004106, Oscar): a
# stream that ends mid-tool with no ResultMessage used to return silently,
# with nothing recorded anywhere.
# ---------------------------------------------------------------------------

class _ToolUseBlockWithName:
    def __init__(self, name, input_):
        self.name = name
        self.input = input_


def _bind_delegation_diagnostics(tmp_path):
    """Bind the thread-locals _record_stream_diagnostic reads, so it writes
    to tmp_path/debug/diagnostics.jsonl; returns that path (unlinking any
    prior teardown state on exit is the caller's job via try/finally)."""
    import json as _json

    from adda._src.backends.base import set_delegation_id, set_run_config_path
    debug = tmp_path / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    rc = debug / "run_config.json"
    rc.write_text(_json.dumps({"store_dir": str(tmp_path), "study_dir": "x"}))
    set_delegation_id("D007")
    set_run_config_path(str(rc))
    return debug / "diagnostics.jsonl"


def _unbind_delegation_diagnostics():
    from adda._src.backends.base import set_delegation_id, set_run_config_path
    set_delegation_id(None)
    set_run_config_path(None)


def _read_diagnostics(path):
    import json as _json
    if not path.exists():
        return []
    return [_json.loads(ln) for ln in path.read_text().splitlines() if ln]


def test_stream_ended_without_result_is_recorded(tmp_path):
    """A stream that ends right after an AssistantMessage carrying only a
    tool call (no TextBlock, no ResultMessage) is exactly what a mid-tool
    CLI death looks like from ainvoke()'s side — confirmed to make
    _invoke_with_report_retry silently re-invoke with the original task
    (its near-empty text reads as malformed). Must now be recorded."""
    async def _gen_dies_mid_tool(prompt, options):
        yield _AssistantMessage([_ToolUseBlockWithName("Bash", {"command": "sleep 20"})])
        return

    _install_fake_sdk(query=_gen_dies_mid_tool, ToolUseBlock=_ToolUseBlockWithName)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])

    diag_path = _bind_delegation_diagnostics(tmp_path)
    try:
        result = adapter.invoke([{"role": "user", "content": "original task"}])
    finally:
        _unbind_delegation_diagnostics()

    assert result == ""  # near-empty text, exactly what looks malformed
    records = _read_diagnostics(diag_path)
    hits = [r for r in records if r.get("error_type") == "STREAM_ENDED_WITHOUT_RESULT"]
    assert len(hits) == 1
    assert hits[0]["last_tool_in_flight"] == "Bash"
    assert hits[0]["node"] == "D007"


def test_normal_completion_does_not_record_stream_diagnostic(tmp_path):
    _install_fake_sdk(query=make_async_gen_with_messages("all good"))
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])

    diag_path = _bind_delegation_diagnostics(tmp_path)
    try:
        adapter.invoke([{"role": "user", "content": "hi"}])
    finally:
        _unbind_delegation_diagnostics()

    hits = [r for r in _read_diagnostics(diag_path)
            if r.get("error_type") == "STREAM_ENDED_WITHOUT_RESULT"]
    assert hits == []


def test_route_watcher_break_does_not_record_stream_diagnostic(tmp_path):
    """A deliberate route_watcher break (e.g. Done() closing the run) is the
    NORMAL "no ResultMessage" case — must not be confused with an abnormal
    stream end."""
    class _AssistantMessageWithUsage(_AssistantMessage):
        def __init__(self, blocks, usage):
            super().__init__(blocks)
            self.usage = usage

    async def _gen_route_watcher_breaks(prompt, options):
        yield _AssistantMessageWithUsage(
            [_TextBlock("Run complete.")], {"input_tokens": 1, "output_tokens": 1})

    _install_fake_sdk(query=_gen_route_watcher_breaks)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    adapter.route_watcher = lambda: True

    diag_path = _bind_delegation_diagnostics(tmp_path)
    try:
        adapter.invoke([{"role": "user", "content": "hi"}])
    finally:
        _unbind_delegation_diagnostics()

    hits = [r for r in _read_diagnostics(diag_path)
            if r.get("error_type") == "STREAM_ENDED_WITHOUT_RESULT"]
    assert hits == []


# ---------------------------------------------------------------------------
# SystemMessage: used to be entirely invisible (a compact_boundary would
# leave no trace anywhere an analyst could see).
# ---------------------------------------------------------------------------

def test_compact_boundary_is_recorded_and_flagged_as_a_diagnostic(tmp_path, monkeypatch):
    """A real compact_boundary must reach BOTH the transcript (so an analyst
    reading it directly sees it) and diagnostics.jsonl (so they don't have
    to — CONTEXT_COMPACTED), unconditionally, not gated on debug mode."""
    from adda._src.backends.base import set_transcript_sink

    async def _gen_with_compaction(prompt, options):
        yield _SystemMessage("compact_boundary", {"trigger": "auto", "preTokens": 190000})
        yield _AssistantMessage([_TextBlock("continuing after compaction")])
        yield _ResultMessage()

    monkeypatch.setenv("F3DASM_DEBUG", "1")
    _install_fake_sdk(query=_gen_with_compaction)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    sink = tmp_path / "D001.jsonl"
    set_transcript_sink(str(sink))

    diag_path = _bind_delegation_diagnostics(tmp_path)
    try:
        adapter.invoke([{"role": "user", "content": "hi"}])
    finally:
        set_transcript_sink(None)
        _unbind_delegation_diagnostics()

    import json
    recs = [json.loads(x) for x in sink.read_text().strip().splitlines()]
    system_recs = [r for r in recs if r["type"] == "system"]
    assert len(system_recs) == 1
    assert system_recs[0]["subtype"] == "compact_boundary"
    assert system_recs[0]["data"]["trigger"] == "auto"

    hits = [r for r in _read_diagnostics(diag_path)
            if r.get("error_type") == "CONTEXT_COMPACTED"]
    assert len(hits) == 1
    assert hits[0]["compaction_data"]["preTokens"] == 190000


def test_thinking_tokens_system_message_is_noise_not_recorded(tmp_path, monkeypatch):
    """The overwhelming majority (measured: 47-103 per short session) of
    SystemMessages are subtype 'thinking_tokens' — a per-token heartbeat
    stream_evt already covers. Recording every one would be volume, not
    signal, and must not fire a diagnostic either."""
    from adda._src.backends.base import set_transcript_sink

    async def _gen_with_heartbeat(prompt, options):
        yield _SystemMessage("thinking_tokens", {"count": 12})
        yield _AssistantMessage([_TextBlock("hi")])
        yield _ResultMessage()

    monkeypatch.setenv("F3DASM_DEBUG", "1")
    _install_fake_sdk(query=_gen_with_heartbeat)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    sink = tmp_path / "D001.jsonl"
    set_transcript_sink(str(sink))

    diag_path = _bind_delegation_diagnostics(tmp_path)
    try:
        adapter.invoke([{"role": "user", "content": "hi"}])
    finally:
        set_transcript_sink(None)
        _unbind_delegation_diagnostics()

    import json
    recs = [json.loads(x) for x in sink.read_text().strip().splitlines()]
    assert not [r for r in recs if r["type"] == "system"]
    assert not [r for r in _read_diagnostics(diag_path)
                if r.get("error_type") == "CONTEXT_COMPACTED"]


def test_unknown_system_subtype_defaults_to_recorded(tmp_path, monkeypatch):
    """A subtype this file has never seen before (e.g. 'init', or anything
    added to the SDK later) must default to VISIBLE — the noise list is an
    explicit denylist, not an allowlist, so a new subtype is never silently
    dropped the way every SystemMessage used to be."""
    from adda._src.backends.base import set_transcript_sink

    async def _gen_with_init(prompt, options):
        yield _SystemMessage("init", {"model": "claude-haiku-4-5-20251001"})
        yield _AssistantMessage([_TextBlock("hi")])
        yield _ResultMessage()

    monkeypatch.setenv("F3DASM_DEBUG", "1")
    _install_fake_sdk(query=_gen_with_init)
    ClaudeAdapter = _get_adapter()
    adapter = ClaudeAdapter("claude-3", "sys", None, [])
    sink = tmp_path / "D001.jsonl"
    set_transcript_sink(str(sink))
    adapter.invoke([{"role": "user", "content": "hi"}])
    set_transcript_sink(None)

    import json
    recs = [json.loads(x) for x in sink.read_text().strip().splitlines()]
    system_recs = [r for r in recs if r["type"] == "system"]
    assert len(system_recs) == 1
    assert system_recs[0]["subtype"] == "init"
