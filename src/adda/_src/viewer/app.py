"""Starlette app for the read-only live run viewer.

Requires the ``viewer`` extra (``pip install adda[viewer]``) — imported
lazily by every caller (``agent_runtime.py``'s ``serve_viewer()``,
``viewer/__main__.py``) so the core install never depends on Starlette.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import queue
import re
import secrets
import shlex
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..infra import operator_channel, stop_request
from ..nodes.notices import split_notices
from . import notebook_replay, readers, run_control
from .transcript_events import (  # noqa: F401 — re-exported for the renderers below
    _ASSISTANT_TYPES,
    _HUMAN_TYPES,
    _RESULT_TYPES,
    _compaction_facts,
    _result_event_kind,
    _result_text,
    _tool_input,
    normalise_events,
)

__all__ = ["create_app", "run_viewer"]

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def _run_dir(study_dir: Path, run_id: str) -> Path | None:
    run_dir = study_dir / "runs" / run_id
    if not (run_dir / "debug").is_dir():
        return None
    return run_dir


TOKEN_COOKIE = "adda_viewer_token"

AUDIT_LOG = "viewer_actions.jsonl"


def _audit(study_dir: Path, action: str, **fields) -> None:
    """One line per write that reaches beyond the viewer (spec 14 §3.0): what
    was done, to which run, when, and the exact thing written or run."""
    row = {"ts": time.time(), "action": action, **fields}
    try:
        with open(study_dir / AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _check_write(request, token: str) -> JSONResponse | None:
    """Refuse a write that did not come from a page this viewer served.

    Reads stay open on the bound interface; a write changes a live run, so
    it must carry (1) a JSON Content-Type, which a cross-origin "simple"
    form or text/plain POST cannot set without a CORS preflight the viewer
    never answers, (2) an Origin that is the viewer's own, and (3) the
    per-launch token, held in an HttpOnly SameSite=Strict cookie that only
    the printed session URL sets.
    """
    ctype = request.headers.get("content-type", "").split(";")[0].strip()
    if ctype.lower() != "application/json":
        return JSONResponse(
            {"error": "writes require Content-Type: application/json"},
            status_code=415)
    origin = request.headers.get("origin")
    if origin is not None:
        if urlsplit(origin).netloc != request.headers.get("host", ""):
            return JSONResponse(
                {"error": "cross-origin write refused"}, status_code=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse(
            {"error": "cross-origin write refused"}, status_code=403)
    given = request.cookies.get(TOKEN_COOKIE, "")
    if not hmac.compare_digest(given.encode(), token.encode()):
        return JSONResponse(
            {"error": "read-only session: open the session URL printed at "
                      "viewer launch to be allowed to write"},
            status_code=403)
    return None


def _not_found(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=404)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _display_tool_name(name: str) -> str:
    """Strip a leading ``mcp__<server>__`` prefix for on-screen display —
    the raw registered name (e.g. ``mcp__f3dasm_agent_tools__Delegate``) is
    what the model actually calls, kept in the ``title`` attribute, but
    showing it verbatim in every bubble is unreadable noise the reader has
    to mentally strip on every single line."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return name


# The argument that identifies WHAT a call did, per tool. Ported from the
# convention Claude Code, Codex and opencode all use: the headline is the
# call's most salient argument, shown inline, so a transcript reads without
# expanding anything. The previous rendering showed a literal "args" label
# and a collapsed "Bash result", which meant every single line had to be
# opened by hand to learn anything at all.
_HEADLINE_KEYS = (
    "command",       # Bash
    "file_path",     # Read / Write / Edit / NotebookEdit
    "path",
    "pattern",       # Grep / Glob
    "query",         # ConsultHandbook, store queries
    "intent",        # Delegate
    "question",      # FollowUp
    "statement",     # HypothesisPropose
    "hypothesis_id",
    "delegation_id",
    "url",
)

# Enough of a path to identify the file, without a 90-character absolute
# path pushing everything else off the line.
_PATH_KEYS = {"file_path", "path"}


