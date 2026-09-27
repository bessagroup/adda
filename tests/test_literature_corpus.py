"""Tests for LiteratureCorpus — written before implementation (TDD red phase)."""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adda._src.literature.literature_corpus import LiteratureCorpus


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_corpus(tmp_path: Path) -> LiteratureCorpus:
    return LiteratureCorpus(tmp_path / "literature")


def _inject_paper(corpus: LiteratureCorpus, paper_id: str, title: str = "Test Paper",
                  authors: str = "A. Author", year: str = "2024",
                  source: str = "arxiv", text: str = "",
                  full_text: bool = True) -> None:
    """Inject a fake paper directly into the corpus without network calls.

    ``full_text=True`` by default so that search() finds the paper;
    set to False to test abstract-only behaviour.
    """
    paper_dir = corpus._paper_dir(paper_id)
    paper_dir.mkdir(parents=True, exist_ok=True)
    md_path = paper_dir / "paper.md"
    md_content = text or f"<!-- page 1 -->\nAbstract for {title}\n"
    md_path.write_text(md_content, encoding="utf-8")

    # Populate chunks.jsonl so BM25 search can find this paper
    chunks = corpus._chunk_text(md_content)
    corpus._append_chunks(paper_id, chunks)

    rows = corpus._load_csv()
    rows.append({
        "paper_id": paper_id,
        "title": title,
        "authors": authors,
        "year": year,
        "doi": "",
        "arxiv_id": paper_id.replace("arxiv_", "").replace("_", "."),
        "venue": "",
        "abstract": f"Abstract for {title}",
        "local_pdf_path": "",
        "local_md_path": str(md_path),
        "added_at": "2024-01-01T00:00:00+00:00",
        "source": source,
        "citation_count": "0",
        "full_text": "true" if full_text else "false",
    })
    corpus._save_csv(rows)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def test_corpus_creates_dir_on_init(tmp_path):
    corpus_dir = tmp_path / "lit"
    assert not corpus_dir.exists()
    LiteratureCorpus(corpus_dir)
    assert corpus_dir.exists()
    assert (corpus_dir / "papers").exists()


# ---------------------------------------------------------------------------
# add()
# ---------------------------------------------------------------------------


def test_corpus_csv_created_on_first_add(tmp_path):
    """add() with a local .md file creates corpus.csv and returns the paper_id."""
    corpus = _make_corpus(tmp_path)
    md_file = tmp_path / "paper.md"
    md_file.write_text("<!-- page 1 -->\nThe Transformer model.", encoding="utf-8")

    result = corpus.add(
        file_path=str(md_file),
        arxiv_id="1706.03762",
        title="Attention Is All You Need",
        authors="Vaswani et al.",
        year="2017",
    )

    assert result == "arxiv_1706_03762"
    assert (corpus._corpus_dir / "corpus.csv").exists()
    rows = corpus._load_csv()
    assert len(rows) == 1
    assert rows[0]["paper_id"] == "arxiv_1706_03762"
    assert rows[0]["title"] == "Attention Is All You Need"


def test_add_already_in_corpus(tmp_path):
    """Adding the same arxiv_id twice returns 'Already in corpus'."""
    corpus = _make_corpus(tmp_path)
    _inject_paper(corpus, "arxiv_1706_03762", title="Attention")

    md_file = tmp_path / "paper.md"
    md_file.write_text("content", encoding="utf-8")
    result = corpus.add(str(md_file), arxiv_id="1706.03762")
    assert result == "Already in corpus: arxiv_1706_03762"


def test_add_returns_error_on_complete_failure(tmp_path):
    """add() with a non-existent file path returns an ERROR string."""
    corpus = _make_corpus(tmp_path)
    result = corpus.add("/nonexistent/path/paper.pdf")
    assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# list_papers()
# ---------------------------------------------------------------------------


def test_list_papers_empty_corpus(tmp_path):
    corpus = _make_corpus(tmp_path)
    assert corpus.list_papers() == "Corpus is empty."


def test_list_papers_shows_added_paper(tmp_path):
    corpus = _make_corpus(tmp_path)
    _inject_paper(corpus, "arxiv_1706_03762", title="Attention Is All You Need",
                  authors="Vaswani et al.", year="2017", source="arxiv")

    table = corpus.list_papers()
    assert "arxiv_1706_03762" in table
    assert "Attention Is All You Need" in table
    assert "2017" in table


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def test_search_no_results(tmp_path):
    """Empty corpus returns an informative message (error or no results)."""
    corpus = _make_corpus(tmp_path)
    result = corpus.search("xyzzy_nonexistent_term_12345")
    # Either "No results found." or the full-text ERROR guidance
    assert "No results found." in result or "ERROR" in result


