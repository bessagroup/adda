"""Anthropic Messages API in front of an OpenAI-compatible server.

Claude Code speaks the Anthropic Messages API (``POST /v1/messages``). A
vLLM server speaks the OpenAI chat API. vLLM's own ``/v1/messages`` route
exists but rejects the ``system`` role that Claude Code puts inside
``messages`` (HTTP 400, observed on vLLM 0.19.1 and reported upstream as
vllm-project/vllm#44000). This proxy translates each request to
``/v1/chat/completions`` and each reply back, streaming included, so a
node on the ``claude`` backend with ``base_url`` pointing here runs real
Claude Code on any OpenAI-compatible model.

Start it next to the model server::

    python -m adda._src.backends.anthropic_proxy \\
        --upstream http://localhost:8000/v1 --port 8001

and give the node ``backend: claude``, ``model: <served model>``,
``base_url: http://127.0.0.1:8001``.

Translation rules:

* ``system`` plus every ``system``-role message become ONE leading system
  message (chat templates such as Qwen's accept a system message only first).
* ``tool_use`` blocks become ``tool_calls``; ``tool_result`` blocks become
  ``tool`` messages placed before the rest of the same user turn.
* ``thinking`` blocks are dropped; upstream reasoning text is not returned.
* Whatever model Claude Code asks for (it also asks for a small "haiku" model
  for side tasks), the upstream model is ``--model``, else the first model the
  upstream lists.
* Output tokens come from the upstream ``usage``; nothing is estimated there.
  ``/v1/messages/count_tokens`` is a character-count estimate that only
  Claude Code's own context display reads.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import requests

__all__ = ["serve", "to_openai_request", "to_anthropic_message",
           "StreamTranslator"]

_STOP = {"tool_calls": "tool_use", "length": "max_tokens"}


# -- request: Anthropic -> OpenAI --------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or []
                     if isinstance(b, dict) and b.get("type") == "text")


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _image_url(block: dict) -> str:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
    return src.get("url", "")


def _user_messages(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    tool_msgs: list[dict] = []
    parts: list[dict] = []
    for b in content or []:
        kind = b.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif kind == "image":
            parts.append({"type": "image_url",
                          "image_url": {"url": _image_url(b)}})
        elif kind == "tool_result":
            tool_msgs.append({"role": "tool",
                              "tool_call_id": b.get("tool_use_id", ""),
                              "content": _result_text(b)})
    out = list(tool_msgs)
    if parts:
        if all(p["type"] == "text" for p in parts):
            out.append({"role": "user",
                        "content": "\n".join(p["text"] for p in parts)})
        else:
            out.append({"role": "user", "content": parts})
    return out


def _assistant_message(content: Any) -> dict:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    texts: list[str] = []
    calls: list[dict] = []
    for b in content or []:
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            calls.append({"id": b.get("id", ""), "type": "function",
                          "function": {"name": b.get("name", ""),
                                       "arguments": json.dumps(
                                           b.get("input") or {})}})
    msg: dict = {"role": "assistant",
                 "content": "\n".join(texts) if texts else
                 (None if calls else "")}
    if calls:
        msg["tool_calls"] = calls
    return msg


def _tool_choice(choice: Any) -> Any:
    kind = (choice or {}).get("type")
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "function": {"name": choice.get("name")}}
    return "auto"


def to_openai_request(body: dict, model: str) -> dict:
    """The ``/v1/chat/completions`` body for one Anthropic messages body."""
    system = [_text_of(body.get("system"))]
    messages: list[dict] = []
    for m in body.get("messages") or []:
        role = m.get("role")
        if role == "system":
            system.append(_text_of(m.get("content")))
        elif role == "assistant":
            messages.append(_assistant_message(m.get("content")))
        else:
            messages.extend(_user_messages(m.get("content")))
    merged = "\n\n".join(s for s in system if s)
    if merged:
        messages.insert(0, {"role": "system", "content": merged})
    req: dict = {"model": model, "messages": messages,
                 "stream": bool(body.get("stream"))}
    for src, dst in (("max_tokens", "max_tokens"), ("temperature", "temperature"),
                     ("top_p", "top_p"), ("top_k", "top_k"),
                     ("stop_sequences", "stop")):
        if body.get(src) is not None:
            req[dst] = body[src]
    tools = [{"type": "function",
              "function": {"name": t["name"],
                           "description": t.get("description", ""),
                           "parameters": t.get("input_schema")
                           or {"type": "object", "properties": {}}}}
             for t in body.get("tools") or []
             if t.get("name") and t.get("input_schema") is not None]
    if tools:
        req["tools"] = tools
        req["tool_choice"] = _tool_choice(body.get("tool_choice"))
    if req["stream"]:
        req["stream_options"] = {"include_usage": True}
    return req


# -- reply: OpenAI -> Anthropic ----------------------------------------------

def _input_of(arguments: str) -> Any:
    try:
        parsed = json.loads(arguments or "{}")
    except ValueError:
        return {"_raw_arguments": arguments}
    return parsed if isinstance(parsed, dict) else {"_raw_arguments": arguments}


def _usage(u: dict | None) -> dict:
    u = u or {}
    return {"input_tokens": int(u.get("prompt_tokens") or 0),
            "output_tokens": int(u.get("completion_tokens") or 0)}


def to_anthropic_message(resp: dict, model: str) -> dict:
    """The Anthropic message for one non-streaming chat completion."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: list[dict] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        blocks.append({"type": "tool_use", "id": call.get("id")
                       or "toolu_" + uuid.uuid4().hex[:24],
                       "name": fn.get("name", ""),
                       "input": _input_of(fn.get("arguments", ""))})
    stop = _STOP.get(choice.get("finish_reason"), "end_turn")
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message",
            "role": "assistant", "model": model, "content": blocks,
            "stop_reason": stop, "stop_sequence": None,
            "usage": _usage(resp.get("usage"))}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class StreamTranslator:
    """Chat-completion chunks in, Anthropic SSE events out."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._started = False
        self._index = -1
        self._open: str | None = None
        self._calls: dict[int, int] = {}
        self._finish: str | None = None
        self._usage: dict = {}
        self._tools_seen = False

    def _start(self) -> list[str]:
        if self._started:
            return []
        self._started = True
        return [_sse("message_start", {"type": "message_start", "message": {
            "id": "msg_" + uuid.uuid4().hex[:24], "type": "message",
            "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})]

    def _close(self) -> list[str]:
        if self._open is None:
            return []
        self._open = None
        return [_sse("content_block_stop", {"type": "content_block_stop",
                                            "index": self._index})]

    def _begin(self, kind: str, block: dict) -> list[str]:
        out = self._close()
        self._index += 1
        self._open = kind
        out.append(_sse("content_block_start", {
            "type": "content_block_start", "index": self._index,
            "content_block": block}))
        return out

    def feed(self, chunk: dict) -> list[str]:
        out = self._start()
        if chunk.get("usage"):
            self._usage = _usage(chunk["usage"])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                if self._open != "text":
                    out += self._begin("text", {"type": "text", "text": ""})
                out.append(_sse("content_block_delta", {
                    "type": "content_block_delta", "index": self._index,
                    "delta": {"type": "text_delta", "text": delta["content"]}}))
            for call in delta.get("tool_calls") or []:
                pos = call.get("index", 0)
                fn = call.get("function") or {}
                if pos not in self._calls:
                    self._tools_seen = True
                    out += self._begin("tool", {
                        "type": "tool_use", "id": call.get("id")
                        or "toolu_" + uuid.uuid4().hex[:24],
                        "name": fn.get("name", ""), "input": {}})
                    self._calls[pos] = self._index
                if fn.get("arguments"):
                    out.append(_sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": self._calls[pos],
                        "delta": {"type": "input_json_delta",
                                  "partial_json": fn["arguments"]}}))
            if choice.get("finish_reason"):
                self._finish = choice["finish_reason"]
        return out

    def finish(self) -> list[str]:
        out = self._start()
        if self._index < 0:
            out += self._begin("text", {"type": "text", "text": ""})
        out += self._close()
        stop = "tool_use" if self._tools_seen else _STOP.get(
            self._finish, "end_turn")
        out.append(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": self._usage or {"input_tokens": 0, "output_tokens": 0}}))
        out.append(_sse("message_stop", {"type": "message_stop"}))
        return out


# -- the server --------------------------------------------------------------

def _error(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _estimate_tokens(body: dict) -> int:
    return max(1, len(json.dumps(
        [body.get("system"), body.get("messages"), body.get("tools")])) // 4)


class _Proxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, upstream: str, model: str | None,
                 api_key: str | None) -> None:
        super().__init__(addr, _Handler)
        self.upstream = upstream.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._lock = threading.Lock()

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def model(self) -> str:
        with self._lock:
            if self._model is None:
                r = requests.get(self.upstream + "/models",
                                 headers=self.headers(), timeout=10)
                r.raise_for_status()
                self._model = r.json()["data"][0]["id"]
            return self._model


class _Handler(BaseHTTPRequestHandler):
    server: _Proxy

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/v1/models":
            self._json(200, {"data": [{"type": "model", "id": self.server.model(),
                                       "display_name": self.server.model()}]})
        else:
            self._json(200 if path == "/" else 404,
                       {} if path == "/" else _error("not_found_error", path))

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json(400, _error("invalid_request_error", "bad JSON"))
        if path == "/v1/messages/count_tokens":
            return self._json(200, {"input_tokens": _estimate_tokens(body)})
        if path != "/v1/messages":
            return self._json(404, _error("not_found_error", path))
        try:
            model = self.server.model()
            req = to_openai_request(body, model)
            r = requests.post(self.server.upstream + "/chat/completions",
                              json=req, headers=self.server.headers(),
                              stream=req["stream"], timeout=(10, 900))
        except requests.RequestException as exc:
            return self._json(502, _error("api_error", f"upstream: {exc}"))
        if r.status_code >= 400:
            kind = ("invalid_request_error" if r.status_code < 500
                    else "api_error")
            return self._json(r.status_code, _error(kind, r.text[:2000]))
        if not req["stream"]:
            return self._json(200, to_anthropic_message(r.json(), model))
        self._stream(r, model)

    def _stream(self, r: requests.Response, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        tr = StreamTranslator(model)
        try:
            for event in _events(r, tr):
                self.wfile.write(event.encode())
                self.wfile.flush()
        except (requests.RequestException, BrokenPipeError,
                ConnectionResetError) as exc:
            try:
                self.wfile.write(_sse("error", _error(
                    "api_error", f"upstream stream broke: {exc}")).encode())
            except OSError:
                pass


def _events(r: requests.Response, tr: StreamTranslator) -> Iterator[str]:
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        yield from tr.feed(json.loads(data))
    yield from tr.finish()


def serve(upstream: str, model: str | None = None, host: str = "127.0.0.1",
          port: int = 0, api_key: str | None = None) -> _Proxy:
    """Start the proxy on a daemon thread and return the server; its address
    is ``server.server_address``. ``server.shutdown()`` stops it."""
    server = _Proxy((host, port), upstream, model, api_key)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--upstream", required=True,
                   help="OpenAI-compatible base URL, e.g. http://host:8000/v1")
    p.add_argument("--model", help="upstream model id (default: the first "
                   "model the upstream lists)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    a = p.parse_args(argv)
    server = serve(a.upstream, a.model, a.host, a.port,
                   os.environ.get("VLLM_API_KEY"))
    print(f"anthropic proxy on http://{a.host}:{server.server_address[1]} "
          f"-> {a.upstream}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
