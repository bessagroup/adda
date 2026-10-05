"""Claude-SDK-backed adapter for the f3dasm LangGraph agentic runtime."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect as _inspect
import re
import threading
import time
from pathlib import Path
from typing import Any

from .base import record_stream_diagnostic

__all__ = ["ClaudeAdapter"]

# SystemMessage subtypes that are pure per-token/streaming noise at
# transcript granularity — everything else (init, status, compact_boundary,
# any subtype not seen yet) is recorded verbatim rather than guessed at, so
# a new subtype defaults to VISIBLE, not silently dropped like every
# SystemMessage used to be. Measured via a raw claude_agent_sdk.query()
# session (bypassing this file's own filtering) with debug logging: a
# single short (~5-8s) Haiku turn produced 50-119 SystemMessages, the
# overwhelming majority (47-103 of them) subtype "thinking_tokens" —
# stream_evt already covers liveness, so recording every one of these too
# would just be volume, not signal.
_SYSTEM_MESSAGE_NOISE_SUBTYPES = frozenset({"thinking_tokens"})

# Models that accept ``thinking={"type": "adaptive"}`` (Anthropic docs, "Thinking
# support ... by model", verified 2026-09-29): Opus/Sonnet 4.6+ and the 5.x
# families. Haiku 4.5, Opus/Sonnet 4.5 and earlier are extended-thinking only
# and reject "adaptive" with a 400, so they get no thinking option at all
# (their default display is already "summarized").
_ADAPTIVE_THINKING_MODEL = re.compile(
    r"^claude-(?:(?:opus|sonnet)-(?:4-[6-9]|[5-9])|(?:fable|mythos)-[5-9])")

_THINKING_DISPLAYS = ("summarized", "omitted")


def _thinking_options(model: str | None) -> dict:
    """``ClaudeAgentOptions`` kwargs for the ``thinking_display`` knob.

    Newer models default to "omitted": every ThinkingBlock arrives with an
    empty ``thinking`` field and only a signature, so the transcript and
    viewer show bare tool calls. "summarized" returns readable text. Billing
    is identical either way (the full thinking tokens are charged; docs,
    "Controlling thinking display"). Models without adaptive thinking are left
    untouched."""
    from ..runtime.settings import get_str
    display = get_str("thinking_display", "summarized")
    if display not in _THINKING_DISPLAYS:
        raise ValueError(
            f"runtime.thinking_display must be one of {_THINKING_DISPLAYS}, "
            f"got {display!r}")
    if not _ADAPTIVE_THINKING_MODEL.match(model or ""):
        return {}
    return {"thinking": {"type": "adaptive", "display": display}}


# The in-process MCP server name every f3dasm closure is registered under. The
# Claude SDK exposes each closure to the model ONLY by its qualified name
# ``mcp__<server>__<tool>`` (that is also what allowed_tools carries), so the
# prompt's <tools> catalog must advertise the SAME qualified names — a bare-name
# catalog tells the model to call a tool that does not exist ("No such tool
# available"). One constant + one helper feed BOTH the registration and the
# catalog so they can never drift apart.
_CLOSURE_MCP_SERVER = "f3dasm_agent_tools"


def _qualify_closure_names(closure_tools: dict) -> dict:
    """Re-key a bare closure dict by the MCP-qualified names the SDK exposes.

    Pure; returns ``{}`` unchanged on an empty dict. Used for both the
    allowed_tools list and the <tools> catalog so the model is shown exactly
    the names it can call.
    """
    return {
        f"mcp__{_CLOSURE_MCP_SERVER}__{name}": fn
        for name, fn in closure_tools.items()
    }


_SDK_AVAILABLE: bool | None = None  # None = not yet checked


def _require_sdk() -> None:
    global _SDK_AVAILABLE
    if _SDK_AVAILABLE is None:
        try:
            import claude_agent_sdk  # noqa: F401
            _SDK_AVAILABLE = True
        except ImportError:
            _SDK_AVAILABLE = False
    if not _SDK_AVAILABLE:
        raise ImportError(
            "claude-agent-sdk is required for the Claude backend. "
            "Install it with: uv add claude-agent-sdk"
        )


_TYPE_MAP: dict = {
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    dict: {"type": "object"},
    list: {"type": "array"},
    str: {"type": "string"},
}


def _infer_schema_from_callable(fn: Any) -> dict:
    """Build a JSON schema dict from a Python callable's type annotations."""
    sig = _inspect.signature(fn)
    props: dict = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        # Skip leading-underscore params: by convention these are closure
        # constants bound via the default-arg idiom (e.g. `_ws=delegation_ws`,
        # `_did=delegation_id`), NOT model inputs. Exposing them let a model
        # pass e.g. `_ws` as a string → `str / path` TypeError in a write tool.
        if pname.startswith("_"):
            continue
        ann = param.annotation
        if ann is _inspect.Parameter.empty:
            json_type = {"type": "string"}
        else:
            json_type = _TYPE_MAP.get(ann, {"type": "string"})
        props[pname] = json_type
        if param.default is _inspect.Parameter.empty:
            required.append(pname)
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _run_async_safe(coro: Any) -> Any:
    """Run *coro* safely whether or not an event loop is already running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Nested-loop case (e.g. the critic invoked from inside the
        # strategizer's running ainvoke): we hop to a fresh thread, so
        # propagate the thread-local context (transcript sink + delegation id)
        # into it — otherwise the critic's stream/env would silently lose them.
        from .base import (
            get_delegation_id,
            get_transcript_sink,
            set_delegation_id,
            set_transcript_sink,
        )
        _sink, _did = get_transcript_sink(), get_delegation_id()

        def _runner() -> Any:
            set_transcript_sink(_sink)
            set_delegation_id(_did)
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_runner).result()
    else:
        return asyncio.run(coro)


def _format_messages_as_prompt(messages: list[dict]) -> str:
    """Convert LangChain-style message dicts to a plain conversation string."""
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        if role in ("human", "user"):
            prefix = "Human"
        elif role in ("ai", "assistant"):
            prefix = "Assistant"
        else:
            continue  # skip system messages — passed via system_prompt
        parts.append(f"{prefix}: {content}")
    return "\n\n".join(parts)


_STREAM_DONE = object()


_USAGE_KEYS = ("input_tokens", "output_tokens",
               "cache_read_input_tokens", "cache_creation_input_tokens")


def _track_message_usage(
    event: Any, by_id: dict[str, dict], cur: list[str | None]
) -> None:
    """Fold one raw stream event into a per-API-call usage map.

    ``message_start`` carries the call's input/cache tokens; ``message_delta``
    carries its FINAL output tokens. ``AssistantMessage.usage`` is a snapshot
    taken before generation finishes (output_tokens 5 vs a true 140), so it is
    only a fallback for calls whose stream events never arrived.
    """
    if not isinstance(event, dict):
        return
    et = event.get("type")
    if et == "message_start":
        m = event.get("message") or {}
        cur[0] = m.get("id")
        if cur[0]:
            by_id[cur[0]] = dict(m.get("usage") or {})
    elif et == "message_delta" and cur[0]:
        by_id.setdefault(cur[0], {}).update(event.get("usage") or {})


def _usage_fields(event: Any) -> dict:
    """Usage worth persisting on a transcript ``stream_evt`` row, so a run's
    spend can be recomputed from the transcript alone (see
    ``adda._src.infra.usage_recompute``)."""
    if not isinstance(event, dict):
        return {}
    et = event.get("type")
    if et == "message_start":
        m = event.get("message") or {}
        u = m.get("usage") or {}
        return {"message_id": m.get("id"),
                "usage": {k: u.get(k) for k in _USAGE_KEYS if k in u}}
    if et == "message_delta":
        u = event.get("usage") or {}
        return {"usage": {k: u.get(k) for k in _USAGE_KEYS if k in u}}
    return {}


def _cache_split(usage: dict) -> dict:
    """Flat cache-write TTL split from the SDK's nested ``cache_creation``;
    empty when the usage carries none (never a guessed zero split)."""
    cc = usage.get("cache_creation")
    if not isinstance(cc, dict):
        return {}
    return {"cache_creation_1h_tokens": cc.get("ephemeral_1h_input_tokens") or 0,
            "cache_creation_5m_tokens": cc.get("ephemeral_5m_input_tokens") or 0}


def _sum_message_usage(usages: Any) -> dict:
    total = dict.fromkeys(_USAGE_KEYS, 0)
    split: dict[str, int] = {}
    for u in usages:
        for k in _USAGE_KEYS:
            total[k] += (u.get(k) or 0)
        for k, v in _cache_split(u).items():
            split[k] = split.get(k, 0) + v
    return {**total, **split}


def _combine_attempt_usage(attempts: list[dict] | None) -> dict:
    """Sum the usage of every attempt of one ``invoke`` -- failed and retried
    sessions were billed too. ``total_cost_usd`` is the sum of the known
    values, None if any attempt lacks one ("unknown, not zero")."""
    attempts = [a for a in (attempts or []) if a]
    if not attempts:
        return {}
    if len(attempts) == 1:
        return attempts[0]
    out: dict = {}
    for a in attempts:
        for k, v in a.items():
            if k == "total_cost_usd":
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
            elif isinstance(v, dict) and isinstance(out.get(k, {}), dict):
                d = out.setdefault(k, {})
                for kk, vv in v.items():
                    if isinstance(vv, (int, float)) and not isinstance(vv, bool):
                        d[kk] = d.get(kk, 0) + vv
            else:
                out[k] = v
    costs = [a.get("total_cost_usd") for a in attempts]
    out["total_cost_usd"] = (
        None if any(c is None for c in costs) else sum(costs))
    return out


async def _anext_or_done(ait: Any) -> Any:
    """Return the next item, or the _STREAM_DONE sentinel when exhausted.

    Catches StopAsyncIteration INSIDE the coroutine so it never escapes into an
    asyncio Task (which would surface as a RuntimeError under wait_for).
    """
    try:
        return await ait.__anext__()
    except StopAsyncIteration:
        return _STREAM_DONE


async def _stream_with_idle_timeout(
    gen: Any,
    idle_timeout: float,
    *,
    tool_timeout: float = 0.0,
    classify: Any = None,
):
    """Yield messages from an SDK async stream, raising TimeoutError if the
    stream goes idle while AWAITING MODEL GENERATION for longer than
    ``idle_timeout`` seconds.

    This catches a *stalled* API response — an ESTABLISHED-but-silent
    connection that streams nothing and never errors — and turns it into a
    transient TimeoutError that ``retry_on_transient`` (wrapping ``invoke``)
    retries with backoff. It is an IDLE timeout (reset on every message), NOT a
    total cap, so a legitimately long call that keeps streaming tokens is never
    cut; only a true stall is.

    The window is scoped to *model generation*. While a TOOL is executing — a
    worker running a multi-minute Bash compute job emits no stream messages —
    the tight window stands down, because that silence is the tool working, not
    the API stalling. Penalising it false-fires and (via the retry) re-runs the
    whole, non-idempotent worker. ``classify(msg)`` reports the phase:
    ``True`` a tool is now executing (suspend the tight window), ``False``
    generation/tool-result (re-arm it), ``None`` leave the phase unchanged.
    While a tool is pending the next message is awaited with ``tool_timeout``
    (``<= 0`` means no cap — a runaway tool is the delegation watchdog's job,
    not this stream timeout's). With ``classify=None`` the tight window applies
    always (backward-compatible).
    """
    ait = gen.__aiter__()
    tool_pending = False
    while True:
        if tool_pending:
            _wait = tool_timeout if tool_timeout and tool_timeout > 0 else None
        else:
            _wait = idle_timeout
        try:
            msg = await asyncio.wait_for(_anext_or_done(ait), timeout=_wait)
        except asyncio.TimeoutError as exc:
            # Only generation silence reaches here; while a tool is pending
            # _wait is None (uncapped) so this never trips on tool execution.
            raise TimeoutError(
                f"Anthropic stream stalled: no model output for "
                f"{idle_timeout:.0f}s (transient; will retry)"
            ) from exc
        if msg is _STREAM_DONE:
            return
        if classify is not None:
            phase = classify(msg)
            if phase is True:
                tool_pending = True
            elif phase is False:
                tool_pending = False
        yield msg


def _build_session_env() -> dict:
    """Per-session env vars injected into the worker subprocess (thread-local).

    - ``F3DASM_DELEGATION_ID`` (race-safe) so get_evaluator() resolves without
      the worker cd-ing into its D### dir (audit Finding 2).
    - ``F3DASM_RUN_CONFIG`` — explicit path to run_config.json so get_evaluator()
      resolves it regardless of cwd (the SDK spawns the worker in study_dir, from
      which the old walk-up never reached runs/<id>/debug/run_config.json).
    - ``F3DASM_CANONICAL_STORE`` — the canonical store path, derived from
      run_config["store_dir"]. get_evaluator() reads store_dir from the config,
      but a worker's OWN campaign scripts read os.environ["F3DASM_CANONICAL_STORE"]
      directly; without this the var is empty in the worker shell, so a campaign
      defaults to the wrong namespace and can overwrite another delegation's
      scratch data (audit run 20260624T021359, D005→D006 sim-dir clobber).
    - ``CLAUDE_CODE_DISABLE_AUTO_MEMORY`` — the bundled CLI injects the
      developer's personal auto-memory index (keyed off cwd, unrelated to this
      run) into every agent turn. ``ClaudeAgentOptions.setting_sources=[]``
      does NOT gate this — it only covers hooks/filesystem settings; the CLI
      checks this env var independently. Without it, a study agent's context
      leaks the operator's own MEMORY.md (run 20260926T124841: the strategizer
      quoted lines from it verbatim in its own reasoning).
    """
    import os
    import sys

    from .base import (
        get_delegation_id,
        get_namespace,
        get_run_config_path,
    )
    env: dict = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    # The agent's shell must run the SAME interpreter as the agent loop, so
    # `python`/`uv run python` in Bash can import whatever the framework can
    # (f3dasm, adda, the study's deps). Without this, bash `python` resolves
    # via the inherited PATH — which need not include the run's venv when the
    # loop was launched by invoking the venv's python binary directly (no
    # activation), leaving the agent unable to import the package and forced
    # into off-ledger workarounds. Prepend the run interpreter's bin dir.
    _bin = os.path.dirname(sys.executable)
    if _bin:
        env["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")
    from ..runtime.settings import get_float as _get_float
    env["BASH_DEFAULT_TIMEOUT_MS"] = str(
        int(_get_float("bash_timeout_s", 120.0) * 1000))
    did = get_delegation_id()
    if did:
        env["F3DASM_DELEGATION_ID"] = did
    ns = get_namespace()
    if ns:
        # Scope this worker to a design namespace so get_evaluator() resolves
        # that namespace's oracle + ledger (Axis 3a). Absent → single-study path.
        env["F3DASM_NAMESPACE"] = ns
    rc = get_run_config_path()
    if rc:
        env["F3DASM_RUN_CONFIG"] = rc
        try:
            import json as _json
            store = _json.loads(Path(rc).read_text()).get("store_dir")
            if store:
                env["F3DASM_CANONICAL_STORE"] = str(store)
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            pass
    return env


class ClaudeAdapter:
    """Wraps claude-agent-sdk; runs one agent turn and returns assistant text.

    The SDK handles its own tool-execution loop (Bash, Read, Write, Edit).
    This adapter converts a list of LangChain-style message dicts to the SDK
    format, runs the query, and assembles the final text response.

    Parameters
    ----------
    model : str
        Claude model identifier.
    system_prompt : str
        System prompt for the agent.
    study_dir : Path or None
        Working directory passed to the SDK as ``cwd``.
    native_tools : list[str]
        Tool names to enable (e.g. ``["Bash", "Read", "Write"]``).
    closure_tools : dict[str, callable] or None
        Extra Python callables exposed to the model as MCP tools.
    """

    # CLI tools the Claude SDK executes natively. A node's other declared tools
    # are injected as Python closures (MCP), not passed here.
    NATIVE_TOOLS = frozenset({
        "Bash", "Edit", "Read", "Write", "Glob", "Grep",
        # Bash's own SDK companions: poll a backgrounded shell / kill it. These
        # are SDK built-ins we previously omitted, so an agent that got a
        # backgroundTaskId (from auto-background on timeout) had no tool to act
        # on it. Granting them by declaration closes that awareness gap.
        "BashOutput", "KillShell",
        "Task", "WebFetch", "WebSearch",
    })

    @classmethod
    def select_native_tools(cls, agent_tools) -> list[str]:
        """Pick which of an agent's declared tools are native SDK CLI tools.

        Mirror of OpenAICompatibleAdapter.select_native_tools so the runtime
        can choose native tools generically for any backend (forward-compatible
        dispatch)."""
        return [t for t in agent_tools if t in cls.NATIVE_TOOLS]

    def __init__(
        self,
        model: str,
        system_prompt: str,
        study_dir: Path | None = None,
        native_tools: list[str] | None = None,
        closure_tools: dict[str, Any] | None = None,
        extra_mcp_servers: dict | None = None,
        extra_allowed_tools: list[str] | None = None,
        persistent: bool = False,
        max_history_pairs: int = 5,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.study_dir = Path(study_dir) if study_dir else None
        self.native_tools = list(native_tools or [])
        self.closure_tools = dict(closure_tools or {})
        self.extra_mcp_servers: dict = dict(extra_mcp_servers or {})
        self.extra_allowed_tools: list[str] = list(extra_allowed_tools or [])
        # persistent and max_history_pairs kept for backward compatibility with
        # Agent subclasses and tests that read these attributes; not used
        # in the core invocation path (history is demand-driven via DelegationLog).
        self.persistent: bool = persistent
        self.max_history_pairs: int = max_history_pairs
        # Serialises calls on THIS adapter object; each delegation runs on its own copy().
        self._lock: threading.Lock = threading.Lock()
        # Set by an orchestrating node; when truthy, the generator is closed after
        # the next AssistantMessage so the session ends on a routing decision.
        self.route_watcher: Any = None
        # Populated after each ainvoke() with token counts from ResultMessage.
        self.last_usage: dict = {}
        self._attempt_usages: list[dict] | None = None
        # Populated after each ainvoke() with the CLI session id (spec 12
        # item 3: session-resumption plumbing, capture-only for now) --
        # ResultMessage's when the turn completed normally, else whatever
        # the last AssistantMessage carried.
        self.last_session_id: str | None = None

    def _render_system_prompt(self) -> str:
        """The system prompt exactly as the model sees it: base prompt plus
        the ``<tools>`` catalog, with every tool the prose names rewritten to
        the qualified name the SDK exposes (catalog and prose must agree)."""
        from ..prompts.tool_catalog import (
            qualify_tool_mentions,
            system_prompt_with_catalog,
        )
        qualified = _qualify_closure_names(self.closure_tools)
        rendered = system_prompt_with_catalog(self.system_prompt, qualified)
        return qualify_tool_mentions(rendered, {
            bare: f"mcp__{_CLOSURE_MCP_SERVER}__{bare}"
            for bare in self.closure_tools
        })

    def _compute_allowed_tools(self, qualified_mcp_tools) -> list[str]:
        """All allowed tool names, ALWAYS as a list (never None).

        The SDK does ``list(options.allowed_tools)`` when building its command,
        which raises ``TypeError`` on ``None`` — so a tool-less agent (e.g. the
        one-shot problem-statement reviewer) must still get ``[]`` here, not
        ``None``. An empty list correctly means "no tools allowed".
        """
        return (
            list(qualified_mcp_tools)
            + list(self.native_tools)
            + list(self.extra_allowed_tools)
        )

    def copy(self) -> ClaudeAdapter:
        """An independent adapter for ONE delegation.

        Shares configuration; owns everything a delegation mutates: its own
        ``closure_tools`` (dispatch binds ReportEvals / Write / FollowUp to
        that delegation's id), its own ``_lock`` and its own per-call
        ``last_*`` state. Same-role delegations therefore run concurrently
        instead of queueing behind one shared lock.
        """
        import copy as _copy
        twin = _copy.copy(self)
        twin.closure_tools = dict(self.closure_tools)
        twin.native_tools = list(self.native_tools)
        twin.extra_allowed_tools = list(self.extra_allowed_tools)
        twin.extra_mcp_servers = dict(self.extra_mcp_servers)
        twin._lock = threading.Lock()
        twin.last_usage = {}
        twin._attempt_usages = None
        twin.last_session_id = None
        return twin

    async def ainvoke(
        self, messages: list[dict], *, idle_timeout: float | None = None,
        resume: str | None = None,
    ) -> str:
        """Run one agent turn asynchronously; return assembled text.

        ``idle_timeout`` overrides the run-wide ``llm_stream_idle_timeout`` for
        THIS call only — used by short advisory side-calls (e.g. the verdict
        validator) that must not inherit a real agent turn's generous window.

        ``resume`` (spec 12 item 3): a CLI session id to resume rather than
        starting fresh -- ``messages`` then carries only the NEW turn (the
        prior conversation is loaded from the resumed session itself, not
        replayed here). ``fork_session=False`` always, so this continues the
        SAME session rather than branching a copy of it.
        """
        _require_sdk()
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            SdkMcpTool,
            StreamEvent,
            SystemMessage,
            TextBlock,
            ToolUseBlock,
            UserMessage,
            create_sdk_mcp_server,
            query,
        )

        from ..prompts.tool_catalog import tool_summary

        # Build MCP server from closure_tools if any
        mcp_servers: dict = {}
        qualified_mcp_tools: list[str] = []
        if self.closure_tools:
            server_name = _CLOSURE_MCP_SERVER
            sdk_tools: list[Any] = []
            for tool_name, fn in self.closure_tools.items():
                schema = _infer_schema_from_callable(fn)

                async def _handler(args: dict, bound_fn: Any = fn) -> dict:
                    try:
                        result = bound_fn(**args)
                    except Exception as exc:
                        return {
                            "content": [
                                {"type": "text", "text": f"ERROR: {exc}"}
                            ],
                            "is_error": True,
                        }
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    str(result) if result is not None else ""
                                ),
                            }
                        ]
                    }

                sdk_tools.append(
                    SdkMcpTool(
                        name=tool_name,
                        description=tool_summary(fn, tool_name),
                        input_schema=schema,
                        handler=_handler,
                    )
                )

            mcp_cfg = create_sdk_mcp_server(
                name=server_name, tools=sdk_tools or None
            )
            mcp_servers = {server_name: mcp_cfg}
            qualified_mcp_tools = list(_qualify_closure_names(self.closure_tools))

        # Merge external stdio MCP servers declared by the Agent subclass.
        if self.extra_mcp_servers:
            mcp_servers.update(self.extra_mcp_servers)

        _base_disallowed = ["WebSearch", "WebFetch", "Task", "ExitPlanMode"]
        # Under permission_mode="bypassPermissions" the allowed_tools allowlist
        # is NOT enforced — disallowed_tools is the only thing that binds. So a
        # native tool the agent never declared (e.g. Bash/Write for a read-only
        # reviewer) would otherwise be silently usable. Disallow every native
        # tool this agent did not declare, making its declared toolset binding.
        _ungranted_native = [
            t for t in self.NATIVE_TOOLS if t not in self.native_tools
        ]
        _effective_disallowed = [
            t for t in dict.fromkeys([*_base_disallowed, *_ungranted_native])
            if t not in self.extra_allowed_tools
        ]

        # Non-blocking raw-oracle nudge: a PostToolUse hook that injects a
        # reminder (capped per delegation = per ainvoke) when a Bash/Write
        # call reaches the oracle directly instead of via get_evaluator().
        # Best-effort — if the SDK hook API is unavailable, run without it.
        _hooks = None
        try:
            from claude_agent_sdk import HookMatcher

            from .base import OracleNudgeBudget, oracle_registered
            # Silent until an oracle is registered: pre-registration work (the
            # datagenerator wrapping/validating its raw source) has no
            # get_evaluator() to use, so nudging it is a false positive.
            _nudge = OracleNudgeBudget(enabled=oracle_registered())
            # Expose on the adapter so the runtime can drain + log its
            # firings as direct evidence (see _record_intervention).
            self._oracle_nudge = _nudge

            async def _oracle_hook(input_data, tool_use_id, context):
                msg = _nudge.check(
                    input_data.get("tool_name", ""),
                    input_data.get("tool_input") or {},
                )
                if not msg:
                    return {}
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": msg,
                    }
                }

            _hooks = {"PostToolUse": [HookMatcher(hooks=[_oracle_hook])]}
        except Exception:  # noqa: BLE001 — nudge is best-effort, never fatal
            _hooks = None

        # Per-session env: the SDK MERGES this over the inherited environment
        # (PATH etc. preserved), so bare extra keys are safe. See
        # _build_session_env for what is injected and why.
        _sess_env: dict = _build_session_env()

        from ..runtime.settings import get_float
        _max_buf_mb = get_float("llm_max_buffer_mb", 30.0)
        _max_buf = int(_max_buf_mb * 1024 * 1024)

        # The SDK spawns the CLI with cwd=self.study_dir; if that directory
        # doesn't exist the subprocess dies with a cryptic CLIConnectionError
        # ("Working directory does not exist") mid-delegation. Create it
        # defensively so a missing worker workspace can never abort a run.
        if self.study_dir:
            try:
                self.study_dir.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001 — best-effort; spawn surfaces real errors
                pass

        options = ClaudeAgentOptions(
            system_prompt=self._render_system_prompt(),
            model=self.model,
            cwd=str(self.study_dir) if self.study_dir else None,
            tools=self.native_tools or [],
            mcp_servers=mcp_servers if mcp_servers else {},
            allowed_tools=self._compute_allowed_tools(qualified_mcp_tools),
            disallowed_tools=_effective_disallowed,
            permission_mode="bypassPermissions",
            strict_mcp_config=bool(mcp_servers) or bool(self.extra_mcp_servers),
            # Hermetic session: load NO filesystem settings, so worker/critic
            # subprocesses don't inherit the developer's global ~/.claude hooks
            # (e.g. cbm-code-discovery-gate, which blocked legitimate Read calls
            # for workers AND the critic). Our own hooks are passed
            # programmatically via options.hooks below (audit/#1: fresh hooks).
            setting_sources=[],
            env=_sess_env,
            # Stream-message buffer ceiling. Default 1MB is far too small for a
            # literature reviewer whose tools return full PDFs — a single >1MB
            # MCP tool result overflowed it and FATALLY (non-retried) killed the
            # whole delegation (D003). 30MB clears realistic PDFs; non-PDF
            # results never approach it. A result still exceeding this is caught
            # gracefully below (turn cut short + marker), not a fatal crash.
            # Tune via F3DASM_LLM_MAX_BUFFER_MB.
            max_buffer_size=_max_buf,
            # Partial streaming → a fine-grained heartbeat: the stream emits a
            # StreamEvent sub-second while genuinely generating, so total
            # silence becomes a reliable stall signal and the
            # idle timeout can be bounded without false-positiving a
            # slow-but-working generation.
            include_partial_messages=True,
            **_thinking_options(self.model),
            **({"hooks": _hooks} if _hooks else {}),
            **(
                {"resume": resume, "fork_session": False}
                if resume is not None else {}
            ),
        )

        prompt_str = _format_messages_as_prompt(messages)

        last_assistant = None
        last_result: Any = None
        _buffer_overflowed = False
        _deliberate_break = False
        _msg_usage: dict[str, dict] = {}
        _cur_msg_id: list[str | None] = [None]
        gen = query(prompt=prompt_str, options=options)
        # Idle-stream timeout — turns a silent stream into a retryable
        # TimeoutError. Resets on EVERY stream message. Scoped to model
        # generation: while a tool runs the window stands down (see _phase).
        # MEASURED (the stream_evt instrumentation settled this): the bundled
        # CLI forwards NO ping events — only message lifecycle — so during a
        # mid-generation pause we get ZERO liveness signal. Two supercompressible
        # (Sonnet) runs captured LEGITIMATE (recovered) active-generation gaps of
        # 53.6s AND 252-256s (one mid-tool_use-block composition, one
        # tool_result→next message_start delay). So earlier 60s/180s windows
        # would guillotine legitimate slow generations. The observed ~256s legit
        # gap left only ~44s of headroom under the prior 300s default, and with
        # NO ping liveness signal a longer legit generation is indistinguishable
        # from a stall — so the default is 600s: ample margin over the measured
        # legit gaps while still catching a truly dead (silent-forever) stream in
        # ~10 min. A genuine runaway is the delegation watchdog's concern, not
        # this window's. Knobs (config.yaml runtime block; env overrides):
        # llm_stream_idle_timeout (0 disables); llm_tool_idle_timeout caps tool
        # execution (0 = uncapped).
        from ..runtime.settings import get_float as _get_float
        _idle = (idle_timeout if idle_timeout is not None
                 else _get_float("llm_stream_idle_timeout", 600.0))
        _tool_idle = _get_float("llm_tool_idle_timeout", 0.0)

        def _phase(msg: Any):
            # True: a tool is now executing → suspend the tight idle window.
            # False: generation active / tool result returned → tight window.
            # None: leave phase unchanged.
            if isinstance(msg, AssistantMessage):
                return any(
                    isinstance(b, ToolUseBlock) for b in msg.content
                )
            if isinstance(msg, StreamEvent):
                # Only message_start opens a new generation. The SDK emits
                # content_block_stop / message_delta / message_stop AFTER the
                # AssistantMessage that carries a ToolUseBlock, while the tool
                # is still running; treating those as "generation resumed"
                # re-armed the stall window mid-tool and killed a 91-minute
                # delegation that was blocked in a long tool call.
                ev = getattr(msg, "event", None)
                if isinstance(ev, dict) and ev.get("type") == "message_start":
                    return False
                return None
            if isinstance(msg, UserMessage):
                return False
            return None

        from .base import append_transcript, debug_enabled

        def _record(msg: Any):
            # Full reasoning + tool-calls + tool-results; StreamEvent partials
            # are skipped (the assembled AssistantMessage carries the text).
            if isinstance(msg, AssistantMessage):
                texts, tools, thinking = [], [], []
                thinking_omitted = 0
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        texts.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        tools.append({"name": b.name, "input": b.input,
                                      "tool_use_id": getattr(b, "id", None)})
                    else:
                        t = (getattr(b, "thinking", None)
                             or getattr(b, "text", None))
                        if t:
                            thinking.append(t)
                        elif hasattr(b, "thinking"):
                            # A thinking block that arrived with no text
                            # (display "omitted"): counted, so "the model
                            # thought but it was hidden" is distinguishable
                            # from "the model did not think".
                            thinking_omitted += 1
                return {"type": "assistant", "text": "".join(texts),
                        "tools": tools, "thinking": thinking,
                        "thinking_omitted": thinking_omitted,
                        "message_id": getattr(msg, "message_id", None)}
            if isinstance(msg, UserMessage):
                results = []
                for b in (getattr(msg, "content", None) or []):
                    results.append({
                        "tool_use_id": getattr(b, "tool_use_id", None),
                        "content": getattr(b, "content", b),
                    })
                return {"type": "tool_result", "results": results}
            if isinstance(msg, ResultMessage):
                return {"type": "result",
                        "usage": getattr(msg, "usage", None),
                        "model_usage": getattr(msg, "model_usage", None),
                        "cost_usd": getattr(msg, "total_cost_usd", None)}
            if isinstance(msg, SystemMessage):
                # SystemMessage used to be invisible here entirely (this
                # function returned None for anything it didn't recognize) —
                # so a real compact_boundary (the SDK's own context-
                # compaction event) left NO trace in debug/transcripts/,
                # the viewer, or any post-run analysis. Confirmed empirically
                # (a short forced-window test): a single short session
                # produced 50-119 SystemMessages, the overwhelming majority
                # subtype "thinking_tokens" — a per-token streaming heartbeat,
                # pure noise at transcript granularity (stream_evt already
                # covers liveness). Record everything ELSE verbatim,
                # including (especially) compact_boundary's own metadata.
                if msg.subtype in _SYSTEM_MESSAGE_NOISE_SUBTYPES:
                    return None
                return {"type": "system", "subtype": msg.subtype,
                        "data": msg.data}
            return None

        _capture = debug_enabled()
        # Partial-stream checkpointing: an agent that never completes a message
        # (infinite thinking / an unresolved turn) emits only StreamEvents and
        # would otherwise disclose nothing. Buffer the deltas and flush a
        # "partial" record every N events (and on any stream teardown) so the
        # transcript reveals what a stuck turn is doing in near-real-time.
        _PARTIAL_FLUSH_EVERY = 25
        _pbuf: list[str] = []
        _pcount = [0]

        def _extract_delta(ev: Any) -> str:
            if not isinstance(ev, dict):
                return ""
            d = ev.get("delta") or {}
            return (d.get("text") or d.get("thinking")
                    or d.get("partial_json") or "")

        def _flush_partial() -> None:
            if _pbuf:
                append_transcript({"type": "partial", "text": "".join(_pbuf),
                                   "events": _pcount[0]})
                _pbuf.clear()

        try:
            _stream = (
                _stream_with_idle_timeout(
                    gen, _idle,
                    tool_timeout=_tool_idle, classify=_phase,
                )
                if _idle > 0 else gen
            )
            # Measurement clock: the first event's gap below = time-to-first
            # stream event (≈ prefill latency).
            _last_evt = [time.monotonic()]
            async for msg in _stream:
                if isinstance(msg, StreamEvent):
                    _track_message_usage(
                        getattr(msg, "event", None), _msg_usage, _cur_msg_id)
                elif isinstance(msg, AssistantMessage) \
                        and getattr(msg, "usage", None) \
                        and getattr(msg, "message_id", None):
                    _msg_usage.setdefault(msg.message_id, dict(msg.usage))
                if _capture:
                    if isinstance(msg, StreamEvent):
                        _pcount[0] += 1
                        _ev = getattr(msg, "event", {}) or {}
                        _et = _ev.get("type", "?") if isinstance(
                            _ev, dict) else "?"
                        _now = time.monotonic()
                        _gap = _now - _last_evt[0]
                        _last_evt[0] = _now
                        # Record every NON-delta event (ping, message_start/
                        # stop, content_block_start/stop, message_delta) and any
                        # delta after a >2s pause — so we can see whether the
                        # stream stays alive (pings) during silent/prefill
                        # phases and the true inter-event gap distribution.
                        # This is what settles whether a 60s silence is a dead
                        # stream or a legitimately-slow first token.
                        if _et != "content_block_delta" or _gap > 2.0:
                            append_transcript({
                                "type": "stream_evt", "evt": _et,
                                "gap_s": round(_gap, 2),
                                **_usage_fields(_ev)})
                        _d = _extract_delta(_ev)
                        if _d:
                            _pbuf.append(_d)
                        if _pcount[0] % _PARTIAL_FLUSH_EVERY == 0:
                            _flush_partial()
                    else:
                        # A complete message: flush any buffered partial first,
                        # then the structured record.
                        _flush_partial()
                        _rec = _record(msg)
                        if _rec is not None:
                            append_transcript(_rec)
                if isinstance(msg, SystemMessage) and msg.subtype == "compact_boundary":
                    # Unconditional (not gated on _capture/debug mode): a
                    # compaction is a run-level fact an analyst should never
                    # have to enable debug transcripts to discover.
                    record_stream_diagnostic(
                        "CONTEXT_COMPACTED",
                        "The SDK compacted this session's context "
                        "mid-turn (compact_boundary).",
                        compaction_data=msg.data,
                    )
                if isinstance(msg, AssistantMessage):
                    last_assistant = msg
                    if self.route_watcher and self.route_watcher():
                        _deliberate_break = True
                        break
                elif isinstance(msg, ResultMessage):
                    last_result = msg
                    break
        except Exception as exc:  # noqa: BLE001
            _m = str(exc).lower()
            if "maximum buffer size" in _m or "exceeded maximum" in _m:
                # Graceful contour: a single tool result (e.g. a huge PDF)
                # overflowed the stream buffer. The old behavior was a fatal,
                # NON-retried crash that killed the whole delegation (D003).
                # Instead, end the turn with what we have + a marker so the
                # agent can retry the fetch smaller — the delegation survives.
                _buffer_overflowed = True
            else:
                raise
        finally:
            if _capture:
                _flush_partial()  # disclose a stuck/torn-down turn's tail
            self._settle_usage(last_result, _msg_usage, last_assistant)
            aclose = getattr(gen, "aclose", None)
            if aclose:
                try:
                    await aclose()
                except Exception:
                    pass

        # A stream that ends with neither a ResultMessage NOR a deliberate
        # route_watcher break, and wasn't already explained by a buffer
        # overflow, is abnormal: the CLI session ended (or the SDK's async
        # generator was exhausted) without ever completing its turn — report
        # 7 (run 20260830T004106, Oscar): the last AssistantMessage carried
        # only a tool call (e.g. Bash/TaskOutput), no result ever arrived,
        # and the near-empty `text` this then returns reads as a malformed
        # report to _invoke_with_report_retry, which silently re-invokes
        # with the ORIGINAL task — restarting the delegation from scratch,
        # orphaning whatever the first attempt launched, with nothing
        # logged anywhere. This does not change that return behaviour (a
        # future decision, pending Elvis) — it only makes the fact visible.
        if last_result is None and not _deliberate_break and not _buffer_overflowed:
            _last_tool = None
            if last_assistant is not None:
                for _b in last_assistant.content:
                    if isinstance(_b, ToolUseBlock):
                        _last_tool = _b.name
            record_stream_diagnostic(
                "STREAM_ENDED_WITHOUT_RESULT",
                "CLI stream ended without a ResultMessage or a deliberate "
                "route break — the turn may not have completed; its "
                "returned text can look like a malformed report and "
                "trigger a silent report-retry.",
                last_tool_in_flight=_last_tool,
                cli_session_id=getattr(last_assistant, "session_id", None),
            )

        text = ""
        if last_assistant is not None:
            for block in last_assistant.content:
                if isinstance(block, TextBlock):
                    text += block.text
        if _buffer_overflowed:
            _note = (
                f"[STREAM NOTE: a tool returned more than {_max_buf_mb:.0f} MB "
                "in a single result and overflowed the message buffer; that "
                "result was dropped and this turn was cut short (the delegation "
                "did NOT crash). Re-run the tool with a smaller/narrower request "
                "— fewer items, or a summary/extract instead of full text.]"
            )
            text = (text + "\n\n" + _note) if text else _note
        return text

    def _settle_usage(self, last_result: Any, _msg_usage: dict,
                      last_assistant: Any) -> None:
        """Record this attempt's usage and session id. Runs from ainvoke's
        ``finally`` so a stream that RAISES (idle TimeoutError, API error)
        still reports the usage it had streamed; ``invoke`` sums the
        attempts."""
        # Capture token usage from ResultMessage for run-level accounting.
        if last_result is not None:
            self.last_usage = {
                **(last_result.usage or {}),
                **_cache_split(last_result.usage or {}),
                "total_cost_usd": last_result.total_cost_usd,
            }
        elif _msg_usage:
            # route_watcher broke the stream on the AssistantMessage that
            # closes the run (Done()) before the SDK's ResultMessage -- the
            # only carrier of the session's cumulative usage and cost -- ever
            # arrived. The strategizer's whole run is ONE long stream, so
            # taking just the last message's usage recorded ~1 output token
            # for the entire run (run 20260928T141126). Sum the per-API-call
            # usage the stream itself reported instead. total_cost_usd stays
            # None: cost is a session-level rollup no single message carries,
            # the same "unknown, not zero" convention openai_compatible uses.
            self.last_usage = {
                **_sum_message_usage(_msg_usage.values()),
                "total_cost_usd": None,
            }
        elif last_assistant is not None and getattr(last_assistant, "usage", None):
            # A backend/stream that never sent message_start/delta events or a
            # message id: the lone message's own (possibly stale) usage.
            self.last_usage = {
                **last_assistant.usage, "total_cost_usd": None,
            }
        else:
            self.last_usage = {}

        self.last_session_id = (
            getattr(last_result, "session_id", None)
            or getattr(last_assistant, "session_id", None)
        )
        if self._attempt_usages is not None:
            self._attempt_usages.append(dict(self.last_usage))

    def invoke(
        self, messages: list[dict], *,
        idle_timeout: float | None = None, retry_max: int | None = None,
        resume: str | None = None,
        on_session_start: Any = None,
        on_session_end: Any = None,
    ) -> str:
        """Synchronous wrapper around :meth:`ainvoke`.

        Acquires _lock to serialize concurrent callers of this adapter object
        (each delegation has its own copy(), so they do not contend). Transient API/network
        failures are retried with exponential backoff (see retry_on_transient).

        ``idle_timeout`` / ``retry_max`` override the run-wide stream-idle and
        retry budgets for THIS call only. A short advisory side-call (verdict
        validator) passes a tight idle + ``retry_max=1`` so a hung CLI stream
        aborts in ~that window instead of inheriting a real turn's
        5×600s budget (which once froze a whole run for ~89 min).

        ``resume``: see :meth:`ainvoke`.

        ``on_session_start``: called with no arguments the instant ``_lock``
        is actually acquired -- i.e. when this call's real work begins, not
        when it was merely requested. This is the point at which the call's real work begins,
        distinct from when it was asked to start.
        Best-effort: swallows any exception so a broken callback never
        breaks the real turn.

        ``on_session_end``: called as ``on_session_end(session_id, usage)``
        with THIS call's own session id and token usage while ``_lock`` is
        still held, once the turn is over (also when it raised, with
        whatever it produced: ``None`` / ``{}`` if nothing). Both are
        per-call output; a caller that needs ITS OWN values takes them from
        here rather than from ``last_session_id`` / ``last_usage``. Best-effort
        like ``on_session_start``.
        """
        from .base import retry_on_transient
        with self._lock:
            if on_session_start is not None:
                try:
                    on_session_start()
                except Exception:  # noqa: BLE001
                    pass
            # Cleared so a turn that raises before ainvoke() sets them cannot
            # hand its caller the PREVIOUS call's values.
            self.last_session_id = None
            self.last_usage = {}
            self._attempt_usages = []
            try:
                return retry_on_transient(
                    lambda: _run_async_safe(
                        self.ainvoke(
                            messages, idle_timeout=idle_timeout,
                            resume=resume)),
                    max_attempts=retry_max,
                    on_retry=getattr(self, "on_retry", None),
                )
            finally:
                self.last_usage = _combine_attempt_usage(self._attempt_usages)
                self._attempt_usages = None
                if on_session_end is not None:
                    try:
                        on_session_end(
                            self.last_session_id, dict(self.last_usage or {}))
                    except Exception:  # noqa: BLE001
                        pass