def test_search_finds_passage(tmp_path):
    corpus = _make_corpus(tmp_path)
    _inject_paper(corpus, "arxiv_1706_03762", title="Attention Is All You Need",
                  text="<!-- page 1 -->\nThe Transformer model uses self-attention.\n")

    result = corpus.search("self-attention")
    assert "self-attention" in result.lower()
    assert "Attention Is All You Need" in result


def test_search_respects_top_k(tmp_path):
    corpus = _make_corpus(tmp_path)
    # Paper with 5 matching lines
    many_lines = "<!-- page 1 -->\n" + "\n".join(
        [f"Line {i}: keyword here" for i in range(10)]
    )
    _inject_paper(corpus, "arxiv_0000_00001", title="Paper One", text=many_lines)
    result = corpus.search("keyword", top_k=3)
    # Should return at most 3 passages
    passage_count = result.count("--- Paper One")
    assert passage_count <= 3


def test_bm25_ranks_by_relevance_where_substring_cannot(tmp_path):
    """BM25 must rank the truly relevant paper first; naive substring can't.

    The original version of this test (``test_bm25_search_finds_paraphrase``)
    injected ONE paper and asserted only ``result != "No results found."`` —
    that passes identically under ``retrieval_mode="substring"`` (ranking
    disabled entirely), so it could not tell BM25 apart from no ranking at
    all (verified: it does pass with substring). This version discriminates
    the two on purpose, with THREE papers:

    - ``distractor`` (added FIRST, so it is what a naive first-match scan
      would return) contains the query's exact words as one contiguous
      substring, but only once, in a single unrelated aside — thin lexical
      relevance.
    - ``filler`` shares no vocabulary with the query at all.
    - ``target`` (added LAST, so a naive scan reaches it last) never
      contains the query's exact word sequence anywhere, but densely
      repeats the query's vocabulary across several sentences — high BM25
      relevance despite zero substring overlap.

    Under ``retrieval_mode="bm25"`` the densely-relevant ``target`` must
    rank first. Under ``retrieval_mode="substring"`` it CANNOT (it contains
    no substring match for the query at all — see the corpus's own
    docstring: substring is "no ranking at all"), and the query instead
    surfaces ``distractor``'s thin, incidental match first because
    ``_search_substring`` returns hits in corpus.csv row order, not by
    relevance. That gap — ranking vs. none — is the actual claim BM25
    retrieval makes, and is what this test asserts.
    """
    from adda._src.runtime import settings

    corpus = _make_corpus(tmp_path)
    query = "maximum sigma_crit buckling stress"

    _inject_paper(
        corpus, "arxiv_distractor", title="Coating Durability Under Thermal Cycling",
        text=(
            "<!-- page 1 -->\n"
            "This paper investigates coating durability under thermal cycling "
            "in aerospace composites. As an aside, unrelated prior work once "
            "reported a maximum sigma_crit buckling stress figure in a "
            "different context, which is not the focus here. The remainder "
            "of this paper is entirely about fatigue crack growth in "
            "adhesive bonds and thermal expansion mismatch.\n"
        ),
    )
    _inject_paper(
        corpus, "arxiv_filler", title="Polymer Crystallization Kinetics",
        text=(
            "<!-- page 1 -->\n"
            "This work studies polymer crystallization kinetics under "
            "isothermal annealing conditions, unrelated to mechanical "
            "loading or structural stability.\n"
        ),
    )
    _inject_paper(
        corpus, "arxiv_target", title="Critical Plate Buckling Under Compression",
        text=(
            "<!-- page 1 -->\n"
            "The critical buckling stress sigma_crit is maximized when the "
            "plate thickness is optimized to resist buckling. We analyze "
            "sigma_crit under a range of buckling stress conditions, showing "
            "sigma_crit reaches its maximum stress at the critical buckling "
            "threshold. This buckling stress analysis determines sigma_crit's "
            "maximum value under critical loading, and the maximum buckling "
            "stress corresponds to the critical sigma_crit onset.\n"
        ),
    )

    try:
        settings.configure({"retrieval_mode": "bm25"})
        bm25_result = corpus.search(query, top_k=5)
        assert corpus.resolved_mode == "bm25"
        assert bm25_result.index("Critical Plate Buckling Under Compression") < \
            bm25_result.index("Coating Durability Under Thermal Cycling")

        settings.configure({"retrieval_mode": "substring"})
        substring_result = corpus.search(query, top_k=5)
        assert corpus.resolved_mode == "substring"
        # The exact 4-word query never appears verbatim in the target paper,
        # so substring search cannot find it at all -- unlike BM25, it is
        # not merely outranked, it is ABSENT.
        assert "Critical Plate Buckling Under Compression" not in substring_result
        assert "Coating Durability Under Thermal Cycling" in substring_result
    finally:
        settings.configure(None)


