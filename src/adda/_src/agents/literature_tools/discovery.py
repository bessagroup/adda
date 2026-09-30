"""The literature reviewer's discovery tools: find papers, walk their citation
graph, read one paper's record.

WHY THREE TOOLS AND NOT THIRTEEN
    The reviewer used to hold one tool per provider endpoint -- three searches,
    two citation lookups on OpenAlex, one on Semantic Scholar, recommendations,
    author details, an arXiv category listing -- plus an async pool
    (``wait=False`` and a CollectSearches step) so the searches could run
    concurrently. The agent had to know which provider was good at what, fan
    out by hand, collect, and merge duplicates itself. Everything it needed to
    decide that is known here, so it is done here:

    - ``SearchPapers`` asks every provider at once, merges the same paper found
      twice, and says in its first line how each provider did -- ok, throttled,
      failed or timed out -- so a provider failing is visible without being a
      separate tool the agent has to notice was never answered.
    - ``CitationGraph`` walks citing papers, references or similar papers, from
      whichever provider can answer, and says which one did.
    - ``PaperDetails`` is one paper's record.

    The per-provider calls stay as plain functions (``openalex.py``,
    ``semantic_scholar.py``) so each keeps its own tests; only this layer is
    registered as tools.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, wait

from ...prompts.tool_catalog import tool_examples
from .throttle import _cap_result

log = logging.getLogger(__name__)

#: How long SearchPapers waits for any one provider. A provider past this is
#: reported as timed out and its late answer is dropped; the others' results
#: are returned without it.
PROVIDER_TIMEOUT_S = 120.0

_SOURCES = ("arxiv", "semantic_scholar", "openalex")
_ARXIV_DOI = "10.48550/arxiv."
_ARXIV_ABS = re.compile(r"arxiv\.org/(?:abs|pdf)/([^/?#]+?)(?:v\d+)?(?:\.pdf)?$", re.I)


# ── Provider adapters: each returns (records, status) ────────────────────────

_ARXIV_STOP = frozenset(
    "a an the of for in on and or with to by from via using is are as at".split())
_ARXIV_FIELDED = re.compile(r"\b(?:all|ti|au|abs|co|jr|cat|rn|id):|\b(?:AND|OR|ANDNOT)\b|\"")


def _arxiv_queries(text: str) -> list[str]:
    """Queries to try in order: every word required (arXiv ORs bare words,
    so "coilable longeron mast" returned tokamak papers), then the longest
    ~60% of the words, then the text as given. A query the caller already
    wrote in arXiv syntax is used as-is."""
    if _ARXIV_FIELDED.search(text):
        return [text]
    words = list(dict.fromkeys(
        w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", text.lower())
        if w not in _ARXIV_STOP))
    if len(words) < 2:
        return [text]
    def _and(ws: list[str]) -> str:
        return " AND ".join(f"all:{w}" for w in ws)
    keep = max(2, -(-len(words) * 3 // 5))
    longest = set(sorted(words, key=len, reverse=True)[:keep])
    relaxed = [w for w in words if w in longest]
    out = [_and(words)]
    if len(relaxed) < len(words):
        out.append(_and(relaxed))
    out.append(text)
    return out


def arxiv_search(query: str, limit: int) -> list[dict]:
    """arXiv search as records (the arXiv tool returned display text)."""
    import arxiv as _arxiv
    client = _arxiv.Client()
    results: list = []
    for q in _arxiv_queries(query):
        results = list(client.results(
            _arxiv.Search(query=q, max_results=int(limit))))
        if results:
            break
    out = []
    for r in results:
        m = _ARXIV_ABS.search(r.entry_id or "")
        out.append({
            "title": r.title, "year": r.published.year if r.published else "",
            "authors": ", ".join(a.name for a in (r.authors or [])[:3]),
            "arxiv": m.group(1) if m else "", "doi": r.doi or "",
            "pdf_url": r.pdf_url or "", "abstract": (r.summary or "")[:300],
        })
    return out


def _from_json(text: str) -> list[dict]:
    """A provider function's JSON result, or its "ERROR: …" string raised."""
    if isinstance(text, str) and text.startswith("ERROR"):
        raise RuntimeError(text.removeprefix("ERROR: ").removeprefix("ERROR:"))
    return json.loads(text)


