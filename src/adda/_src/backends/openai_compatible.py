"""Shared base for OpenAI-compatible chat backends (Ollama, OpenRouter, vLLM).

All three speak the OpenAI chat-completions API, so they share one
implementation: ChatOpenAI pointed at the backend's ``base_url`` plus
``create_react_agent`` for the tool-execution loop. A concrete backend is a
thin subclass that only declares its endpoint default + auth (see the class
attributes ``DEFAULT_BASE_URL`` / ``API_KEY`` /
``API_KEY_ENV``) — the invoke/tool/usage machinery is inherited verbatim.

The adapter exposes the same public surface as ClaudeAdapter (a mutable
``closure_tools`` dict, ``invoke()``, ``last_usage``, ``copy()``,
``select_native_tools()``, …) — enforced by tests/test_backend_parity.py.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from ..infra.run_abandon import raise_if_stopped
from ..infra.telemetry import call_shape, normalized_usage
from . import context_budget, context_compaction
from .base import DEFAULT_TOOLS

log = logging.getLogger(__name__)

__all__ = ["OpenAICompatibleAdapter"]

# Tool names that are injected as Python CLOSURES (not native CLI tools); the
# OpenAI-compatible backends pass everything ELSE through as a native tool.
_CLOSURE_TOOL_NAMES = frozenset(
    {"Done", "FollowUp", "WriteNote", "ReadNote", "ReportEvals"}
)


#: What the token "Default" expands to on this backend: the native tools below.
DEFAULT_NATIVE_TOOLS = (
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "Glob", "Grep",
)


class UserlessPayloadError(ValueError):
    """Raised when a request would carry no user turn at all.

    Deliberately not classified transient (see backends.base
    ``_TRANSIENT_SUBSTRINGS``): retrying an unanswerable payload five times
    with backoff wastes wall budget and buries the cause deeper.
    """


def _flatten_content(content) -> str:
    """Collapse LangChain/Anthropic block content to text WITHOUT losing it.

    A block list was previously joined on ``c.get("text", "")``, so any block
    that is not a text block contributed the empty string. A HumanMessage
    carrying only tool-result blocks therefore converted to ``content=""`` —
    the payload destroyed, not merely reformatted — and the request that went
    out had a user turn with nothing in it. Providers reject that; Ollama's
    OpenAI-compatible endpoint answers 500 "no user query found in messages",
    which names neither the message nor the role and reads as intermittent.

    So: prefer ``text``, fall back to a string ``content`` (tool_result blocks
    carry their payload there), and only then give up on the block.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            parts.append(str(c))
            continue
        val = c.get("text")
        if not isinstance(val, str) or not val:
            val = c.get("content")
        if isinstance(val, str) and val:
            parts.append(val)
        elif val is not None and not isinstance(val, str):
            parts.append(str(val))
    return " ".join(parts)


def _to_lc_messages(messages: list[dict]) -> list:
    from langchain_core.messages import AIMessage, HumanMessage

    result = []
    for m in messages:
        role = m.get("role", "user")
        content = _flatten_content(m.get("content", ""))
        # An EMPTY turn is dropped rather than sent. It carries nothing, and a
        # provider that counts non-empty user turns rejects the whole request
        # because of it — so passing it on can only turn a no-op into a 500.
        if not content.strip():
            continue
        if role in ("human", "user"):
            result.append(HumanMessage(content=content))
        elif role in ("ai", "assistant"):
            result.append(AIMessage(content=content))
    return result


def _make_edit_tool(cwd: Path | None) -> Any:
    from langchain_core.tools import StructuredTool

    workspace = (cwd or Path(".")).resolve()

    def edit_file(path: str, old_str: str, new_str: str) -> str:
        """Replace old_str with new_str in file at path (first occurrence).
        Restricted to the workspace directory."""
        try:
            candidate = (workspace / path).resolve()
            candidate.relative_to(workspace)
        except ValueError:
            return f"ERROR: edit rejected — {path!r} resolves outside workspace."
        except Exception as exc:
            return f"ERROR: invalid path {path!r}: {exc}"
        if not candidate.exists():
            return f"ERROR: {path} not found"
        text = candidate.read_text(encoding="utf-8")
        if old_str not in text:
            return f"ERROR: string not found in {path}"
        candidate.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return f"Edited {candidate}"

    return StructuredTool.from_function(edit_file, name="Edit")