# ---------------------------------------------------------------------------
# get_paper()
# ---------------------------------------------------------------------------


def test_get_paper_not_found(tmp_path):
    corpus = _make_corpus(tmp_path)
    result = corpus.get_paper("no_such_paper_id")
    assert result.startswith("ERROR")


def test_get_paper_returns_content(tmp_path):
    corpus = _make_corpus(tmp_path)
    known_text = "<!-- page 1 -->\nThis is the paper content.\n"
    _inject_paper(corpus, "arxiv_1706_03762", text=known_text)

    result = corpus.get_paper("arxiv_1706_03762")
    assert "This is the paper content." in result


# ---------------------------------------------------------------------------
# _extract_pdf_to_md()
# ---------------------------------------------------------------------------


@pytest.mark.timeout(600)
@pytest.mark.xdist_group(name="docling_pdf")
def test_extract_pdf_to_md_page_annotations(tmp_path):
    """If fitz is available, output contains <!-- page N --> annotations.

    Shrunk from 8 pages to 2: the assertions below only need >=1 page
    marker / some body text -- they never depended on page COUNT, so 8
    was arbitrary test data that just multiplied docling's real per-page
    cost (table-structure model inference; OCR itself is skipped for a
    text PDF like this one, see literature_corpus.py's own do_ocr logic)
    for no assertion benefit. `xdist_group`/600s stay as backstop
    headroom for the table-structure inference this test still exercises
    for real when docling is installed.
    """
    try:
        import fitz  # noqa: F401
        fitz_available = True
    except ImportError:
        fitz_available = False

    corpus = _make_corpus(tmp_path)

    if fitz_available:
        # A realistic text PDF so extraction is treated as a full parse,
        # not a thin/scanned stub -- 2 pages is enough for the page-N
        # annotation assertions below, which never needed more.
        doc = fitz.open()
        for _ in range(2):
            pg = doc.new_page()
            pg.insert_textbox(fitz.Rect(40, 40, 560, 760), ("word " * 150))
        pdf_path = tmp_path / "test.pdf"
        doc.save(str(pdf_path))
        doc.close()

        result = corpus._extract_pdf_to_md(pdf_path)
        # The <!-- page N --> annotations are the fitz path. Docling, when it is
        # installed and parses the PDF, legitimately wins (it is preferred, and
        # its markdown carries no page markers). So assert the page markers only
        # on the lean install where Docling is absent; otherwise assert the body
        # text was extracted.
        try:
            import docling  # noqa: F401
            docling_available = True
        except ImportError:
            docling_available = False
        if docling_available:
            assert "word" in result.lower()
        else:
            assert "<!-- page 1 -->" in result
    else:
        # Graceful fallback expected
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")
        result = corpus._extract_pdf_to_md(fake_pdf)
        assert "unavailable" in result.lower()


def test_extract_pdf_to_md_fallback_when_fitz_missing(tmp_path):
    """When fitz is patched as None, fallback string is returned."""
    corpus = _make_corpus(tmp_path)
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    import adda._src.literature.literature_corpus as lc_module
    original_fitz = lc_module.fitz
    try:
        lc_module.fitz = None
        result = corpus._extract_pdf_to_md(fake_pdf)
        assert "unavailable" in result.lower()
    finally:
        lc_module.fitz = original_fitz


