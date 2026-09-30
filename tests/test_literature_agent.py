"""Tests for LiteratureReviewAgent.build_closure_tools() — covers the corpus
and discovery tool closures without network calls."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_agent():
    from adda._src.agents.literature import LiteratureReviewAgent
    return LiteratureReviewAgent()


# ---------------------------------------------------------------------------
# Basic agent attributes
# ---------------------------------------------------------------------------


def test_literature_agent_has_correct_tools():
    """LiteratureReviewAgent declares Read, Grep, Glob tools."""
    agent = _make_agent()
    assert "Read" in agent.tools
    assert "Grep" in agent.tools
    assert "Glob" in agent.tools


def test_literature_agent_can_read_problem_statement():
    """LiteratureReviewAgent gets the problem statement via the uniform
    ReadProblemStatement() tool now, not the removed per-agent
    inject_problem_statement push flag (every agent has this tool equally)."""
    agent = _make_agent()
    assert "ReadProblemStatement" in agent.tools
    assert not hasattr(agent, "inject_problem_statement")


def test_literature_agent_has_system_prompt():
    """LiteratureReviewAgent has a non-empty system_prompt."""
    agent = _make_agent()
    assert isinstance(agent.system_prompt, str)
    assert len(agent.system_prompt) > 100


# ---------------------------------------------------------------------------
# build_closure_tools returns corpus tools
# ---------------------------------------------------------------------------


def test_build_closure_tools_returns_corpus_tools(tmp_path):
    """build_closure_tools returns CorpusAdd, ConsultLiterature, etc."""
    agent = _make_agent()
    tools = agent.build_closure_tools(
        study_dir=tmp_path,
        lit_reviewer_notes_dir=tmp_path / "lit",
    )

    assert "CorpusAdd" in tools
    assert "ConsultLiterature" in tools
    assert "CorpusGetPaper" not in tools  # folded into ConsultLiterature
    assert "CorpusList" not in tools  # folded into ConsultLiterature


def test_corpus_closures_produce_typed_json_schema_for_every_param(tmp_path):
    """CorpusAdd/ConsultLiterature/CorpusGetPaper must carry explicit type
    annotations on every parameter, or StructuredTool.from_function (what
    the OpenAI-compatible backends -- Ollama/vLLM/OpenRouter -- use to build
    each tool's JSON schema) silently omits the "type" key for that
    parameter. Claude's own native adapter never goes through this
    schema-inference path, so a missing annotation is invisible there --
    confirmed for real: a local model (Qwen3.8:27b via Ollama) calling
    ConsultLiterature failed exactly here."""
    from langchain_core.tools import StructuredTool

    agent = _make_agent()
    tools = agent.build_closure_tools(
        study_dir=tmp_path, lit_reviewer_notes_dir=tmp_path / "lit",
    )
    for name in ("CorpusAdd", "ConsultLiterature"):
        tool = StructuredTool.from_function(tools[name], name=name)
        for param_name, schema in tool.args.items():
            assert "type" in schema, (
                f"{name}'s parameter {param_name!r} has no 'type' key in "
                f"its generated JSON schema ({schema}) -- missing a type "
                "annotation on the closure's own parameter."
            )


def test_discovery_tools_produce_typed_json_schema(tmp_path):
    """Every parameter of the reviewer's discovery tools needs a JSON-schema
    "type", or StructuredTool.from_function (the OpenAI-compatible backends'
    schema builder) omits it -- and a union annotation has no single type.
    The async wrapper these replaced once aborted the whole Ollama tool-build
    loop the same way (a KeyError on its hand-set signature), confirmed on a
    local model."""
    from langchain_core.tools import StructuredTool

    agent = _make_agent()
    tools = agent.build_closure_tools(
        study_dir=tmp_path, lit_reviewer_notes_dir=tmp_path / "lit",
    )
    for name in ("SearchPapers", "CitationGraph", "PaperDetails"):
        assert name in tools, f"expected {name!r} among literature closures"
        tool = StructuredTool.from_function(tools[name], name=name)
        for param_name, schema in tool.args.items():
            assert "type" in schema, (
                f"{name}'s parameter {param_name!r} has no 'type' key in "
                f"its generated JSON schema ({schema})."
            )


def test_the_reviewer_holds_the_merged_tools_only(tmp_path):
    """Thirteen per-provider tools and an async collect step became three:
    the provider calls stay, but not as tools."""
    tools = _make_agent().build_closure_tools(
        study_dir=tmp_path, lit_reviewer_notes_dir=tmp_path / "lit")
    assert set(tools) == {"ConsultLiterature", "CorpusAdd", "SearchPapers",
                          "CitationGraph", "PaperDetails"}


def test_corpus_add_closure_works(tmp_path):
    """CorpusAdd closure adds a .md file to the corpus."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    tools = agent.build_closure_tools(
        study_dir=tmp_path,
        lit_reviewer_notes_dir=lit_dir,
    )

    md_file = tmp_path / "paper.md"
    md_file.write_text("<!-- page 1 -->\nContent about transformers.\n", encoding="utf-8")

    result = tools["CorpusAdd"](
        str(md_file),
        title="Transformer Paper",
        authors="A. Author",
        year="2017",
        arxiv_id="1706.03762",
    )

    assert result == "arxiv_1706_03762"


