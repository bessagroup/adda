"""Local corpus tools: add a paper (from disk or a URL), and read the corpus."""

from __future__ import annotations

from ...literature.http_client import SourceCooldownError, _robust_get
from ...prompts.tool_catalog import tool_examples


def build_corpus_closures(corpus, cache_dir) -> dict:
    """Tools over the study-scoped LiteratureCorpus at *corpus*."""
    # Defined as named functions (not lambdas) so each carries a docstring:
    # the generated <tools> catalog renders these, making it the single
    # source of tool docs — no hand-written list in the prompt to drift.
    def _download_pdf(url: str, filename: str) -> str:
        """Fetch a PDF URL into the corpus papers directory; the saved path,
        or ``"ERROR: …"``."""
        import re as _re
        from pathlib import Path as _Path
        try:
            resp = _robust_get(
                url,
                cache_dir=cache_dir,
                headers={
                    "User-Agent": (
                        "f3dasm-agent/1.0"
                        " (mailto:f3dasm@brown.edu)"
                    ),
                },
            )
        except SourceCooldownError as exc:
            return f"ERROR: {exc}"
        except Exception as exc:
            return f"ERROR: download failed: {exc}"

        # Validate content
        ct = ""
        if hasattr(resp, "headers"):
            ct = resp.headers.get("Content-Type", "")
        elif hasattr(resp, "_content_type"):
            ct = resp._content_type
        body = resp.content
        if "pdf" not in ct.lower() and not body.startswith(
            b"%PDF"
        ):
            return (
                "ERROR: not a PDF — content-type is"
                f" {ct!r} and body does not start with %PDF."
                " Check the URL."
            )

        if not filename:
            stem = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
            filename = _re.sub(r"[^A-Za-z0-9._-]", "_", stem) or "paper"
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        dest = _Path(filename)
        if not dest.is_absolute():
            dest = corpus._papers_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return str(dest)

    @tool_examples(
        "CorpusAdd('papers/2506.14097.pdf', title='Buckling of lattices', arxiv_id='2506.14097', citation_count=12)",
        "CorpusAdd('https://arxiv.org/pdf/1706.03762', title='Attention is all you need', arxiv_id='1706.03762')",
    )
    def CorpusAdd(source: str, title: str = "", authors: str = "",
                  year: str = "", doi: str = "", arxiv_id: str = "",
                  venue: str = "", abstract: str = "",
                  citation_count: int = 0, filename: str = ""):
        """Index a paper into the corpus so its passages become searchable.

        source is either a PATH ON DISK to a .pdf/.md/.txt (e.g.
        "papers/2506.14097.pdf"), or a direct PDF URL (http/https), which is
        downloaded into the corpus first — saved as `filename` if given,
        otherwise named after the arxiv_id or the URL. It is NOT a provider
        name and NOT a paper id. The
        remaining arguments are metadata about the paper, copied from the
        search result that identified it; arxiv_id/doi are recorded as
        metadata and used to derive a stable paper_id, they are not fetched.
        citation_count boosts BM25 retrieval weight (log10(c+1) scaling) —
        pass the citationCount from Semantic Scholar or OpenAlex."""
        if str(source).lower().startswith(("http://", "https://")):
            saved = _download_pdf(
                source, filename or (f"{arxiv_id}.pdf" if arxiv_id else ""))
            if saved.startswith("ERROR"):
                return saved
            source = saved
        return corpus.add(
            source, title=title, authors=authors, year=year, doi=doi,
            arxiv_id=arxiv_id, venue=venue, abstract=abstract,
            citation_count=int(citation_count or 0))

    tools = build_corpus_read_closures(corpus)
    tools["CorpusAdd"] = CorpusAdd
    return tools


def build_corpus_read_closures(corpus) -> dict:
    """The corpus lookup every agent holds: one tool, one definition.

    It used to be three tools (search, list, read one paper) written twice --
    here for the literature reviewer and again in ``backends/base.py`` for
    everyone else, with different wording -- so the same name meant two
    docstrings and the prompt map could not say which one an agent read.
    """

    @tool_examples(
        "ConsultLiterature()",
        "ConsultLiterature('lattice buckling under axial compression', limit=5)",
        "ConsultLiterature('<a paper_id from the list>')",
    )
    def ConsultLiterature(query: str = "", limit: int = 10):
        """Read the study's literature corpus. It is shared across every run
        of the study, so it may already hold a prior run's answer — look here
        before searching the databases again.

        No argument → what is in it: every paper with its paper_id, each
        tagged [full-text] or [abstract-only] so you know which you may quote
        from. A paper_id from that list → the full extracted, page-annotated
        text of that paper. Anything else → passage search across the
        full-text papers, best match first, up to `limit`.

        Papers enter the corpus only when the literature reviewer adds them;
        a search over a corpus with no full-text papers says so."""
        return corpus.consult(query, int(limit))

    # Lets orchestration.py's _wrap_closure (the one place with both a
    # per-run diagnostics path and a handle back here) notice, once per run,
    # when the dense embedder is unavailable and retrieval silently
    # downgraded to BM25-only — see LiteratureCorpus.pop_diagnostic_event.
    ConsultLiterature._adda_diagnostic_source = corpus

    return {"ConsultLiterature": ConsultLiterature}
