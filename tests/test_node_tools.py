"""The ``Default`` tool token and the config.yaml ``nodes:`` block (headless)."""
from __future__ import annotations

import json
import logging

import pytest

from adda._src.backends.base import DEFAULT_TOOLS, Agent, Graph
from adda._src.backends.openai_compatible import (
    DEFAULT_NATIVE_TOOLS,
    OpenAICompatibleAdapter,
    _native_tool_map,
)
from adda._src.runtime import node_tools as nt
from adda._src.viewer.study_edit import validate_config
from tests.test_claude_adapter import (
    _capture_options_gen,
    _get_adapter,
    _install_fake_sdk,
    _SystemMessage,
)


class _A(Agent):
    description = "a"
    tools = frozenset({"Read", "Bash"})


class _B(Agent):
    description = "b"
    tools = frozenset({"Read"})


def _graph() -> Graph:
    return Graph(nodes={"a": _A(), "b": _B()}, edges=(), entry="a")


def test_default_is_the_token_Default():
    assert nt.DEFAULT == DEFAULT_TOOLS == "Default"


def test_config_list_replaces_class_set_and_records_the_diff():
    g = _graph()
    recs = {r["node"]: r for r in nt.apply_node_config(
        g, {"a": {"tools": ["Default", "Read"]}})}
    assert g.nodes["a"].tools == frozenset({"Default", "Read"})
    assert recs["a"]["source"] == "config"
    assert recs["a"]["added"] == ["Default"] and recs["a"]["removed"] == ["Bash"]
    assert g.nodes["b"].tools == frozenset({"Read"}) and recs["b"]["source"] == "class"


def test_second_config_on_same_graph_starts_from_the_class_set():
    g = _graph()
    nt.apply_node_config(g, {"a": {"tools": ["Default"]}})
    recs = {r["node"]: r for r in nt.apply_node_config(g, {})}
    assert g.nodes["a"].tools == frozenset({"Read", "Bash"})
    assert recs["a"]["source"] == "class"


def test_class_attribute_is_never_mutated():
    g = _graph()
    nt.apply_node_config(g, {"a": {"tools": ["Default"]}})
    assert _A.tools == frozenset({"Read", "Bash"})


def test_unknown_node_is_a_loud_error():
    with pytest.raises(ValueError, match="no such node"):
        nt.apply_node_config(_graph(), {"nope": {"tools": ["Read"]}})


@pytest.mark.parametrize("bad", [
    ["a"], {"a": ["Read"]}, {"a": {"tools": "Read"}}, {"a": {"tools": [1]}},
    {"a": {"temperature": 1}},
])
def test_malformed_block_is_rejected_by_runtime_and_viewer(bad):
    with pytest.raises(ValueError):
        nt.apply_node_config(_graph(), bad)
    import yaml
    out = validate_config(yaml.safe_dump({"budget": 60, "nodes": bad}))
    assert not out["ok"]


def test_validate_config_accepts_a_good_block():
    text = "budget: 60\nnodes:\n  a:\n    tools: [Default, Read]\n"
    assert validate_config(text)["ok"]


def _rows(tmp_path):
    return [json.loads(x) for x in
            (tmp_path / "diagnostics.jsonl").read_text().splitlines()]


def test_notices_are_informational_diagnostics_and_a_record(tmp_path):
    g = _graph()
    recs = nt.apply_node_config(g, {"a": {"tools": ["Default", "Read"]}})
    nt.record_resolution(tmp_path, recs, logging.getLogger("t"))
    kinds = {r["error_type"] for r in _rows(tmp_path)}
    assert kinds == {"TOOLS_CONFIG_DIFFERS", "DEFAULT_TOOLS_BYPASS"}
    assert all(r["fault"] == "nudge" for r in _rows(tmp_path))
    rec = json.loads((tmp_path / "node_tools.json").read_text())
    assert rec["a"]["resolved"] == ["Default", "Read"] and rec["b"]["source"] == "class"


def test_no_notice_when_nothing_differs(tmp_path):
    recs = nt.apply_node_config(_graph(), {})
    nt.record_resolution(tmp_path, recs, logging.getLogger("t"))
    assert not (tmp_path / "diagnostics.jsonl").exists()


def test_builtins_record_names_unreviewed_ones(tmp_path):
    nt.record_builtins(tmp_path, "a", ["Read", "Frobnicate", "mcp__x__y"])
    (row,) = _rows(tmp_path)
    assert row["error_type"] == "TOOLS_RESOLVED"
    assert row["unreviewed"] == ["Frobnicate"] and "mcp__x__y" not in row["builtins"]


def test_other_backend_default_expands_to_native_set_with_notice(tmp_path):
    native = OpenAICompatibleAdapter.select_native_tools({"Default", "Done"})
    assert sorted(native) == sorted(DEFAULT_NATIVE_TOOLS)
    nt.record_native_expansion(tmp_path, "a", "ollama", native, logging.getLogger("t"))
    assert _rows(tmp_path)[0]["error_type"] == "DEFAULT_TOOLS_EXPANDED"


def test_default_native_names_equal_the_native_tool_map():
    assert set(DEFAULT_NATIVE_TOOLS) == set(_native_tool_map(None))


def _options(native, closures=None, **kw):
    cap: dict = {}
    _install_fake_sdk(query=_capture_options_gen(cap),
                      SdkMcpTool=lambda **k: k)
    cls = _get_adapter()
    adapter = cls("claude-3", "sys", None, cls.select_native_tools(native),
                  closures or {}, **kw)
    adapter.invoke([{"role": "user", "content": "hi"}])
    return cap["options"]


