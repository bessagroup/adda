"""arXiv ORs bare words: "coilable longeron mast ..." returned tokamak papers."""
from __future__ import annotations

import sys
import types

from adda._src.agents.literature_tools.discovery import (
    _arxiv_queries,
    arxiv_search,
)


def test_bare_words_become_a_fielded_and_query():
    qs = _arxiv_queries("coilable longeron mast, deployable boom")
    assert qs[0] == ("all:coilable AND all:longeron AND all:mast "
                     "AND all:deployable AND all:boom")
    assert qs[-1] == "coilable longeron mast, deployable boom"
    assert len(qs) == 3 and qs[1].count("AND") < qs[0].count("AND")


def test_stopwords_dropped_and_arxiv_syntax_left_alone():
    assert _arxiv_queries("buckling of the shell")[0] == (
        "all:buckling AND all:shell")
    assert _arxiv_queries('ti:"coilable mast" AND cat:cs.CE') == [
        'ti:"coilable mast" AND cat:cs.CE']
    assert _arxiv_queries("buckling") == ["buckling"]


def test_zero_results_relaxes_then_stops_at_the_first_hit(monkeypatch):
    seen = []

    class _Search:
        def __init__(self, query, max_results):
            self.query = query

    class _Client:
        def results(self, search):
            seen.append(search.query)
            if search.query.count("AND") >= 3:
                return iter([])
            return iter([types.SimpleNamespace(
                entry_id="http://arxiv.org/abs/2401.00001v1", title="T",
                published=None, authors=[], doi=None, pdf_url="",
                summary="s")])

    fake = types.SimpleNamespace(Client=_Client, Search=_Search)
    monkeypatch.setitem(sys.modules, "arxiv", fake)
    out = arxiv_search("coilable longeron mast deployable boom", 5)
    assert [r["arxiv"] for r in out] == ["2401.00001"]
    assert len(seen) == 2 and seen[0].count("AND") == 4
