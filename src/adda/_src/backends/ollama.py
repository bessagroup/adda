"""Ollama-backed adapter for the f3dasm LangGraph agentic runtime.

Ollama exposes an OpenAI-compatible endpoint, so this is a thin subclass of
``OpenAICompatibleAdapter`` (see backends/openai_compatible.py) that only
declares Ollama's local endpoint default + env override. All invoke/tool/usage
machinery is inherited and shared with the OpenRouter and vLLM backends.

The module re-exports the shared tool-builder helpers
(``_build_arxiv_closures``, ``_make_literature_tools``, ``_native_tool_map``,
``_to_lc_messages``, the ``_make_*_tool`` factories) so existing
``from ...backends.ollama import _build_arxiv_closures`` style imports keep
working.
"""

from __future__ import annotations

from .openai_compatible import (
    OpenAICompatibleAdapter,
    _build_arxiv_closures,
    _make_bash_tool,
    _make_edit_tool,
    _make_glob_tool,
    _make_grep_tool,
    _make_literature_tools,
    _make_read_tool,
    _make_write_tool,
    _native_tool_map,
    _to_lc_messages,
)

__all__ = [
    "OllamaAdapter",
    "_build_arxiv_closures",
    "_make_bash_tool",
    "_make_edit_tool",
    "_make_glob_tool",
    "_make_grep_tool",
    "_make_literature_tools",
    "_make_read_tool",
    "_make_write_tool",
    "_native_tool_map",
    "_to_lc_messages",
]


class OllamaAdapter(OpenAICompatibleAdapter):
    """Adapter for Ollama-served open-weight models.

    Uses ChatOpenAI pointed at Ollama's local OpenAI-compatible endpoint
    (default ``http://localhost:11434/v1``, overridable via ``base_url`` in
    config.yaml). Ollama needs no real auth, so the API key is
    the conventional placeholder ``"local"``.
    """

    DEFAULT_BASE_URL = "http://localhost:11434/v1"
    API_KEY = "local"
    API_KEY_ENV = None

    def _probe_context_window(self) -> int | None:
        """Ollama's SERVED window, which is not the model's trained length.

        ``/v1/models`` reports neither, so this uses Ollama's native
        ``/api/show``. The distinction matters more here than anywhere: a
        Modelfile\'s ``num_ctx`` is what the server will actually accept, and
        Ollama\'s default is far below what any of these models were trained
        for. Taking ``context_length`` from ``model_info`` would report the
        trained window, sail past the real limit, and produce exactly the
        server-side truncation this trimming exists to prevent — so the
        parameter wins and the trained length is only a fallback.
        """
        import re

        import requests
        root = self._base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        try:
            r = requests.post(f"{root}/api/show",
                              json={"model": self.model}, timeout=5)
            r.raise_for_status()
            body = r.json()
        except Exception:
            return None
        # `parameters` is the Modelfile block, e.g. "num_ctx   262144"
        params = body.get("parameters")
        if isinstance(params, str):
            m = re.search(r"^\s*num_ctx\s+(\d+)", params, re.M)
            if m:
                return int(m.group(1))
        info = body.get("model_info")
        if isinstance(info, dict):
            for key, val in info.items():
                if key.endswith(".context_length") and isinstance(val, int):
                    return val
        return None