def test_corpus_list_closure_works(tmp_path):
    """CorpusList closure lists all papers in the corpus."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    tools = agent.build_closure_tools(
        study_dir=tmp_path,
        lit_reviewer_notes_dir=lit_dir,
    )

    # Empty corpus
    result = tools["ConsultLiterature"]()
    assert result == "Corpus is empty."


def test_corpus_search_closure_on_empty_corpus(tmp_path):
    """ConsultLiterature returns an informative response on empty corpus."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    tools = agent.build_closure_tools(
        study_dir=tmp_path,
        lit_reviewer_notes_dir=lit_dir,
    )

    result = tools["ConsultLiterature"]("neural networks")
    # Empty corpus has no full-text papers → ERROR guidance or no results
    assert "No results found." in result or "ERROR" in result


def test_corpus_get_paper_not_found(tmp_path):
    """An id that is not in the corpus is not read as a paper: on an empty
    corpus it falls through to search, which says there is nothing to
    search."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    tools = agent.build_closure_tools(
        study_dir=tmp_path,
        lit_reviewer_notes_dir=lit_dir,
    )

    result = tools["ConsultLiterature"]("no_such_paper")
    assert "ERROR" in result


# ---------------------------------------------------------------------------
# build_closure_tools with fallback lit_reviewer_notes_dir
# ---------------------------------------------------------------------------


def test_build_closure_tools_fallback_dir(tmp_path):
    """build_closure_tools uses study_dir/runs/lit_reviewer_notes when
    lit_reviewer_notes_dir=None — matching _make_adapter's real (study-scoped,
    not per-run) default, so the fallback and the real default never drift
    apart again."""
    agent = _make_agent()
    tools = agent.build_closure_tools(
        study_dir=tmp_path,
        lit_reviewer_notes_dir=None,
    )

    # Should still return the corpus tools
    assert "CorpusAdd" in tools
    assert "CorpusList" not in tools  # folded into ConsultLiterature
    assert (tmp_path / "runs" / "lit_reviewer_notes").is_dir()


# ---------------------------------------------------------------------------
# search_openalex tool (mocked network)
# ---------------------------------------------------------------------------


def test_search_openalex_returns_results_on_success(tmp_path):
    """search_openalex returns JSON results when requests.get succeeds."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    from adda._src.agents.literature_tools import build_literature_providers
    tools = build_literature_providers(tmp_path, lit_dir)

    # search_openalex is unconditional once the literature stack imports
    # (build_openalex_closures gates on nothing else) -- semanticscholar,
    # pymupdf and rank-bm25 are all hard `dependencies`, not optionals, so
    # this branch is latent, not live: assert it rather than let a real
    # regression (the tool silently vanishing) pass as "environment".
    assert "search_openalex" in tools

    oa_result = {
        "results": [
            {
                "id": "W123",
                "title": "Test Paper",
                "publication_year": 2023,
                "doi": "10.1234/test",
                "authorships": [{"author": {"display_name": "A. Author"}}],
                "primary_location": {"pdf_url": "https://example.com/paper.pdf"},
                "best_oa_location": None,
                "open_access": {"oa_url": None},
                "abstract_inverted_index": {"test": [0], "abstract": [1]},
            }
        ]
    }
    import json as _json
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.text = _json.dumps(oa_result)
    mock_resp.json.return_value = oa_result

    with patch("requests.get", return_value=mock_resp):
        with patch("adda._src.literature.http_client._sleep"):
            result = tools["search_openalex"]("neural networks", n_results=5)

    import json
    data = json.loads(result)
    assert len(data) >= 1
    titles = [d["title"] for d in data]
    assert "Test Paper" in titles


