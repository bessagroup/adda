"""Every agent is told which agents EXIST, from the live graph.

A campaign on a 2- and 3-node graph logged 17 delegations to agents that were
not wired: 15 to 'implementer', 2 to 'literature_reviewer'. The cause was not
the resolver — ``resolve_target`` matches by live node name then role, and the
failure message already prints ``Valid targets: {node._outgoing}``. It was the
static prompt.

That prompt is written against the full cast, and it was careful about most of
it: the literature-reviewer and datagenerator routes are both qualified "WHEN
PRESENT", and the agent is told to take exact names from the Delegate hints.
But it also designated a FALLBACK, unconditionally — "if no specialist matches
a block, the general implementer handles it" — and routed blocks 2-4, the bulk
of the work, to "the implementer role" with no qualifier. In a graph with no
implementer that is a guaranteed miss on almost every delegation, which is why
one name accounts for 15 of the 17 and the qualified roles for 2.

So the fix is topology, not emphasis: the run's real roster is generated into
the prompt, and the prose stops asserting that any particular agent exists.

It was the entry node's alone at first. Workers were told nothing about the
team -- their preamble named "the Strategizer" by hand -- while every worker
holds Confer, which takes an agent's name. Now it is one list, the same for
every agent.
"""
from __future__ import annotations

from adda._src.backends.base import Edge, Graph
from adda._src.agents.critic import AdversarialCritiqueAgent
from adda._src.agents.strategizer import StrategizerAgent


def _two_node_graph():
    return Graph(
        nodes={"strategizer": StrategizerAgent(),
               "critic": AdversarialCritiqueAgent()},
        edges=(Edge(source="strategizer", target="critic"),),
        entry="strategizer",
    )


def _roster(graph, name="strategizer") -> str:
    """The roster exactly as the runtime builds it, without starting a run."""
    from adda._src.runtime.agent_runtime import AgenticRun

    run = AgenticRun.__new__(AgenticRun)
    run._graph_spec = graph
    return run._team_roster(name)


def test_the_roster_names_only_the_agents_that_exist():
    out = _roster(_two_node_graph())

    assert "critic" in out
    assert "implementer" not in out
    assert "literature_reviewer" not in out


def test_the_roster_is_read_from_the_edges_not_the_node_dict():
    """A node declared but not wired from the entry is NOT on the team.

    Keying off `graph.nodes` would list it and reintroduce the bug in a new
    form: a name the agent can see but cannot reach.
    """
    g = Graph(
        nodes={"strategizer": StrategizerAgent(),
               "critic": AdversarialCritiqueAgent(),
               "orphan": AdversarialCritiqueAgent()},
        edges=(Edge(source="strategizer", target="critic"),),
        entry="strategizer",
    )

    out = _roster(g)

    assert "critic" in out
    assert "orphan" not in out


def test_a_one_node_graph_gets_no_roster():
    """There is no one else to name; an empty roster would be noise."""
    g = Graph(nodes={"strategizer": StrategizerAgent()}, edges=(),
              entry="strategizer")

    assert _roster(g) == ""


def test_the_roster_says_an_unlisted_role_is_not_wired():
    """The static prompt still describes the full cast — that is where the
    method lives. What it must not do is imply the cast is present."""
    out = _roster(_two_node_graph())

    assert "AUTHORITATIVE" in out
    assert "IS NOT\nWIRED" in out or "IS NOT WIRED" in out
    assert "no general-purpose fallback" in out


def test_the_prompt_designates_no_unconditional_fallback_agent():
    """The exact sentence behind 15 of the 17 misses."""
    p = StrategizerAgent().system_prompt

    assert "the general implementer handles it" not in p
    assert "to the\n    implementer role." not in p
    # the roles it does describe are all marked conditional
    assert p.count("WHEN PRESENT") >= 3


def test_the_prompt_points_at_the_roster_for_names():
    p = StrategizerAgent().system_prompt

    assert "<team>" in p


def test_every_agent_reads_the_same_list():
    """One roster, not one per viewer: only the closing line differs."""
    g = _two_node_graph()
    lead, worker = _roster(g, "strategizer"), _roster(g, "critic")
    assert lead.endswith("You are strategizer.\n</team>\n")
    assert worker.endswith("You are critic.\n</team>\n")
    strip = lambda r: r.rsplit("You are ", 1)[0]
    assert strip(lead) == strip(worker)


def test_the_roster_says_who_hands_each_agent_work():
    """The wiring, not just the cast: a worker that delegates onward shows up
    as a sender, so an agent can see who may message it and why."""
    from adda._src.agents.literature import LiteratureReviewAgent
    g = Graph(
        nodes={"lead": StrategizerAgent(), "critic": AdversarialCritiqueAgent(),
               "lit": LiteratureReviewAgent()},
        edges=(Edge("lead", "critic"), Edge("lead", "lit"),
               Edge("critic", "lit")),
        entry="lead")
    out = _roster(g, "lit")
    assert "lead  (role: strategizer; entry; delegates to: critic, lit)" in out
    assert "lit  (role: literature_reviewer; tasks from lead, critic)" in out


def test_the_roster_shows_outgoing_delegation_targets_only_within_the_team():
    """A node's outgoing edges are its capability to delegate onward — shown
    for every node, not just the entry's own targets, so an agent reading
    about a PEER can see what that peer may do next, not only who feeds it."""
    from adda._src.agents.implementer import F3dasmImplementerAgent
    from adda._src.agents.math_expert import MathExpertAgent
    g = Graph(
        nodes={"lead": StrategizerAgent(),
               "implementer": F3dasmImplementerAgent(),
               "math_expert": MathExpertAgent()},
        edges=(Edge("lead", "implementer"), Edge("implementer", "math_expert")),
        entry="lead")
    out = _roster(g, "lead")
    assert "implementer  (role: implementer; tasks from lead; delegates to: math_expert)" in out
    assert "math_expert  (role:" in out and "delegates to:" not in out.split(
        "math_expert  (role:")[1].splitlines()[0]


def test_the_worker_preamble_names_the_entry_from_the_graph():
    """It said "the Strategizer" by hand; an entry node named otherwise made
    the preamble name an agent that does not exist."""
    from adda._src.prompts.agent_prompts import WORKSPACE_PREAMBLE_TEMPLATE
    assert "Strategizer" not in WORKSPACE_PREAMBLE_TEMPLATE
    assert "{entry}" in WORKSPACE_PREAMBLE_TEMPLATE
    assert "{roster}" in WORKSPACE_PREAMBLE_TEMPLATE