def _make_grep_tool() -> Any:
    from langchain_core.tools import StructuredTool

    def grep_files(pattern: str, path: str = ".") -> str:
        """Search for pattern in files under path; return matching lines."""
        import subprocess

        try:
            result = subprocess.run(
                ["grep", "-r", "-n", pattern, path],
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout or "(no matches)"
        except Exception as exc:
            return f"ERROR: {exc}"

    return StructuredTool.from_function(grep_files, name="Grep")


def _make_glob_tool(cwd: Path | None) -> Any:
    from langchain_core.tools import StructuredTool

    base = cwd or Path(".")

    def glob_files(pattern: str) -> str:
        """Find files matching a glob pattern relative to the workspace."""
        # An ABSOLUTE pattern is anchored at its own root rather than refused:
        # the SDK's Glob accepts one, so a model that has seen absolute paths
        # in its own transcript will hand one over, and Path.glob raises
        # NotImplementedError on it. Any other bad pattern comes back as an
        # error STRING the agent can read and correct — a malformed tool
        # argument is a message to the caller, never the end of the run. The
        # sibling Grep tool already had that containment; this one did not.
        try:
            p = Path(pattern)
            if p.is_absolute():
                anchor = Path(p.anchor)
                hits = anchor.glob(str(p.relative_to(anchor)))
            else:
                hits = base.glob(pattern)
            matches = sorted(str(m) for m in hits)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {type(exc).__name__}: {exc}"
        return "\n".join(matches) if matches else "(no matches)"

    return StructuredTool.from_function(glob_files, name="Glob")


# --------------------------------------------------------------------------
# Bash + BashOutput + KillShell: one SDK-compatible surface for the non-Claude
# backends (Claude gets these from the real SDK). A command that exceeds its
# timeout is BACKGROUNDED (not killed) and returned with a bash_id, matching
# the SDK's auto-background; BashOutput polls it, KillShell stops it. The one
# deliberate deviation from the SDK is that the background is made VISIBLE
# (interrupted + bash_id in the result) so the agent never wakes up thinking a
# still-running job finished.
# --------------------------------------------------------------------------

_BASH_TIMEOUT_MAX_MS = 600000        # cap = SDK max
_BASH_INLINE_CAP = 30000             # chars kept inline before spilling to a file


class _BashSession:
    """Per-adapter registry of shells backing Bash/BashOutput/KillShell.

    Background shells key on a ``bash_id``. Shared by the three tool factories
    (built together in ``_native_tool_map``) so a worker can launch with
    ``run_in_background`` (or hit the timeout) and then poll ``BashOutput`` /
    ``KillShell`` within the same ReAct turn.
    """

    def __init__(self, cwd: Path | None) -> None:
        self.cwd = str(cwd) if cwd else None
        self._bg: dict[str, dict] = {}   # bash_id -> {proc, spool, handle, cursor}
        self._n = 0
        self._lock = threading.Lock()

    def add_background(self, proc, spool: str, handle) -> str:
        with self._lock:
            self._n += 1
            bid = f"bash_{self._n}"
            self._bg[bid] = {"proc": proc, "spool": spool,
                             "handle": handle, "cursor": 0}
        return bid

    def get(self, bid: str) -> dict | None:
        with self._lock:
            return self._bg.get(bid)

    def drop(self, bid: str) -> None:
        with self._lock:
            self._bg.pop(bid, None)

    def register_governor(self, pid: int) -> None:
        """Best-effort: add a backgrounded pid to the run's governor_pids.jsonl
        so the memory watcher tracks/kills it per-delegation. If the run
        context is not resolvable from the env, the process still stays in the
        run's process group (no setsid), so the whole-run group-kill remains
        the backstop; we simply skip fine-grained tracking."""
        try:
            import json as _json
            from datetime import datetime, timezone
            store = os.environ.get("F3DASM_CANONICAL_STORE")
            did = os.environ.get("F3DASM_DELEGATION_ID")
            if not store or not did:
                return
            reg = Path(store).parent / "debug" / "governor_pids.jsonl"
            if not reg.parent.exists():
                return
            from ..infra.resource_backend import get_resource_backend
            be = get_resource_backend()
            rec = {
                "delegation_id": did, "pid": pid,
                "start_time": be.proc_start_time(pid),
                "ts": datetime.now(tz=timezone.utc).isoformat(
                    timespec="seconds"),
            }
            with reg.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass


def _read_spill(spool: str, start: int) -> tuple[str, int]:
    """Read spool[start:] as text; return (text, new_end_offset)."""
    try:
        with open(spool, "rb") as f:
            f.seek(start)
            data = f.read()
        return data.decode("utf-8", errors="replace"), start + len(data)
    except OSError:
        return "", start


def _render(out: str, spool: str, *, exit_code=None, interrupted=False,
            bash_id=None, waited_s=None, nudge_msg=None) -> str:
    """SDK-shaped rendering as text (this backend delivers tool results as
    strings). Plain output on clean foreground success — for minimal
    disruption and backwards-compat — with a labeled trailer only when there
    is something the agent must know (nonzero exit, timeout/background)."""
    body = out if out else "(no output)"
    # Spill large output to the file and keep an inline tail.
    if len(body) > _BASH_INLINE_CAP:
        body = (f"[output truncated to last {_BASH_INLINE_CAP} chars; full "
                f"output at {spool}]\n" + body[-_BASH_INLINE_CAP:])
    parts = [body]
    if interrupted and bash_id is not None:
        parts.append(
            f"\n[interrupted: still running in the background after "
            f"~{waited_s}s — NOT killed]\nbash_id: {bash_id}  "
            f"(BashOutput('{bash_id}') for more output; "
            f"KillShell('{bash_id}') to stop it)")
    elif bash_id is not None:               # explicit run_in_background
        parts.append(
            f"\n[running in the background]\nbash_id: {bash_id}  "
            f"(BashOutput('{bash_id}') to poll; KillShell('{bash_id}') to stop)")
    elif exit_code not in (None, 0):
        parts.append(f"\n[exit {exit_code}]")
    if nudge_msg:
        parts.append(f"\n\n{nudge_msg}")
    return "".join(parts)


def _shell_env() -> dict:
    """The agent's shell must run the SAME interpreter as the agent loop (the
    Claude backend does the same): else bare ``python`` resolves via the
    inherited PATH to whatever else is installed, e.g. a stale ``adda``."""
    env = dict(os.environ)
    _bin = os.path.dirname(sys.executable)
    if _bin:
        env["PATH"] = _bin + os.pathsep + env.get("PATH", "")
    return env


def _make_bash_tool(cwd: Path | None, nudge: Any = None,
                    session: _BashSession | None = None) -> Any:
    import subprocess
    import tempfile

    from langchain_core.tools import StructuredTool

    sess = session if session is not None else _BashSession(cwd)

    def bash(command: str, timeout: int | None = None,
             run_in_background: bool = False, description: str | None = None,
             dangerouslyDisableSandbox: bool = False) -> str:
        """Run a shell command. Foreground by default; a command that exceeds
        `timeout` (ms) is BACKGROUNDED — not killed — and returned with a
        `bash_id` you poll via BashOutput / stop via KillShell. Set
        run_in_background=true to background immediately (for a long solve).
        stdout and stderr are merged. `description` is advisory;
        `dangerouslyDisableSandbox` is accepted for SDK compatibility and has
        no effect here (Bash is not sandbox-enforced on this backend)."""
        from ..runtime.settings import get_float
        default_ms = int(get_float("bash_timeout_s", 120.0) * 1000)
        try:
            timeout_ms = default_ms if timeout is None else int(timeout)
        except (TypeError, ValueError):
            timeout_ms = default_ms
        timeout_s = max(1.0, min(timeout_ms, _BASH_TIMEOUT_MAX_MS) / 1000.0)
        if isinstance(run_in_background, str):
            run_in_background = run_in_background.strip().lower() in (
                "true", "1", "yes")

        spool = tempfile.NamedTemporaryFile(
            prefix="f3dasm_bash_", suffix=".log", delete=False)
        spool_path = spool.name
        spool.close()
        handle = open(spool_path, "wb")
        # No start_new_session: the child MUST stay in the run's process group
        # so the watchdog group-kill and governor tree-walk can reach it.
        proc = subprocess.Popen(
            command, shell=True, cwd=sess.cwd, env=_shell_env(),
            stdout=handle, stderr=subprocess.STDOUT)

        def _nudge_msg():
            return nudge("Bash", {"command": command}) if nudge else None

        if run_in_background:
            sess.register_governor(proc.pid)
            bid = sess.add_background(proc, spool_path, handle)
            out, _ = _read_spill(spool_path, 0)
            return _render(out, spool_path, bash_id=bid,
                           nudge_msg=_nudge_msg())
        try:
            proc.wait(timeout=timeout_s)
            handle.close()
            out, _ = _read_spill(spool_path, 0)
            os.unlink(spool_path) if len(out) <= _BASH_INLINE_CAP else None
            return _render(out, spool_path, exit_code=proc.returncode,
                           nudge_msg=_nudge_msg())
        except subprocess.TimeoutExpired:
            # Auto-background on timeout — matches the SDK, but made VISIBLE.
            sess.register_governor(proc.pid)
            bid = sess.add_background(proc, spool_path, handle)
            out, cursor = _read_spill(spool_path, 0)
            sess._bg[bid]["cursor"] = cursor
            return _render(out, spool_path, interrupted=True, bash_id=bid,
                           waited_s=int(timeout_s), nudge_msg=_nudge_msg())

    return StructuredTool.from_function(bash, name="Bash")


def _make_bashoutput_tool(session: _BashSession) -> Any:
    from langchain_core.tools import StructuredTool

    def bash_output(bash_id: str) -> str:
        """Return output produced by a background shell (started via Bash with
        run_in_background, or auto-backgrounded on timeout) SINCE THE LAST
        call. Reports whether it is still running or has exited (with code)."""
        entry = session.get(bash_id)
        if entry is None:
            return (f"ERROR: no background shell {bash_id!r}. Use the bash_id "
                    "returned by Bash.")
        new_out, cursor = _read_spill(entry["spool"], entry["cursor"])
        entry["cursor"] = cursor
        rc = entry["proc"].poll()
        if rc is None:
            status = "[still running]"
        else:
            try:
                entry["handle"].close()
            except OSError:
                pass
            session.drop(bash_id)
            status = f"[exited {rc}]"
        return f"{status}\n{new_out if new_out else '(no new output)'}"

    return StructuredTool.from_function(bash_output, name="BashOutput")


def _make_killshell_tool(session: _BashSession) -> Any:
    from langchain_core.tools import StructuredTool

    def kill_shell(bash_id: str) -> str:
        """Terminate a background shell and its process tree (SIGTERM then
        SIGKILL). The only per-delegation teardown for a long job."""
        entry = session.get(bash_id)
        if entry is None:
            return f"ERROR: no background shell {bash_id!r}."
        pid = entry["proc"].pid
        try:
            from ..infra.resource_backend import get_resource_backend
            get_resource_backend().kill([pid])   # tree kill (recursive)
        except Exception:  # noqa: BLE001
            try:
                entry["proc"].kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            entry["handle"].close()
        except OSError:
            pass
        session.drop(bash_id)
        return f"[killed {bash_id} (pid {pid}) and its process tree]"

    return StructuredTool.from_function(kill_shell, name="KillShell")


def _make_read_tool(cwd: Path | None) -> Any:
    from langchain_core.tools import StructuredTool

    def read_file(path: str) -> str:
        """Read a file and return its contents."""
        p = (
            Path(path) if Path(path).is_absolute()
            else (cwd or Path(".")) / path
        )
        if not p.exists():
            return f"ERROR: {p} not found"
        return p.read_text(encoding="utf-8")

    return StructuredTool.from_function(read_file, name="Read")


def _make_write_tool(cwd: Path | None, nudge: Any = None) -> Any:
    from langchain_core.tools import StructuredTool

    def write_file(path: str, content: str) -> str:
        """Write content to a file, creating it if it doesn't exist."""
        p = (
            Path(path) if Path(path).is_absolute()
            else (cwd or Path(".")) / path
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        out = f"Written: {p}"
        if nudge is not None:
            msg = nudge("Write", {"file_path": path, "content": content})
            if msg:
                out = f"{out}\n\n{msg}"
        return out

    return StructuredTool.from_function(write_file, name="Write")


def _guard_native(tool: Any, on_error: Any = None) -> Any:
    """Make a tool's failure a result the model reads, never a crash.

    An exception that leaves a tool propagates through LangGraph and ends the
    whole delegation, so one bad path (a directory handed to Read) cost the
    entire delegation. The wrapper returns ``ERROR: <Type>: <message>`` instead
    and reports it to *on_error* (``tool_name, message, args``), so it counts
    as ERROR_RETURN as the closure tools do. A result that already starts with
    "ERROR:" is reported too. LangChain's ``handle_tool_error`` covers only
    ``ToolException``, not these errors. The sink is a callable and not the
    thread-local run context, because LangGraph runs tools on its own threads.
    """
    import functools

    inner = tool.func

    @functools.wraps(inner)
    def guarded(*args, **kwargs):
        raise_if_stopped(tool.name)
        try:
            result = inner(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            result = f"ERROR: {type(exc).__name__}: {exc}"
        if (on_error is not None and isinstance(result, str)
                and result.lstrip().startswith("ERROR:")):
            try:
                on_error(tool.name, result[:2000],
                         {k: str(v)[:2000] for k, v in kwargs.items()})
            except Exception:  # noqa: BLE001
                pass
        return result

    tool.func = guarded
    return tool


def _native_tool_map(cwd: Path | None, nudge: Any = None,
                     on_error: Any = None) -> dict[str, Any]:
    # One BashSession shared by Bash/BashOutput/KillShell so background shells
    # launched by Bash are visible to the companion tools.
    session = _BashSession(cwd)
    tools = {
        "Bash":  _make_bash_tool(cwd, nudge, session=session),
        "BashOutput": _make_bashoutput_tool(session),
        "KillShell":  _make_killshell_tool(session),
        "Read":  _make_read_tool(cwd),
        "Write": _make_write_tool(cwd, nudge),
        "Edit":  _make_edit_tool(cwd),
        "Glob":  _make_glob_tool(cwd),
        "Grep":  _make_grep_tool(),
    }
    return {name: _guard_native(t, on_error) for name, t in tools.items()}


def _build_arxiv_closures() -> dict:
    """Return arxiv tool callables keyed by their MCP-compatible tool name.

    Returns an empty dict if the ``arxiv`` package is not installed.
    Used by the OpenAI-compatible adapters (via _make_literature_tools) and
    LiteratureReviewAgent.build_closure_tools() so both backends share
    identical arxiv tool implementations.
    """
    try:
        import arxiv as _arxiv
    except ImportError:
        return {}
    from ..prompts.tool_catalog import tool_examples

    @tool_examples(
        "arxiv_search_papers('topology optimization neural network', max_results=10)",
    )
    def search_papers(query: str, max_results: int = 10) -> str:
        """Search arxiv for papers matching query."""
        # MCP string-in tools pass numbers as strings ("5"); the arxiv library
        # does int arithmetic on max_results internally → 'str - int' crash.
        max_results = int(max_results)
        client = _arxiv.Client()
        results = list(client.results(_arxiv.Search(query=query, max_results=max_results)))
        lines = []
        for r in results:
            lines.append(f"[{r.entry_id}] {r.title} ({r.published.year})\n  {r.summary[:300]}")
        return "\n\n".join(lines) or "(no results)"

    @tool_examples(
        "arxiv_list_papers('cs.LG', max_results=10)",
    )
    def list_papers(category: str, max_results: int = 10) -> str:
        """List recent arxiv papers in a category (e.g. 'cs.LG')."""
        max_results = int(max_results)
        client = _arxiv.Client()
        results = list(client.results(_arxiv.Search(query=f"cat:{category}", max_results=max_results)))
        return "\n".join(f"[{r.entry_id}] {r.title}" for r in results) or "(no results)"

    def _fetch_pdf(paper_id: str, dest: str) -> None:
        """Fetch an arXiv PDF straight from its canonical URL into ``dest``.

        Direct-by-URL on purpose: the arxiv library's Result.download_pdf was
        removed in recent versions ('Result' object has no attribute
        'download_pdf'), and resolving the id through client.results() costs a
        rate-limited round-trip we don't need — the id alone determines the PDF
        URL. So there is no library call to fail and no reason for the agent to
        re-try a different tool for the same result.
        """
        import urllib.request
        pid = paper_id.strip().rstrip("/").split("/")[-1]  # tolerate abs URLs
        url = f"https://arxiv.org/pdf/{pid}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "f3dasm-agent/1.0 (mailto:f3dasm@brown.edu)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        with open(dest, "wb") as f:
            f.write(data)

    @tool_examples(
        "arxiv_download_paper('1706.03762', output_dir='papers')",
    )
    def download_paper(paper_id: str, output_dir: str = ".") -> str:
        """Download a paper PDF from arxiv by its ID (direct URL)."""
        pid = paper_id.strip().rstrip("/").split("/")[-1]
        out = Path(output_dir)
        # The agent names where it wants the paper; a directory it has not
        # created yet is an ordinary thing to name, not an error. Without
        # this the open() below raises FileNotFoundError on the DIRECTORY and
        # the message blames the file, which reads as a failed download.
        out.mkdir(parents=True, exist_ok=True)
        dest = str(out / f"{pid}.pdf")
        _fetch_pdf(paper_id, dest)
        return f"Downloaded: {dest}"

    @tool_examples(
        "arxiv_read_paper('1706.03762')",
    )
    def read_paper(paper_id: str) -> str:
        """Download (direct URL) and extract text from an arxiv paper."""
        import tempfile
        # The PDF handle MUST be closed before the temp dir is torn down.
        # fitz.open() holds the file open, and on NFS (every cluster scratch)
        # deleting a still-open file silly-renames it to .nfsXXXX instead of
        # unlinking it, so the rmdir that follows fails with ENOTEMPTY and
        # takes the whole delegation down AFTER the text was already
        # extracted. ignore_cleanup_errors is the second belt: a leaked temp
        # dir is a janitor's problem, never a reason to lose a run's work.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "paper.pdf")
            _fetch_pdf(paper_id, path)
            try:
                import fitz
                doc = fitz.open(path)
                try:
                    return "\n".join(page.get_text() for page in doc)
                finally:
                    doc.close()
            except ImportError:
                import subprocess
                result = subprocess.run(
                    ["pdftotext", path, "-"], capture_output=True, text=True
                )
                return result.stdout or "(could not extract text — install pymupdf or pdftotext)"

    return {
        "arxiv_search_papers": search_papers,
        "arxiv_list_papers": list_papers,
        "arxiv_download_paper": download_paper,
        "arxiv_read_paper": read_paper,
    }


def _make_literature_tools() -> list:
    """Build LangChain StructuredTools equivalent to the literature MCP stack."""
    from langchain_core.tools import StructuredTool

    tools = []

    # --- arxiv (via shared _build_arxiv_closures) ---
    for name, fn in _build_arxiv_closures().items():
        tools.append(StructuredTool.from_function(fn, name=name))

    # --- semantic scholar ---
    try:
        import semanticscholar  # noqa: F401 — absent => no S2 tools

        from ..agents.literature_tools.semantic_scholar import (
            get_semantic_scholar_client,
        )
        from ..agents.literature_tools.throttle import _throttled_ss

        _ss = get_semantic_scholar_client()

        def get_paper_details(paper_id: str) -> str:
            """Get details for a paper by its S2, DOI, or arxiv ID."""
            import json
            paper = _throttled_ss(_ss.get_paper, paper_id, fields=[
                "title", "year", "venue", "citationCount",
                "influentialCitationCount", "tldr", "authors",
            ])
            return json.dumps({
                "title": paper.title,
                "year": paper.year,
                "venue": paper.venue,
                "citations": paper.citationCount,
                "influential_citations": paper.influentialCitationCount,
                "tldr": paper.tldr.get("text") if paper.tldr else None,
                "authors": [a["name"] for a in (paper.authors or [])],
            }, indent=2)

        def get_citations_and_references(paper_id: str) -> str:
            """Get citing papers and references for a paper."""
            import json
            paper = _throttled_ss(
                _ss.get_paper, paper_id, fields=["citations", "references"])
            refs = [{"title": r.get("title"), "paperId": r.get("paperId")} for r in (paper.references or [])]
            cits = [{"title": c.get("title"), "paperId": c.get("paperId")} for c in (paper.citations or [])]
            return json.dumps({"references": refs[:20], "citations": cits[:20]}, indent=2)

        tools += [
            StructuredTool.from_function(
                get_paper_details,
                name="mcp__semanticscholar__get_semantic_scholar_paper_details",
            ),
            StructuredTool.from_function(
                get_citations_and_references,
                name="mcp__semanticscholar__get_semantic_scholar_citations_and_references",
            ),
        ]
    except ImportError:
        pass  # semanticscholar package not installed

    # --- zotero (read-only) ---
    try:
        import os as _os

        from pyzotero import zotero as _pyzotero

        _lib_id = _os.environ.get("ZOTERO_LIBRARY_ID")
        _api_key = _os.environ.get("ZOTERO_API_KEY")

        if _lib_id and _api_key:
            _zot = _pyzotero.Zotero(_lib_id, "user", _api_key)

            def zotero_search_items(query: str, limit: int = 10) -> str:
                """Search Zotero library for items matching query."""
                import json
                items = _zot.items(q=query, limit=limit)
                return json.dumps([
                    {"key": i["key"], "title": i["data"].get("title"), "year": i["data"].get("date", "")[:4]}
                    for i in items
                ], indent=2)

            def zotero_get_item_metadata(item_key: str) -> str:
                """Get metadata for a Zotero item by key."""
                import json
                return json.dumps(_zot.item(item_key)["data"], indent=2)

            def zotero_get_item_fulltext(item_key: str) -> str:
                """Get full text content of a Zotero item."""
                try:
                    return _zot.fulltext_item(item_key).get("content", "(no fulltext)")
                except Exception as e:
                    return f"(fulltext unavailable: {e})"

            def zotero_semantic_search(query: str, limit: int = 10) -> str:
                """Semantic search over Zotero library."""
                return zotero_search_items(query, limit)  # fallback to keyword

            tools += [
                StructuredTool.from_function(zotero_search_items, name="mcp__zotero-mcp__zotero_search_items"),
                StructuredTool.from_function(zotero_get_item_metadata, name="mcp__zotero-mcp__zotero_get_item_metadata"),
                StructuredTool.from_function(zotero_get_item_fulltext, name="mcp__zotero-mcp__zotero_get_item_fulltext"),
                StructuredTool.from_function(zotero_semantic_search, name="mcp__zotero-mcp__zotero_semantic_search"),
            ]
    except ImportError:
        pass  # pyzotero not installed

    return tools


class OpenAICompatibleAdapter:
    """Base adapter for any OpenAI chat-completions-compatible backend.

    Concrete backends subclass this and set only the endpoint/auth class
    attributes below; the invoke/tool/usage machinery is inherited. Exposes
    the same public interface as ClaudeAdapter: a mutable closure_tools dict
    and invoke().

    Class attributes (override per backend)
    ---------------------------------------
    DEFAULT_BASE_URL : str
        Endpoint used when no explicit base_url (config.yaml) is given.
    API_KEY : str | None
        Default API key when none is supplied / no API_KEY_ENV is set.
    API_KEY_ENV : str | None
        Environment variable consulted for the API key (env > API_KEY).

    Resolution precedence: explicit constructor arg > environment > default.
    """

    DEFAULT_BASE_URL: str = "http://localhost:11434/v1"
    API_KEY: str | None = "local"
    API_KEY_ENV: str | None = None

    @classmethod
    def select_native_tools(cls, agent_tools) -> list[str]:
        """Pick which of an agent's declared tools are NATIVE (CLI) tools.

        OpenAI-compatible backends run every tool through LangChain, so a tool
        is native unless it is one of the Python closure tools the node injects
        separately. (ClaudeAdapter overrides this with its own CLI-tool set.)
        """
        picked = [t for t in agent_tools
                  if t not in _CLOSURE_TOOL_NAMES and t != DEFAULT_TOOLS]
        if DEFAULT_TOOLS in agent_tools:
            picked += [t for t in DEFAULT_NATIVE_TOOLS if t not in picked]
        return picked

    def __init__(
        self,
        model: str,
        system_prompt: str,
        study_dir: Any = None,
        native_tools: list[str] | None = None,
        closure_tools: dict[str, Any] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        extra_mcp_servers: dict | None = None,
        extra_allowed_tools: list[str] | None = None,
        persistent: bool = False,
        max_history_pairs: int = 5,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.study_dir = Path(study_dir) if study_dir else None
        # Use 'native_tools' (not '_native_tool_names') so a leaf node's
        # sandboxed-Write setup can find it by the same attribute name as
        # ClaudeAdapter.
        self.native_tools: list[str] = list(native_tools or [])
        # Parity with ClaudeAdapter: "Default" is already expanded to the
        # native set by select_native_tools, and there is no init record.
        self.use_default_tools: bool = False
        self.on_init_tools: Any = None
        self.closure_tools: dict[str, Any] = dict(closure_tools or {})
        # Endpoint: explicit arg (from config.yaml) > class default. Auth: explicit
        # arg > environment > class default.
        self._base_url = base_url or self.DEFAULT_BASE_URL
        if api_key is None and self.API_KEY_ENV:
            api_key = os.environ.get(self.API_KEY_ENV)
        self._api_key = api_key if api_key is not None else self.API_KEY
        self.extra_mcp_servers: dict = dict(extra_mcp_servers or {})
        self.extra_allowed_tools: list[str] = list(extra_allowed_tools or [])
        # persistent and max_history_pairs kept for backward compatibility with
        # Agent subclasses and tests that read these attributes; not used
        # in the core invocation path (history is demand-driven via DelegationLog).
        self.persistent: bool = persistent
        self.max_history_pairs: int = max_history_pairs
        # Serialises calls on THIS adapter object; each delegation runs on its own copy().
        self._lock: threading.Lock = threading.Lock()
        # Summaries, keyed by the span they were built from. The pre_model_hook
        # runs on every model call inside a turn and usually wants to compact
        # the SAME span it just compacted; without this, "one generation per
        # compaction" would be an aspiration rather than a fact.
        self._summary_cache: dict[str, str] = {}
        # Built lazily so that closure_tools are fully populated before first
        # invoke().  Reset to None whenever native_tools or closure_tools change
        # so the next invoke() picks up the updated tool set.
        self._agent: Any = None
        # route_watcher is set by an orchestrating node; unused by these adapters
        # (create_react_agent runs the full tool loop to completion) but must
        # be present so Node's orchestration setup doesn't raise AttributeError.
        self.route_watcher: Any = None
        # Non-blocking raw-oracle nudge, capped per delegation (= per invoke).
        from .base import OracleNudgeBudget
        self._oracle_nudge = OracleNudgeBudget()
        self._notice_ctx: tuple = (None, None)
        # Populated after each invoke() with token counts for run-level accounting.
        self.last_usage: dict = {}
        # Parity with ClaudeAdapter's session-resumption capture (spec 12
        # item 3) -- always None here. This backend has no server-side
        # session to resume; review resumption for it replays the
        # RETAINED HISTORY instead (a separate mechanism, not this
        # attribute), exactly as the spec's own risk note says.
        self.last_session_id: str | None = None

    def copy(self) -> OpenAICompatibleAdapter:
        """An independent adapter for ONE delegation (see ClaudeAdapter.copy):
        own ``closure_tools``, ``_lock``, built agent, oracle-nudge budget,
        summary cache and per-call ``last_*`` state."""
        import copy as _copy

        from .base import OracleNudgeBudget
        twin = _copy.copy(self)
        twin.closure_tools = dict(self.closure_tools)
        twin.native_tools = list(self.native_tools)
        twin.extra_allowed_tools = list(self.extra_allowed_tools)
        twin.extra_mcp_servers = dict(self.extra_mcp_servers)
        twin._lock = threading.Lock()
        twin._summary_cache = {}
        twin._agent = None
        twin._oracle_nudge = OracleNudgeBudget()
        twin._notice_ctx = (None, None)
        twin.last_usage = {}
        twin.last_session_id = None
        return twin

    def _post_tool_context(self, tool_name: str, tool_input: dict) -> str | None:
        """Raw-oracle nudge plus queued campaign notices, same text the Claude
        backend's post-tool hook returns. Tool closures run on other threads,
        so the delegation context is the one bound at invoke time."""
        from ..infra.pending_notices import post_tool_context
        debug_dir, did = self._notice_ctx
        return post_tool_context(
            self._oracle_nudge, tool_name, tool_input, debug_dir, did)

    def _record_native_error(self, tool_name: str, message: str,
                             args: dict) -> None:
        """Write a native tool's error result as an ERROR_RETURN row."""
        from .base import append_diagnostic
        debug_dir, did = self._notice_ctx
        if debug_dir is None:
            return
        append_diagnostic(debug_dir, did or "", "ERROR_RETURN", message,
                          tool=tool_name, args=args, fault="agent")

    def _build_tools(self) -> list[Any]:
        import functools

        from langchain_core.tools import StructuredTool

        from ..prompts.tool_catalog import tool_summary
        native_map = _native_tool_map(
            self.study_dir, self._post_tool_context, self._record_native_error)
        tools: list[Any] = [
            native_map[name]
            for name in self.native_tools
            if name in native_map
        ]
        # _agent (and this tool list) is built once and cached forever
        # (see _invoke_once below) -- copy() deliberately returns self, so
        # every delegation to the same worker shares this one instance.
        # nodes/tools/routing.py reassigns closure_tools["Write"] to a
        # freshly-sandboxed closure before EACH delegation, expecting that
        # to take effect live. StructuredTool.from_function(fn, ...) below
        # would otherwise bake in whichever callable `fn` was THE FIRST
        # time this method ran, silently freezing every later delegation
        # to the first one's sandbox (confirmed for real: MathExpert on
        # Ollama, 3 separate _sandboxed_write ERROR_RETURNs all attributing
        # later delegations' writes to the first delegation's own folder —
        # BACKLOG #29). Indirect through a live dict lookup instead, so a
        # later closure_tools[name] reassignment is honoured immediately.
        for name, fn in self.closure_tools.items():
            def _dispatch(*args, _name=name, **kwargs):
                return self.closure_tools[_name](*args, **kwargs)
            _dispatch = functools.wraps(fn)(_dispatch)
            # The one-line summary, not the docstring: the full text is
            # already in the system prompt's <tools> section, and
            # from_function's default would send it a second time.
            tools.append(StructuredTool.from_function(
                _dispatch, name=name, description=tool_summary(fn, name)))
        # Inject MCP-equivalent tools for declared extra_allowed_tools.
        if self.extra_allowed_tools:
            lit_tools = _make_literature_tools()
            allowed = set(self.extra_allowed_tools)
            tools += [_guard_native(t, self._record_native_error) for t in lit_tools
                      if t.name in allowed]
        return tools

    def _build_agent(self) -> Any:
        from langchain_core.messages import SystemMessage
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent

        from ..prompts.tool_catalog import system_prompt_with_catalog
        max_output = self._resolve_max_output_tokens()
        llm = ChatOpenAI(
            model=self.model, base_url=self._base_url, api_key=self._api_key,
            max_tokens=max_output,
        )
        system = system_prompt_with_catalog(
            self.system_prompt, self.closure_tools)
        return create_react_agent(
            llm,
            self._build_tools(),
            prompt=SystemMessage(content=system),
            pre_model_hook=self._context_hook(system),
        )

    # -- context budget -----------------------------------------------------

    def _probe_context_window(self) -> int | None:
        """Ask the SERVER how much context it will accept, or None.

        OpenAI's ``/models`` is the common shape: vLLM puts ``max_model_len``
        on each model object and OpenRouter puts ``context_length``. Neither is
        in the OpenAI spec, so this is best-effort by construction — a backend
        that answers with neither simply returns None and the caller falls back
        to a declared value.
        """
        import requests
        try:
            r = requests.get(f"{self._base_url.rstrip('/')}/models", timeout=5)
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception:
            return None
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") not in (self.model, None):
                continue
            for field in ("max_model_len", "context_length",
                          "max_context_length"):
                val = entry.get(field)
                if isinstance(val, int) and val > 0:
                    return val
        return None

    def _resolve_context_window(self) -> tuple[int, str]:
        """``(window, source)`` — explicit setting, then the server, then a
        declared default.

        The source is returned, not just the number, because a run whose window
        came from a guess and a run whose window came from the server are not
        the same run. ``settings.py``'s precedence applies to the explicit
        channel, so a study can pin it and an ablation can sweep it.
        """
        cached = getattr(self, "_ctx_window", None)
        if cached is not None:
            return cached
        from ..runtime import settings
        explicit = settings.get_int("context_window", 0)
        if explicit > 0:
            out = (explicit, "setting")
        else:
            probed = self._probe_context_window()
            out = ((probed, "server") if probed
                   else (context_budget.DEFAULT_CONTEXT_WINDOW, "default"))
        self._ctx_window = out
        return out

    def _resolve_max_output_tokens(self) -> int | None:
        """Cap on ONE reply, or None for deliberately uncapped.

        This is NOT part of the ``context_trim`` feature, and that is on
        purpose. Trimming decides what the model SEES and is scaffolding whose
        value is an open question; this bounds what the server will DO and is
        a safety limit. Gating it on the same knob would mean the arm that
        answers "is trimming worth it" also removes the only thing stopping a
        turn generating for an hour, which is not the question being asked. A
        constant applied to both arms is not a confound.

        The resolved number is logged, so a run whose reply was cut short is
        distinguishable from a run whose model simply stopped.
        """
        from ..runtime import settings
        window, source = self._resolve_context_window()
        explicit = settings.get_int("max_output_tokens", 0)
        cap = context_budget.resolve_max_output_tokens(window, explicit)
        log.info(
            "max output tokens: %s (window %d from %s, setting %d)",
            cap if cap is not None else "uncapped", window, source, explicit,
        )
        return cap

    def _summarize(self, prompt: str) -> str:
        """One summary, from the model this run is already using.

        Deliberately not a second, smaller model. A fixed summariser would
        remove one confound (summary quality no longer varies with the arm)
        and introduce two: a dependency the study does not otherwise have, and
        a capability the run itself never had. The interesting hypothesis on
        cheap models is that scaffolding compensates for capability, and a
        27B run whose context is curated by a frontier model is not testing
        that.

        Goes straight to the chat model, NOT through the agent: a summary
        produced by a tool-loop could call tools, and a pre_model_hook that
        re-enters the agent is a recursion, not a hook.
        """
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=self.model, base_url=self._base_url, api_key=self._api_key,
            max_tokens=self._resolve_max_output_tokens(),
        )
        return str(llm.invoke([HumanMessage(content=prompt)]).content or "")

    def _context_hook(self, system_prompt: str):
        """A ``pre_model_hook`` that keeps one turn inside the served window.

        ``context_policy`` picks HOW. Both values manage the context; neither
        is "off", because an unmanaged context is not an experimental arm —
        it is the crash this subsystem was written to stop (Ollama evicts the
        original user turn and its renderer then rejects the request with
        ``500 no user query found in messages``).

        ``compact`` (the default) replaces the middle of the conversation with
        a summary, so a delegation's RESULT survives even when its prose does
        not. ``trim`` drops those messages instead: free, deterministic, and
        blind to what it is throwing away. The trade is fidelity against
        determinism — a summary can quietly restate a number, a dropped
        message obviously cannot — and it is recorded per run so an analysis
        can condition on it rather than assume it.

        The hook returns ``llm_input_messages``, which changes only what is
        SENT. Graph state keeps every message, so the transcript on disk stays
        complete and a compacted run is still fully auditable.
        """
        window, source = self._resolve_context_window()
        reserve = context_budget.estimate_tokens(system_prompt)
        policy = self._context_policy()

        seen: list = [None]

        def _hook(state):
            msgs = (state or {}).get("messages") or []
            if policy == "compact":
                kept, report = context_compaction.compact_to_budget(
                    msgs, context_window=window, reserve_tokens=reserve,
                    summarize=self._summarize, cache=self._summary_cache)
            else:
                kept, report = context_budget.trim_to_budget(
                    msgs, context_window=window, reserve_tokens=reserve)
            # The hook re-runs on every model call and graph state keeps the
            # full history, so an unchanged compaction re-fires each turn.
            # Record a compaction once, and again only when it moves.
            marker = (report.dropped, report.truncated)
            if report.fired and marker != seen[0]:
                seen[0] = marker
                from .base import (
                    append_transcript,
                    debug_enabled,
                    record_stream_diagnostic,
                )
                log.warning(
                    "context %s fired: %d message(s) removed, %d truncated "
                    "(%d -> %d est. tokens, budget %d, window %d from %s)",
                    policy, report.dropped, report.truncated, report.before,
                    report.after, report.budget, window, source,
                )
                summary = ""
                if policy == "compact":
                    header = context_compaction.summary_header(report.dropped)
                    for m in kept:
                        text = context_budget._text_of(m)
                        if text.startswith(header):
                            summary = text[len(header):]
                            break
                record_stream_diagnostic(
                    "CONTEXT_COMPACTED",
                    f"adda's {policy} context policy fired: "
                    f"{report.dropped} message(s) dropped, "
                    f"{report.truncated} truncated "
                    f"({report.before} -> {report.after} est. tokens).",
                    compaction_data={
                        "policy": policy, "window": window,
                        "window_source": source, **report.as_dict()},
                )
                if debug_enabled():
                    append_transcript({
                        "type": "ContextCompaction",
                        "text": f"policy={policy} window={window} source={source}",
                        "trim": report.as_dict(),
                        "policy": policy,
                        "summary": summary,
                    })
            return {"llm_input_messages": kept}

        return _hook

    @staticmethod
    def _context_policy() -> str:
        """``"compact"`` or ``"trim"``. An unknown value RAISES.

        Silently falling back to a default would make a typo'd arm run as the
        baseline and report as a null result, which is the one failure an
        ablation cannot afford.
        """
        from ..runtime import settings
        value = settings.get_str("context_policy", "compact").strip().lower()
        if value not in ("compact", "trim"):
            raise ValueError(
                f"context_policy must be 'compact' or 'trim', got {value!r}")
        return value

    def invoke(
        self, messages: list[dict], *,
        idle_timeout: float | None = None, retry_max: int | None = None,
        on_session_start: Any = None,
        on_session_end: Any = None,
    ) -> str:
        """Run one full agent turn; return final assistant text.

        Acquires _lock to serialize concurrent callers of this adapter object
        (each delegation has its own copy(), so they do not contend). Transient API/network
        failures are retried with exponential backoff (see retry_on_transient).

        ``idle_timeout`` / ``retry_max`` give short advisory side-calls a tight
        budget. ``idle_timeout`` is accepted for signature parity with the
        Claude backend (HTTP requests carry their own socket timeout, so it is
        not separately applied here); ``retry_max`` caps retries for this call.

        ``on_session_start``: see ClaudeAdapter.invoke's docstring -- called
        the instant ``_lock`` is actually acquired, so a caller queued
        behind another same-role delegation learns when its wait is really
        over. Best-effort.

        ``on_session_end``: see ClaudeAdapter.invoke's docstring -- called
        as ``on_session_end(session_id, usage)`` under the lock, also when
        the turn raised (``_capture_usage`` runs on the failure path too).
        This backend has no resumable sessions, so the id is always None.
        """
        from .base import retry_on_transient
        with self._lock:
            if on_session_start is not None:
                try:
                    on_session_start()
                except Exception:  # noqa: BLE001
                    pass
            # Cleared so a turn that raises before _capture_usage() runs
            # cannot hand its caller the PREVIOUS call's values.
            self.last_session_id = None
            self.last_usage = {}
            try:
                return retry_on_transient(
                    lambda: self._invoke_once(messages), max_attempts=retry_max,
                    on_retry=getattr(self, "on_retry", None))
            finally:
                if on_session_end is not None:
                    try:
                        on_session_end(
                            self.last_session_id, dict(self.last_usage or {}))
                    except Exception:  # noqa: BLE001
                        pass

    def _invoke_once(self, messages: list[dict]) -> str:
        """Core invoke logic — build agent if needed, run, return text."""
        # One invoke == one delegation's worker run; reset the per-delegation
        # nudge cap. The cached agent's tool closures read this live.
        self._oracle_nudge.reset()
        from .base import get_delegation_id, get_run_config_path
        _rc = get_run_config_path()
        self._oracle_nudge.run_config_path = _rc
        self._notice_ctx = (Path(_rc).parent if _rc else None,
                            get_delegation_id())
        if self._agent is None:
            self._agent = self._build_agent()

        lc_msgs = _to_lc_messages(messages)
        # A request with no user turn is malformed, and some providers answer
        # it with an opaque 500 ("no user query found in messages") that names
        # nothing and looks intermittent. It is reachable here: BOTH
        # converters between graph state and this call — nodes.parsing's
        # _to_adapter_messages and _to_lc_messages above — keep only
        # Human/AI messages and silently discard every other role, so a
        # history carrying only system/tool messages filters to an empty
        # list. thread_id is fresh per invoke, so nothing server-side
        # backfills the missing turn either.
        #
        # This check tests for a NON-EMPTY user turn, not merely for a
        # HumanMessage. Testing the type alone is what let the reported 500
        # through: HumanMessage(content="") is a HumanMessage, so the guard
        # passed it, and the provider then rejected the request for having no
        # user query. An empty turn and a missing one are the same thing to
        # the server, so they are the same thing here.
        #
        # Fail here instead, naming what arrived and what survived. This is
        # an instrument as much as a guard: an attributable error is how the
        # question of which shape a real run reaches gets answered rather
        # than argued.
        from langchain_core.messages import HumanMessage as _HumanMessage
        if not any(isinstance(m, _HumanMessage)
                   and str(m.content).strip() for m in lc_msgs):
            roles = [str(m.get("role", "user")) for m in messages]
            raise UserlessPayloadError(
                f"refusing to send a request with no user turn: "
                f"{len(messages)} message(s) in with role(s) {roles!r}, "
                f"{len(lc_msgs)} survived conversion. Two things are dropped "
                "by _to_adapter_messages and _to_lc_messages: roles other "
                "than user/human/ai/assistant, and turns whose content is "
                "empty after flattening. Empty content most often means a "
                "message carried only blocks with no readable payload."
            )
        cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}

        # DEBUG: capture reasoning + tool-calls (parity with Claude).
        #
        # Streamed, and recorded AS IT GOES, for two reasons that only show
        # up on a real run. A batch write after invoke() returns means a
        # delegation in flight has no transcript at all — a literature
        # review that runs for half an hour is completely unobservable while
        # it is the thing you most want to watch. And if the call raises
        # (a provider error mid-turn), a write placed after it never
        # happens, so the delegation that failed leaves no trace of what it
        # did before dying — exactly the case where the transcript is worth
        # most. Observed on run 20260906T122744: D002 failed inside
        # langchain_openai and D003 ran 25+ minutes, and neither had a
        # single line on disk.
        from .base import append_transcript, debug_enabled
        _debug = debug_enabled()
        result = None
        seen = 0

        def _flush(state) -> None:
            """Append whatever messages are new since the last flush."""
            nonlocal seen
            msgs = (state or {}).get("messages") or []
            for _m in msgs[seen:]:
                append_transcript({
                    "type": _m.__class__.__name__,
                    "text": str(getattr(_m, "content", "")),
                    "tools": getattr(_m, "tool_calls", None) or [],
                })
            seen = max(seen, len(msgs))

        # stream_mode="values" yields the whole state after each step, so the
        # last one seen is the final state invoke() would have returned — the
        # compiled graph's invoke() is itself that loop. Streaming is used on
        # BOTH paths (not just under --debug) so that a turn which dies
        # mid-loop still leaves its partial state behind: invoke() raises with
        # nothing in hand, and the tokens the server already generated for that
        # turn would be unrecoverable.
        try:
            raise_if_stopped("model call")
            for state in self._agent.stream(
                {"messages": lc_msgs}, config=cfg, stream_mode="values",
            ):
                result = state
                if _debug:
                    _flush(state)
                raise_if_stopped("model call")
        except Exception:
            # A failed turn keeps everything captured up to the failure.
            if _debug and result is not None:
                _flush(result)
            self._capture_usage(lc_msgs, result)
            raise
        if result is None:
            result = {"messages": []}
        all_msgs = result["messages"]
        last = all_msgs[-1]

        self._capture_usage(lc_msgs, result)

        return str(last.content)

    def _capture_usage(self, lc_msgs: list, result: dict | None) -> None:
        """Set ``last_usage`` from every model call this turn produced.

        Called on the success path AND from the failure path, because a turn
        that raises has still spent whatever the server generated before it
        died — dropping it undercounts by the whole turn, which on a run whose
        delegations fail is most of the run.

        ``result["messages"]`` is the WHOLE graph state, i.e. the input
        messages we passed in (``lc_msgs``) followed by everything the agent
        loop generated this call. The model is invoked once per tool-calling
        round trip, so a strategizer turn that loops several times before its
        final reply produces several AI messages, each carrying its own
        ``usage_metadata`` — reading only the last one silently dropped every
        intermediate call's tokens.

        Summing must stop at the input boundary: ``thread_id`` is fresh per
        invoke, so the graph never carries earlier turns' messages into this
        call, and ``lc_msgs`` is exactly the prefix the graph started from —
        the reducer only ever appends. Slicing at ``len(lc_msgs)`` therefore
        counts exactly the messages this invocation produced, never the
        history that seeded it; summing over the whole state instead would
        double-count that history on every call and inflate every total.

        Only AI messages carry usage_metadata (tool/human messages don't), so
        anything without it contributes zero rather than raising.
        """
        new_msgs = ((result or {}).get("messages") or [])[len(lc_msgs):]
        total_in = total_out = total_cache_read = total_cache_creation = 0
        total_fresh = 0
        call_inputs: list[int] = []
        for _m in new_msgs:
            meta = getattr(_m, "usage_metadata", None)
            if not meta:
                continue
            call_inputs.append(meta.get("input_tokens", 0) or 0)
            total_in += meta.get("input_tokens", 0) or 0
            total_out += meta.get("output_tokens", 0) or 0
            details = meta.get("input_token_details") or {}
            total_cache_read += details.get("cache_read", 0) or 0
            total_cache_creation += details.get("cache_creation", 0) or 0
            # prompt_tokens is the WHOLE prompt, cached part included; the
            # schema's fresh_input is the rest. Per call, so one call's cache
            # figure can never offset another's.
            total_fresh += max(
                (meta.get("input_tokens", 0) or 0)
                - (details.get("cache_read", 0) or 0)
                - (details.get("cache_creation", 0) or 0), 0)
        self.last_usage = {
            **normalized_usage(
                fresh_input=total_fresh, cache_read=total_cache_read,
                cache_write=total_cache_creation, output=total_out),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read_input_tokens": total_cache_read,
            "cache_creation_input_tokens": total_cache_creation,
            "total_cost_usd": None,  # not available from open-weight/self-hosted
            # input_tokens above is a sum over the turn's model calls; these
            # show what one call read: how many calls, the first, the largest.
            **call_shape(call_inputs),
        }
