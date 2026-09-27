"""LiteratureCorpus — owns a canonical paper corpus on disk.

Manages a directory with::

    corpus_dir/
        corpus.csv          — metadata index
        papers/
            <paper_id>/
                paper.pdf   — source file (optional copy)
                paper.md    — extracted text, page-annotated
        .http_cache/        — on-disk GET response cache (TTL 24h;
                              written by :mod:`.http_client`)

**Design principle:** this module does NO network I/O in the corpus
methods.  Discovery and download belong to the agent tools: the
``CorpusAdd`` tool fetches a URL itself, then hands this module a path
already on disk.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock

from .embedder import _SubprocessEmbedder

try:
    import fitz  # type: ignore[import]
    # Silence MuPDF's per-object C-library stderr ("zlib error: incorrect header
    # check" on PDFs with non-standard FlateDecode streams). These are non-fatal
    # — fitz still extracts the readable text — but they spam the run log. Genuine
    # extraction failure is detected by the extracted-text length instead (see
    # add()), not by these warnings.
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:  # noqa: BLE001 — older/newer pymupdf may differ
        pass
except ImportError:
    fitz = None  # type: ignore[assignment]

__all__ = [
    "LiteratureCorpus",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Out-of-process embedder: probed once per process (see embedder.py)
# ---------------------------------------------------------------------------

# Tri-state cache for subprocess embedder availability:
#   None   → not yet probed
#   instance of _SubprocessEmbedder → available and ready
#   False  → probed and unavailable (log once, never re-probe)
_subprocess_embedder_state: _SubprocessEmbedder | None | bool = None
_subprocess_embedder_warned = False

#: Declared ranking strategies. Only "auto" is allowed to degrade.
_RETRIEVAL_MODES = frozenset({"auto", "hybrid", "bm25", "substring"})


_CSV_FIELDS = [
    "paper_id",
    "title",
    "authors",
    "year",
    "doi",
    "arxiv_id",
    "venue",
    "abstract",
    "local_pdf_path",
    "local_md_path",
    "added_at",
    "source",
    "citation_count",
    "full_text",
]

_ARXIV_BARE_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_DOI_BARE_RE = re.compile(r"^10\.\d{4,}/\S+$")

_FULL_TEXT_MD_THRESHOLD = 5000  # chars; beyond this → real body, not abstract


def _slugify(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", s).strip("_")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


_PUNCT_RE = re.compile(r"[^\w\-]", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lowercase and strip trailing/leading punctuation from each token.

    Preserves hyphens so that ``self-attention`` stays as one token.
    """
    return [
        _PUNCT_RE.sub("", w).lower()
        for w in text.split()
        if _PUNCT_RE.sub("", w)
    ]


