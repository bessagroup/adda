"""Every node's turns are filed under that node's own name."""
from __future__ import annotations

import pytest

from adda._src.backends import base
from adda._src.nodes import Node
from adda._src.runtime import settings

from .test_route_aware_termination import StubAdapter, _minimal_spec


@pytest.fixture(autouse=True)
def _clean():
    settings.configure(None)
    yield
    settings.configure(None)
    base.set_transcript_sink(None)


class _SinkAdapter(StubAdapter):
    def invoke(self, messages):
        self.sink = base.get_transcript_sink()
        return super().invoke(messages)


@pytest.mark.parametrize("name", ["strategizer", "planner"])
def test_turn_transcript_and_usage_phase_use_the_node_name(tmp_path, name):
    settings.configure({"debug": True})
    adapter = _SinkAdapter()
    node = Node(adapter, name=name, outgoing=["implementer"],
                spec=_minimal_spec(name))
    node._current_notes_dir = tmp_path / "debug" / f"{name}_notes"
    seen = []
    node._record_usage = lambda usage, **kw: seen.append(kw)
    node._invoke_turn([{"role": "user", "content": "go"}])
    assert adapter.sink == str(
        tmp_path / "debug" / "transcripts" / name / "turn_001.jsonl")
    assert seen[0]["phase"] == f"{name}_turn"