def test_claude_default_uses_the_preset_and_no_floor():
    o = _options(["Default"])
    assert o["tools"] == {"type": "preset", "preset": "claude_code"}
    for floor in ("WebSearch", "WebFetch", "Task", "ExitPlanMode", "Bash", "Read"):
        assert floor not in o["disallowed_tools"]


def test_claude_without_default_keeps_the_floor():
    o = _options(["Read"])
    assert o["tools"] == ["Read"]
    for floor in ("WebSearch", "WebFetch", "Task", "ExitPlanMode", "Bash"):
        assert floor in o["disallowed_tools"]


def test_claude_default_still_blocks_a_builtin_a_closure_replaces():
    o = _options(["Default"], {"Write": lambda path, content: ""})
    assert "Write" in o["disallowed_tools"]
    assert "Bash" not in o["disallowed_tools"]


def test_init_record_reaches_the_callback_once_per_message():
    seen: list = []

    async def _gen(prompt, options):
        yield _SystemMessage("init", {"tools": ["Read", "WebSearch"]})
        from tests.test_claude_adapter import _AssistantMessage, _ResultMessage, _TextBlock
        yield _AssistantMessage([_TextBlock("ok")])
        yield _ResultMessage()

    _install_fake_sdk(query=_gen)
    adapter = _get_adapter()("claude-3", "sys", None, ["Default"])
    adapter.on_init_tools = seen.append
    adapter.invoke([{"role": "user", "content": "hi"}])
    assert seen == [["Read", "WebSearch"]]


def test_agentic_run_applies_config_tools_and_rejects_unknown_node(tmp_path):
    import yaml

    from adda._src.runtime.agent_runtime import AgenticRun

    (tmp_path / "PROBLEM_STATEMENT.md").write_text("test")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"nodes": {"a": {"tools": ["Default"]}}}))
    run = AgenticRun(tmp_path, graph=_graph())
    assert run._graph_spec.nodes["a"].tools == frozenset({"Default"})
    cfg.write_text(yaml.dump({"nodes": {"zzz": {"tools": ["Read"]}}}))
    with pytest.raises(ValueError, match="no such node"):
        AgenticRun(tmp_path, graph=_graph())


def test_viewer_refuses_a_secret_in_the_runtime_block():
    from adda._src.viewer.study_edit import validate_config

    out = validate_config("runtime:\n  semantic_scholar_api_key: abc\n")
    assert any("SEMANTIC_SCHOLAR_API_KEY" in e for e in out["errors"])


def _live_closures(tmp_path, nodes_block):
    import shutil
    from pathlib import Path

    import yaml

    from adda._src.runtime import settings
    from adda._src.runtime.agent_runtime import AgenticRun
    from adda._src.runtime.graph_builder import build_graph

    src = Path(__file__).resolve().parent.parent / "studies" / "example_study"
    study = tmp_path / "study"
    study.mkdir(parents=True)
    shutil.copy(src / "PROBLEM_STATEMENT.md", study)
    cfg = yaml.safe_load((src / "config.yaml").read_text()) or {}
    if nodes_block:
        cfg["nodes"] = nodes_block
    (study / "config.yaml").write_text(yaml.safe_dump(cfg))
    logging.disable(logging.CRITICAL)
    try:
        run = AgenticRun(study_dir=study, review_statement=False, interactive=False)
        settings.configure(run._study_runtime, run._runtime_override)
        ctx = run._prepare_run()
        live: dict = {}
        build_graph(run._graph_spec, run._make_adapter, study_dir=run.study_dir,
                    interactive=False, notes_dir=ctx.notes_dir,
                    workspace_dir=ctx.workspace_dir,
                    delegation_log=ctx.delegation_log, node_registry=live)
        return {n: set(node.adapter.closure_tools) for n, node in live.items()}
    finally:
        logging.disable(logging.NOTSET)
        settings.configure(None)


def test_a_config_tools_list_withholds_the_always_on_closures(tmp_path):
    base = _live_closures(tmp_path / "a", None)
    assert {"ConsultHandbook", "ReportEvals", "RecallHistory", "Write"} <= base["implementer"]
    cut = _live_closures(tmp_path / "b", {"implementer": {"tools": ["Read", "Bash"]}})
    assert not ({"ConsultHandbook", "ConsultLiterature", "ReportEvals",
                 "RecallHistory", "Write"} & cut["implementer"])
    assert cut["strategizer"] == base["strategizer"]
    kept = _live_closures(tmp_path / "c", {"implementer": {
        "tools": ["Read", "ReportEvals", "ConsultHandbook"]}})
    assert {"ReportEvals", "ConsultHandbook"} <= kept["implementer"]
    assert not {"RecallHistory", "Write"} & kept["implementer"]


def test_default_keeps_the_sandboxed_write(tmp_path):
    cut = _live_closures(tmp_path, {"implementer": {"tools": ["Default"]}})
    assert "Write" in cut["implementer"]
    assert "ConsultHandbook" not in cut["implementer"]


def test_the_notice_names_the_withheld_closures(tmp_path):
    g = _graph()
    rec = nt.apply_node_config(g, {"a": {"tools": ["Read"]}})
    assert "ConsultHandbook" in rec[0]["withheld"]
    assert "ReportEvals" in nt.DIFF_NOTICE.format(
        node="a", added=[], removed=[], withheld=rec[0]["withheld"])
