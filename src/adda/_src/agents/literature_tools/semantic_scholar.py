"""Semantic Scholar calls — client-library calls (throttled) and the
recommendations endpoint (via http_client's _robust_post).

Plain functions, not agent tools: ``discovery.py`` builds the tools the
reviewer holds on top of them."""

from __future__ import annotations

import json as _json
import logging
import os
import re
import threading
import weakref

from ...literature.http_client import SourceCooldownError, _robust_post
from .throttle import _throttled_ss

log = logging.getLogger(__name__)

# arXiv ids: "2506.14097", "2506.14097v2", or the old "math.AG/0309136" form.
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}(v\d+)?|[a-z-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)$")


def _s2_paper_id(paper_id: str) -> str:
    """Namespace a bare external id for Semantic Scholar's API.

    S2 resolves a non-S2 identifier only when it carries its namespace
    (``ARXIV:2506.14097``, ``DOI:10.1016/j.ijnonlinmec.2013.01.010``); a bare
    arXiv id 404s with "Paper with id … not found". Our own arXiv search hands
    the agent bare ids, so the id that identifies a paper in one tool has to be
    accepted by the next one. Anything already namespaced, or a 40-hex S2
    paperId, passes through untouched.
    """
    pid = (paper_id or "").strip()
    if ":" in pid:  # already namespaced (ARXIV:, DOI:, CorpusId:, …)
        return pid
    if pid.lower().startswith("arxiv"):  # "arxiv2506.14097", "arXiv 2506.14097"
        return "ARXIV:" + pid[5:].lstrip(": ")
    if _ARXIV_ID.match(pid):
        return f"ARXIV:{pid}"
    if pid.startswith("10."):
        return f"DOI:{pid}"
    return pid


def resolve_semantic_scholar_key() -> str | None:
    """The configured Semantic Scholar key, or None.

    config.yaml's runtime: block (or F3DASM_SEMANTIC_SCHOLAR_API_KEY) is the
    explicit-config channel; the bare SEMANTIC_SCHOLAR_API_KEY is honoured
    too since it is Semantic Scholar's own documented convention, not ours to
    rename out from under anyone already using it.
    """
    from ...runtime.settings import get_str
    return (
        get_str("semantic_scholar_api_key", "")
        or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        or None
    )


_state_lock = threading.Lock()
_key_rejected = False
_pending_event: tuple[str, str, dict] | None = None
_clients: weakref.WeakSet[_S2Client] = weakref.WeakSet()


def _build_inner(key: str | None):
    from semanticscholar import SemanticScholar as _SS
    return _SS(api_key=key, retry=False)


class _S2Client:
    """The semanticscholar client, resolved at call time so that dropping a
    rejected key reaches every live holder (and the call being retried)."""

    def __init__(self) -> None:
        key = None if _key_rejected else resolve_semantic_scholar_key()
        if not key:
            log.warning(
                "no usable Semantic Scholar key — proceeding with "
                "unauthenticated access (very low rate limit). Set "
                "semantic_scholar_api_key in config.yaml's runtime: block (or "
                "SEMANTIC_SCHOLAR_API_KEY / F3DASM_SEMANTIC_SCHOLAR_API_KEY) "
                "for reliable access."
            )
        self.keyed = bool(key)
        self._inner = _build_inner(key)
        _clients.add(self)

    def _drop_key(self) -> None:
        if self.keyed:
            self._inner = _build_inner(None)
            self.keyed = False

    def get_paper(self, *a, **kw):
        return self._inner.get_paper(*a, **kw)

    def search_paper(self, *a, **kw):
        return self._inner.search_paper(*a, **kw)


def get_semantic_scholar_client() -> _S2Client:
    """The ONE Semantic Scholar client every tool path builds.

    ``retry=False``: the library's own internal 429 retry (tenacity, up to 10
    attempts, 5-60s exponential backoff each) would silently absorb every 429
    before it reaches ``_throttled_ss``, which is the sole retry authority for
    this traffic (pacing, backoff, shared circuit breaker). Disabling it makes
    each request's real outcome surface at once, which is also what lets
    ``_call_in_fresh_thread``'s 30s timeout actually bound a call. Calls on
    the returned client must go through ``_throttled_ss``.
    """
    return _S2Client()


def key_in_use() -> bool:
    return not _key_rejected and bool(resolve_semantic_scholar_key())