def test_pdf_failed_extraction_is_rejected_not_stored_as_fulltext(
    tmp_path, monkeypatch
):
    """A PDF whose extraction yields too little (scanned / corrupt streams) must
    NOT be stored as quotable full-text. The old code hardcoded full_text=True
    for every PDF and kept a ~236-char placeholder the reviewer would 'quote'."""
    corpus = _make_corpus(tmp_path)
    fake_pdf = tmp_path / "scanned.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 dummy")
    monkeypatch.setattr(
        corpus, "_extract_pdf_to_md", lambda p: "<!-- page 1 -->\n(tiny)")
    result = corpus.add(str(fake_pdf), arxiv_id="1502.05700")
    assert result.startswith("ERROR"), result
    assert "different pdf_url" in result  # points to an alternative that can work
    assert corpus._load_csv() == []      # nothing phantom entered the corpus


@pytest.mark.timeout(600)
@pytest.mark.xdist_group(name="docling_pdf")
def test_extraction_returns_real_body_from_text_pdf(tmp_path):
    """Regression: a text PDF must extract its full body. The old code tried
    Docling first and returned ANY result >100 chars, so a botched 236-char
    Docling parse of a 13-page paper (Snoek et al. 2015) was accepted while
    PyMuPDF extracts ~53k chars. Now PyMuPDF runs first and a real body wins."""
    import pytest as _pt
    try:
        import fitz
    except ImportError:
        _pt.skip("fitz not available")
    corpus = _make_corpus(tmp_path)
    doc = fitz.open()
    for _ in range(8):  # ~6000 chars of real text, comfortably > threshold
        pg = doc.new_page()
        pg.insert_textbox(fitz.Rect(40, 40, 560, 760), ("word " * 150))
    pdf = tmp_path / "text_paper.pdf"
    doc.save(str(pdf))
    doc.close()

    md = corpus._extract_pdf_to_md(pdf)
    assert len(md) > 5000, f"expected a real body, got {len(md)} chars"
    assert "word" in md
    assert "unavailable" not in md.lower()


# ---------------------------------------------------------------------------
# OCR-on-demand (spec: OCR only when the PDF actually needs it) -- a real
# arXiv paper almost always has an embedded text layer, and running OCR
# against one anyway finds nothing while costing ~20-40s/page. These tests
# fake the docling module entirely (it isn't installed on Intel macOS, this
# dev machine included -- pyproject.toml's own marker excludes it there) so
# the do_ocr DECISION is exercised and inspected directly, never timed.
# ---------------------------------------------------------------------------


