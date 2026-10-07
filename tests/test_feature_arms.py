"""Every ablation arm, built end to end, honours its declaration.

``test_features.py`` checks the registry piece by piece (the strip function,
the tool subtraction, one catalog). This file builds the DEFAULT graph through
the real ``AgenticRun._make_adapter`` + ``build_graph`` path, with one feature
off at a time, and reads what each node's model would actually receive: the
rendered system prompt (preamble, role prompt, ``<tools>`` catalog) and the
tool set. A leftover mention of a withheld tool is the "agent told about a tool
it does not have" failure the registry exists to prevent.

The same is done for a graph with one node removed and with a node added.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from adda._src.backends.base import Edge, Graph
from adda._src.runtime import features, settings


@pytest.fixture(autouse=True)
def _clean_settings():
    settings.configure(None)
    yield
    settings.configure(None)


@dataclass
class Built:
    prompts: dict[str, str]
    tools: dict[str, frozenset[str]]
    nodes: dict


def _default_spec() -> Graph:
    from adda._src.agents._graphs import _default_graph
    return _default_graph()


def _spec_without(name: str) -> Graph:
    spec = _default_spec()
    return Graph(
        nodes={n: a for n, a in spec.nodes.items() if n != name},
        edges=tuple(e for e in spec.edges if name not in (e.source, e.target)),
        entry=spec.entry,
    )


def _spec_with(name: str, agent) -> Graph:
    spec = _default_spec()
    return Graph(
        nodes={**spec.nodes, name: agent},
        edges=(*spec.edges, Edge("strategizer", name)),
        entry=spec.entry,
    )


def _build(tmp_path, spec: Graph, runtime: dict | None = None) -> Built:
    """The default run path, minus the LLM: real adapters, real Nodes."""
    import yaml

    from adda._src.backends.base import Agent  # noqa: F401
    from adda._src.infra.delegation_log import DelegationLog
    from adda._src.runtime.agent_runtime import AgenticRun
    from adda._src.runtime.graph_builder import build_graph
    from tests.test_claude_adapter import _get_adapter, _install_fake_sdk

    _install_fake_sdk()
    _get_adapter()
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "PROBLEM_STATEMENT.md").write_text("A test problem.")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"backend": "claude"}))
    run = AgenticRun(tmp_path, graph=spec, runtime=runtime or {})
    settings.configure(run._study_runtime, run._runtime_override)

    run_dir = tmp_path / "runs" / "r1"
    notes = run_dir / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    (run_dir / "experiment_data").mkdir()
    run._run_dir = run_dir

    live: dict = {}
    build_graph(
        spec, run._make_adapter, study_dir=tmp_path, notes_dir=notes,
        workspace_dir=run_dir / "debug" / "delegations",
        delegation_log=DelegationLog(run_dir / "debug" / "delegation_log.jsonl"),
        node_registry=live,
    )
    prompts = {n: node.adapter._render_system_prompt() for n, node in live.items()}
    tools = {n: frozenset(node.adapter.closure_tools) for n, node in live.items()}
    return Built(prompts=prompts, tools=tools, nodes=live)


def _mentions(text: str, name: str) -> bool:
    """``name`` as a word, including the qualified ``mcp__<server>__name``.

    f3dasm's own ``@datagenerator`` decorator and its import line share a name
    with the ``datagenerator`` node; they are API symbols, not the node.
    """
    text = re.sub(r"@datagenerator\b", "", text)
    text = text.replace("create_sampler, datagenerator)", "")
    return re.search(
        rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text) is not None


FEATURE_KEYS = [f.key for f in features.FEATURES]


# --- (e) everything on is the baseline ---------------------------------------

def test_all_features_on_is_byte_identical_to_the_baseline(tmp_path):
    baseline = _build(tmp_path / "a", _default_spec())
    explicit = _build(
        tmp_path / "b", _default_spec(),
        runtime={k: True for k in FEATURE_KEYS})
    assert explicit.tools == baseline.tools
    for node, text in baseline.prompts.items():
        assert explicit.prompts[node].replace(str(tmp_path / "b"), "<s>") == \
            text.replace(str(tmp_path / "a"), "<s>"), node


# --- one feature off at a time ------------------------------------------------

@pytest.mark.parametrize("key", FEATURE_KEYS)
def test_arm_withholds_its_tools_everywhere(tmp_path, key):
    """(a) No withheld tool is in any node's tool set or rendered catalog."""
    built = _build(tmp_path, _default_spec(), runtime={key: False})
    withheld = features.disabled_tool_names()
    assert withheld >= features.by_key(key).tools
    for node in built.nodes:
        assert not (built.tools[node] & withheld), (node, built.tools[node] & withheld)
        headings = set(re.findall(r"^### (\w+)$", built.prompts[node], re.M))
        assert not (headings & withheld), (node, headings & withheld)