def reject_key() -> bool:
    """The configured key got a 403: Semantic Scholar rejects an invalid key
    outright instead of falling back to the shared quota (a 429 is quota
    exhaustion). Drop the key for the rest of the process and queue one
    LIT_KEY_REJECTED event. False when there was no key to drop."""
    global _key_rejected, _pending_event
    with _state_lock:
        if _key_rejected or not resolve_semantic_scholar_key():
            return False
        _key_rejected = True
        _pending_event = (
            "LIT_KEY_REJECTED",
            "Semantic Scholar returned 403 for the configured API key, so the "
            "key is invalid or revoked. Continuing without it for the rest of "
            "this run (shared unauthenticated quota, much slower); fix "
            "semantic_scholar_api_key for reliable access.",
            {"source": "semantic_scholar"},
        )
    for c in list(_clients):
        c._drop_key()
    return True


class _KeyEvents:
    """Diagnostic source (see orchestration._wrap_closure): reports the
    rejected-key fact once per process."""

    def pop_diagnostic_event(self):
        global _pending_event
        with _state_lock:
            ev, _pending_event = _pending_event, None
        return ev


KEY_EVENTS = _KeyEvents()


def _reset_key_state() -> None:
    global _key_rejected, _pending_event
    _key_rejected, _pending_event = False, None
    _clients.clear()


def build_semantic_scholar_closures() -> dict:
    """The three semanticscholar-library calls; {} (with a warning) when
    the library is not installed."""
    tools: dict = {}
    # Semantic Scholar tools via the semanticscholar library.
    try:
        import semanticscholar  # noqa: F401 — absent => no S2 tools

        _sch = get_semantic_scholar_client()

        def _ss_forbidden_error() -> str:
            """Message for a 403 (PermissionError) that survived the
            keyless fallback: Semantic Scholar refused the request outright
            (403 is a refusal, not quota exhaustion — that is a 429), and
            repeating it will not change that."""
            return (
                "ERROR: Semantic Scholar refused the request (403 "
                "Forbidden), with no API key in use. Retrying will not "
                "help; use OpenAlex/arXiv instead, or set "
                "semantic_scholar_api_key in config.yaml's runtime: block."
            )

        def search_semantic_scholar(
            query: str, num_results: int = 10
        ) -> str:
            """Search for papers on Semantic Scholar."""
            def _search():
                # search_paper() itself is NON-blocking — it returns a
                # LAZY PaginatedResults shell with no network call made
                # yet (unlike get_paper/get_author, which fetch eagerly
                # inside the call). The real HTTP request + retry only
                # fires on iteration — materializing it HERE, inside the
                # _throttled_ss-wrapped call, is what actually puts the
                # network I/O under the timeout/rate-limiter/circuit-
                # breaker. Iterating outside (the original shape) left
                # the real work completely unprotected: _throttled_ss
                # would return instantly having "successfully" produced
                # an empty shell, and the genuine multi-minute hang
                # happened afterwards, in code with no timeout at all.
                return list(_sch.search_paper(
                    query,
                    limit=int(num_results),
                    fields=[
                        "title", "authors", "year", "abstract",
                        "externalIds", "venue", "citationCount",
                    ],
                ))
            try:
                results = _throttled_ss(_search)
            except TimeoutError:
                return (
                    "ERROR: Semantic Scholar request timed out"
                    " after 30s. Try again or use OpenAlex."
                )
            except PermissionError:
                return _ss_forbidden_error()
            except SourceCooldownError as exc:
                return f"ERROR: {exc}"
            except Exception as exc:
                return f"ERROR: Semantic Scholar search failed: {exc}"
            papers = []
            for p in results:
                papers.append({
                    "paperId": p.paperId,
                    "title": p.title,
                    "year": p.year,
                    "venue": p.venue,
                    "citationCount": p.citationCount,
                    "authors": [
                        a["name"] for a in (p.authors or [])
                    ],
                    "abstract": (p.abstract or "")[:300],
                    "externalIds": p.externalIds or {},
                })
            return _json.dumps(papers, indent=2)

        def get_semantic_scholar_paper_details(
            paper_id: str,
        ) -> str:
            """Get details for a paper. paper_id may be a bare arXiv id
            ("2506.14097"), a bare DOI ("10.1016/j.cma.2020.113029"), an
            already-namespaced id ("ARXIV:2506.14097") or an S2 paperId —
            bare ids are namespaced for you."""
            try:
                paper = _throttled_ss(
                    _sch.get_paper,
                    _s2_paper_id(paper_id),
                    fields=[
                        "title", "authors", "year", "abstract",
                        "venue", "citationCount",
                        "influentialCitationCount",
                        "tldr", "externalIds",
                    ],
                )
            except TimeoutError:
                return (
                    "ERROR: Semantic Scholar request timed out"
                    " after 30s. Try again or use OpenAlex."
                )
            except PermissionError:
                return _ss_forbidden_error()
            except SourceCooldownError as exc:
                return f"ERROR: {exc}"
            except Exception as exc:
                return (
                    f"ERROR: Semantic Scholar paper details failed: {exc}"
                )
            return _json.dumps({
                "paperId": paper.paperId,
                "title": paper.title,
                "year": paper.year,
                "venue": paper.venue,
                "citationCount": paper.citationCount,
                "influentialCitationCount": (
                    paper.influentialCitationCount
                ),
                "tldr": (paper.tldr or {}).get("text"),
                "authors": [
                    a["name"] for a in (paper.authors or [])
                ],
                "abstract": paper.abstract,
                "externalIds": paper.externalIds or {},
            }, indent=2)

        def get_semantic_scholar_citations_and_references(
            paper_id: str,
        ) -> str:
            """Get citing papers and references (≤20 each). paper_id takes
            the same forms as get_semantic_scholar_paper_details: a bare arXiv
            id or DOI is namespaced for you."""
            try:
                paper = _throttled_ss(
                    _sch.get_paper,
                    _s2_paper_id(paper_id),
                    fields=["citations", "references"],
                )
            except TimeoutError:
                return (
                    "ERROR: Semantic Scholar request timed out"
                    " after 30s. Try again or use OpenAlex."
                )
            except PermissionError:
                return _ss_forbidden_error()
            except SourceCooldownError as exc:
                return f"ERROR: {exc}"
            except Exception as exc:
                return (
                    "ERROR: Semantic Scholar citations/references"
                    f" failed: {exc}"
                )
            refs = [
                {
                    "paperId": r.get("paperId"),
                    "title": r.get("title"),
                }
                for r in (paper.references or [])[:20]
            ]
            cits = [
                {
                    "paperId": c.get("paperId"),
                    "title": c.get("title"),
                }
                for c in (paper.citations or [])[:20]
            ]
            return _json.dumps(
                {"references": refs, "citations": cits}, indent=2
            )

        tools.update({
            "search_semantic_scholar": search_semantic_scholar,
            "get_semantic_scholar_paper_details": (
                get_semantic_scholar_paper_details
            ),
            "get_semantic_scholar_citations_and_references": (
                get_semantic_scholar_citations_and_references
            ),
        })
    except ImportError:
        log.warning(
            "semanticscholar not installed — S2 tools not"
            " registered for literature_reviewer"
        )
    return tools