def _tool_headline(inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in _HEADLINE_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            text = " ".join(val.split())
            if key in _PATH_KEYS:
                parts = text.split("/")
                text = "/".join(parts[-3:]) if len(parts) > 3 else text
            return text if len(text) <= 160 else text[:157] + "..."
    return ""


# Measured against a real run rather than guessed. Across the 78 tool
# results of one benchmark delegation the median result is 15 lines and the 90th percentile is 146, so a
# 3-line budget left only 19% of results readable without a click — a
# transcript of stubs. The share fully visible by budget: 3 -> 19%,
# 6 -> 40%, 8 -> 46%, 12 -> 49%, 16 -> 53%, 30 -> 54%. The knee is at 8 and
# the curve is flat past 16 (what remains are the 146+ line dumps, which
# belong behind a disclosure whatever the budget). 12 sits past the knee,
# keeps a long result from swamping the pane, and roughly halves the
# clicking.
_PREVIEW_LINES = 12


def _preview_block(text: str, css: str) -> str:
    """First lines inline, the REMAINDER behind a "+N lines" disclosure.

    Inline-with-expansion rather than collapsed-by-default: the point of a
    transcript is to be skimmable, and a reader should not have to open
    anything to see that a command printed three passing tests.

    The disclosure holds only the lines the preview did not show. It used
    to hold the entire text again, so expanding a result re-printed the
    lines already on screen — the reader had to find their place in a
    second copy — and the DOM carried the whole payload twice, which on
    this run's largest result (834 lines) is not a rounding error.
    """
    text = (text or "").rstrip()
    if not text:
        return f"<div class='{css} empty-result'>(no output)</div>"
    lines = text.splitlines()
    head = "\n".join(lines[:_PREVIEW_LINES])
    rest = lines[_PREVIEW_LINES:]
    html = f"<pre class='{css}'>{_esc(head)}</pre>"
    if rest:
        html += (
            f"<details class='more'><summary>+{len(rest)} "
            f"line{'s' if len(rest) != 1 else ''}</summary>"
            f"<pre class='{css}'>{_esc(chr(10).join(rest))}</pre></details>"
        )
    return html


# What adda is saying, told apart by the marker its injection site writes.
# Each kind gets its own colour AND a text tag, so colour is never the only
# channel. Science-monitor injections are the frequent, low-urgency ones, so
# they render collapsed to their rule name.
_NOTICE_KINDS = (
    ("[SCIENCE MONITOR", "monitor", "science monitor"),
    ("[VERDICT VALIDATOR", "validator", "verdict validator"),
    ("[OPERATOR NOTE", "operator", "operator"),
)


def _notice_kind(text: str) -> tuple[str, str]:
    head = (text or "").lstrip()
    for prefix, kind, tag in _NOTICE_KINDS:
        if head.startswith(prefix):
            return kind, tag
    return "adda", "adda"


def _notice_html(text: str) -> str:
    kind, tag = _notice_kind(text)
    if kind == "monitor":
        first = (text.strip().splitlines() or [""])[0]
        return (
            f"<details class='notice notice-{kind}'>"
            f"<summary><span class='notice-tag'>{tag}</span> "
            f"<span class='notice-first'>{_esc(first)}</span></summary>"
            f"<div class='notice-body'>{_preview_block(text, 'notice-pre')}"
            "</div></details>"
        )
    return (
        f"<div class='notice notice-{kind}'><span class='notice-tag'>{tag}"
        "</span>"
        f"<div class='notice-body'>{_preview_block(text, 'notice-pre')}</div>"
        "</div>"
    )


# Two backends write transcripts in two shapes. The Claude backend records
# type "assistant" with a `tools` list of {name, input}; the
# OpenAI-compatible one (used when a node points at a local Ollama/vLLM
# model) records the LangChain class name — AIMessage, ToolMessage,
# HumanMessage — with `tools` as LangChain tool_calls ({name, args}).
# Rendering only "assistant" meant a qwen3.8-backed agent's transcript
# displayed as completely empty even once it had finished.
# Delegation ids as the runtime mints them (D001, GATE164710) — matched
# rather than trusted, since one arrives from a request body and becomes a
# routing key on the run side.
_DELEGATION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

def _compaction_html(facts: dict) -> str:
    """An inline marker at the point the context was compacted."""
    bits = []
    if facts["before"] is not None and facts["after"] is not None:
        bits.append(f"{facts['before']} \u2192 {facts['after']} tokens")
    elif facts["before"] is not None:
        bits.append(f"{facts['before']} tokens before")
    if facts["dropped"] is not None:
        bits.append(f"{facts['dropped']} message(s) dropped")
    if facts["policy"]:
        bits.append(f"policy: {facts['policy']}")
    line = " \u00b7 ".join(bits)
    head = ("<span class='compaction-tag'>context compacted</span> "
            f"<span class='compaction-facts'>{_esc(line)}</span>")
    if facts["summary"].strip():
        return ("<details class='compaction'>"
                f"<summary>{head}</summary>"
                f"<div class='compaction-summary'>{_esc(facts['summary'])}</div>"
                "</details>")
    return f"<div class='compaction'>{head}</div>"


def _bubble_html(event: dict, call_index: int = 0,
                 resolved_calls: int | None = None) -> str:
    """Render one ``assistant`` event as a chat-turn HTML fragment.

    *call_index* is this event's first tool call's position in the
    transcript's whole in-order call queue, and *resolved_calls* how many
    of those calls already have a result on disk (``None`` when the
    transcript's shape does not allow the comparison). A call at or past
    that count has not come back yet, and is marked as still running —
    the one thing a reader watching a live run cannot otherwise tell,
    since a call awaiting its result and a call that returned nothing
    render identically.

    Thinking, then text, then each tool call as a subordinate child of the
    SAME turn — the nested trace-tree convention agent-trace viewers use,
    rather than a flat list of same-weight bubbles. A call's matching
    result is attached separately by ``_render_fragment`` via
    ``_tool_result_html``, because results arrive as their own later event
    on disk. ``stream_evt``/``partial`` are intentionally not rendered.
    """
    etype = str(event.get("type") or "").lower()
    if etype in _RESULT_TYPES:
        # LangChain reports a tool's OUTPUT as its own message; render it as
        # the result row it is, not as another speaking turn.
        return _tool_result_html(
            {"results": [{"content": event.get("text") or ""}]},
            [event.get("name") or "tool"],
        )
    if etype in _HUMAN_TYPES:
        # The agent's INBOX: the human's task, and everything adda injects
        # in-band on the same "user" role — budget warnings, the no-source
        # nudge, the milestone backlog, pushed notifications. Dropping these
        # meant a reader watching a run never saw the runtime speak to the
        # agent at all; the nudge simply was not in the transcript view, and
        # the agent's next turn appeared to react to nothing.
        notices, body = split_notices(event.get("text") or "")
        if not notices and not body.strip():
            return ""
        notices_html = "".join(_notice_html(n) for n in notices)
        body_html = (f"<div class='bubble-text'>{_esc(body)}</div>"
                     if body.strip() else "")
        return ("<div class='turn turn-human'>"
                "<div class='turn-body'>"
                f"{notices_html}{body_html}"
                "</div></div>")
    compaction = _compaction_facts(event)
    if compaction is not None:
        return _compaction_html(compaction)
    if etype not in _ASSISTANT_TYPES:
        return ""
    text = event.get("text") or ""
    thinking = "".join(event.get("thinking") or [])
    thinking_html = (
        "<details class='thinking'><summary>thinking</summary>"
        f"<div class='thinking-text'>{_esc(thinking)}</div></details>"
    ) if thinking.strip() else ""
    tools_html = ""
    for offset, tool in enumerate(event.get("tools") or []):
        raw_name = tool.get("name", "tool")
        pending = (
            resolved_calls is not None
            and (call_index + offset) >= resolved_calls
        )
        inp = _tool_input(tool)
        headline = _tool_headline(inp)
        # The full arguments stay available, but behind a disclosure — the
        # headline is what the reader needs on the line itself.
        # Nothing to reveal when the headline IS the whole input — a
        # Bash call's only argument is the command already on the line.
        only_headline = (
            len(inp) == 1 and headline
            and str(next(iter(inp.values()))).strip() == headline
        )
        args_html = (
            "<details class='more'><summary>arguments</summary>"
            f"<pre>{_esc(json.dumps(inp, indent=2))}</pre></details>"
        ) if inp and not only_headline else ""
        tools_html += (
            f"<div class='tool-call{' pending' if pending else ''}'"
            f"{' data-pending' if pending else ''}>"
            "<div class='tool-line'>"
            "<span class='call-glyph'></span>"
            f"<span class='tool-name' title='{_esc(raw_name)}'>"
            f"{_esc(_display_tool_name(raw_name))}</span>"
            f"<span class='tool-arg'>{_esc(headline)}</span>"
            "</div>"
            f"{args_html}"
            "</div>"
        )
    if not text and not thinking_html and not tools_html:
        return ""
    # An event carrying only tool calls is a continuation of the same
    # speaker, not a new one. Giving each its own avatar and divider — as
    # happens when a model emits one tool per event — turned a single
    # coherent turn into a column of identical badges.
    bare = not text.strip() and not thinking_html
    body = (
        f"{thinking_html}"
        + (f"<div class='bubble-text'>{_esc(text)}</div>" if text.strip() else "")
        + tools_html
    )
    if bare:
        return f"<div class='turn turn-cont'><div class='turn-body'>{body}</div></div>"
    return (
        "<div class='turn'>"
        "<span class='avatar avatar-assistant' title='assistant'>A</span>"
        f"<div class='turn-body'>{body}</div>"
        "</div>"
    )


def _tool_result_html(event: dict, names: list[str]) -> str:
    """Render a ``tool_result`` event as the continuation of the call it
    answers: a turnstile glyph, the tool's name, and the output's first
    lines inline.

    *names* is this event's ``results`` list positionally resolved against
    the transcript's full in-order tool-call queue by ``_render_fragment``
    (a tool_result event carries only ``tool_use_id``, never the name).
    The name is kept as the glyph's tooltip rather than a visible line:
    the row already sits under the named call it answers, so printing the
    name again made the output read as a second call rather than the tail
    of the first.

    Wrapped in the same ``turn-cont`` as a tool call. It used to be
    emitted as a bare top-level sibling, which put a result at the chat
    body's own left edge while its call sat 42px in — so every output
    rendered OUTDENTED from, and visually detached from, the command that
    produced it. Claude Code, Codex and opencode all nest the result
    under the call instead (``●`` then ``⎿``/``└``); this now does too,
    and that containment is what marks where one call ends and the next
    begins.
    """
    results = event.get("results") or []
    html = ""
    for i, r in enumerate(results):
        name = names[i] if i < len(names) else "tool"
        text = _result_text(r.get("content", ""))
        # An error is the one thing a reader must not have to expand to
        # see. Deliberately only the explicit flag and adda's own
        # ERROR_RETURN convention: scanning output for "error"/"failed"
        # was measured against this run and matched 3 of 78 results, all
        # three of them false — SBATCH scripts whose #SBATCH --output
        # lines carry the word — while catching no real failure at all.
        # A notice is adda speaking to the agent (a nudge, science-monitor
        # drift, a budget warning, an operator note), prepended to whatever
        # the tool actually returned. Lift it out and give it its own band:
        # rendered inside the result block it read as the tool's own output,
        # which is the wrong attribution for every one of them. Split on the
        # marker the injection sites write (nodes/notices.py) rather than on
        # a text heuristic — tools emit bracketed lines of their own.
        notices, text = split_notices(text)
        # ERROR_RETURN is a property of the TOOL's output, so test it after
        # the notice is out of the way. Testing the raw text meant any
        # result carrying a prefix — a budget warning, an operator note —
        # failed the startswith() and lost its error styling, which is
        # precisely the result a reader most needs to see flagged.
        is_error = bool(r.get("is_error")) or text.lstrip().startswith("ERROR")
        notices_html = "".join(_notice_html(n) for n in notices)
        ev = "" if is_error else _result_event_kind(text)
        body_html = (
            f"{_preview_block(text, 'result-pre')}" if text.strip() else ""
        )
        html += (
            "<div class='turn turn-cont turn-res'><div class='turn-body'>"
            f"{notices_html}"
            f"<div class='tool-result{' is-error' if is_error else ''}"
            f"{(' ev-' + ev) if ev else ''}'>"
            "<span class='result-glyph' "
            f"title='{_esc(_display_tool_name(name))}'>&#9151;</span>"
            "<div class='result-body'>"
            f"{body_html}"
            "</div></div></div></div>"
        )
    return html


def _render_fragment(events: list[dict], after: int) -> str:
    """Render ``events[after:]`` to HTML, resolving each ``tool_result``
    event's real tool name(s) by position against the FULL event list.

    A ``tool_result`` event only carries ``tool_use_id`` (confirmed against
    a real transcript — the preceding ``assistant`` event's ``tools`` list
    carries no id to match against). Claude's own conversation structure
    strictly alternates assistant-tool-call -> tool_result before the next
    assistant turn, so the Nth item across every tool_result event, in file
    order, is always the Nth tool call across every assistant event, in file
    order — recomputed over the WHOLE file (not just this slice) so the
    mapping is correct regardless of where ``after`` falls, then only the
    slice past ``after`` is actually rendered.
    """
    all_names = [
        tool.get("name", "tool")
        for e in events
        if str(e.get("type") or "").lower() in _ASSISTANT_TYPES
        for tool in (e.get("tools") or [])
    ]
    consumed = sum(
        len(e.get("results") or [])
        for e in events[:after] if e.get("type") == "tool_result"
    )
    # How many calls have a result on disk, so a call still waiting can be
    # marked as such. Only meaningful for the Claude shape, where results
    # are their own `tool_result` events in strict call order. The
    # OpenAI-compatible backend records a LangChain ToolMessage per
    # result, paired by name rather than position, so the comparison does
    # not hold there and the marking is switched off (None) instead of
    # declaring every one of its calls unfinished forever.
    langchain = any(
        str(e.get("type") or "").lower() in _RESULT_TYPES
        or str(e.get("type") or "").lower() in {"aimessage", "aimessagechunk"}
        for e in events
    )
    resolved_calls = None if langchain else sum(
        len(e.get("results") or [])
        for e in events if e.get("type") == "tool_result"
    )
    call_index = sum(
        len(e.get("tools") or [])
        for e in events[:after]
        if str(e.get("type") or "").lower() in _ASSISTANT_TYPES
    )
    html_parts = []
    for e in events[after:]:
        if e.get("type") == "tool_result":
            results = e.get("results") or []
            names = all_names[consumed:consumed + len(results)]
            consumed += len(results)
            html_parts.append(_tool_result_html(e, names))
        else:
            html_parts.append(_bubble_html(e, call_index, resolved_calls))
            if str(e.get("type") or "").lower() in _ASSISTANT_TYPES:
                call_index += len(e.get("tools") or [])
    return "".join(html_parts)


def _esc(s: str) -> str:
    """HTML-escape, INCLUDING the single quote.

    _bubble_html emits tool names into single-quoted attributes
    (title='...'), so leaving ' unescaped let a tool name of
    ``x' onmouseover='alert(1)`` close the attribute and add a live event
    handler to the served page. Tool names come from the model's own
    output on disk, so any prompt-injected or malformed name became script
    execution in the operator's browser.
    """
    return (
        (s or "").replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")
    )


def create_app(
    study_dir: Path | str, graph=None, token: str | None = None,
) -> Starlette:
    """*graph*: pass the study's real, already-in-memory ``Graph`` when one
    is available (``AgenticRun.serve_viewer()`` always has it — the same
    object ``self._graph_spec`` holds, no need to reconstruct anything).
    Left as ``None`` for the standalone CLI path (a separate process from
    the run itself, with no in-memory Graph to hand over), which falls back
    to ``readers.load_graph_for_study()`` instead.

    *token*: the per-launch write token (random when omitted, readable as
    ``app.state.token``). Writes are refused without it; see ``_check_write``.
    """
    study_dir = Path(study_dir)
    token = token or secrets.token_urlsafe(24)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # The Graph is the same for every run of one study — resolved once at
    # app construction, not per-request.
    if graph is None:
        graph = readers.load_graph_for_study(study_dir)

    async def list_runs(request):
        return JSONResponse(readers.read_runs(study_dir))

    # graph_spec_json() calls each agent's build_closure_tools(), which is
    # NOT free: LiteratureReviewAgent's constructs a LiteratureCorpus, whose
    # __init__ mkdir()s runs/lit_reviewer_notes/ and a papers/ subdir inside
    # the study. Running that per request meant a read-only viewer wrote to
    # the study on every /graph hit — and then listed the directory it had
    # just created back as a "shared workspace". So it is still cached — but
    # PER RUN, not once per study: the spec is NOT identical across a study's
    # runs, because which model a node ran on is a property of the run (see
    # readers._read_node_models), and a study's build_graph() may compose
    # differently from one run to the next. Caching one spec for the whole
    # study served the first run's models for every later run.
    _spec_cache: dict = {}

    async def get_graph(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        if graph is None:
            return JSONResponse({"nodes": [], "edges": [], "entry": None})
        if run_id not in _spec_cache:
            _spec_cache[run_id] = readers.graph_spec_json(
                graph, study_dir, run_dir)
        return JSONResponse(_spec_cache[run_id])

    async def get_delegations(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_delegations(run_dir))

    async def get_ledger(request):
        """The run's epistemic state: hypotheses and milestones.

        Polled rather than streamed. Both are whole-file rewrites, not
        append-only logs, so there is nothing for the JSONL tailer behind
        /stream to tail — and a ledger changes at most a handful of times
        per run, which does not warrant a second live channel.
        """
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse({
            "hypotheses": readers.read_hypotheses(run_dir),
            "milestones": readers.read_milestones(run_dir),
        })

    async def get_notebook(request):
        run_id = request.path_params["run_id"]
        if _run_dir(study_dir, run_id) is None:
            return _not_found(f"no such run {run_id!r}")
        nb = readers.read_notebook(study_dir, run_id)
        if nb is None:
            return JSONResponse({"cells": [], "missing": True})
        return JSONResponse(nb)

    async def get_vitals(request):
        """The run's real cost and wall clock — see readers.read_vitals for
        why these cannot come from the delegation log."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_vitals(run_dir))

    async def get_artifacts(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_artifacts(run_dir, study_dir))

    async def get_artifact(request):
        run_id = request.path_params["run_id"]
        if _run_dir(study_dir, run_id) is None:
            return _not_found(f"no such run {run_id!r}")
        rel = request.query_params.get("path", "")
        text = readers.read_artifact_text(study_dir, rel)
        if text is None:
            return _not_found("not a readable artifact inside this study")
        return JSONResponse({"path": rel, "text": text})

    async def get_operator(request):
        """Pending questions for the human, and a heartbeat saying one is here.

        Reading this endpoint IS the heartbeat: the viewer polls it while a
        run is open, and a run only waits for an answer when that heartbeat
        is fresh (operator_channel.is_watched). So an unattended run never
        stalls on a question nobody can see.
        """
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        operator_channel.touch_watch(run_dir)
        return JSONResponse({
            "questions": operator_channel.pending_questions(run_dir),
        })

    async def post_answer(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed body
            return JSONResponse({"error": "malformed body"}, status_code=400)
        qid, text = body.get("id", ""), body.get("answer", "")
        if not operator_channel.answer_question(run_dir, qid, text):
            # Already answered, already given up on, or never asked — all of
            # which mean this answer must NOT be presented as accepted.
            return JSONResponse(
                {"error": "question is not awaiting an answer"}, status_code=409)
        return JSONResponse({"ok": True})

    async def post_note(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed body
            return JSONResponse({"error": "malformed body"}, status_code=400)
        # An optional delegation id addresses the note at work already in
        # flight; without one it goes to the entry node on its next turn.
        # Validated as an id rather than trusted: it arrives from a request
        # body and is used as a routing key.
        to_node = str(body.get("delegation_id") or "").strip()
        if to_node and not _DELEGATION_ID_RE.match(to_node):
            return JSONResponse(
                {"error": "bad delegation id"}, status_code=400)
        if not operator_channel.queue_note(
                run_dir, body.get("text", ""), to_node=to_node):
            return JSONResponse({"error": "empty note"}, status_code=400)
        return JSONResponse({"ok": True})

    async def post_stop(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        if readers.read_run_status(run_dir) is not None:
            return JSONResponse(
                {"error": "run already closed"}, status_code=409)
        pending = stop_request.read_stop_request(run_dir)
        if pending is not None:
            return JSONResponse(
                {"error": "a stop is already requested", "stop_request": pending},
                status_code=409)
        reason = "operator pressed Stop in the viewer"
        if not stop_request.write_stop_request(
                run_dir, by="viewer", reason=reason):
            return JSONResponse(
                {"error": "could not write the stop request"}, status_code=500)
        written = stop_request.read_stop_request(run_dir)
        _audit(study_dir, "stop", run_id=run_id,
               wrote=str(run_dir / "debug" / "stop_request.json"),
               stop_request=written)
        return JSONResponse({"ok": True, "stop_request": written})

    async def get_preflight(request):
        checks = run_control.preflight(study_dir)
        try:
            lc = run_control.launcher_config(study_dir)
            launcher = None if lc is None else {
                "command": shlex.join(lc["command"]),
                "can_stop": "stop_command" in lc}
        except run_control.LauncherError as exc:
            launcher = {"error": str(exc)}
        return JSONResponse({
            "launcher": launcher,
            "checks": checks,
            "can_start": not any(c["ok"] is False for c in checks),
            "launched": run_control.launched(study_dir),
        })

    async def post_start(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        try:
            entry = await asyncio.to_thread(run_control.start_run, study_dir)
        except run_control.StartRefused as exc:
            return JSONResponse(
                {"error": str(exc), "checks": exc.checks}, status_code=409)
        _audit(study_dir, "start", pid=entry["pid"],
               command=entry["cmdline"], log=entry["log"])
        return JSONResponse({"ok": True, **entry})

    async def post_launch(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        audit = lambda action, **f: _audit(study_dir, action, **f)  # noqa: E731
        try:
            entry = await asyncio.to_thread(
                run_control.start_via_launcher, study_dir, audit=audit)
        except run_control.StartRefused as exc:
            return JSONResponse(
                {"error": str(exc), "checks": exc.checks}, status_code=409)
        except run_control.LauncherError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, **entry})

    async def post_launch_stop(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        audit = lambda action, **f: _audit(study_dir, action, **f)  # noqa: E731
        try:
            out = await asyncio.to_thread(
                run_control.stop_via_launcher, study_dir, audit=audit)
        except run_control.LauncherError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        if out is None:
            return JSONResponse(
                {"error": "this viewer has recorded no launch that is still "
                          "to be stopped"}, status_code=404)
        return JSONResponse({"ok": out["returncode"] == 0, **out})

    async def post_reexecute(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        run_id = request.path_params["run_id"]
        if _run_dir(study_dir, run_id) is None:
            return _not_found(f"no such run {run_id!r}")
        try:
            out = await asyncio.to_thread(
                notebook_replay.replay_notebook, study_dir, run_id,
                audit=lambda action, **f: _audit(study_dir, action, **f))
        except notebook_replay.ReplayError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(out)

    async def post_kill(request):
        refused = _check_write(request, token)
        if refused is not None:
            return refused
        out = run_control.kill_run(
            study_dir, audit=lambda action, **f: _audit(study_dir, action, **f))
        if out is None:
            return JSONResponse(
                {"error": "this viewer has not started a run that is still "
                          "alive"}, status_code=404)
        return JSONResponse({"ok": True, **out})

    async def get_log(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        q = request.query_params
        try:
            after = int(q["after"]) if "after" in q else None
            limit = int(q.get("limit", readers._LOG_DEFAULT_BYTES))
        except ValueError:
            return JSONResponse({"error": "after/limit must be integers"},
                                status_code=400)
        out = readers.read_log_tail(
            run_dir, q.get("name", "run"), after=after, limit=limit)
        if out is None:
            return _not_found("unknown log name")
        return JSONResponse(out)

    async def get_oracle(request):
        """Oracle ledger rows, newest first, paged by offset (``after``,
        ``limit`` default 400 / max 5000; ``namespace=`` empty = the canonical
        store). ``next_cursor`` is an int offset, or ``null`` once drained
        (a ledger page is finite). ``total_evals`` always sums every store."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        q = request.query_params
        try:
            after = max(0, int(q.get("after", 0)))
            limit = min(5000, max(1, int(q.get("limit", 400))))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "after and limit must be integers"}, status_code=400)
        # ``?namespace=`` (present, empty) pages the canonical store.
        kw = {}
        if "namespace" in q:
            kw["namespace"] = q.get("namespace") or None
        return JSONResponse(
            readers.read_oracle(run_dir, after=after, limit=limit, **kw))

    async def get_trajectory(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_trajectory(run_dir))

    async def get_funnel(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        raw = request.query_params.get("stages")
        stages = [c for c in raw.split(",") if c] if raw is not None else None
        return JSONResponse(readers.read_funnel(run_dir, stages))

    async def get_figure_of_merit(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_figure_of_merit(run_dir))

    async def get_monitor(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_monitor_injections(run_dir))

    async def get_problem_statement(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        text = readers.read_problem_statement(run_dir)
        if text is None:
            return _not_found("PROBLEM_STATEMENT_snapshot.md not found for this run")
        return JSONResponse({"text": text})

    async def get_diagnostics(request):
        """Diagnostics rows paged by line cursor (``after``, ``limit`` default
        200 / max 2000, optional ``kind`` = the row's error_type or tool).
        ``next_cursor`` is always an int: a live run keeps appending, so
        compare it with ``total`` to know whether you have caught up.
        ``counts`` is the whole file's kind vocabulary, ignoring the filter."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        try:
            after = max(0, int(request.query_params.get("after", "0")))
            limit = min(2000, max(1, int(request.query_params.get("limit", "200"))))
        except ValueError:
            return JSONResponse(
                {"error": "after and limit must be integers"}, status_code=400)
        return JSONResponse(readers.read_diagnostics(
            run_dir, after=after, limit=limit,
            kind=request.query_params.get("kind") or None))

    async def get_notes(request):
        """The strategizer's ``strategizer_notes/*.md`` (strategy files, study
        summary), oldest first, each with its mtime."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_strategizer_notes(run_dir))

    async def get_evidence(request):
        """``evidence_index.md`` and every delegation's workspace commit with
        its changed files (read-only git; ``repo: false`` when the run has no
        workspace repository)."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_evidence(run_dir))

    async def get_evidence_stat(request):
        """``git show --stat`` of one delegation's commit and ``git diff
        --stat`` against the commit before it. 404 for an unknown delegation
        or one with no recorded sha; 502 if git cannot answer."""
        from . import safe_git
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        did = request.path_params["delegation_id"]
        try:
            stat = readers.read_evidence_stat(run_dir, did)
        except safe_git.GitViewError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        if stat is None:
            return _not_found(f"no committed delegation {did!r}")
        return JSONResponse(stat)

    async def get_study_history(request):
        """The study's git history (commits touching the study directory,
        each with changed files and +/- counts). ``limit`` (default 200,
        max 2000). 502 if git cannot answer."""
        from . import safe_git
        try:
            limit = max(1, min(int(request.query_params.get("limit", 200)),
                               2000))
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"},
                                status_code=400)
        try:
            return JSONResponse(readers.read_study_history(study_dir, limit))
        except safe_git.GitViewError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def get_literature(request):
        """The study-scoped paper corpus (each paper flagged ``in_run`` by its
        ``added_at`` against this run's window), this run's literature-tool
        errors and its RETRIEVAL_DEGRADED rows."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_literature(run_dir))

    async def get_retrospectives(request):
        """Every retrospective split into CONSISTENCY / DECISION / FRICTION /
        BLOCKED / TIME, plus ``missing``: the runtime's RETROSPECTIVES_MISSING
        rows, so a node that never answered is visible rather than absent."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_retrospectives(run_dir))

    async def get_critic_reviews(request):
        """The persisted critic reviews (``debug/critic_reviews/call_NNN.md``)
        with the gate's own verdict parse, ``Numbers`` block and ``Findings``."""
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        return JSONResponse(readers.read_critic_reviews(run_dir))

    async def get_node_transcripts(request):
        run_id = request.path_params["run_id"]
        name = request.path_params["name"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        is_entry = graph is not None and name == graph.entry
        return JSONResponse(
            readers.list_node_transcripts(run_dir, name, is_entry=is_entry))

    async def get_transcript(request):
        """Raw transcript records, paged (``after``, ``limit`` default 200 /
        max 1000). ``next_cursor`` is ALWAYS an int (the index of the next
        record), never null: a live transcript keeps growing, so a client
        polls from ``next_cursor`` and compares it with ``total``."""
        run_id = request.path_params["run_id"]
        key = request.path_params["key"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        try:
            after = max(0, int(request.query_params.get("after", 0)))
            limit = min(1000, max(1, int(request.query_params.get("limit", 200))))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "after and limit must be integers"}, status_code=400)
        events = readers.read_transcript(run_dir, key)
        if events is None:
            return _not_found(
                "transcripts not recorded for this run (debug flag was off)")
        if not events:
            return _not_found(f"no such transcript {key!r}")
        page = events[after:after + limit]
        return JSONResponse({"events": page, "next_cursor": after + len(page),
                             "total": len(events)})

    async def get_transcript_events(request):
        """The transcript in one backend-neutral event shape (see
        ``transcript_events.normalise_events``). ``after`` is a RAW record
        index and ``limit`` caps emitted events; a raw record is never split
        across pages. ``next_cursor`` is ALWAYS an int, never null (live
        transcripts never drain); compare it with ``total``."""
        run_id = request.path_params["run_id"]
        key = request.path_params["key"]
        try:
            after = max(0, int(request.query_params.get("after", 0)))
            limit = min(1000, max(1, int(request.query_params.get("limit", 200))))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "after and limit must be integers"}, status_code=400)
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        raw = readers.read_transcript(run_dir, key)
        if raw is None:
            return _not_found(
                "transcripts not recorded for this run (debug flag was off)")
        if not raw:
            return _not_found(f"no such transcript {key!r}")
        events, cursor = normalise_events(raw, after, limit)
        return JSONResponse(
            {"events": events, "next_cursor": cursor, "total": len(raw)})

    async def get_transcript_fragment(request):
        run_id = request.path_params["run_id"]
        key = request.path_params["key"]
        try:
            after = max(0, int(request.query_params.get("after", 0)))
        except (TypeError, ValueError):
            # A non-numeric cursor is a client bug, not a server error; a
            # negative one silently re-rendered the tail of the transcript.
            after = 0
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")
        events = readers.read_transcript(run_dir, key)
        if events is None:
            return HTMLResponse(
                "<p class='empty'>Transcripts not recorded for this run "
                "(debug flag was off).</p>", status_code=404)
        if not events:
            # The FULL list (before slicing by `after`) being empty means
            # this key has no transcript at all — e.g. a Done()-gate-check
            # delegation_log entry (confirmed for real: "GATE190739",
            # status GATE:PASS, has no transcripts/GATE190739.jsonl at all,
            # since the gate check is recorded as a bookkeeping row, not a
            # real sub-delegation with its own captured conversation) — NOT
            # "an open, still-running transcript with nothing new since the
            # last poll" (there, `events` itself is non-empty; only the
            # `events[after:]` SLICE is). Without this check the endpoint
            # silently returned 200 with an empty body for a key that will
            # NEVER have content, and the panel just went blank with no
            # explanation at all.
            return HTMLResponse(
                f"<p class='empty'>No transcript recorded for {key!r} — "
                "likely a bookkeeping record (e.g. a gate-check verdict) "
                "rather than a captured conversation.</p>",
                status_code=404)
        html = _render_fragment(events, after)
        # `after`/the response cursor MUST count raw events, not rendered
        # bubbles — most events (stream_evt/partial/result) render to no
        # bubble at all (confirmed for real: a genuine transcript had 527
        # raw events, only 47 of them assistant/tool_result). A cursor
        # counting bubbles instead falls behind the raw list on every call,
        # so events already shown keep re-matching `events[after:]` and get
        # duplicated on every poll, without the underlying file ever
        # changing. X-Event-Count is the actual raw count consumed this
        # call — the client's next `after` value, not a bubble count.
        return HTMLResponse(
            html, headers={"X-Event-Count": str(len(events))})

    async def stream(request):
        run_id = request.path_params["run_id"]
        run_dir = _run_dir(study_dir, run_id)
        if run_dir is None:
            return _not_found(f"no such run {run_id!r}")

        async def event_gen():
            for row in readers.read_delegations(run_dir):
                yield _sse("delegation", row)
            for row in readers.read_diagnostics_tail(run_dir):
                yield _sse("diagnostic", row)

            # run_status.json is written ONCE, at the very end (GATED/
            # UNGATED/FAILED/crashed) -- distinct, global "how did the
            # WHOLE run turn out" signal, separate from any per-node
            # delegation status (a node's own dot only ever reflects its
            # most recent incoming delegation, which the entry node never
            # has one of at all). Absence means "still running" -- there
            # is no separate "RUNNING" state written anywhere; it's the
            # client's own default until this event ever arrives.
            run_status_seen = False
            initial_status = readers.read_run_status(run_dir)
            if initial_status is not None:
                yield _sse("run_status", initial_status)
                run_status_seen = True

            q: queue.Queue = queue.Queue()
            # Set when this connection ends, so the tailer threads below stop
            # instead of polling the disk forever into a queue with no
            # consumer.
            done = threading.Event()

            def _tail_delegations():
                for _ in readers.tail_jsonl(
                    run_dir / "debug" / "delegation_log.jsonl",
                    should_stop=done.is_set,
                ):
                    q.put(("delegation_touched", None))

            def _tail_diagnostics():
                for row in readers.tail_jsonl(
                    run_dir / "debug" / "diagnostics.jsonl",
                    should_stop=done.is_set,
                ):
                    q.put(("diagnostic", row))

            threading.Thread(target=_tail_delegations, daemon=True).start()
            threading.Thread(target=_tail_diagnostics, daemon=True).start()

            # A queued delegation starts by a PATCH row that changes
            # session_started_at and leaves status RUNNING, so the status
            # alone cannot tell the page it has started.
            def _seen(r):
                return (r["status"], r.get("session_started_at"))

            last_status = {
                r["id"]: _seen(r) for r in readers.read_delegations(run_dir)
            }
            # try/finally, not a bare loop: the generator is also closed when
            # the client vanishes mid-yield, which raises GeneratorExit here
            # rather than returning through the disconnect check below.
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    if not run_status_seen:
                        # Written once, so a plain existence poll here (not
                        # the append-aware tail_jsonl, which is for growing
                        # files) is enough — cheap, and stops once found.
                        current = readers.read_run_status(run_dir)
                        if current is not None:
                            yield _sse("run_status", current)
                            run_status_seen = True
                    try:
                        kind, payload = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.2)
                        continue
                    if kind == "delegation_touched":
                        for row in readers.read_delegations(run_dir):
                            if last_status.get(row["id"]) != _seen(row):
                                last_status[row["id"]] = _seen(row)
                                yield _sse("delegation", row)
                    elif kind == "diagnostic":
                        yield _sse("diagnostic", payload)
            finally:
                done.set()

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    async def graph_page(request):
        run_id = request.path_params["run_id"]
        if _run_dir(study_dir, run_id) is None:
            return _not_found(f"no such run {run_id!r}")
        return templates.TemplateResponse(
            request, "graph.html",
            {"run_id": run_id, "study_name": Path(study_dir).name})

    async def session(request):
        """Set the write cookie when the launch token is presented, then go
        on to the requested page (a same-site path only)."""
        nxt = request.query_params.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        resp = RedirectResponse(nxt, status_code=303)
        given = request.query_params.get("token", "")
        if hmac.compare_digest(given.encode(), token.encode()):
            resp.set_cookie(
                TOKEN_COOKIE, token, httponly=True, samesite="strict",
                path="/")
        return resp

    async def session_state(request):
        given = request.cookies.get(TOKEN_COOKIE, "")
        return JSONResponse({"can_write": hmac.compare_digest(
            given.encode(), token.encode())})

    async def index(request):
        runs = readers.read_runs(study_dir)
        if not runs:
            return _not_found("no runs in this study yet")
        return RedirectResponse(f"/runs/{runs[0]['run_id']}", status_code=303)

    async def ui_page(request):
        return FileResponse(_STATIC_DIR / "ui" / "index.html")

    routes = [
        Route("/", index),
        Route("/ui", ui_page),
        Route("/session", session),
        Route("/api/session", session_state),
        Route("/api/runs", list_runs),
        Route("/api/runs/{run_id}/graph", get_graph),
        Route("/api/runs/{run_id}/delegations", get_delegations),
        Route("/api/runs/{run_id}/ledger", get_ledger),
        Route("/api/runs/{run_id}/operator", get_operator),
        Route("/api/runs/{run_id}/answer", post_answer, methods=["POST"]),
        Route("/api/runs/{run_id}/note", post_note, methods=["POST"]),
        Route("/api/study/preflight", get_preflight),
        Route("/api/study/history", get_study_history),
        Route("/api/study/start", post_start, methods=["POST"]),
        Route("/api/study/launch", post_launch, methods=["POST"]),
        Route("/api/study/launch/stop", post_launch_stop, methods=["POST"]),
        Route("/api/study/kill", post_kill, methods=["POST"]),
        Route("/api/runs/{run_id}/log", get_log),
        Route("/api/runs/{run_id}/stop", post_stop, methods=["POST"]),
        Route("/api/runs/{run_id}/vitals", get_vitals),
        Route("/api/runs/{run_id}/oracle", get_oracle),
        Route("/api/runs/{run_id}/trajectory", get_trajectory),
        Route("/api/runs/{run_id}/funnel", get_funnel),
        Route("/api/runs/{run_id}/figure_of_merit", get_figure_of_merit),
        Route("/api/runs/{run_id}/monitor", get_monitor),
        Route("/api/runs/{run_id}/artifacts", get_artifacts),
        Route("/api/runs/{run_id}/artifact", get_artifact),
        Route("/api/runs/{run_id}/notebook", get_notebook),
        Route("/api/runs/{run_id}/notebook/reexecute", post_reexecute,
              methods=["POST"]),
        Route("/api/runs/{run_id}/problem_statement", get_problem_statement),
        Route("/api/runs/{run_id}/diagnostics", get_diagnostics),
        Route("/api/runs/{run_id}/notes", get_notes),
        Route("/api/runs/{run_id}/literature", get_literature),
        Route("/api/runs/{run_id}/evidence", get_evidence),
        Route("/api/runs/{run_id}/evidence/{delegation_id}", get_evidence_stat),
        Route("/api/runs/{run_id}/retrospectives", get_retrospectives),
        Route("/api/runs/{run_id}/critic_reviews", get_critic_reviews),
        Route("/api/runs/{run_id}/node/{name}/transcripts", get_node_transcripts),
        Route(
            "/api/runs/{run_id}/transcript/{key:path}/events",
            get_transcript_events,
        ),
        Route(
            "/api/runs/{run_id}/transcript/{key:path}/fragment",
            get_transcript_fragment,
        ),
        Route("/api/runs/{run_id}/transcript/{key:path}", get_transcript),
        Route("/api/runs/{run_id}/stream", stream),
        Route("/runs/{run_id}", graph_page),
        Mount("/static", StaticFiles(directory=_STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes)
    app.state.token = token
    return app


def _is_loopback(host: str) -> bool:
    import ipaddress

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_viewer(
    study_dir: Path | str, host: str = "127.0.0.1", port: int = 8765,
    graph=None, allow_network: bool = False,
) -> None:
    """Launch the viewer web server (blocking). Requires ``uvicorn``.

    The viewer can start and kill runs, so binding anywhere but the loopback
    interface must be asked for with ``allow_network=True``.
    """
    import uvicorn

    if not _is_loopback(host):
        if not allow_network:
            raise ValueError(
                f"refusing to bind {host!r}: the viewer can start and stop "
                "runs, so it listens on the loopback interface only unless "
                "you pass --allow-network")
        print(
            f"adda viewer WARNING: bound to {host!r}. Anyone on that network "
            "who gets the session URL can START and KILL runs on this "
            "machine.", flush=True)
    app = create_app(study_dir, graph=graph)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(
        "adda viewer: reads are open on this interface; writes (notes, "
        "answers) need this session URL once per browser:\n"
        f"  http://{shown}:{port}/session?token={app.state.token}",
        flush=True)
    uvicorn.run(app, host=host, port=port)