def _install_fake_docling(monkeypatch, captured: dict, md_body: str = None):
    """Install a minimal fake docling package tree in sys.modules, deep
    enough for literature_corpus.py's own import statements to resolve.
    Records the do_ocr value and the PdfFormatOption actually built, and
    returns export_to_markdown() from `md_body` (or a generic default) so
    the extraction's own body/annotations can be checked unchanged."""
    import sys
    import types

    body = md_body if md_body is not None else (
        "<!-- page 1 -->\nfake docling body " * 20)

    class _InputFormat:
        PDF = "pdf"

    class _PdfPipelineOptions:
        def __init__(self, do_ocr=True):
            self.do_ocr = do_ocr
            captured["do_ocr"] = do_ocr

    class _PdfFormatOption:
        def __init__(self, backend=None, pipeline_options=None):
            self.backend = backend
            self.pipeline_options = pipeline_options
            captured["pipeline_options"] = pipeline_options

    class _FakeDocument:
        def export_to_markdown(self):
            return body

    class _FakeResult:
        document = _FakeDocument()

    class _DocumentConverter:
        def __init__(self, format_options=None):
            captured["format_options"] = format_options

        def convert(self, path):
            captured["converted_path"] = path
            return _FakeResult()

    class _DoclingParseDocumentBackend:
        pass

    mod_docling = types.ModuleType("docling")
    mod_datamodel = types.ModuleType("docling.datamodel")
    mod_base_models = types.ModuleType("docling.datamodel.base_models")
    mod_base_models.InputFormat = _InputFormat
    mod_pipeline_options = types.ModuleType("docling.datamodel.pipeline_options")
    mod_pipeline_options.PdfPipelineOptions = _PdfPipelineOptions
    mod_document_converter = types.ModuleType("docling.document_converter")
    mod_document_converter.DocumentConverter = _DocumentConverter
    mod_document_converter.PdfFormatOption = _PdfFormatOption
    mod_backend = types.ModuleType("docling.backend")
    mod_backend_dp = types.ModuleType("docling.backend.docling_parse_backend")
    mod_backend_dp.DoclingParseDocumentBackend = _DoclingParseDocumentBackend

    for name, mod in [
        ("docling", mod_docling),
        ("docling.datamodel", mod_datamodel),
        ("docling.datamodel.base_models", mod_base_models),
        ("docling.datamodel.pipeline_options", mod_pipeline_options),
        ("docling.document_converter", mod_document_converter),
        ("docling.backend", mod_backend),
        ("docling.backend.docling_parse_backend", mod_backend_dp),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def test_ocr_skipped_when_pdf_already_has_a_text_layer(tmp_path, monkeypatch):
    """A PDF with a real embedded text layer (the vast majority of real
    papers) must NOT pay for OCR -- do_ocr=False reaches docling's own
    pipeline options."""
    import fitz

    captured: dict = {}
    # Long enough to clear _FULL_TEXT_MD_THRESHOLD (5000 chars) so this
    # docling result is the one actually returned, not the pymupdf
    # fallback -- the point being verified is that OCR's on/off state
    # doesn't change what the extraction returns.
    long_body = "<!-- page 1 -->\nfake docling body " * 400
    _install_fake_docling(monkeypatch, captured, md_body=long_body)
    corpus = _make_corpus(tmp_path)
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_textbox(fitz.Rect(40, 40, 560, 760), "word " * 150)
    pdf_path = tmp_path / "text.pdf"
    doc.save(str(pdf_path))
    doc.close()

    result = corpus._extract_pdf_to_md(pdf_path)

    assert captured["do_ocr"] is False
    assert "fake docling body" in result  # extraction output unaffected


def test_ocr_enabled_for_an_image_only_pdf(tmp_path, monkeypatch):
    """A PDF with no embedded text layer at all (a genuinely scanned
    document) must still get OCR -- do_ocr=True."""
    import fitz

    captured: dict = {}
    _install_fake_docling(monkeypatch, captured)
    corpus = _make_corpus(tmp_path)
    doc = fitz.open()
    doc.new_page()  # blank page: no text layer whatsoever
    pdf_path = tmp_path / "scanned.pdf"
    doc.save(str(pdf_path))
    doc.close()

    corpus._extract_pdf_to_md(pdf_path)

    assert captured["do_ocr"] is True


def test_pdf_has_text_layer_defaults_to_ocr_on_a_corrupt_file(tmp_path):
    """The text-layer check itself failing (corrupt file, fitz absent)
    must default to "needs OCR" -- slower, never silently wrong for a
    genuinely scanned document this check couldn't read."""
    bad_pdf = tmp_path / "corrupt.pdf"
    bad_pdf.write_bytes(b"not a pdf at all")
    corpus = _make_corpus(tmp_path)
    assert corpus._pdf_has_text_layer(bad_pdf) is False


def test_pdf_real_extraction_is_stored_as_fulltext(tmp_path, monkeypatch):
    """The fix must not over-reject: a PDF with a real body is full-text."""
    corpus = _make_corpus(tmp_path)
    fake_pdf = tmp_path / "real.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 dummy")
    body = "<!-- page 1 -->\n" + ("word " * 2000)  # > 5000 chars
    monkeypatch.setattr(corpus, "_extract_pdf_to_md", lambda p: body)
    result = corpus.add(str(fake_pdf), arxiv_id="1807.02811")
    assert not result.startswith("ERROR"), result
    rows = corpus._load_csv()
    assert len(rows) == 1 and rows[0]["full_text"] == "true"


# ---------------------------------------------------------------------------
# _derive_paper_id() — replaces _normalize_identifier
# ---------------------------------------------------------------------------


def test_normalize_arxiv_id(tmp_path):
    corpus = _make_corpus(tmp_path)
    paper_id = corpus._derive_paper_id("2312.12345", "", Path("paper.pdf"))
    assert paper_id == "arxiv_2312_12345"


def test_normalize_arxiv_prefix(tmp_path):
    corpus = _make_corpus(tmp_path)
    paper_id = corpus._derive_paper_id("arxiv:2312.12345", "", Path("paper.pdf"))
    assert paper_id == "arxiv_arxiv_2312_12345"  # slugified with prefix


def test_normalize_doi(tmp_path):
    corpus = _make_corpus(tmp_path)
    paper_id = corpus._derive_paper_id("", "10.1234/test", Path("paper.pdf"))
    assert paper_id == "doi_10_1234_test"


def test_normalize_bare_doi(tmp_path):
    """DOI in doi field → doi_ prefix."""
    corpus = _make_corpus(tmp_path)
    paper_id = corpus._derive_paper_id("", "10.5678/other", Path("paper.pdf"))
    assert paper_id.startswith("doi_")


