"""Every Semantic Scholar tool path builds its client through one function and
routes calls through ``_throttled_ss`` (the sole retry authority), with the
library's own 429 retry storm disabled."""
from __future__ import annotations

import types

import pytest

pytest.importorskip("semanticscholar")
pytest.importorskip("langchain_core")

from adda._src.agents.literature_tools import throttle  # noqa: E402
from adda._src.agents.literature_tools.semantic_scholar import (  # noqa: E402
    get_semantic_scholar_client,
)
from adda._src.backends.openai_compatible import _make_literature_tools  # noqa: E402

_S2_TOOLS = (
    "mcp__semanticscholar__get_semantic_scholar_paper_details",
    "mcp__semanticscholar__get_semantic_scholar_citations_and_references",
)


def test_the_one_builder_disables_the_library_retry_and_reads_config(monkeypatch):
    built = []
    import semanticscholar
    monkeypatch.setattr(
        semanticscholar, "SemanticScholar",
        lambda **kw: built.append(kw) or types.SimpleNamespace())
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k123")
    get_semantic_scholar_client()
    assert built == [{"api_key": "k123", "retry": False}]


def test_openai_compatible_s2_tools_go_through_throttled_ss(monkeypatch):
    built, throttled = [], []
    paper = types.SimpleNamespace(
        title="T", year=2019, venue="V", citationCount=1,
        influentialCitationCount=0, tldr=None, authors=[], references=[],
        citations=[])

    class _Fake:
        def __init__(self, **kw):
            built.append(kw)

        def get_paper(self, pid, fields=None):
            raise AssertionError("called the client directly, not via _throttled_ss")

    import semanticscholar
    monkeypatch.setattr(semanticscholar, "SemanticScholar", _Fake)
    monkeypatch.setattr(
        throttle, "_throttled_ss",
        lambda fn, *a, **kw: throttled.append((fn.__name__, a)) or paper)
    tools = {t.name: t for t in _make_literature_tools()}
    assert built and all(kw["retry"] is False for kw in built)
    for name in _S2_TOOLS:
        tools[name].invoke({"paper_id": "ARXIV:1"})
    assert [n for n, _ in throttled] == ["get_paper", "get_paper"]