def test_search_openalex_returns_error_on_failure(tmp_path):
    """search_openalex returns ERROR when requests.get raises."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    from adda._src.agents.literature_tools import build_literature_providers
    tools = build_literature_providers(tmp_path, lit_dir)

    # search_openalex is unconditional once the literature stack imports
    # (build_openalex_closures gates on nothing else) -- semanticscholar,
    # pymupdf and rank-bm25 are all hard `dependencies`, not optionals, so
    # this branch is latent, not live: assert it rather than let a real
    # regression (the tool silently vanishing) pass as "environment".
    assert "search_openalex" in tools

    import requests as _requests
    with patch("requests.get", side_effect=_requests.RequestException("Connection failed")):
        with patch("adda._src.literature.http_client._sleep"):
            result = tools["search_openalex"]("neural networks")

    assert "ERROR" in result


# ---------------------------------------------------------------------------
# get_semantic_scholar_recommendations tool (mocked)
# ---------------------------------------------------------------------------


def test_get_ss_recommendations_returns_json(tmp_path):
    """get_semantic_scholar_recommendations returns JSON on success."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    from adda._src.agents.literature_tools import build_literature_providers
    tools = build_literature_providers(tmp_path, lit_dir)

    # get_semantic_scholar_recommendations comes from
    # build_recommendations_closure(), which imports no optional client
    # library (unlike build_semantic_scholar_closures) -- it is present
    # whenever the literature stack imports at all, same reasoning as
    # search_openalex above.
    assert "get_semantic_scholar_recommendations" in tools

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.json.return_value = {
        "recommendedPapers": [
            {
                "paperId": "P1",
                "title": "Similar Paper",
                "year": 2022,
                "authors": [{"name": "B. Author"}],
                "abstract": "Abstract text.",
                "externalIds": {},
                "openAccessPdf": None,
            }
        ]
    }

    with patch("requests.post", return_value=mock_resp):
        with patch("adda._src.literature.http_client._sleep"):
            result = tools["get_semantic_scholar_recommendations"](
                "1706.03762", n_results=5
            )

    import json
    data = json.loads(result)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["title"] == "Similar Paper"


def test_get_ss_recommendations_returns_error_on_failure(tmp_path):
    """get_semantic_scholar_recommendations returns ERROR on network failure."""
    agent = _make_agent()
    lit_dir = tmp_path / "lit"
    from adda._src.agents.literature_tools import build_literature_providers
    tools = build_literature_providers(tmp_path, lit_dir)

    # get_semantic_scholar_recommendations comes from
    # build_recommendations_closure(), which imports no optional client
    # library (unlike build_semantic_scholar_closures) -- it is present
    # whenever the literature stack imports at all, same reasoning as
    # search_openalex above.
    assert "get_semantic_scholar_recommendations" in tools

    import requests as _requests
    with patch("requests.post", side_effect=_requests.RequestException("Network error")):
        with patch("adda._src.literature.http_client._sleep"):
            result = tools["get_semantic_scholar_recommendations"]("1706.03762")

    assert "ERROR" in result


def test_cap_result_truncates_oversized_payloads():
    """A search/read payload bigger than the cap is truncated with a marker, so it
    never overflows the tool-result token limit and gets dropped whole (observed
    every run: 'exceeds maximum allowed tokens')."""
    from adda._src.agents.literature_tools.throttle import _MAX_RESULT_CHARS, _cap_result
    small = "ok" * 10
    assert _cap_result(small) == small               # under cap: untouched
    big = "x" * (_MAX_RESULT_CHARS + 5000)
    out = _cap_result(big)
    assert len(out) < len(big) and "more" in out
    assert out.startswith("x" * 100)                 # keeps the head
    assert _cap_result(12345) == "12345"             # coerces non-str


def test_cap_result_pages_through_the_payload_by_offset():
    """Nothing past the cap is lost: each page names the offset of the next, and
    the pages concatenate back to the whole payload."""
    from adda._src.agents.literature_tools.throttle import _MAX_RESULT_CHARS, _cap_result
    body = "".join(chr(97 + i % 26) for i in range(2 * _MAX_RESULT_CHARS + 700))
    got, offset = "", 0
    while True:
        page = _cap_result(body, offset, "Tool(offset=")
        text = page.split("\n\n[", 1)[0]
        got += text
        if "Next page: Tool(offset=" not in page:
            break
        offset = int(page.rsplit("offset=", 1)[1].rstrip(")]"))
    assert got == body
    assert "past the end" in _cap_result(body, len(body) + 1)


def test_consult_literature_lists_reads_and_searches(tmp_path):
    """ConsultLiterature absorbed CorpusList and CorpusGetPaper: no query
    lists the corpus, a paper_id from that list reads the paper in full,
    anything else searches passages -- the same shape as every other
    reference lookup."""
    md_file = tmp_path / "paper.md"
    md_file.write_text("<!-- page 1 -->\n"
                       + "Content about tensegrity metamaterials. " * 200,
                       encoding="utf-8")
    agent = _make_agent()
    tools = agent.build_closure_tools(
        study_dir=tmp_path, lit_reviewer_notes_dir=tmp_path / "lit")
    pid = tools["CorpusAdd"](str(md_file), title="Tensegrity Paper",
                             arxiv_id="9999.99999")
    consult = tools["ConsultLiterature"]
    assert "Tensegrity Paper" in consult() and pid in consult()
    full = consult(pid)
    assert full.count("tensegrity metamaterials") >= 100   # the whole text
    hit = consult("tensegrity metamaterials", limit=2)
    assert "tensegrity" in hit.lower() and hit != full