def test_normalize_unknown(tmp_path):
    """No arxiv_id, no doi → fall back to filename stem."""
    corpus = _make_corpus(tmp_path)
    paper_id = corpus._derive_paper_id("", "", Path("my_paper_2024.pdf"))
    assert "my_paper_2024" in paper_id


# ---------------------------------------------------------------------------
# Dense embedding fallback
# ---------------------------------------------------------------------------


def test_search_falls_back_gracefully_without_embeddings(tmp_path):
    """Search still returns BM25 results when no chunks.npy files exist."""
    corpus = _make_corpus(tmp_path)
    # _inject_paper does NOT create chunks.npy — simulates missing dense embeddings
    _inject_paper(
        corpus,
        "arxiv_1234_56789",
        title="Fallback Test Paper",
        text="<!-- page 1 -->\nThis paper discusses neural network optimization.\n",
    )

    # Verify no chunks.npy exists
    npy_path = corpus._paper_dir("arxiv_1234_56789") / "chunks.npy"
    assert not npy_path.exists(), "chunks.npy should not exist for injected paper"

    result = corpus.search("neural network optimization", top_k=5)
    assert result != "No results found.", "Expected BM25 fallback to find results"
    assert "Fallback Test Paper" in result


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_add_is_thread_safe(tmp_path):
    """Two threads adding different local .md files → CSV has exactly 2 rows."""
    corpus = _make_corpus(tmp_path)

    md1 = tmp_path / "paper1.md"
    md2 = tmp_path / "paper2.md"
    md1.write_text("<!-- page 1 -->\nContent one.", encoding="utf-8")
    md2.write_text("<!-- page 1 -->\nContent two.", encoding="utf-8")

    errors: list[str] = []

    def add_paper(md_path: Path, arxiv_id: str) -> None:
        result = corpus.add(str(md_path), arxiv_id=arxiv_id, title=f"Paper {arxiv_id}")
        if result.startswith("ERROR"):
            errors.append(result)

    t1 = threading.Thread(target=add_paper, args=(md1, "1111.11111"))
    t2 = threading.Thread(target=add_paper, args=(md2, "2222.22222"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"Unexpected errors: {errors}"
    rows = corpus._load_csv()
    assert len(rows) == 2
    ids = {r["paper_id"] for r in rows}
    assert "arxiv_1111_11111" in ids
    assert "arxiv_2222_22222" in ids


def test_concurrent_add_from_separate_corpus_instances_is_safe(tmp_path):
    """Regression: the corpus is now study-scoped (shared across every run of
    a study, see agent_runtime.py's _make_adapter), so two SEPARATE
    AgenticRun processes can genuinely write to it at once — two entirely
    separate LiteratureCorpus objects (each with its OWN in-process lock),
    not two threads sharing one instance and its one threading.Lock.
    A threading.Lock would not protect this at all (each process/instance
    gets its own, uncoordinated lock object); the FileLock does, because it
    coordinates through the shared lock FILE on disk, not in-process state.

    Deterministically widens the race window instead of relying on a bare
    2-thread race (timing-dependent — confirmed by hand it can pass by luck
    even without the fix): corpus_a's FINAL read-modify-write sleeps right
    after loading, unconditionally, with no synchronization on corpus_b (a
    sync point corpus_b sets would deadlock once the fix makes corpus_a hold
    a real cross-instance lock during the sleep). If the two instances don't
    coordinate at all (the bug), corpus_b's entire add() completes during
    that window and corpus_a's stale in-memory rows overwrite it on save. If
    they do coordinate (the fix), corpus_b's own attempt to acquire the same
    lock file simply blocks until corpus_a's slow section releases it — no
    deadlock, both rows land correctly either way the timing falls.
    """
    corpus_a = _make_corpus(tmp_path)
    corpus_b = LiteratureCorpus(tmp_path / "literature")  # a second, independent instance

    md1 = tmp_path / "paper1.md"
    md2 = tmp_path / "paper2.md"
    md1.write_text("<!-- page 1 -->\nContent one.", encoding="utf-8")
    md2.write_text("<!-- page 1 -->\nContent two.", encoding="utf-8")

    real_load_csv = corpus_a._load_csv
    call_count = {"n": 0}

    def a_load_csv_slow():
        call_count["n"] += 1
        rows = real_load_csv()
        if call_count["n"] == 2:  # the final read-modify-write's load
            time.sleep(0.5)
        return rows

    corpus_a._load_csv = a_load_csv_slow

    errors: list[str] = []

    def add_paper(corpus: LiteratureCorpus, md_path: Path, arxiv_id: str) -> None:
        result = corpus.add(str(md_path), arxiv_id=arxiv_id, title=f"Paper {arxiv_id}")
        if result.startswith("ERROR"):
            errors.append(result)

    t1 = threading.Thread(target=add_paper, args=(corpus_a, md1, "3333.33333"))
    t2 = threading.Thread(target=add_paper, args=(corpus_b, md2, "4444.44444"))
    t1.start()
    time.sleep(0.1)  # let t1 reach its slow final-load window before t2 starts
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == [], f"Unexpected errors: {errors}"
    with (tmp_path / "literature" / "corpus.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2, (
        "a lost update: corpus_a's stale save clobbered corpus_b's row "
        f"instead of both landing — rows: {rows}"
    )
    ids = {r["paper_id"] for r in rows}
    assert "arxiv_3333_33333" in ids
    assert "arxiv_4444_44444" in ids


def test_embed_with_spec_has_an_upper_bound():
    """Regression: an unbounded floating spec (fastembed>=0.3, no ceiling)
    is exactly the footgun that caused docling's CI segfault (2026-08-26 —
    a transitive default changed under an unpinned version bump). Observed
    directly in a real run (20260830T004106, delegation D035): the embed
    worker's ephemeral uv env hit "ImportError: cannot import name
    'TextEmbedding' from 'fastembed' (unknown location)". Not confirmed to
    be THIS exact fix (Oscar is read-only, so it can't be reproduced there),
    but the unbounded spec must never come back unpinned."""
    from adda._src.literature.embedder import _EMBED_WITH
    assert "<" in _EMBED_WITH, (
        f"_EMBED_WITH={_EMBED_WITH!r} has no upper bound — an unpinned "
        "floating spec for an ephemeral env dependency is the exact "
        "footgun this pin exists to prevent."
    )


# ---------------------------------------------------------------------------
# Semantic Scholar id namespacing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("given", "expected"), [
    # Regression (run 20260912T142229, D001): our arXiv search hands the agent
    # a bare id; passing it straight to S2 404s with "Paper with id … not
    # found". The id that names a paper in one tool must work in the next.
    ("2506.14097", "ARXIV:2506.14097"),
    ("2506.14097v2", "ARXIV:2506.14097v2"),
    ("math.AG/0309136", "ARXIV:math.AG/0309136"),
    ("10.1016/j.ijnonlinmec.2013.01.010", "DOI:10.1016/j.ijnonlinmec.2013.01.010"),
    # already namespaced, or an S2 paperId — untouched
    ("ARXIV:2506.14097", "ARXIV:2506.14097"),
    ("DOI:10.1016/j.cma.2020.113029", "DOI:10.1016/j.cma.2020.113029"),
    ("649def34f8be52c8b66281af98ae884c09aef38b",
     "649def34f8be52c8b66281af98ae884c09aef38b"),
    # the model's own prose spellings
    ("arXiv:2506.14097", "arXiv:2506.14097"),
    ("arxiv 2506.14097", "ARXIV:2506.14097"),
    ("", ""),
])
def test_s2_paper_id_namespacing(given, expected):
    from adda._src.agents.literature_tools.semantic_scholar import _s2_paper_id
    assert _s2_paper_id(given) == expected


