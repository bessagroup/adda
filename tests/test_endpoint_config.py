"""config.yaml owns the endpoint, the node identity and the top-level keys."""
from __future__ import annotations

import pytest

from adda._src.backends.base import Agent, Graph
from adda._src.runtime import node_tools as nt
from adda._src.runtime.agent_runtime import AgenticRun
from adda._src.runtime.study_config import validate_top_level
from adda._src.viewer.study_edit import validate_config


class _A(Agent):
    description = "a"
    tools = frozenset({"Read"})
    model = "class-model"


def _graph() -> Graph:
    return Graph(nodes={"a": _A()}, edges=(), entry="a")


def _run(tmp_path, cfg_text: str = "") -> AgenticRun:
    (tmp_path / "config.yaml").write_text(cfg_text, encoding="utf-8")
    return AgenticRun(tmp_path, graph=_graph(), interactive=False)


def test_unknown_top_level_key_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="modle.*did you mean model"):
        _run(tmp_path, "modle: x\n")


def test_the_study_mapping_is_reserved_and_never_read(tmp_path):
    _run(tmp_path, "study:\n  ablate: {a: 1}\n")
    assert validate_top_level({"study": {"anything": [1]}}) == []
    assert validate_top_level({"study": 3})


def test_viewer_rejects_an_unknown_top_level_key():
    assert not validate_config("bogus: 1\nbudget: 60\n")["ok"]
    assert validate_config("study: {x: 1}\nbudget: 60\n")["ok"]


def test_node_identity_comes_from_the_nodes_block():
    g = _graph()
    nt.apply_node_config(g, {"a": {"model": "m2", "backend": "vllm",
                                   "base_url": "http://h:1/v1"}})
    a = g.nodes["a"]
    assert (a.model, a.backend, a.base_url) == ("m2", "vllm", "http://h:1/v1")
    nt.apply_node_config(g, {})
    assert (a.model, a.backend, a.base_url) == ("class-model", None, None)


def test_node_identity_keys_must_be_strings():
    assert nt.validate_nodes_block({"a": {"model": 3}})
    assert nt.validate_nodes_block({"a": {"base_url": ""}})


def test_base_url_precedence_node_then_top_then_served(tmp_path):
    from adda._src.backends.vllm import VLLMAdapter
    run = _run(tmp_path, "backend: vllm\nbase_url: http://top:1/v1\n")
    agent = _A()
    assert run._resolve_base_url("a", agent, VLLMAdapter) == "http://top:1/v1"
    agent.base_url = "http://node:2/v1"
    assert run._resolve_base_url("a", agent, VLLMAdapter) == "http://node:2/v1"
    run._base_url = None
    agent.base_url = None
    run._served_base_url = "http://gpu:3/v1"
    assert run._resolve_base_url("a", agent, VLLMAdapter) == "http://gpu:3/v1"
    run._served_base_url = None
    assert run._resolve_base_url("a", agent, VLLMAdapter) is None


def test_endpoint_reaches_the_adapter_without_the_environment(tmp_path, monkeypatch):
    from adda._src.backends.vllm import VLLMAdapter
    run = _run(tmp_path, "backend: vllm\nbase_url: http://cfg:1/v1\n")
    assert run._resolve_base_url("a", _A(), VLLMAdapter) == "http://cfg:1/v1"


def test_a_node_endpoint_on_a_backend_without_one_is_refused(tmp_path):
    class NoEndpoint:
        def __init__(self, model=None):
            pass

    run = _run(tmp_path)
    agent = _A()
    agent.base_url = "http://x/v1"
    with pytest.raises(ValueError, match="no endpoint"):
        run._resolve_base_url("a", agent, NoEndpoint)
    agent.base_url = None
    run._base_url = "http://x/v1"
    assert run._resolve_base_url("a", agent, NoEndpoint) is None


@pytest.mark.parametrize("var", ["VLLM_BASE_URL", "OLLAMA_BASE_URL",
                                 "OPENROUTER_BASE_URL"])
def test_an_exported_endpoint_variable_is_refused(var, monkeypatch):
    from adda._src.runtime import settings
    monkeypatch.setenv(var, "http://env:9/v1")
    with pytest.raises(ValueError, match="base_url"):
        settings.reject_stale_env()