class LiteratureCorpus:
    """Local paper corpus: extraction, indexing, search.

    No network I/O — the agent downloads files via MCP tools and hands
    local paths to :meth:`add`.

    Parameters
    ----------
    corpus_dir:
        Root directory.  Created on construction if absent.
    """

    #: Ranking strategy the last search actually used — recorded, not
    #: assumed, so an arm labelled 'hybrid' can be verified as one.
    resolved_mode: str | None = None

    #: Fraction of the last search()'s full-text chunks that carried a
    #: usable embedding: 1.0 at full coverage, <1.0 when one or more
    #: papers lacked chunks.npy (or had fewer embedding rows than
    #: chunks), None when dense ranking had no embeddings to work with at
    #: all. Recorded, not assumed — see `_load_all_embeddings`.
    dense_coverage: float | None = None

    #: Why the dense embedder is unavailable (fastembed unimportable AND the
    #: out-of-process embed-worker probe failed), or None while a model is
    #: available. Set by `_get_embedding_model`. Recorded so a caller can
    #: tell "BM25-only because the embedder is down" apart from "BM25-only
    #: because retrieval_mode='bm25' was requested" — the log.warning() this
    #: used to be paired with never reached debug/diagnostics.jsonl, so a
    #: degraded run looked identical to a healthy one in the run record.
    embedder_fallback_reason: str | None = None

    def __init__(self, corpus_dir: Path) -> None:
        self._corpus_dir = Path(corpus_dir)
        self._papers_dir = self._corpus_dir / "papers"
        self._csv_path = self._corpus_dir / "corpus.csv"
        self._chunks_path = self._corpus_dir / "chunks.jsonl"
        self._http_cache_dir = self._corpus_dir / ".http_cache"
        self._corpus_dir.mkdir(parents=True, exist_ok=True)
        self._papers_dir.mkdir(parents=True, exist_ok=True)
        # FileLock, not threading.Lock: the corpus is now study-scoped (see
        # agent_runtime.py's _make_adapter), so two runs of the same study —
        # separate PROCESSES — can genuinely overlap and both write to the
        # same corpus.csv/chunks.jsonl. A threading.Lock only ever protected
        # concurrent THREADS within one process; FileLock (already a project
        # dependency, same pattern instrumented.py uses for the canonical
        # ledger) guards the real cross-process case too, and is a drop-in
        # replacement for the `with self._lock:` sites below.
        self._lock = FileLock(str(self._corpus_dir / ".lock"))
        self._embedding_model = None  # lazy-loaded on first CorpusAdd
        self._fastembed_warned = False
        self._degradation_reported = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        file_path: str,
        title: str = "",
        authors: str = "",
        year: str = "",
        doi: str = "",
        arxiv_id: str = "",
        venue: str = "",
        abstract: str = "",
        citation_count: int = 0,
    ) -> str:
        """Add a paper to the corpus from a local file.

        Parameters
        ----------
        file_path:
            Absolute or relative path to a PDF (``.pdf``) or extracted
            text (``.md`` / ``.txt``) file already on disk.  A path, not
            a provider name and not a paper id.  The ``CorpusAdd`` tool
            downloads a PDF URL to disk first.
        title, authors, year, doi, arxiv_id, venue, abstract:
            Optional metadata.  Pass values obtained from the MCP
            search result that identified this paper.

        Returns
        -------
        str
            ``paper_id`` on success, ``"Already in corpus: {id}"``,
            or ``"ERROR: …"`` on failure.
        """
        src = Path(file_path)
        if not src.exists():
            return f"ERROR: file not found: {file_path!r}. Download it first."

        # Derive a stable paper_id from arxiv_id, doi, or filename
        paper_id = self._derive_paper_id(arxiv_id, doi, src)

        with self._lock:
            rows = self._load_csv()
            if any(r["paper_id"] == paper_id for r in rows):
                return f"Already in corpus: {paper_id}"

        paper_dir = self._paper_dir(paper_id)
        paper_dir.mkdir(parents=True, exist_ok=True)

        # Copy the source file into the corpus directory
        suffix = src.suffix.lower()
        local_pdf_path = ""
        local_md_path = ""
        md_content = ""
        full_text = False

        if suffix == ".pdf":
            dest_pdf = paper_dir / "paper.pdf"
            if src.resolve() != dest_pdf.resolve():
                shutil.copy2(src, dest_pdf)
            local_pdf_path = str(dest_pdf)
            # Extract text
            md_content = self._extract_pdf_to_md(dest_pdf)
            dest_md = paper_dir / "paper.md"
            dest_md.write_text(md_content, encoding="utf-8")
            local_md_path = str(dest_md)
            # Full-text ONLY if extraction produced a real body. A scanned or
            # corrupt PDF can extract near-nothing; the old code hardcoded
            # full_text=True and stored a 236-char placeholder as quotable
            # full-text. Treat a failed extraction as a hard error with an
            # actionable alternative — do NOT enter a phantom paper.
            if len(md_content) <= _FULL_TEXT_MD_THRESHOLD:
                return (
                    f"ERROR: extracted only {len(md_content)} chars from this "
                    f"PDF (need >{_FULL_TEXT_MD_THRESHOLD}) — it is likely "
                    "scanned or has corrupt text streams, so it is NOT quotable "
                    "and was not added. Try another copy of the paper (a "
                    "different pdf_url from the search results, e.g. an "
                    "open-access or publisher version), or choose a different "
                    "source."
                )
            full_text = True
        elif suffix in {".md", ".txt"}:
            dest_md = paper_dir / "paper.md"
            if src.resolve() != dest_md.resolve():
                shutil.copy2(src, dest_md)
            md_content = dest_md.read_text(encoding="utf-8")
            local_md_path = str(dest_md)
            # Full-text if markdown body exceeds threshold
            full_text = len(md_content) > _FULL_TEXT_MD_THRESHOLD
        else:
            return (
                f"ERROR: unsupported file type {suffix!r}. "
                "Provide a .pdf, .md, or .txt file."
            )

        # Chunk and index the text
        chunks = self._chunk_text(md_content)
        self._append_chunks(paper_id, chunks)

        # Embed chunks and save per-paper .npy for dense search
        model = self._get_embedding_model()
        if model is not None and chunks:
            import numpy as _np
            texts = [c["text"] for c in chunks]
            embs = _np.array(
                list(model.embed(texts)), dtype=_np.float32
            )
            _np.save(str(paper_dir / "chunks.npy"), embs)

        # Infer source label
        source_label = (
            "arxiv" if arxiv_id else ("doi" if doi else "local")
        )

        row = {
            "paper_id": paper_id,
            "title": title,
            "authors": authors,
            "year": year,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "venue": venue,
            "abstract": abstract,
            "local_pdf_path": local_pdf_path,
            "local_md_path": local_md_path,
            "added_at": _now_iso(),
            "source": source_label,
            "citation_count": str(int(citation_count)),
            "full_text": "true" if full_text else "false",
        }

        with self._lock:
            rows = self._load_csv()
            if any(r["paper_id"] == paper_id for r in rows):
                return f"Already in corpus: {paper_id}"
            rows.append(row)
            self._save_csv(rows)

        return paper_id

    def _get_embedding_model(self):
        """Lazy-load bge-small-en-v1.5 via fastembed.

        Resolution order:
        1. In-process fastembed (fast path).
        2. Out-of-process via _embed_worker.py in an ephemeral uv env
           with numpy<2 (for Intel macOS where onnxruntime wheels are
           NumPy-1.x builds).  Probed once per process.
        3. None — BM25-only fallback (warning emitted once when BOTH
           routes fail).
        """
        global _subprocess_embedder_state, _subprocess_embedder_warned

        if self._embedding_model is not None:
            return self._embedding_model

        # --- fast path: in-process fastembed ---
        try:
            from fastembed import TextEmbedding

            # Same reason as the subprocess worker (see _embed_worker):
            # unless the thread count is named, onnxruntime pins its workers
            # to CPU indices taken from the machine rather than from the
            # cgroup's mask, and pthread_setaffinity_np fails with EINVAL on
            # every Slurm allocation. The worker's own copy of _thread_count
            # has to stay standalone (it runs in an isolated uv env that
            # cannot import adda), but this path can just use it.
            from ._embed_worker import _thread_count
            self._embedding_model = TextEmbedding(
                "BAAI/bge-small-en-v1.5", threads=_thread_count()
            )
            self.embedder_fallback_reason = None
            return self._embedding_model
        except ImportError:
            pass  # fall through to subprocess route
        except Exception as exc:
            self._embedding_model = None
            self.embedder_fallback_reason = (
                f"in-process fastembed failed to initialize ({exc})"
            )
            return self._embedding_model

        # --- subprocess route (probe once per process) ---
        if _subprocess_embedder_state is None:
            # Not yet probed — check uv availability then probe
            import shutil
            if shutil.which("uv") is not None:
                candidate = _SubprocessEmbedder()
                try:
                    candidate.embed(["probe"])
                    _subprocess_embedder_state = candidate
                    log.info(
                        "dense retrieval via out-of-process embed"
                        " worker (ephemeral numpy<2 env)"
                    )
                except Exception:
                    _subprocess_embedder_state = False
            else:
                _subprocess_embedder_state = False

            if _subprocess_embedder_state is False:
                if not _subprocess_embedder_warned:
                    log.warning(
                        "fastembed unavailable in-process and embed"
                        " worker probe failed — ConsultLiterature falls"
                        " back to BM25-only"
                    )
                    _subprocess_embedder_warned = True

        if _subprocess_embedder_state is not False and \
                _subprocess_embedder_state is not None:
            self._embedding_model = _subprocess_embedder_state
            self.embedder_fallback_reason = None
            return self._embedding_model

        # Both routes failed
        self._embedding_model = None
        self.embedder_fallback_reason = (
            "fastembed unavailable in-process and the out-of-process embed"
            " worker probe failed (uv missing, or the worker itself errored)"
        )
        return self._embedding_model

    def pop_diagnostic_event(self) -> tuple[str, str] | None:
        """One-shot: the first time this corpus instance's retrieval degrades
        to BM25-only because the dense embedder is unavailable, return
        ``("RETRIEVAL_DEGRADED", <reason>)``; every call after that (and
        every call before degradation is ever observed) returns None. A
        corpus instance lives for one run (see agent_runtime.py), so this
        reports the fact exactly once per run rather than on every
        ConsultLiterature call — see orchestration.py's `_wrap_closure`,
        the only place that has both the per-run diagnostics path and a
        handle back to this corpus."""
        if self.embedder_fallback_reason and not self._degradation_reported:
            self._degradation_reported = True
            return (
                "RETRIEVAL_DEGRADED",
                "ConsultLiterature: dense retrieval is unavailable this "
                f"run ({self.embedder_fallback_reason}) — retrieval is "
                "BM25 (lexical) only; treat literature coverage as reduced.",
            )
        return None

    def _load_all_embeddings(self) -> tuple[list[dict], object, object]:
        """Load all chunks and whatever embeddings are actually on disk.

        Returns ``(chunks, embeddings_matrix, has_embedding)``:

        - *embeddings_matrix* is aligned 1:1, POSITIONALLY, with *chunks* —
          same contract callers already relied on (``search()`` slices both
          by the same ``full_idx`` list). Row *i* is chunk *i*'s embedding
          when one exists, and an all-zero placeholder row otherwise.
        - *has_embedding* is a boolean array, same length, ``True`` at every
          row that carries a REAL embedding. A paper missing
          ``chunks.npy`` entirely, or one with fewer embedding rows than
          chunks (re-chunked without a re-embed), gets ``False`` at just
          ITS affected rows — it drops out of DENSE ranking only, and
          keeps participating in BM25 like any other chunk (see
          ``search()``).
        - Both are ``None`` only when NOT ONE chunk anywhere in the corpus
          has a usable embedding — that is the one condition that
          legitimately disables dense ranking corpus-wide.

        A prior version aborted the WHOLE matrix — returned
        ``(chunks, None)`` — the instant ANY single paper's embeddings were
        missing or short, which silently discarded dense ranking for every
        OTHER paper too. Confirmed on the frozen litcorpus fixture: 1 of
        132 papers (5.60% of chunks) had no ``chunks.npy``, which disabled
        dense ranking for the other 131 papers (94.4%) that DID have one,
        and silently downgraded ``retrieval_mode="auto"`` to BM25-only.
        Keeping the matrix full-length and positionally aligned (rather
        than building a smaller subset matrix with a separate index map)
        means every existing positional-slicing caller — ``search()``'s
        ``full_idx`` filter chief among them — needs no index remapping at
        all: a wrong remap would point dense ranks at the wrong chunks,
        which is worse than no dense ranking, so this sidesteps that risk
        entirely rather than accepting it.
        """
        import numpy as _np
        chunks = self._load_chunks()
        if not chunks:
            return chunks, None, None

        # Group chunks by paper_id to load matching .npy files
        paper_embs: dict = {}
        for paper_id in dict.fromkeys(c["paper_id"] for c in chunks):
            npy_path = self._paper_dir(paper_id) / "chunks.npy"
            if npy_path.exists():
                paper_embs[paper_id] = _np.load(str(npy_path))

        if not paper_embs:
            return chunks, None, None

        dim = next(iter(paper_embs.values())).shape[1]

        # Build the aligned embedding matrix (rows match chunks order) and
        # a parallel has_embedding mask — a paper with no chunks.npy, or
        # fewer embedding rows than chunks, gets a zero placeholder row and
        # False in the mask for just the affected chunk(s), instead of
        # aborting the whole corpus.
        paper_chunk_idx: dict = {}
        rows = []
        has_embedding = []
        for chunk in chunks:
            pid = chunk["paper_id"]
            embs = paper_embs.get(pid)
            idx = paper_chunk_idx.get(pid, 0)
            if embs is not None and idx < len(embs):
                rows.append(embs[idx])
                has_embedding.append(True)
            else:
                rows.append(_np.zeros(dim, dtype=_np.float32))
                has_embedding.append(False)
            paper_chunk_idx[pid] = idx + 1

        has_embedding = _np.array(has_embedding, dtype=bool)
        if not has_embedding.any():
            # Every paper is missing/short — corpus-wide fallback is the
            # correct behaviour, not a bug (nothing to rank densely).
            return chunks, None, None

        return chunks, _np.array(rows, dtype=_np.float32), has_embedding

    def consult(self, query: str = "", limit: int = 10) -> str:
        """The canonical lookup from ``knowledge.protocol``, and what the
        ConsultLiterature tool calls.

        Shaped like every other reference lookup (the handbook, the Abaqus
        manual): no query lists the contents, an id from that list opens one
        entry in full, anything else searches. Listing, reading a paper and
        searching were three tools over the same corpus, told apart only by
        which argument the agent passed.

        A query that is exactly a paper_id in the corpus reads that paper; ids
        are derived from arXiv ids, DOIs or file names, so a search phrase does
        not collide with one.
        """
        q = (query or "").strip()
        if not q:
            return self.list_papers()
        if any(row.get("paper_id") == q for row in self._load_csv()):
            return self.get_paper(q)
        return self.search(q, top_k=limit)

    def search(self, query: str, top_k: int = 10) -> str:
        """Search FULL-TEXT papers for passages relevant to *query*.

        Only chunks belonging to papers with ``full_text=true`` are
        searched.  If the corpus contains no full-text papers, returns
        an actionable error guiding the agent to acquire full text.

        The ranking strategy is DECLARED, not inferred. ``retrieval_mode``
        (runtime knob) is one of:

        ``auto``       RRF over BM25 + dense when both ``rank_bm25`` and
                       ``fastembed`` are importable, else BM25-only, else
                       substring. The historical behaviour, and the default.
        ``hybrid``     RRF over BM25 + dense. Errors if either is missing.
        ``bm25``       lexical only.  Errors if ``rank_bm25`` is missing.
        ``substring``  no ranking at all.

        Only ``auto`` degrades. An explicitly requested mode that cannot be
        satisfied is an ERROR, because silently falling back means a run
        labelled "hybrid" was actually BM25-only and nobody could tell — which
        for an ablation arm is a mislabelled condition, not a graceful
        recovery. ``self.resolved_mode`` records what actually ran.

        Returns up to *top_k* formatted passages with page citations,
        or ``"No results found."``.
        """
        kind, payload = self._rank_chunks(query, top_k)
        if kind != "ranked":
            # "error" / "empty" / "substring" are all already the final
            # text to hand back verbatim -- see `_rank_chunks`.
            return payload

        top_idx, rrf_scores, chunks, meta = payload
        passages = []
        for idx in top_idx:
            if rrf_scores[idx] <= 0:
                break
            chunk = chunks[idx]
            pid = chunk["paper_id"]
            m = meta.get(pid, {})
            title = m.get("title", pid)
            year = m.get("year", "")
            passages.append(
                f"--- {title} ({year}), p.{chunk['page']} ---\n"
                f"{chunk['text']}\n"
            )

        if not passages:
            return "No results found."
        return self._coverage_header(len(meta)) + "\n".join(passages)

    @staticmethod
    def _coverage_header(n_papers: int) -> str:
        """One line saying what these passages ARE, before the agent reads them.

        Every other knowledge tool states its boundary -- ConsultBasilisk says
        a miss means "not a Basilisk concept", ConsultF3dasm says "not an
        f3dasm symbol". This one had no equivalent, and it renders the best of
        a bad corpus identically to a strong hit: asked what happens when a
        bubble collapses near a wall, a corpus with no bubble papers returned
        confident prose about honeycomb cell walls and silo buckling. Nothing
        in the output let the reader tell those apart.

        That matters more here than elsewhere because this is the one-hop
        tool: its text goes straight into reasoning, with no second call at
        which a mismatch might be noticed.
        """
        return (
            f"(closest matches in a corpus of {n_papers} full-text paper"
            f"{'s' if n_papers != 1 else ''} -- these are the nearest "
            "passages available, NOT necessarily an answer; if none is on "
            "topic the corpus may simply not cover it)\n\n"
        )

    def search_chunk_scores(
        self, query: str, top_k: int = 10
    ) -> list[tuple[str, float]] | str:
        """The numeric twin of :meth:`search`: ``(paper_id, score)`` per
        ranked chunk, in rank order, NOT deduped or collapsed to one row
        per paper.

        ``search()`` answers a literature reviewer with formatted, citable
        passages -- one paper can legitimately appear many times, and that
        is correct for a reader. A caller that instead needs to rank
        PAPERS (an evaluation harness scoring recall@k against paper-level
        gold labels, e.g. ``internal/tools/literature_retrieval_baseline.py``)
        cannot recover per-chunk magnitude by re-parsing that formatted
        text -- only chunk ORDER survives the round trip, which silently
        forces "first chunk wins" as the only representable aggregation.
        This method exposes the same per-chunk ranking scores
        :meth:`search` already computes (the fused RRF value, or the
        RRF-transformed BM25-only value when no dense ranking ran) so a
        caller can aggregate a paper's evidence across ALL its retrieved
        chunks (sum / max / mean / first) instead of being limited to one.

        Purely additive: :meth:`search`'s signature, return type, and
        behaviour for every existing caller are unchanged by this method's
        existence -- both share the same ranking core (`_rank_chunks`) so
        there is exactly one code path computing scores, and this one only
        reads it back numerically instead of formatting it to text.

        ``retrieval_mode="substring"`` has no per-chunk score (a plain text
        scan, not a ranking) -- an ERROR string is returned for it, same
        style as :meth:`search`'s other ERROR strings. Records
        ``self.resolved_mode`` / ``self.dense_coverage`` exactly as
        :meth:`search` does.
        """
        kind, payload = self._rank_chunks(query, top_k)
        if kind in ("error", "empty"):
            return payload
        if kind == "substring":
            return (
                "ERROR: search_chunk_scores does not support"
                " retrieval_mode='substring' (a plain text scan has no"
                " per-chunk score to aggregate) -- call search() for"
                " substring output, or request retrieval_mode='bm25' /"
                " 'hybrid'."
            )
        top_idx, rrf_scores, chunks, _meta = payload
        out: list[tuple[str, float]] = []
        for idx in top_idx:
            score = rrf_scores[idx]
            if score <= 0:
                break
            out.append((chunks[idx]["paper_id"], float(score)))
        return out

    def _rank_chunks(self, query: str, top_k: int):
        """Ranking core shared by :meth:`search` and
        :meth:`search_chunk_scores` -- the ONE place chunk scores are
        computed, so the two callers can never drift apart.

        Returns a ``(kind, payload)`` pair:

        ``("error", <str>)``        an ERROR string -- return verbatim.
        ``("empty", "No results found.")``
        ``("substring", <str>)``    fully-formatted substring passages
                                     (explicit ``retrieval_mode="substring"``,
                                     or an ``"auto"`` degrade to it) -- no
                                     per-chunk score exists for this branch.
        ``("ranked", (top_idx, rrf_scores, chunks, meta))``
                                     ``top_idx`` is chunk indices into
                                     ``chunks``, already sorted by
                                     ``rrf_scores`` descending and sliced to
                                     *top_k*; ``meta`` is ``paper_id -> csv
                                     row``.

        ``self.resolved_mode`` / ``self.dense_coverage`` are recorded here,
        exactly as the pre-refactor inline version of this logic did.
        """
        import math as _math

        import numpy as _np

        from ..runtime.settings import get_bool, get_str

        _mode = get_str("retrieval_mode", "auto").strip().lower()
        if _mode not in _RETRIEVAL_MODES:
            return "error", (
                f"ERROR: unknown retrieval_mode {_mode!r}."
                f" Valid: {', '.join(sorted(_RETRIEVAL_MODES))}."
            )

        csv_rows = self._load_csv()
        full_text_ids = {
            r["paper_id"]
            for r in csv_rows
            if r.get("full_text", "false").lower() == "true"
        }

        if not full_text_ids:
            n = len(csv_rows)
            return "error", (
                f"ERROR: corpus contains no full-text papers"
                f" ({n} abstract-only entr{'y' if n == 1 else 'ies'})."
                " Quotes require full text — download a PDF or"
                " full-text file into the corpus first."
            )

        all_chunks, emb_matrix_all, has_emb_all = self._load_all_embeddings()
        if not all_chunks:
            return "empty", "No results found."

        # Filter to full-text chunks only
        full_idx = [
            i for i, c in enumerate(all_chunks)
            if c["paper_id"] in full_text_ids
        ]
        if not full_idx:
            return "empty", "No results found."

        chunks = [all_chunks[i] for i in full_idx]
        if emb_matrix_all is not None:
            emb_matrix = _np.array(
                [emb_matrix_all[i] for i in full_idx],
                dtype=_np.float32,
            )
            has_emb = has_emb_all[full_idx]
        else:
            emb_matrix = None
            has_emb = None

        # Partial dense coverage is a first-class, VISIBLE fact — not
        # silent. `dense_coverage` records the fraction of full-text
        # chunks that carried a usable embedding on the last search(),
        # same recorded-not-assumed pattern as `resolved_mode`: None when
        # dense ranking had nothing to run on at all, 1.0 at full
        # coverage, otherwise the partial fraction. See
        # `_load_all_embeddings`'s docstring for what "missing" means.
        self.dense_coverage = (
            float(has_emb.sum()) / len(has_emb)
            if has_emb is not None and len(has_emb) else None
        )

        citation_counts = {
            r["paper_id"]: int(r.get("citation_count") or 0)
            for r in csv_rows
        }
        meta = {r["paper_id"]: r for r in csv_rows}

        K_RRF = 60  # standard RRF constant

        if _mode == "substring":
            self.resolved_mode = "substring"
            return "substring", self._search_substring(
                query, top_k, full_text_ids=full_text_ids
            )

        # --- BM25 ranking ---
        bm25_ranks: dict = {}
        try:
            from rank_bm25 import BM25Okapi  # type: ignore[import]
            tokenized = [_tokenize(c["text"]) for c in chunks]
            bm25 = BM25Okapi(tokenized)
            bm25_scores = bm25.get_scores(_tokenize(query))
            # Citation weight: a POPULARITY prior multiplied into the lexical
            # scores only, before rank fusion, so it also breaks the symmetry
            # of the RRF weighting. It may well help; nobody has measured it.
            # Switchable so that question can be asked.
            if get_bool("citation_weighting", True):
                weighted = [
                    bm25_scores[i]
                    * (1 + _math.log10(
                        citation_counts.get(chunks[i]["paper_id"], 0) + 1
                    ))
                    for i in range(len(chunks))
                ]
            else:
                weighted = list(bm25_scores)
            bm25_order = sorted(
                range(len(chunks)),
                key=lambda i: weighted[i],
                reverse=True,
            )
            for rank, idx in enumerate(bm25_order):
                bm25_ranks[idx] = rank
        except ImportError:
            if _mode != "auto":
                return "error", (
                    f"ERROR: retrieval_mode={_mode!r} needs rank_bm25, which is"
                    " not installed. Install it, or set retrieval_mode to"
                    " 'substring' (or 'auto' to allow a fallback)."
                )
            self.resolved_mode = "substring"
            return "substring", self._search_substring(
                query, top_k, full_text_ids=full_text_ids
            )

        # --- Dense ranking (if embeddings available) ---
        dense_ranks: dict = {}
        if _mode != "bm25" and emb_matrix is not None:
            model = self._get_embedding_model()
            if model is None and _mode == "hybrid":
                return "error", (
                    "ERROR: retrieval_mode='hybrid' needs a working embedder"
                    " (fastembed), which is unavailable. Install it, or set"
                    " retrieval_mode to 'bm25' (or 'auto' to allow a"
                    " fallback)."
                )
            if model is not None:
                q_emb = _np.array(
                    # model.embed() may return a LIST (not a generator); next()
                    # on a list raises "'list' object is not an iterator".
                    # iter() makes next() work for both forms (audit: this crashed
                    # ConsultLiterature dense ranking in the bb3d wet run 20260621).
                    next(iter(model.embed([query]))), dtype=_np.float32
                )
                q_norm = q_emb / (_np.linalg.norm(q_emb) + 1e-9)
                norms = _np.linalg.norm(
                    emb_matrix, axis=1, keepdims=True
                )
                emb_n = emb_matrix / (norms + 1e-9)
                cos_scores = emb_n @ q_norm
                # Rank only over chunks that carry a REAL embedding — the
                # placeholder zero-rows _load_all_embeddings() fills in for
                # unembedded chunks must never enter dense_ranks, or a
                # missing paper would get a fabricated (and wrong) dense
                # rank instead of simply not participating.
                valid_idx = (
                    _np.nonzero(has_emb)[0] if has_emb is not None
                    else _np.arange(len(chunks))
                )
                if len(valid_idx):
                    order = _np.argsort(cos_scores[valid_idx])[::-1]
                    dense_order = valid_idx[order].tolist()
                    for rank, idx in enumerate(dense_order):
                        dense_ranks[idx] = rank
                if has_emb is not None and not has_emb.all():
                    missing = int(len(has_emb) - has_emb.sum())
                    log.warning(
                        "dense retrieval: %d/%d full-text chunks (%.1f%%)"
                        " have no usable embedding (missing chunks.npy, or"
                        " fewer embedding rows than chunks) and are"
                        " excluded from DENSE ranking only — still"
                        " searched via BM25. self.dense_coverage=%.3f",
                        missing, len(has_emb),
                        100.0 * missing / len(has_emb), self.dense_coverage,
                    )

        if _mode == "hybrid" and not dense_ranks:
            return "error", (
                "ERROR: retrieval_mode='hybrid' needs dense embeddings, and"
                " this corpus has none. Embed it, or set retrieval_mode to"
                " 'bm25' (or 'auto' to allow a fallback)."
            )
        self.resolved_mode = "hybrid" if dense_ranks else "bm25"

        # --- RRF fusion ---
        if dense_ranks:
            rrf_scores = {
                i: (2.0 / 3.0) / (
                    K_RRF + dense_ranks.get(i, len(chunks))
                )
                + (1.0 / 3.0) / (
                    K_RRF + bm25_ranks.get(i, len(chunks))
                )
                for i in range(len(chunks))
            }
        else:
            rrf_scores = {
                i: 1.0 / (K_RRF + bm25_ranks.get(i, len(chunks)))
                for i in range(len(chunks))
            }

        top_idx = sorted(
            rrf_scores, key=lambda i: rrf_scores[i], reverse=True
        )[:top_k]

        return "ranked", (top_idx, rrf_scores, chunks, meta)

    def _search_substring(
        self,
        query: str,
        top_k: int,
        full_text_ids: Optional[set] = None,
    ) -> str:
        """Fallback substring search (rank_bm25 not installed)."""
        query_lower = query.lower()
        rows = self._load_csv()
        passages: list[str] = []

        for row in rows:
            if len(passages) >= top_k:
                break
            pid = row.get("paper_id", "")
            if full_text_ids is not None and pid not in full_text_ids:
                continue
            md_path_str = row.get("local_md_path", "")
            if not md_path_str:
                continue
            md_path = Path(md_path_str)
            if not md_path.exists():
                continue

            text = md_path.read_text(encoding="utf-8")
            lines = text.splitlines()
            current_page = 1

            for i, line in enumerate(lines):
                if len(passages) >= top_k:
                    break
                page_match = re.match(
                    r"<!--\s*page\s*(\d+)\s*-->", line
                )
                if page_match:
                    current_page = int(page_match.group(1))
                    continue
                if query_lower in line.lower():
                    start = max(0, i - 1)
                    end = min(len(lines), i + 2)
                    context = "\n".join(lines[start:end])
                    title = row.get("title", row["paper_id"])
                    year = row.get("year", "")
                    passages.append(
                        f"--- {title} ({year}), p.{current_page}"
                        f" ---\n{context}\n"
                    )

        return "\n".join(passages) if passages else "No results found."

    def get_paper(self, paper_id: str) -> str:
        """Return full extracted Markdown of *paper_id*, or ERROR."""
        for row in self._load_csv():
            if row["paper_id"] == paper_id:
                md_path_str = row.get("local_md_path", "")
                if not md_path_str:
                    return (
                        f"ERROR: paper '{paper_id}' has no extracted"
                        " text. Add it with CorpusAdd using a local"
                        " PDF or .md file."
                    )
                md_path = Path(md_path_str)
                if not md_path.exists():
                    return (
                        f"ERROR: text file not found at"
                        f" {md_path_str!r}."
                    )
                return md_path.read_text(encoding="utf-8")
        return f"ERROR: paper '{paper_id}' not found in corpus."

    def list_papers(self) -> str:
        """Return a Markdown table of all papers, or 'Corpus is empty.'

        Each row is annotated with ``[full-text]`` or
        ``[abstract-only]`` to show primary-source status.
        """
        rows = self._load_csv()
        if not rows:
            return "Corpus is empty."
        header = (
            "| paper_id | title | authors | year"
            " | source | full_text |"
        )
        sep = "| --- | --- | --- | --- | --- | --- |"
        lines = [header, sep]
        for r in rows:
            ft = r.get("full_text", "false").lower() == "true"
            ft_label = "[full-text]" if ft else "[abstract-only]"
            lines.append(
                f"| {r.get('paper_id','')} | {r.get('title','')} | "
                f"{r.get('authors','')} | {r.get('year','')} | "
                f"{r.get('source','')} | {ft_label} |"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # PDF extraction
    # ------------------------------------------------------------------

    def _extract_pdf_to_md(self, pdf_path: Path) -> str:
        """Extract page-annotated Markdown from *pdf_path*.

        Docling first (layout-aware — the accuracy path) WHERE AVAILABLE; fall
        back to PyMuPDF (``fitz``; the same library) when Docling is absent
        (e.g. Intel macOS, where the pyproject marker excludes it by design) or
        returns a thin/failed parse. Return the longer of the two.

        The only real bug in the original was the acceptance floor: it returned
        ANY Docling result > 100 chars, so a *failed* ~236-char Docling parse
        would beat the PyMuPDF fallback. Require a real body (> the full-text
        threshold) before trusting Docling; otherwise fall through.
        """
        docling_md = ""
        # 1. Docling — layout-aware, the accuracy path. Absent on Intel macOS.
        try:
            from docling.datamodel.base_models import (
                InputFormat,  # type: ignore
            )
            from docling.document_converter import (  # type: ignore
                DocumentConverter,
                PdfFormatOption,
            )
            # Force the pre-2.123.0 single-threaded backend explicitly.
            # docling 2.123.0 changed PdfFormatOption's own default from
            # DoclingParseDocumentBackend to ThreadedDoclingParseDocumentBackend
            # ("Default to threaded docling-parse across SDK, CLI, service, and
            # extraction") — that new default SEGFAULTS the whole interpreter
            # (SIGSEGV, not a catchable exception — the try/except below never
            # even runs) on macos-latest (arm64) + Python 3.13 in CI, first seen
            # 2026-08-26 (docling 2.122.0->2.123.0, nothing else in the PDF
            # stack changed). Pinning the backend, not the docling version,
            # keeps every other 2.123.0+ fix/feature while avoiding the one
            # default that crashes.
            try:
                from docling.backend.docling_parse_backend import (  # type: ignore
                    DoclingParseDocumentBackend,
                )
                _format_options = {
                    InputFormat.PDF: PdfFormatOption(
                        backend=DoclingParseDocumentBackend),
                }
            except ImportError:
                # Older docling without the threaded-default split — its own
                # default is already the single-threaded backend.
                _format_options = None
            converter = DocumentConverter(format_options=_format_options)
            result = converter.convert(str(pdf_path))
            docling_md = result.document.export_to_markdown() or ""
            if len(docling_md.strip()) > _FULL_TEXT_MD_THRESHOLD:
                return docling_md  # Docling actually parsed it — trust it.
        except Exception:  # noqa: BLE001
            pass

        # 2. PyMuPDF (fitz) — the lean fallback; the only extractor on Intel
        #    macOS. Page-annotated so the reviewer can cite by page.
        pymupdf_md = ""
        if fitz is not None:
            try:
                doc = fitz.open(str(pdf_path))
                pymupdf_md = "\n\n".join(
                    f"<!-- page {n} -->\n{pg.get_text()}"
                    for n, pg in enumerate(doc, start=1))
                doc.close()
            except Exception:  # noqa: BLE001
                pass

        # Docling was absent or thin → take whichever recovered more text.
        best = max((docling_md, pymupdf_md), key=lambda c: len(c.strip()))
        if len(best.strip()) > 100:
            return best
        return "(PDF extraction unavailable — install docling or pymupdf)"

    # ------------------------------------------------------------------
    # Chunking infrastructure
    # ------------------------------------------------------------------

    def _chunk_text(
        self, md_text: str, chunk_size: int = 150, overlap: int = 50
    ) -> list[dict]:
        """Split page-annotated markdown into overlapping word chunks.

        Respects ``<!-- page N -->`` markers: each chunk records its
        starting page.

        Returns
        -------
        list[dict]
            Each entry has ``{"page": int, "text": str}``.
        """
        lines = md_text.splitlines()
        current_page = 1
        all_words: list[tuple[str, int]] = []  # (word, page)
        for line in lines:
            pm = re.match(r"<!--\s*page\s*(\d+)\s*-->", line)
            if pm:
                current_page = int(pm.group(1))
                continue
            for w in line.split():
                all_words.append((w, current_page))

        chunks: list[dict] = []
        i = 0
        while i < len(all_words):
            window = all_words[i: i + chunk_size]
            if not window:
                break
            page = window[0][1]
            text = " ".join(w for w, _ in window)
            chunks.append({"page": page, "text": text})
            i += max(1, chunk_size - overlap)
        return chunks

    def _append_chunks(
        self, paper_id: str, chunks: list[dict]
    ) -> None:
        """Append *chunks* for *paper_id* to chunks.jsonl."""
        with self._lock:
            with self._chunks_path.open("a", encoding="utf-8") as f:
                for i, chunk in enumerate(chunks):
                    f.write(
                        json.dumps(
                            {
                                "paper_id": paper_id,
                                "chunk_id": i,
                                "page": chunk["page"],
                                "text": chunk["text"],
                            }
                        )
                        + "\n"
                    )

    def _load_chunks(self) -> list[dict]:
        """Load all chunks from chunks.jsonl."""
        if not self._chunks_path.exists():
            return []
        with self._lock:
            with self._chunks_path.open(encoding="utf-8") as f:
                return [
                    json.loads(line)
                    for line in f
                    if line.strip()
                ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _derive_paper_id(
        self, arxiv_id: str, doi: str, src: Path
    ) -> str:
        """Derive a stable paper_id from available metadata."""
        if arxiv_id:
            return "arxiv_" + _slugify(arxiv_id.strip())
        if doi:
            return "doi_" + _slugify(doi.strip())
        # Fall back to the source filename without extension
        return _slugify(src.stem) or "paper_unknown"

    def _paper_dir(self, paper_id: str) -> Path:
        return self._papers_dir / paper_id

    def _load_csv(self) -> list[dict]:
        if not self._csv_path.exists():
            return []
        with self._csv_path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _save_csv(self, rows: list[dict]) -> None:
        with self._csv_path.open(
            "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {k: row.get(k, "") for k in _CSV_FIELDS}
                )