# ---------------------------------------------------------------------------
# The embed worker names its own thread count
#
# Left to decide for itself, onnxruntime pins worker threads to CPU indices
# derived from the machine's CPU count, not from the set the cgroup granted.
# Under Slurm those differ and pthread_setaffinity_np fails with EINVAL,
# which blocks every cgroup-limited allocation — i.e. every cluster run.
# Observed: bioreactor-2d__critic__20260921T211633, delegation D002.
# ---------------------------------------------------------------------------

def test_thread_count_follows_the_cpus_we_actually_own(monkeypatch):
    import os

    from adda._src.literature import _embed_worker

    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3},
                        raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)

    assert _embed_worker._thread_count() == 4, (
        "thread count came from the machine, not from the cgroup's mask"
    )


def test_thread_count_honours_an_explicit_omp_setting(monkeypatch):
    from adda._src.literature import _embed_worker

    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    assert _embed_worker._thread_count() == 2


def test_thread_count_is_never_zero(monkeypatch):
    import os

    from adda._src.literature import _embed_worker

    monkeypatch.setenv("OMP_NUM_THREADS", "0")   # ignored: not a usable count
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(), raising=False)
    assert _embed_worker._thread_count() >= 1


def test_worker_stderr_tail_says_how_much_it_dropped():
    from adda._src.literature.embedder import _STDERR_TAIL, _tail

    short = "boom"
    assert _tail(short) == short

    long = "x" * (_STDERR_TAIL + 250)
    out = _tail(long)
    assert "250 earlier chars of stderr omitted" in out
    assert out.endswith("x" * 50)


