"""Transcript events in one backend-neutral shape (spec 14 Phase 2).

The two backends write transcripts differently (Claude: ``assistant`` events
with a ``tools`` list and separate ``tool_result`` events; the OpenAI-compatible
one: LangChain class names). ``normalise_events`` turns either into the same
flat list, so a front end renders events and never parses a backend's format.
The HTML renderers in ``app.py`` share the helpers below.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..nodes.notices import split_notices

__all__ = ["normalise_events"]


def _result_text(content) -> str:
    """Flatten a tool result into the text a human would read.

    Results arrive as a list of content blocks ({"type": "text", "text":
    ...}), which the previous renderer json.dumps()ed — so the reader was
    shown a JSON envelope of the thing they wanted, and only after
    expanding it.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, indent=2)


_VERDICT_RE = re.compile(
    r"###\s*Verdict\b[\s:>*_`\"'\-]*(PASS|REVISE|REJECT)\b", re.IGNORECASE)


def _result_event_kind(text: str) -> str:
    """A result that is itself a critic verdict or a review approval."""
    m = _VERDICT_RE.search(text or "")
    if m:
        return f"verdict-{m.group(1).lower()}"
    if (text or "").lstrip().startswith("Approved."):
        return "review"
    return ""


_ASSISTANT_TYPES = {"assistant", "aimessage", "aimessagechunk"}
_RESULT_TYPES = {"toolmessage", "functionmessage"}
# The agent's inbox side of the conversation. Both backends write it: the
# Claude backend as "user", the OpenAI-compatible one as the LangChain class
# name. adda's own in-band injections arrive on this role too, marked.
_HUMAN_TYPES = {"user", "human", "humanmessage"}


def _tool_input(tool: dict) -> dict:
    """A tool call's arguments, under whichever key the backend used."""
    for key in ("input", "args", "arguments"):
        val = tool.get(key)
        if isinstance(val, dict):
            return val
    return {}


def _compaction_facts(event: dict) -> dict | None:
    """Normalise both backends' compaction record to one shape, or ``None``.

    Claude: ``{"type":"system","subtype":"compact_boundary","data":{...}}``,
    where the SDK's own metadata names the token counts (``preTokens`` /
    ``postTokens``, sometimes under ``compact_metadata``, in either case
    convention). Local: ``{"type":"ContextCompaction","policy":...,
    "trim":{tokens_before,...},"summary":...}``.
    """
    etype = str(event.get("type") or "").lower()
    if etype == "contextcompaction":
        trim = event.get("trim") or {}
        return {
            "policy": event.get("policy") or "",
            "before": trim.get("tokens_before"),
            "after": trim.get("tokens_after"),
            "dropped": trim.get("dropped"),
            "detail": event.get("text") or "",
            "summary": event.get("summary") or "",
        }
    if etype == "system" and event.get("subtype") == "compact_boundary":
        data = event.get("data") or {}
        meta = data.get("compact_metadata") or data.get("compactMetadata") or {}

        def pick(*keys):
            for src in (data, meta):
                for k in keys:
                    if src.get(k) is not None:
                        return src[k]
            return None

        return {
            "policy": pick("trigger") or "sdk",
            "before": pick("preTokens", "pre_tokens"),
            "after": pick("postTokens", "post_tokens"),
            "dropped": None,
            "detail": "",
            "summary": pick("summary") or "",
        }
    return None


def _name_of(tool: dict) -> str:
    return tool.get("name", "tool")


def _normalise_one(
    e: dict, names: list[str], consumed: int, call_index: int,
    resolved_calls: int | None,
) -> list[dict[str, Any]]:
    """Normalised events for one raw event; ``names`` is the whole transcript's
    call queue, so a ``tool_result`` can say which tool it answers."""
    ts = e.get("ts")
    etype = str(e.get("type") or "").lower()
    out: list[dict[str, Any]] = []

    if etype == "tool_result" or etype in _RESULT_TYPES:
        if etype == "tool_result":
            items = [(r.get("content", ""), bool(r.get("is_error")),
                      names[consumed + i] if consumed + i < len(names) else "tool")
                     for i, r in enumerate(e.get("results") or [])]
        else:
            items = [(e.get("text") or "", False, e.get("name") or "tool")]
        for content, flagged, name in items:
            notices, text = split_notices(_result_text(content))
            ok = not (flagged or text.lstrip().startswith("ERROR"))
            ev: dict[str, Any] = {
                "ts": ts, "kind": "tool_result", "role": "tool", "text": text,
                "result": {"ok": ok, "text": text, "name": name,
                           "event": "" if not ok else _result_event_kind(text)}}
            if notices:
                ev["notices"] = notices
            out.append(ev)
        return out

    if etype in _HUMAN_TYPES:
        notices, body = split_notices(e.get("text") or "")
        if notices or body.strip():
            ev = {"ts": ts, "kind": "user", "role": "user", "text": body}
            if notices:
                ev["notices"] = notices
            out.append(ev)
        return out

    facts = _compaction_facts(e)
    if facts is not None:
        return [{"ts": ts, "kind": "system", "role": "system",
                 "text": facts["summary"], "compaction": facts}]

    if etype in _ASSISTANT_TYPES:
        thinking = "".join(e.get("thinking") or [])
        if thinking.strip():
            out.append({"ts": ts, "kind": "thinking", "role": "assistant",
                        "text": thinking})
        if (e.get("text") or "").strip():
            out.append({"ts": ts, "kind": "assistant", "role": "assistant",
                        "text": e["text"]})
        for offset, tool in enumerate(e.get("tools") or []):
            out.append({
                "ts": ts, "kind": "tool_use", "role": "assistant", "text": "",
                "tool": {"name": _name_of(tool), "input": _tool_input(tool)},
                "pending": (resolved_calls is not None
                            and call_index + offset >= resolved_calls)})
    return out


def normalise_events(
    raw: list[dict], after: int = 0, limit: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """Normalised events from ``raw[after:]`` and the cursor to resume from.

    The cursor counts RAW events (most raw records are streaming partials that
    normalise to nothing, so counting output would drift behind the file).
    ``limit`` caps normalised events; a raw event is never split across pages,
    so a page may exceed it by the events of its last raw record. The cursor
    is always an int: a live transcript is polled from ``next_cursor`` and
    ``total`` says whether the page drained it.

    Tool names and pending calls are resolved against the WHOLE list, as the
    HTML renderer does, so they are right wherever ``after`` falls.
    """
    names = [
        _name_of(t) for e in raw
        if str(e.get("type") or "").lower() in _ASSISTANT_TYPES
        for t in (e.get("tools") or [])]
    langchain = any(
        str(e.get("type") or "").lower() in _RESULT_TYPES
        or str(e.get("type") or "").lower() in {"aimessage", "aimessagechunk"}
        for e in raw)
    resolved = None if langchain else sum(
        len(e.get("results") or []) for e in raw if e.get("type") == "tool_result")
    consumed = sum(len(e.get("results") or []) for e in raw[:after]
                   if e.get("type") == "tool_result")
    call_index = sum(
        len(e.get("tools") or []) for e in raw[:after]
        if str(e.get("type") or "").lower() in _ASSISTANT_TYPES)
    events: list[dict[str, Any]] = []
    cursor = after
    for e in raw[after:]:
        if len(events) >= limit:
            break
        events += _normalise_one(e, names, consumed, call_index, resolved)
        if e.get("type") == "tool_result":
            consumed += len(e.get("results") or [])
        if str(e.get("type") or "").lower() in _ASSISTANT_TYPES:
            call_index += len(e.get("tools") or [])
        cursor += 1
    return events, cursor
