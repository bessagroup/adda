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
    monkeypatch.setattr("adda._src.runtime.settings.get_str", lambda k, d="": d)
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


# ---- a rejected key: fall back keyless once, say so once ---------------------

@pytest.fixture
def key_state(monkeypatch):
    from adda._src.agents.literature_tools import semantic_scholar as s2
    s2._reset_key_state()
    monkeypatch.setattr(throttle, "_SS_MIN_INTERVAL", 0.0)
    yield s2
    s2._reset_key_state()


class _Lib:
    """Stands in for semanticscholar.SemanticScholar: 403 for a key."""

    built: list = []

    def __init__(self, api_key=None, retry=True):
        self.api_key = api_key
        _Lib.built.append(api_key)

    def get_paper(self, pid, fields=None):
        if self.api_key:
            raise PermissionError("403")
        return "paper"


def test_first_403_with_a_key_falls_back_keyless_once_and_reports_once(
        monkeypatch, key_state):
    import semanticscholar
    _Lib.built = []
    key_state._reset_key_state()  # drop clients left live by earlier tests
    monkeypatch.setattr(semanticscholar, "SemanticScholar", _Lib)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "bad")
    monkeypatch.setattr("adda._src.runtime.settings.get_str", lambda k, d="": d)
    client = get_semantic_scholar_client()
    assert throttle._throttled_ss(client.get_paper, "x") == "paper"
    assert _Lib.built == ["bad", None]          # rebuilt keyless, retried once
    assert throttle._throttled_ss(client.get_paper, "y") == "paper"
    assert _Lib.built == ["bad", None]          # not rebuilt again
    assert get_semantic_scholar_client().keyed is False   # new holders keyless
    ev = key_state.KEY_EVENTS.pop_diagnostic_event()
    assert ev[0] == "LIT_KEY_REJECTED" and ev[2] == {"source": "semantic_scholar"}
    assert key_state.KEY_EVENTS.pop_diagnostic_event() is None


def test_403_without_a_key_is_not_retried(monkeypatch, key_state):
    import semanticscholar
    calls = []

    class _NoKey403(_Lib):
        def get_paper(self, pid, fields=None):
            calls.append(1)
            raise PermissionError("403")

    monkeypatch.setattr(semanticscholar, "SemanticScholar", _NoKey403)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.setattr(
        "adda._src.runtime.settings.get_str", lambda k, d="": d)
    client = get_semantic_scholar_client()
    with pytest.raises(PermissionError):
        throttle._throttled_ss(client.get_paper, "x")
    assert calls == [1]
    assert key_state.KEY_EVENTS.pop_diagnostic_event() is None


def test_forbidden_wording_does_not_claim_quota_exhaustion(monkeypatch, key_state):
    import semanticscholar
    from adda._src.agents.literature_tools.semantic_scholar import (
        build_semantic_scholar_closures,
    )

    class _Always403(_Lib):
        def get_paper(self, pid, fields=None):
            raise PermissionError("403")

    monkeypatch.setattr(semanticscholar, "SemanticScholar", _Always403)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.setattr(
        "adda._src.runtime.settings.get_str", lambda k, d="": d)
    out = build_semantic_scholar_closures()[
        "get_semantic_scholar_paper_details"]("x")
    assert out.startswith("ERROR:") and "refused" in out
    assert "exhausted" not in out