def test_worker_failure_names_the_exit_code(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from adda._src.literature.embedder import _SubprocessEmbedder

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=3, stdout="", stderr="the cause"),
    )
    with pytest.raises(RuntimeError, match=r"exit 3.*the cause"):
        _SubprocessEmbedder().embed(["hello"])


# ---------------------------------------------------------------------------
# Search results state their own boundary
#
# Every other knowledge tool says what a miss MEANS -- ConsultBasilisk's miss
# is "not a Basilisk concept", ConsultF3dasm's is "not an f3dasm symbol".
# Literature search had no equivalent, so the best of a bad corpus rendered
# identically to a strong hit. This is the one-hop tool: its text goes
# straight into the agent's reasoning with no second call at which a mismatch
# might be noticed.
# ---------------------------------------------------------------------------

def test_search_results_declare_what_they_are(tmp_path):
    corpus = _make_corpus(tmp_path)
    _inject_paper(
        corpus, "arxiv_0000_00001", title="Honeycomb Cell Wall Collapse",
        text="The cell wall buckles inward under compressive load.\n" * 5,
    )

    result = corpus.search("cell wall collapse", top_k=3)

    assert "--- Honeycomb Cell Wall Collapse" in result
    head = result.split("---")[0]
    assert "1 full-text paper" in head, (
        "the header must say how much corpus these passages are the best of"
    )
    assert "NOT necessarily an answer" in head


def test_the_header_counts_papers_not_passages(tmp_path):
    corpus = _make_corpus(tmp_path)
    for i in (1, 2):
        _inject_paper(
            corpus, f"arxiv_0000_0000{i}", title=f"Paper {i}",
            text="keyword here on every line\n" * 6,
        )

    head = corpus.search("keyword", top_k=6).split("---")[0]
    assert "2 full-text papers" in head, (
        "plural and count both come from the paper set, not the passage list"
    )


def test_an_off_topic_query_still_returns_passages_and_says_so(tmp_path):
    """The failure the header exists for, demonstrated.

    A query the corpus cannot answer does NOT come back as "No results
    found." -- ranking returns the least-bad passage it has, and the prose
    reads exactly as confidently as a real hit. The header is the only thing
    in the output that lets a reader tell the two apart.
    """
    corpus = _make_corpus(tmp_path)
    _inject_paper(
        corpus, "arxiv_0000_00001", title="Honeycomb Cell Wall Collapse",
        text="The cell wall buckles inward under compressive load.\n" * 5,
    )

    result = corpus.search("cavitation bubble collapse near a rigid wall", top_k=3)

    assert "--- Honeycomb Cell Wall Collapse" in result, (
        "premise of this test: an off-topic query is answered anyway"
    )
    assert "NOT necessarily an answer" in result
    assert "may simply not cover it" in result


def test_the_in_process_embedder_also_names_its_thread_count(tmp_path):
    """The twin of the worker's fix. onnxruntime pins threads to CPU indices
    the cgroup may not own unless the count is given, and that blocks every
    Slurm allocation — on this path as much as in the subprocess worker."""
    import sys
    import types

    seen = {}

    class _FakeTextEmbedding:
        def __init__(self, model_name, threads=None, **kw):
            seen["threads"] = threads

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = _FakeTextEmbedding
    old = sys.modules.get("fastembed")
    sys.modules["fastembed"] = fake
    try:
        corpus = _make_corpus(tmp_path)
        corpus._embedding_model = None
        corpus._get_embedding_model()
    finally:
        if old is None:
            del sys.modules["fastembed"]
        else:
            sys.modules["fastembed"] = old

    assert seen.get("threads") is not None and seen["threads"] >= 1