def _openalex_records(rows: list[dict]) -> list[dict]:
    out = []
    for w in rows:
        doi = (w.get("doi") or "").lower()
        out.append({
            "title": w.get("title", ""), "year": w.get("year", ""),
            "authors": w.get("authors", ""), "doi": doi,
            "arxiv": doi[len(_ARXIV_DOI):] if doi.startswith(_ARXIV_DOI) else "",
            "openalex": (w.get("id") or "").replace("https://openalex.org/", ""),
            "citations": w.get("cited_by_count"),
            "pdf_url": w.get("pdf_url", ""), "abstract": w.get("abstract", ""),
        })
    return out


def _s2_records(rows: list[dict]) -> list[dict]:
    out = []
    for p in rows:
        ext = p.get("externalIds") or {}
        authors = p.get("authors") or []
        out.append({
            "title": p.get("title", ""), "year": p.get("year", ""),
            "authors": ", ".join(authors[:3]) if isinstance(authors, list) else authors,
            "doi": (ext.get("DOI") or "").lower(), "arxiv": ext.get("ArXiv", ""),
            "s2": p.get("paperId", ""), "citations": p.get("citationCount"),
            "pdf_url": p.get("pdf_url", ""), "abstract": p.get("abstract", ""),
        })
    return out


# ── Merging ──────────────────────────────────────────────────────────────────

def _keys(rec: dict) -> list[str]:
    keys = []
    if rec.get("doi") and not rec["doi"].startswith(_ARXIV_DOI):
        keys.append("doi:" + rec["doi"])
    if rec.get("arxiv"):
        keys.append("arxiv:" + re.sub(r"v\d+$", "", rec["arxiv"]).lower())
    title = re.sub(r"[^a-z0-9]", "", (rec.get("title") or "").lower())
    if len(title) > 20:
        keys.append("title:" + title)
    return keys


def _paper_id(rec: dict) -> str:
    """The id to pass to CitationGraph / PaperDetails / CorpusAdd's arxiv_id."""
    if rec.get("arxiv"):
        return re.sub(r"v\d+$", "", rec["arxiv"])
    if rec.get("doi"):
        return rec["doi"]
    return rec.get("openalex") or rec.get("s2") or ""


def merge_records(by_source: dict[str, list[dict]]) -> list[dict]:
    """One entry per paper, however many providers returned it.

    Ordered by the best rank any provider gave it, then by how many found it:
    the providers' own relevance ranking is the signal, and agreement between
    them breaks ties.
    """
    merged: list[dict] = []
    index: dict[str, int] = {}
    for source, recs in by_source.items():
        for rank, rec in enumerate(recs):
            hit = next((index[k] for k in _keys(rec) if k in index), None)
            if hit is None:
                merged.append({"found_in": [], "_rank": rank})
                hit = len(merged) - 1
            entry = merged[hit]
            entry["_rank"] = min(entry["_rank"], rank)
            if source not in entry["found_in"]:
                entry["found_in"].append(source)
            for field, value in rec.items():
                if field == "citations":
                    if value is not None:
                        entry["citations"] = max(entry.get("citations") or 0, int(value))
                elif value and not entry.get(field):
                    entry[field] = value
            for k in _keys(entry):
                index.setdefault(k, hit)
    merged.sort(key=lambda e: (e["_rank"], -len(e["found_in"])))
    out = []
    for e in merged:
        e.pop("_rank")
        out.append({"paper_id": _paper_id(e), **{k: v for k, v in e.items() if v not in ("", None)}})
    return out


