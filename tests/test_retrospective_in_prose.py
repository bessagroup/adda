"""The exit interview answered in prose (run 20261010T173930, lattice adda r1:
the reply ended the turn, the generic re-prompt reached a fresh session, and
its improvised Done() text was recorded as the retrospective)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from langchain_core.messages import AIMessage

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.nodes import Node

PROSE = (
    "### Retrospective\n"
    "- **CONSISTENCY: flagged.** M003 was marked DONE before the oracle was "
    "validated.\n- **DECISION:** local refinement with the last evals.\n"
    "- **FRICTION:** the stable flag tests only positive-definiteness.\n"
    "- **BLOCKED:** none.")


def _node(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent))
    from test_backend_parity import _instantiate

    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done"})
        description = "s"

    class B(Agent):
        description = "i"

    spec = Graph(nodes={"strategizer": A(), "implementer": B()},
                 edges=(Edge("strategizer", "implementer"),),
                 entry="strategizer")
    node = Node(_instantiate("claude"), name="strategizer",
                outgoing=["implementer"], spec=spec, study_dir=tmp_path)
    debug = tmp_path / "debug"
    (debug / "strategizer_notes").mkdir(parents=True)
    node._current_notes_dir = debug / "strategizer_notes"
    node._awaiting_retro = True
    node._final_summary = "the accepted conclusion"
    return node, debug


def _records(debug):
    return [json.loads(x) for x in
            (debug / "retrospectives.jsonl").read_text().splitlines()]


def test_a_prose_retrospective_is_captured_and_closes_the_run(tmp_path):
    node, debug = _node(tmp_path)
    node._route_turn({"study_dir": str(tmp_path)}, AIMessage(content=PROSE))
    (rec,) = _records(debug)
    assert rec["source_id"] == "DONE" and not rec["parse_failed"]
    assert rec["flagged"] is True
    assert node._route["kind"] == "done"
    assert node._awaiting_retro is False


def test_prose_without_a_retrospective_is_not_taken_for_one(tmp_path):
    node, debug = _node(tmp_path)
    held = node._route_turn({"study_dir": str(tmp_path)},
                            AIMessage(content="The run is finished."))
    assert not (debug / "retrospectives.jsonl").exists()
    assert node._awaiting_retro is True
    text = held.update["messages"][-1].content
    assert "waiting for your retrospective" in text
    assert "Call Done() once more" in text


def test_the_reprompt_names_only_tools_the_node_holds(tmp_path):
    node, _ = _node(tmp_path)
    node._holds = lambda tool: False
    held = node._route_turn({"study_dir": str(tmp_path)},
                            AIMessage(content="The run is finished."))
    text = held.update["messages"][-1].content
    assert "Done()" not in text and "Reply with a ### Retrospective" in text
