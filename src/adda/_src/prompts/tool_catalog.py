"""Forward-compatible tool catalog.

The prompt's ``<tools>`` section is GENERATED from the live closure set, so it
can never drift from the actual tools an agent has, and a newly-registered tool
self-documents with no hand-edit. The tool owns its guidance: its docstring is
the description, plus optional usage examples attached via ``@tool_examples``.

ONE ROUTE, BOTH BACKENDS
    A tool's full description reaches the model exactly once, in this
    ``<tools>`` section; the tool-use API definition carries only its
    one-line summary (``tool_summary``) and its parameter schema. The two
    backends used to disagree: Claude got the one line in the API and the full
    text here, the OpenAI-compatible backend got the full text in BOTH — every
    tool's documentation twice, on every model call.

    Why the full text lives here rather than in the API definition: Claude
    Code cuts an MCP tool's description at 2,048 characters, silently, and the
    Claude backend's closures are MCP tools. QueryStore's description alone is
    past that. The system prompt has no such cap, so it is the one place a
    full description is guaranteed to arrive whole.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping

__all__ = [
    "tool_examples", "tool_summary", "render_tool_catalog",
    "qualify_tool_mentions",
]


def tool_examples(*examples: str):
    """Attach usage examples to a tool closure (the tool owns them).

    ``render_tool_catalog`` renders them under the tool. Closures without this
    decorator simply have no examples — rendering still works.
    """
    def deco(fn: Callable) -> Callable:
        fn._tool_examples = list(examples)
        return fn
    return deco


def tool_summary(fn: Callable, name: str = "") -> str:
    """The one line a tool-use API definition carries: the docstring's first
    line. The full description is in ``<tools>`` (see the module docstring)."""
    doc = inspect.cleandoc(getattr(fn, "__doc__", None) or "")
    return (doc.split("\n")[0].strip() if doc else "") or name


def render_tool_catalog(closure_tools: dict[str, Callable]) -> str:
    """Render a ``<tools>`` block from the LIVE closure dict.

    Per tool: the exact registered name (the dict key — so a tool can never be
    missing or misnamed in the prompt), its docstring (``inspect.cleandoc``-ed,
    so the rendered text does not depend on how deeply the closure happens to
    be nested in source), and its examples. Does
    NOT re-emit parameter types/schema — the backends already carry those to the
    model via the tool-use API. Deterministic (sorted by name) for cache
    stability. Returns "" when there are no closures.
    """
    if not closure_tools:
        return ""
    blocks: list[str] = []
    for name in sorted(closure_tools):
        fn = closure_tools[name]
        doc = inspect.cleandoc(getattr(fn, "__doc__", None) or "") or "(no description)"
        block = f"### {name}\n{doc}"
        examples = getattr(fn, "_tool_examples", None)
        if examples:
            block += "\nExamples:\n" + "\n".join(f"  - {e}" for e in examples)
        blocks.append(block)
    return (
        "\n\n<tools>\n"
        "Your available tools, generated from the live tool set (AUTHORITATIVE "
        "— these exact names are the ones you call; anything not listed here is "
        "not available):\n\n"
        + "\n\n".join(blocks)
        + "\n</tools>"
    )


def system_prompt_with_catalog(base_prompt: str, closure_tools: dict) -> str:
    """Base prompt + the generated tool catalog. Computed at prompt-assembly
    time so it always reflects the current closures; never mutates state."""
    from ..runtime.features import resolve_gates
    return resolve_gates(base_prompt + render_tool_catalog(closure_tools))


def qualify_tool_mentions(text: str, names: Mapping[str, str]) -> str:
    """Rewrite bare tool names in ``text`` to their callable names.

    ``names`` maps each bare closure name to the name the backend exposes it
    under. A backend that exposes a tool only under a qualified name (the
    Claude SDK: ``mcp__<server>__<tool>``) must not have prose that names it
    bare: the model calls what the prose says, and a bare name is "No such
    tool" — or, for ``Write``, the CLI's own disabled native tool.

    Only occurrences that are unambiguously a tool reference are rewritten:
    a name written as a call (``Name(``) or in backticks, or a multi-word
    identifier (``ReportEvals``) that cannot be ordinary English. A lone
    capitalised word without call syntax (``Wait for the reviewer``) is left
    alone, since it is as likely a verb or a status as a tool. Names already
    qualified (preceded by ``_``) and attribute access (``x.Name``) are never
    touched.
    """
    if not names:
        return text
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pattern = re.compile(rf"(?<![\w.])({alt})(?![\w])")

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        call_shape = after == "(" or (before == "`" and after in "`(")
        multi_word = re.search(r"[a-z][A-Z]", name) is not None
        return names[name] if call_shape or multi_word else name

    return pattern.sub(_sub, text)
