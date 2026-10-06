"""Wet integration test: all three literature sources, real network.

Each provider's search on its own, then SearchPapers -- the one call a real
literature reviewer makes, which asks all three at once and merges them.

Marked `integration` — NOT run in CI (no `SEMANTIC_SCHOLAR_API_KEY` there,
and CI shouldn't depend on live third-party APIs anyway). Run manually with:
    uv run pytest tests/test_literature_sources_wet.py -v -s --no-cov -m integration

Requires network + the `arxiv`, `semanticscholar`, and `requests` packages
(already project dependencies). A configured SEMANTIC_SCHOLAR_API_KEY is what actually
exercises the authenticated path end to end — the whole reason this test
exists is to catch a REGRESSION exactly like commits 693a971/b5e655f (S2
either 403ing outright, or the pacing interval silently under-pacing the
authenticated tier) the moment it happens, instead of discovering it days
later on a real run.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

QUERY = "graph neural network surrogate model"


def _study() -> Path:
    study = Path(tempfile.mkdtemp(prefix="adda_lit_sources_wet_"))
    (study / "runs").mkdir()
    return study


def _build_tools():
    """The per-provider calls (not agent tools)."""
    from adda._src.agents.literature_tools import build_literature_providers
    return build_literature_providers(_study())


@pytest.mark.integration
def test_arxiv_search_returns_real_results():
    tools = _build_tools()
    papers = tools["arxiv_search"](QUERY, 3)
    assert papers, f"arxiv returned no results for {QUERY!r}"
    assert all(p["arxiv"] for p in papers), papers


@pytest.mark.integration
def test_openalex_search_returns_real_results():
    tools = _build_tools()
    out = tools["search_openalex"](query=QUERY, n_results=3)
    assert not out.startswith("ERROR"), f"OpenAlex search failed: {out!r}"
    papers = json.loads(out)
    assert papers, f"OpenAlex returned zero results for {QUERY!r}"
    assert all("title" in p for p in papers)


@pytest.mark.integration
def test_semantic_scholar_search_returns_real_results():
    """This is the one that actually needs a working key to prove anything
    beyond 'the unauthenticated tier hasn't been exhausted right now' — see
    the module docstring. Skips (not fails) if the semanticscholar package
    isn't installed, matching every other S2 test's convention."""
    tools = _build_tools()
    if "search_semantic_scholar" not in tools:
        pytest.skip("semanticscholar not installed")
    out = tools["search_semantic_scholar"](query=QUERY, num_results=3)
    assert not out.startswith("ERROR"), (
        f"Semantic Scholar search failed: {out!r} — if this is a 403/"
        "cooldown, check SEMANTIC_SCHOLAR_API_KEY is actually configured "
        "and not being silently under-paced (regression class: 693a971, "
        "b5e655f)"
    )
    papers = json.loads(out)
    assert papers, f"Semantic Scholar returned zero results for {QUERY!r}"
    assert all("title" in p for p in papers)


@pytest.mark.integration
def test_search_papers_asks_all_three_like_a_real_agent():
    """The agent-facing call: one SearchPapers, every provider answering."""
    from adda._src.agents.literature import LiteratureReviewAgent
    tools = LiteratureReviewAgent().build_closure_tools(_study())
    out = tools["SearchPapers"](QUERY, limit=3)
    header, body = out.split("\n", 1)
    for provider in ("arxiv", "semantic_scholar", "openalex"):
        assert f"{provider} " in header and "results" in header, header
    assert "failed" not in header and "timed out" not in header, header
    assert json.loads(body), f"no merged results for {QUERY!r}"
