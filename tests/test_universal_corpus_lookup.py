"""Tests for Agent.build_closure_tools()'s default: every agent gets read-only
literature-corpus lookup (ConsultLiterature) without
needing its own override — the same way QueryStore lets every node read the
canonical evaluation ledger without delegating to the data generator.
Acquisition (CorpusAdd, external search) stays literature_reviewer-only,
since finding/vetting a NEW paper needs judgment a raw tool call can't
supply — see LiteratureReviewAgent.build_closure_tools, which overrides the
default entirely rather than extending it."""
from __future__ import annotations

from adda._src.agents.critic import AdversarialCritiqueAgent
from adda._src.agents.datagenerator import DataGeneratorAgent
from adda._src.agents.debugger import DebuggerAgent
from adda._src.agents.implementer import F3dasmImplementerAgent
from adda._src.agents.literature import LiteratureReviewAgent
from adda._src.agents.strategizer import StrategizerAgent

_NON_LITERATURE_AGENTS = (
    StrategizerAgent, F3dasmImplementerAgent, DataGeneratorAgent,
    AdversarialCritiqueAgent, DebuggerAgent,
)


def test_every_non_literature_agent_gets_read_only_corpus_lookup(tmp_path):
    """None of these agents override build_closure_tools — they inherit the
    base default, which must give each of them ConsultLiterature
    (read-only), but never CorpusAdd (acquisition stays
    literature_reviewer-only)."""
    for Ag in _NON_LITERATURE_AGENTS:
        agent = Ag()
        tools = agent.build_closure_tools(study_dir=tmp_path)
        assert "ConsultLiterature" in tools, f"{Ag.__name__} missing ConsultLiterature"
        assert "CorpusList" not in tools  # folded into ConsultLiterature
        assert "CorpusGetPaper" not in tools  # folded into ConsultLiterature
        assert "CorpusAdd" not in tools, (
            f"{Ag.__name__} must not get CorpusAdd — acquisition needs "
            "judgment and stays gated behind a real literature_reviewer "
            "delegation."
        )


def test_non_literature_agent_corpus_lookup_actually_works(tmp_path):
    """The injected closures are not stubs — CorpusList on a fresh corpus
    behaves exactly like the literature_reviewer's own CorpusList."""
    agent = StrategizerAgent()
    tools = agent.build_closure_tools(study_dir=tmp_path)
    assert tools["ConsultLiterature"]() == "Corpus is empty."


def test_non_literature_agent_sees_papers_added_by_literature_reviewer(tmp_path):
    """Regression: the corpus is SHARED, study-scoped storage (see
    agent_runtime.py's _make_adapter and literature_corpus.py) — a paper the
    literature_reviewer adds during its own delegation must show up for any
    other agent's ConsultLiterature/CorpusList, since both resolve the identical
    default path (study_dir/runs/lit_reviewer_notes) when
    lit_reviewer_notes_dir is not explicitly overridden."""
    md_file = tmp_path / "paper.md"
    md_file.write_text(
        "<!-- page 1 -->\n"
        + "Content about tensegrity metamaterials. " * 200,
        encoding="utf-8",
    )

    lit_agent = LiteratureReviewAgent()
    lit_tools = lit_agent.build_closure_tools(study_dir=tmp_path)
    paper_id = lit_tools["CorpusAdd"](
        str(md_file), title="Tensegrity Paper", arxiv_id="9999.99999",
    )
    assert paper_id == "arxiv_9999_99999"

    other_agent = DataGeneratorAgent()
    other_tools = other_agent.build_closure_tools(study_dir=tmp_path)
    listing = other_tools["ConsultLiterature"]()
    assert "Tensegrity Paper" in listing

    result = other_tools["ConsultLiterature"]("tensegrity metamaterials")
    assert "tensegrity" in result.lower() or "Tensegrity" in result


def test_corpus_closures_produce_typed_json_schema_for_every_param(tmp_path):
    """ConsultLiterature/CorpusGetPaper must carry explicit type annotations on
    every parameter, or LangChain's StructuredTool.from_function (what the
    OpenAI-compatible backends -- Ollama/vLLM/OpenRouter -- use to build
    each tool's JSON schema for the model) silently omits the "type" key
    from that parameter's schema entry. Claude's own native adapter never
    goes through this schema-inference path, so a missing annotation is
    invisible there -- confirmed for real: a local model (Qwen3.8:27b via
    Ollama) calling ConsultLiterature failed exactly here. Direct Python
    invocation of the closure (bypassing schema generation) does NOT
    reproduce this -- the bug only shows up in the generated schema, so
    this test checks that, not just that the tool builds/runs."""
    from langchain_core.tools import StructuredTool

    from adda._src.agents.strategizer import StrategizerAgent

    agent = StrategizerAgent()
    tools = agent.build_closure_tools(study_dir=tmp_path)
    for name in ("ConsultLiterature",):
        tool = StructuredTool.from_function(tools[name], name=name)
        for param_name, schema in tool.args.items():
            assert "type" in schema, (
                f"{name}'s parameter {param_name!r} has no 'type' key in "
                f"its generated JSON schema ({schema}) -- missing a type "
                "annotation on the closure's own parameter."
            )


def test_literature_reviewer_still_gets_corpus_add(tmp_path):
    """LiteratureReviewAgent.build_closure_tools fully overrides the base
    default (does not call super()) — confirm it still returns CorpusAdd,
    i.e. this refactor didn't silently drop the literature_reviewer's own
    acquisition capability while adding the universal read-only default."""
    agent = LiteratureReviewAgent()
    tools = agent.build_closure_tools(study_dir=tmp_path)
    assert "CorpusAdd" in tools
    assert "ConsultLiterature" in tools
    assert "CorpusList" not in tools  # folded into ConsultLiterature
    assert "CorpusGetPaper" not in tools  # folded into ConsultLiterature


def test_lit_reviewer_notes_dir_override_respected_by_default(tmp_path):
    """The base default must honor an explicit lit_reviewer_notes_dir (same
    contract as study_dir/runs/lit_reviewer_notes fallback), so callers that
    pass the resolved path (as _make_adapter does) get the SAME corpus a
    literature_reviewer delegation writes to, not a stray default."""
    custom_dir = tmp_path / "custom_corpus_location"
    agent = StrategizerAgent()
    tools = agent.build_closure_tools(
        study_dir=tmp_path, lit_reviewer_notes_dir=custom_dir,
    )
    tools["ConsultLiterature"]()
    assert custom_dir.is_dir()
    assert not (tmp_path / "runs" / "lit_reviewer_notes").exists()


def test_consult_literature_accepts_paper_id_as_a_keyword():
    """Agents call ConsultLiterature(paper_id=...), the name the list output
    and the docstring use; the argument must exist and read that one paper."""
    import inspect

    from adda._src.agents.literature_tools.corpus import (
        build_corpus_read_closures,
    )

    class _Corpus:
        def consult(self, query, limit):
            return f"consult:{query}:{limit}"

        def get_paper(self, paper_id):
            return f"paper:{paper_id}"

    fn = build_corpus_read_closures(_Corpus())["ConsultLiterature"]
    assert "paper_id" in inspect.signature(fn).parameters
    assert "paper_id" in (fn.__doc__ or "")
    assert fn(paper_id="arxiv_2401.00001").startswith("paper:arxiv_2401.00001")
    assert fn("buckling", limit=3).startswith("consult:buckling:3")
    assert fn().startswith("consult::")
