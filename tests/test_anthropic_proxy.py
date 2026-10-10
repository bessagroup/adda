"""The Anthropic-to-OpenAI proxy against a stub vLLM server (no model, no
network beyond localhost): the system role, tool_use and tool_result, and
streaming."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from adda._src.backends import anthropic_proxy


class _Stub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _StubHandler)
        self.requests: list[dict] = []
        self.reply: dict | list | tuple = {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        raw = json.dumps({"data": [{"id": "Qwen/Qwen3-stub"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        reply = self.server.reply
        if isinstance(reply, tuple):  # (status, text)
            raw = reply[1].encode()
            self.send_response(reply[0])
        elif body.get("stream"):
            raw = "".join(f"data: {json.dumps(c)}\n\n" for c in reply
                          ).encode() + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            raw = json.dumps(reply).encode()
            self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def stub():
    s = _Stub()
    yield s
    s.shutdown()


@pytest.fixture()
def proxy(stub):
    p = anthropic_proxy.serve(stub.url)
    yield f"http://127.0.0.1:{p.server_address[1]}"
    p.shutdown()


def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = block.split("\n")
        out.append((ev.removeprefix("event: "),
                    json.loads(data.removeprefix("data: "))))
    return out


def _post(proxy, body, path="/v1/messages?beta=true", **kw):
    return requests.post(proxy + path, json=body, timeout=10, **kw)


def _completion(message, finish="stop", usage=(7, 3)):
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage[0],
                      "completion_tokens": usage[1]}}


def test_system_role_tool_use_and_tool_result_reach_the_server_in_chat_form(
        stub, proxy):
    stub.reply = _completion({"content": "done"})
    body = {
        "model": "claude-sonnet-4",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "You are a CLI."}],
        "messages": [
            {"role": "user", "content": "run ls"},
            {"role": "system", "content": [
                {"type": "text", "text": "reminder: be brief"}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "x"},
                {"type": "text", "text": "Running."},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                 "input": {"command": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "a.py"}]},
                {"type": "text", "text": "now summarise"}]},
        ],
        "tools": [{"name": "Bash", "description": "run a command",
                   "input_schema": {"type": "object", "properties": {
                       "command": {"type": "string"}}}}],
        "tool_choice": {"type": "auto"},
    }
    r = _post(proxy, body)
    assert r.status_code == 200
    sent = stub.requests[0]
    assert sent["model"] == "Qwen/Qwen3-stub"
    assert sent["max_tokens"] == 64 and sent["stream"] is False
    assert sent["messages"] == [
        {"role": "system", "content": "You are a CLI.\n\nreminder: be brief"},
        {"role": "user", "content": "run ls"},
        {"role": "assistant", "content": "Running.", "tool_calls": [
            {"id": "toolu_1", "type": "function", "function": {
                "name": "Bash", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "a.py"},
        {"role": "user", "content": "now summarise"},
    ]
    assert sent["tools"] == [{"type": "function", "function": {
        "name": "Bash", "description": "run a command",
        "parameters": body["tools"][0]["input_schema"]}}]
    assert sent["tool_choice"] == "auto"


def test_a_reply_with_text_becomes_an_end_turn_message_with_usage(stub, proxy):
    stub.reply = _completion({"content": "hello"})
    msg = _post(proxy, {"max_tokens": 8, "messages": [
        {"role": "user", "content": "hi"}]}).json()
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["content"] == [{"type": "text", "text": "hello"}]
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_a_reply_with_a_tool_call_becomes_a_tool_use_block(stub, proxy):
    stub.reply = _completion({"content": None, "tool_calls": [
        {"id": "call_9", "type": "function", "function": {
            "name": "Bash", "arguments": '{"command": "pwd"}'}}]},
        finish="tool_calls")
    msg = _post(proxy, {"max_tokens": 8, "messages": [
        {"role": "user", "content": "hi"}]}).json()
    assert msg["content"] == [{"type": "tool_use", "id": "call_9",
                               "name": "Bash", "input": {"command": "pwd"}}]
    assert msg["stop_reason"] == "tool_use"


def test_a_streamed_text_reply_follows_the_anthropic_event_order(stub, proxy):
    stub.reply = [
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}},
    ]
    r = _post(proxy, {"max_tokens": 8, "stream": True, "messages": [
        {"role": "user", "content": "hi"}]})
    assert stub.requests[0]["stream_options"] == {"include_usage": True}
    ev = _events(r.text)
    assert [e for e, _ in ev] == [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_delta", "content_block_stop", "message_delta",
        "message_stop"]
    assert "".join(d["delta"]["text"] for e, d in ev
                   if e == "content_block_delta") == "Hello"
    last = dict(ev)["message_delta"]
    assert last["delta"]["stop_reason"] == "end_turn"
    assert last["usage"]["output_tokens"] == 2


def test_streamed_tool_call_fragments_join_into_one_tool_use_block(
        stub, proxy):
    stub.reply = [
        {"choices": [{"delta": {"content": "Let me look."}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "Bash", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '{"command": '}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '"ls"}'}}]},
            "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 9}},
    ]
    ev = _events(_post(proxy, {"max_tokens": 8, "stream": True, "messages": [
        {"role": "user", "content": "hi"}]}).text)
    starts = [d["content_block"] for e, d in ev if e == "content_block_start"]
    assert [b["type"] for b in starts] == ["text", "tool_use"]
    assert starts[1]["id"] == "call_1" and starts[1]["name"] == "Bash"
    partial = "".join(d["delta"]["partial_json"] for e, d in ev
                      if e == "content_block_delta"
                      and d["delta"]["type"] == "input_json_delta")
    assert json.loads(partial) == {"command": "ls"}
    assert [d["index"] for e, d in ev if e == "content_block_stop"] == [0, 1]
    last = dict(ev)["message_delta"]
    assert last["delta"]["stop_reason"] == "tool_use"
    assert last["usage"]["output_tokens"] == 9


def test_an_upstream_error_comes_back_as_an_anthropic_error(stub, proxy):
    stub.reply = (400, "context length exceeded")
    r = _post(proxy, {"max_tokens": 8, "messages": [
        {"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert r.json() == {"type": "error", "error": {
        "type": "invalid_request_error", "message": "context length exceeded"}}


def test_count_tokens_and_the_model_list_are_answered_locally(stub, proxy):
    n = _post(proxy, {"messages": [{"role": "user", "content": "x" * 400}]},
              path="/v1/messages/count_tokens").json()["input_tokens"]
    assert n >= 100
    assert stub.requests == []
    models = requests.get(proxy + "/v1/models", timeout=10).json()
    assert models["data"][0]["id"] == "Qwen/Qwen3-stub"


def test_a_node_base_url_points_the_cli_at_the_proxy():
    from adda._src.backends.claude import _build_session_env
    env = _build_session_env(base_url="http://127.0.0.1:8001")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8001"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "adda-proxy"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "ANTHROPIC_BASE_URL" not in _build_session_env()


def test_a_claude_node_takes_only_its_own_base_url():
    from types import SimpleNamespace

    from adda._src.backends.claude import ClaudeAdapter
    from adda._src.runtime.agent_runtime import AgenticRun
    run = SimpleNamespace(_base_url="http://vllm:8000/v1",
                          _served_base_url="http://served:8000/v1",
                          _backend="claude")
    resolve = AgenticRun._resolve_base_url
    own = SimpleNamespace(base_url="http://127.0.0.1:8001", backend=None)
    none = SimpleNamespace(base_url=None, backend=None)
    assert resolve(run, "solo", own, ClaudeAdapter) == "http://127.0.0.1:8001"
    assert resolve(run, "solo", none, ClaudeAdapter) is None
