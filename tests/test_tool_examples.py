"""Every tool an agent holds shows at least one call, and every shown call is
one the tool would accept.

WHY A TEST AND NOT A CONVENTION
    A tool's description is read by a model that has never seen the code.
    Prose tells it what a tool is for; an example tells it how to call it,
    and for a tool with optional or format-sensitive parameters that second
    part is where the mistakes are (Anthropic's tool-use guidance says the
    same, for the same reason). The convention existed -- ``@tool_examples``
    -- and three of the strategizer's thirty tools followed it.

    The second half matters as much as the first. An example that names a
    parameter the tool does not have teaches a call that fails, and it would
    keep teaching it after every rename: examples are hand-written text, so
    they drift exactly the way hard-coded tool names in prompts drifted. So
    each one is parsed and checked against the signature of the function it
    decorates -- its own name, only real parameters, no more positional
    arguments than the tool takes.

Checked twice, because either check alone had a hole:

- from the syntax tree, the way the prompt map reads the catalog, so it covers
  a tool whether or not a test ever builds the node that holds it; and
- off the LIVE function objects the agents are given, because that is what
  the catalog renders. The syntax-tree pass alone let two gaps through: the
  literature reviewer's runtime tools were never in its list of names (so none
  had examples and nothing noticed), and a wrapper around them (since
  removed) dropped the ``_tool_examples`` attribute, so an example written in
  the source would still never have reached the agent.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _promptmap():
    spec = importlib.util.spec_from_file_location(
        "_promptmap_for_examples", _ROOT / "internal" / "tools" / "promptmap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _held_tools() -> set[str]:
    """Every closure tool the shipped agents are given."""
    from adda._src.runtime.agent_runtime import _default_graph

    names: set[str] = set()
    for agent in _default_graph().nodes.values():
        names |= set(agent.tools)
    # Granted by topology, by the runtime, or by a knowledge provider rather
    # than listed in an agent's `tools`.
    names |= {"Delegate", "Wait", "FollowUp", "RecallHistory",
              "ConsultHandbook", "ReportEvals", "AskForFeedback",
              "ConsultF3dasm", "ConsultLiterature", "CorpusAdd"}
    return names


def _definitions() -> dict[str, list[ast.FunctionDef]]:
    """Every definition a registered tool name is rendered from."""
    pm = _promptmap()
    out: dict[str, list[ast.FunctionDef]] = {}
    sites = {**{k: v for k, v in pm.injected_tool_docs().items()},
             **pm.tool_docs()}
    for name, doc in sites.items():
        locs = {f"{doc['file']}:{doc['def_line']}", *doc.get("ambiguous", [])}
        for loc in locs:
            file, line = loc.rsplit(":", 1)
            tree = ast.parse((_ROOT / file).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.lineno == int(line)):
                    out.setdefault(name, []).append(node)
    return out


def _examples(fn: ast.FunctionDef) -> list[str]:
    for dec in fn.decorator_list:
        if (isinstance(dec, ast.Call)
                and getattr(dec.func, "id", getattr(dec.func, "attr", ""))
                == "tool_examples"):
            return [a.value for a in dec.args if isinstance(a, ast.Constant)]
    return []


def _native() -> set[str]:
    """Tools the backend CLI provides itself: not ours to document."""
    from adda._src.backends.claude import ClaudeAdapter
    return set(ClaudeAdapter.NATIVE_TOOLS)


def _live_closures() -> dict:
    """Every closure tool a shipped agent builds for itself, as the object the
    agent is handed -- including the reviewer's runtime-only tools, which no
    `tools` declaration names."""
    from adda._src.agents.abaqus_datagenerator import AbaqusDataGeneratorAgent
    from adda._src.nodes.parsing import _consult_handbook
    from adda._src.runtime.agent_runtime import _default_graph

    tmp = Path(tempfile.mkdtemp(prefix="tool_examples_"))
    out = {"ConsultHandbook": _consult_handbook}
    agents = [*_default_graph().nodes.values(),
              AbaqusDataGeneratorAgent(corpus_dir=str(tmp / "abaqus"))]
    for agent in agents:
        params = inspect.signature(agent.build_closure_tools).parameters
        kw = ({"lit_reviewer_notes_dir": tmp / "lit"}
              if "lit_reviewer_notes_dir" in params else {})
        out.update(agent.build_closure_tools(tmp, **kw))
    return out


_DEFS = _definitions()
_HELD = sorted(t for t in _held_tools() - _native())
_LIVE = _live_closures()


def test_the_held_tools_are_found():
    """Guard the guard: if the scan finds nothing, every check below passes
    vacuously -- and a held tool the scan cannot find is a failure, not a
    skip (skipping is how the gaps above went unnoticed)."""
    assert len(_HELD) >= 25, _HELD
    missing = [t for t in _HELD if t not in _DEFS]
    assert not missing, f"held tools with no definition found: {missing}"
    assert len(_LIVE) >= 8, sorted(_LIVE)


@pytest.mark.parametrize("tool", _HELD)
def test_every_held_tool_shows_a_call(tool):
    for fn in _DEFS[tool]:
        assert _examples(fn), (
            f"{tool} (line {fn.lineno}) has no @tool_examples — the model is "
            "told what it does but never shown how to call it")


@pytest.mark.parametrize("tool", _HELD)
def test_every_example_is_a_call_the_tool_accepts(tool):
    for fn in _DEFS[tool]:
        params = [a.arg for a in fn.args.args if a.arg != "self"]
        kwonly = [a.arg for a in fn.args.kwonlyargs]
        for example in _examples(fn):
            call = ast.parse(example, mode="eval").body
            assert isinstance(call, ast.Call), f"{tool}: {example!r} is not a call"
            called = getattr(call.func, "id", None)
            assert called == tool, (
                f"{tool}'s example calls {called!r}: {example!r}")
            assert len(call.args) <= len(params), (
                f"{tool}: {example!r} passes {len(call.args)} positional "
                f"arguments; the tool takes {params}")
            for kw in call.keywords:
                assert kw.arg in params + kwonly, (
                    f"{tool}: {example!r} names {kw.arg!r}, which is not a "
                    f"parameter of the tool ({params + kwonly})")


def _check_call(tool: str, example: str, params: list[str], kwonly: list[str]):
    call = ast.parse(example, mode="eval").body
    assert isinstance(call, ast.Call), f"{tool}: {example!r} is not a call"
    assert getattr(call.func, "id", None) == tool, (
        f"{tool}'s example calls another tool: {example!r}")
    assert len(call.args) <= len(params), (
        f"{tool}: {example!r} passes {len(call.args)} positional arguments; "
        f"the tool takes {params}")
    for kw in call.keywords:
        assert kw.arg in params + kwonly, (
            f"{tool}: {example!r} names {kw.arg!r}, which is not a parameter "
            f"of the tool ({params + kwonly})")


@pytest.mark.parametrize("tool", sorted(_LIVE))
def test_every_live_tool_carries_examples_the_agent_is_shown(tool):
    """What ``render_tool_catalog`` reads: the attribute on the live object."""
    fn = _LIVE[tool]
    examples = getattr(fn, "_tool_examples", None)
    assert examples, (
        f"{tool}: the object the agent is given has no examples, so its "
        "catalog entry shows none")
    sig = inspect.signature(fn).parameters.values()
    positional = [p.name for p in sig if p.kind in (
        p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    kwonly = [p.name for p in sig if p.kind is p.KEYWORD_ONLY]
    for example in examples:
        _check_call(tool, example, positional, kwonly)