@pytest.mark.parametrize("key", FEATURE_KEYS)
def test_arm_removes_its_prompt_sections_everywhere(tmp_path, key):
    """(b) No owned section tag, and no unresolved gate marker, remains."""
    built = _build(tmp_path, _default_spec(), runtime={key: False})
    owned = [t for f in features.FEATURES if not features.enabled(f.key)
             for t in f.sections]
    for node, text in built.prompts.items():
        for tag in owned:
            assert f"<{tag}>" not in text, (node, tag)
        assert "[[if" not in text and "[[/if]]" not in text, node


@pytest.mark.parametrize("key", FEATURE_KEYS)
def test_arm_leaves_no_other_prompt_text_naming_a_withheld_tool(tmp_path, key):
    """(c) A leftover mention tells the agent about a tool it does not have."""
    built = _build(tmp_path, _default_spec(), runtime={key: False})
    withheld = features.disabled_tool_names()
    leftovers = {
        (node, tool)
        for node, text in built.prompts.items()
        for tool in sorted(withheld) if _mentions(text, tool)
    }
    assert not leftovers, sorted(leftovers)


OBJECT_BACKED = ["hypothesis_ledger", "milestones_enabled", "science_monitor",
                 "reproduction_gate", "verdict_validator"]


@pytest.mark.parametrize("key", OBJECT_BACKED)
def test_arm_does_not_construct_its_backing_object(tmp_path, key):
    """(d) Off means not built, not built-and-ignored."""
    built = _build(tmp_path, _default_spec(), runtime={key: False})
    strat = built.nodes["strategizer"]
    if key == "hypothesis_ledger":
        assert strat._ledger is None
    elif key == "milestones_enabled":
        assert strat._milestones is None
    elif key == "science_monitor":
        assert strat._science_monitor is None
    elif key == "reproduction_gate":
        assert strat._reproduction_gate() is None
    else:
        from adda._src.nodes.critic_gate import verdict_validator_enabled
        assert verdict_validator_enabled() is False


# --- graph composition --------------------------------------------------------

REMOVABLE = ["literature_reviewer", "critic"]

# Removing these two leaves DESCRIPTIVE mentions (the strategizer's roster text
# is conditional "WHEN PRESENT", OracleStatus names the datagenerator's effect,
# the datagenerator's own prompt names the implementer). What must go is text
# that INSTRUCTS the agent to delegate to the absent node.
INSTRUCTING = {
    "datagenerator": ["delegate a datagenerator", "via the datagenerator"],
    "implementer": ["Delegate('implementer'", "delegate implementers"],
}


@pytest.mark.parametrize("name", sorted(INSTRUCTING))
def test_a_removed_node_is_not_instructed_by_any_remaining_prompt(tmp_path, name):
    built = _build(tmp_path, _spec_without(name))
    assert name not in built.nodes
    from adda._src.knowledge.kb import KnowledgeBase
    kb_titles = " ".join(e.title for e in KnowledgeBase.load().entries)
    for phrase in INSTRUCTING[name]:
        assert phrase not in kb_titles, phrase
        for node, text in built.prompts.items():
            assert phrase not in text, (node, phrase)


@pytest.mark.parametrize("name", REMOVABLE)
def test_a_removed_node_is_not_named_by_any_remaining_prompt(tmp_path, name):
    built = _build(tmp_path, _spec_without(name))
    assert name not in built.nodes
    leftovers = sorted(n for n, text in built.prompts.items()
                       if _mentions(text, name))
    assert not leftovers, leftovers


@pytest.mark.parametrize("name", ["math_expert", "debugger"])
def test_an_added_node_is_named_by_its_delegator(tmp_path, name):
    from adda._src.agents.debugger import DebuggerAgent
    from adda._src.agents.math_expert import MathExpertAgent

    agent = {"math_expert": MathExpertAgent, "debugger": DebuggerAgent}[name]()
    built = _build(tmp_path, _spec_with(name, agent))
    assert name in built.nodes
    assert _mentions(built.prompts["strategizer"], name)


def test_the_detector_fires_on_a_known_leftover(tmp_path):
    """Positive control: with the ledger off, a prompt that names a ledger tool is caught."""
    built = _build(tmp_path, _default_spec(), runtime={"hypothesis_ledger": False})
    withheld = features.disabled_tool_names()
    assert withheld
    tool = sorted(withheld)[0]
    assert _mentions(f"call mcp__f3dasm_agent_tools__{tool} now", tool)
    assert _mentions(f"call {tool}(x)", tool)
    assert not _mentions(f"{tool}Extra", tool)
    assert not any(_mentions(t, tool) for t in built.prompts.values())