def build_recommendations_closure() -> dict:
    """The recommendations-API tool (no client library involved)."""
    def get_semantic_scholar_recommendations(
        paper_id: str, n_results: int = 10
    ) -> str:
        """Find semantically similar papers (no citation link).

        paper_id: an S2 paperId, or a DOI or arXiv id in either bare
        ("2506.14097") or namespaced ("ARXIV:2506.14097") form.
        """
        import json as _j
        try:
            resp = _robust_post(
                "https://api.semanticscholar.org"
                "/recommendations/v1/papers/",
                json={"positivePaperIds": [_s2_paper_id(paper_id)]},
                params={
                    "fields": (
                        "paperId,title,authors,year,abstract"
                        ",externalIds,openAccessPdf"
                    ),
                    "limit": min(int(n_results), 50),
                },
            )
            papers = resp.json().get("recommendedPapers", [])
        except SourceCooldownError as exc:
            return f"ERROR: {exc}"
        except Exception as exc:
            return f"ERROR: S2 recommendations failed: {exc}"

        out = []
        for p in papers:
            oa = p.get("openAccessPdf") or {}
            out.append({
                "paperId": p.get("paperId", ""),
                "title": p.get("title", ""),
                "year": p.get("year", ""),
                "authors": [
                    a["name"]
                    for a in (p.get("authors") or [])[:3]
                ],
                "abstract": (p.get("abstract") or "")[:300],
                "externalIds": p.get("externalIds") or {},
                "pdf_url": oa.get("url", ""),
            })
        return _j.dumps(out, indent=2)
    return {"get_semantic_scholar_recommendations": get_semantic_scholar_recommendations}