# ── The tools ────────────────────────────────────────────────────────────────

def build_discovery_closures(providers: dict) -> dict:
    """SearchPapers, CitationGraph and PaperDetails over the provider calls in
    *providers* (as ``build_literature_providers`` returns them). A provider
    whose library is missing is simply absent from every status line."""
    locks = {s: threading.Lock() for s in _SOURCES}

    def _run(source, fn, *args):
        with locks[source]:          # same-provider calls serialize
            return fn(*args)

    searchers = {}
    if "arxiv_search" in providers:
        searchers["arxiv"] = providers["arxiv_search"]
    if "search_semantic_scholar" in providers:
        searchers["semantic_scholar"] = lambda q, n: _s2_records(
            _from_json(providers["search_semantic_scholar"](q, n)))
    if "search_openalex" in providers:
        searchers["openalex"] = lambda q, n: _openalex_records(
            _from_json(providers["search_openalex"](q, n)))

    @tool_examples(
        "SearchPapers('lattice metamaterial buckling under compression')",
        "SearchPapers('physics-informed neural networks', limit=5, sources='openalex,arxiv')",
    )
    def SearchPapers(query: str, limit: int = 10, sources: str = "") -> str:
        """Search the literature databases -- arXiv, Semantic Scholar and
        OpenAlex -- at once, for papers you do not have yet.

        Every provider is asked in parallel; the same paper found by several is
        merged into one entry (`found_in` lists which). `limit` is results per
        provider. `sources` restricts the providers (comma-separated), e.g.
        'openalex' for journal and conference venues arXiv does not cover.

        The first line reports each provider -- its result count, or why it
        gave none (throttled, failed, timed out). A provider that failed says
        nothing about whether the paper exists; the others' results stand.
        Each entry's `paper_id` is what CitationGraph, PaperDetails and
        CorpusAdd(arxiv_id=...) take; `pdf_url`, when present, is what
        CorpusAdd downloads."""
        picked = [s.strip() for s in str(sources or "").split(",") if s.strip()]
        wanted = picked or list(searchers)
        unknown = [s for s in wanted if s not in _SOURCES]
        if unknown:
            return f"ERROR: unknown source(s) {unknown}; choose from {list(_SOURCES)}."
        status, results = {}, {}
        for s in wanted:
            if s not in searchers:
                status[s] = "not installed"
        live = [s for s in wanted if s in searchers]
        pool = ThreadPoolExecutor(max_workers=max(len(live), 1))
        futures = {pool.submit(_run, s, searchers[s], query, int(limit)): s for s in live}
        done, _ = wait(futures, timeout=PROVIDER_TIMEOUT_S)
        pool.shutdown(wait=False, cancel_futures=True)
        for fut, s in futures.items():
            if fut not in done:
                status[s] = f"timed out after {PROVIDER_TIMEOUT_S:.0f}s"
                continue
            try:
                results[s] = fut.result()
                status[s] = f"{len(results[s])} results"
            except Exception as exc:  # noqa: BLE001
                status[s] = f"failed — {exc}"
        for s, msg in status.items():
            if not msg.endswith("results"):
                log.warning("SearchPapers: %s %s", s, msg)
        header = "Searched: " + " · ".join(f"{s} {status[s]}" for s in wanted)
        papers = merge_records({s: results[s] for s in live if s in results})
        return _cap_result(header + "\n" + json.dumps(papers, indent=2))

    @tool_examples(
        "CitationGraph('1706.03762')",
        "CitationGraph('10.1016/j.cma.2020.113029', direction='references')",
        "CitationGraph('1706.03762', direction='similar', limit=10)",
    )
    def CitationGraph(paper_id: str, direction: str = "citing", limit: int = 20) -> str:
        """Walk the citation graph from one paper.

        direction='citing' → papers that cite it (most-cited first);
        'references' → the papers it cites; 'similar' → semantically similar
        papers with no citation link. paper_id is a `paper_id` from
        SearchPapers: an arXiv id, a DOI, or an OpenAlex W-id.

        OpenAlex answers citing/references first and Semantic Scholar is the
        fallback; 'similar' is Semantic Scholar only. The first line says
        which provider answered, and why the other did not."""
        direction = (direction or "").strip().lower()
        if direction not in ("citing", "references", "similar"):
            return ("ERROR: direction must be 'citing', 'references' or "
                    f"'similar', not {direction!r}.")
        n = int(limit)
        failures = []
        if direction == "similar":
            if "get_semantic_scholar_recommendations" not in providers:
                return "ERROR: Semantic Scholar is not available in this run."
            try:
                rows = _from_json(_run("semantic_scholar",
                                       providers["get_semantic_scholar_recommendations"],
                                       paper_id, n))
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: semantic_scholar failed — {exc}"
            return _cap_result("Source: semantic_scholar\n"
                               + json.dumps(merge_records({"semantic_scholar": _s2_records(rows)}), indent=2))
        if "resolve_openalex_id" in providers:
            try:
                wid = _run("openalex", providers["resolve_openalex_id"], paper_id)
                if wid.startswith("ERROR"):
                    raise RuntimeError(wid.removeprefix("ERROR: "))
                fn = providers["get_openalex_citations" if direction == "citing"
                               else "get_openalex_references"]
                args = (wid, n) if direction == "citing" else (wid,)
                raw = _run("openalex", fn, *args)
                rows = [] if raw.startswith("No references") else _from_json(raw)
                recs = _openalex_records(rows)[:n]
                return _cap_result("Source: openalex\n"
                                   + json.dumps(merge_records({"openalex": recs}), indent=2))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"openalex failed — {exc}")
        if "get_semantic_scholar_citations_and_references" in providers:
            try:
                both = _from_json(_run(
                    "semantic_scholar",
                    providers["get_semantic_scholar_citations_and_references"],
                    paper_id))
                rows = both["citations" if direction == "citing" else "references"][:n]
                recs = [{"title": r.get("title", ""), "s2": r.get("paperId", "")}
                        for r in rows]
                return _cap_result(
                    "Source: semantic_scholar (" + "; ".join(failures) + ")\n"
                    + json.dumps(merge_records({"semantic_scholar": recs}), indent=2))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"semantic_scholar failed — {exc}")
        return "ERROR: no provider could answer — " + "; ".join(failures or ["none available"])

    @tool_examples(
        "PaperDetails('1706.03762')",
        "PaperDetails('10.1016/j.cma.2020.113029')",
    )
    def PaperDetails(paper_id: str) -> str:
        """One paper's record: title, authors, venue, year, abstract, citation
        counts and its ids on each database, plus a one-sentence TL;DR when
        Semantic Scholar has one. paper_id is a `paper_id` from SearchPapers:
        an arXiv id, a DOI, or an OpenAlex W-id.

        Semantic Scholar answers first; OpenAlex is the fallback. The first
        line says which answered."""
        failures = []
        if "get_semantic_scholar_paper_details" in providers:
            raw = _run("semantic_scholar",
                       providers["get_semantic_scholar_paper_details"], paper_id)
            if not raw.startswith("ERROR"):
                return "Source: semantic_scholar\n" + raw
            failures.append("semantic_scholar failed — " + raw.removeprefix("ERROR: "))
        if "get_openalex_work" in providers:
            raw = _run("openalex", providers["get_openalex_work"], paper_id)
            if not raw.startswith("ERROR"):
                return ("Source: openalex (" + "; ".join(failures) + ")\n" + raw
                        if failures else "Source: openalex\n" + raw)
            failures.append("openalex failed — " + raw.removeprefix("ERROR: "))
        return "ERROR: no provider could answer — " + "; ".join(failures or ["none available"])

    return {"SearchPapers": SearchPapers, "CitationGraph": CitationGraph,
            "PaperDetails": PaperDetails}
