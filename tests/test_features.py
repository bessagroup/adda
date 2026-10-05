"""A feature owns its knob, its tools and its prompt section — or it is a lie.

Turning a feature off used to disable only its runtime object. The tools stayed
registered (they gate on the agent's static `tools` frozenset, never on whether
the backing object exists) and the prompt kept commanding their use, so the
agent called a tool, got `"ERROR: ... not available in this run."`, and the run
recorded an ERROR_RETURN diagnostic — the one KPI whose target is zero. That
arm measures an agent confused by broken tools, not an agent without the
feature.

`internal/BACKLOG.md` #27, #28 and #30 are three successive rounds of exactly
this for `pipeline_deliverable` alone. These tests exist so the fourth round
does not happen to a different feature.
"""
from __future__ import annotations

import pytest

from adda._src.runtime import features, settings


@pytest.fixture(autouse=True)
def _clean_settings():
    settings.configure(None)
    yield
    settings.configure(None)


def _all_role_prompts() -> dict[str, str]:
    """Every agent's system prompt, by class name.

    A feature's section can live in ANY role's prompt — the hypothesis ledger
    is the strategizer's, the f3dasm API lookup belongs to the two agents that
    write f3dasm. Checking only the strategizer would let a section in another
    role silently stop being stripped.
    """
    import inspect

    from adda._src import agents

    out = {}
    for name in dir(agents):
        obj = getattr(agents, name)
        if inspect.isclass(obj) and isinstance(
                getattr(obj, "system_prompt", None), str):
            out[name] = obj.system_prompt
    return out


def _prompt_for(_feature=None) -> str:
    """All role prompts concatenated — for existence checks."""
    return "\n".join(_all_role_prompts().values())


# --- drift: a declaration that does not match reality ----------------------

@pytest.mark.parametrize(
    "tag", [t for f in features.FEATURES for t in f.sections])
def test_every_declared_section_exists_in_the_prompt(tag):
    """A renamed tag would otherwise silently stop being stripped, and the
    feature would quietly go back to being half-disabled."""
    owners = [r for r, p in _all_role_prompts().items() if f"<{tag}>" in p]
    assert owners, f"no role prompt contains a <{tag}> section to strip"
    for r in owners:
        assert f"</{tag}>" in _all_role_prompts()[r], (
            f"<{tag}> is never closed in {r}")


@pytest.mark.parametrize("feature", features.FEATURES, ids=lambda f: f.key)
def test_every_feature_key_is_a_known_runtime_knob(feature):
    """Otherwise setting it in config.yaml warns that it is unrecognised while
    quietly working — the config lying about itself."""
    assert feature.key in settings.KNOWN_KEYS


@pytest.mark.parametrize("feature", features.FEATURES, ids=lambda f: f.key)
def test_a_feature_owns_something(feature):
    """A knob that withholds nothing nameable is not a feature switch; it is a
    setting, and belongs elsewhere.

    ``behaviours`` was added for the third kind: a capability with no tool and
    no prompt surface, such as context trimming. It changes what the model
    sees without the agent ever being told, which makes measuring it more
    important than for a feature the agent can at least notice is gone.
    """
    assert feature.tools or feature.sections or feature.behaviours


def test_done_is_never_owned_by_a_feature():
    """A run must always be able to close, whatever is switched off."""
    for f in features.FEATURES:
        assert "Done" not in f.tools, f.key


# --- behaviour: off means gone ---------------------------------------------

def test_all_defaults_withhold_nothing():
    """A run with no ablate config must be byte-identical to today.

    Migration-sweep commit (spec 12, internal/specs/12-peer-interaction.md):
    ``peer_interaction`` now defaults True like every other feature, so
    ``disabled_tool_names()`` withholds nothing by default -- SendMessage
    ships, and Confer/Reply/the peer-facing FollowUp/ReportProgress are
    withheld by the SAME knob's ON state (not through this registry --
    see nodes/tools/routing/__init__.py and
    WorkerSession.install_worker_tools, which gate them directly, since
    they are not owned by any Feature.tools entry).
    """
    assert features.disabled_tool_names() == frozenset()
    prompt = _prompt_for(None)
    assert features.strip_disabled_sections(prompt) == prompt


def test_a_disabled_feature_takes_its_tools_with_it():
    settings.configure({"hypothesis_ledger": False})

    withheld = features.disabled_tool_names()

    assert "HypothesisPropose" in withheld
    assert "HypothesisList" in withheld
    # another feature's tools are untouched
    assert "MilestoneList" not in withheld


def test_peer_interaction_off_withholds_sendmessage():
    """The one feature whose OFF arm restores an entire OLD tool surface
    instead of just removing its own -- Confer/Reply/the peer-facing
    FollowUp/ReportProgress are not owned by any Feature.tools entry (they
    predate the Feature registry), so their own on/off gating lives
    directly in nodes/tools/routing/__init__.py and
    WorkerSession.install_worker_tools, not here. This test covers the one
    piece the registry DOES own: SendMessage itself."""
    settings.configure({"peer_interaction": False})

    assert "SendMessage" in features.disabled_tool_names()


def test_a_disabled_feature_takes_its_prompt_section_with_it():
    settings.configure({"science_monitor": False})

    out = features.strip_disabled_sections(_prompt_for(None))

    assert "<science_monitor>" not in out
    assert "SCIENCE MONITOR" not in out
    # the neighbouring section survives — the strip is per-tag, not greedy
    assert "<hypothesis_ledger>" in out


def test_stripping_one_section_leaves_every_other_intact():
    prompt = _prompt_for(None)
    settings.configure({"hypothesis_ledger": False})

    out = features.strip_disabled_sections(prompt)

    assert "<hypothesis_ledger>" not in out
    for tag in ("role", "operating_principles",
                "science_monitor", "failure_modes_to_avoid", "delegation_errors"):
        assert f"<{tag}>" in out, tag


def test_the_hypothesis_ledger_arm_is_declared_partial():
    """Its section and tools go, but the Popperian workflow IS the
    strategizer's method — argued in <role>, enforced in
    <operating_principles>. A run with the ledger off is a PARTIAL ablation and
    the registry has to say so, or the result gets over-claimed."""
    f = features.by_key("hypothesis_ledger")

    assert f.pervasive is True

    settings.configure({"hypothesis_ledger": False})
    out = features.strip_disabled_sections(_prompt_for(None))
    assert "HYPOTHESIS" in out.upper(), (
        "if the concept really were gone, pervasive should be False")


def test_an_unknown_feature_raises_rather_than_reading_false():
    """A typo must not resolve to 'disabled' — that is an ablation arm nobody
    asked for, reported under the wrong label."""
    with pytest.raises(KeyError):
        features.enabled("hypothesys_ledger")


# --- the tool catalog actually honours it ----------------------------------

def test_a_disabled_features_tools_never_reach_the_catalog(tmp_path):
    from adda._src.nodes import Node

    from .test_route_aware_termination import StubAdapter, _minimal_spec

    settings.configure({"milestones_enabled": False})
    node = Node(
        StubAdapter(), name="strategizer", outgoing=["implementer"],
        spec=_minimal_spec(), notes_dir=tmp_path,
    )

    for name in features.by_key("milestones_enabled").tools:
        assert name not in node.adapter.closure_tools, name


# --- the three things <f3dasm_api> used to conflate -------------------------

def test_turning_off_the_lookup_keeps_the_verified_excerpt():
    """The lookup arm must measure the LOOKUP, not "no f3dasm reference".

    `<f3dasm_api>` is the fixed excerpt — canonical imports, the Domain
    surface, and `F3DASM_CORE_IDIOMS`, which `tests/test_f3dasm_idioms.py`
    executes against the installed f3dasm. That is the control condition the
    tool is measured against, so the arm strips `<f3dasm_api_lookup>` and
    nothing else.
    """
    from adda._src.knowledge.idioms import F3DASM_CORE_IDIOMS

    settings.configure({"f3dasm_api": False})
    prompts = _all_role_prompts()
    out = features.strip_disabled_sections(prompts["F3dasmImplementerAgent"])

    assert "<f3dasm_api_lookup>" not in out
    assert "<f3dasm_api>" in out
    assert F3DASM_CORE_IDIOMS.strip() in out
    assert "ConsultF3dasm" in features.disabled_tool_names()


def test_the_oracle_contract_is_not_ablatable():
    """`<oracle_contract>` is run substrate, not a feature.

    An agent without it does not run a worse experiment — it reaches the oracle
    by some other path, and every evaluation it makes is unledgered and
    unreproducible. There is no arm to be had here, only a broken run, so no
    feature may own the tag.
    """
    owned = {t for f in features.FEATURES for t in f.sections}
    assert "oracle_contract" not in owned

    prompts = _all_role_prompts()
    assert "<oracle_contract>" in prompts["F3dasmImplementerAgent"]
    for key in features.FEATURE_KEYS:
        settings.configure({key: False})
        out = features.strip_disabled_sections(
            prompts["F3dasmImplementerAgent"])
        assert "THE ORACLE DOOR" in out, key


def test_turning_off_the_playbook_leaves_the_api_and_the_contract():
    """The method prior is separable from the API facts and from the ledger
    rules; before the split, one tag owned all three."""
    settings.configure({"doe_playbook": False})
    out = features.strip_disabled_sections(
        _all_role_prompts()["F3dasmImplementerAgent"])

    assert "<doe_playbook>" not in out
    assert "SURROGATE-GUIDED EXPLOIT LOOP" not in out
    assert "<f3dasm_api>" in out
    assert "<oracle_contract>" in out
    assert "get_evaluator()" in out


def test_a_disabled_features_shared_closures_never_reach_the_catalog(tmp_path):
    """The read-only shared tools (HypothesisList, ...) are built by a second
    path from the routing tools; it must honour the same subtraction, or the
    ledger-off arm keeps a tool that returns "ERROR: ... not available"."""
    from adda._src.backends.base import Agent, Edge, Graph
    from adda._src.nodes import Node

    from .test_route_aware_termination import StubAdapter

    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "FollowUp", "HypothesisList", "QueryStore"})
        description = "Test strategizer."

    class B(Agent):
        description = "Test implementer."

    spec = Graph(nodes={"strategizer": A(), "implementer": B()},
                 edges=(Edge("strategizer", "implementer"),),
                 entry="strategizer")

    def _node():
        return Node(StubAdapter(), name="strategizer",
                    outgoing=["implementer"], spec=spec, notes_dir=tmp_path,
                    agent_tools=A.tools)

    assert "HypothesisList" in _node().adapter.closure_tools

    settings.configure({"hypothesis_ledger": False})
    tools = _node().adapter.closure_tools
    assert "HypothesisList" not in tools
    assert "QueryStore" in tools


# --- a feature whose prerequisite is off is off ------------------------------

def test_the_verdict_validator_cannot_outlive_the_ledger_it_judges():
    """It reviews HypothesisUpdate verdicts and nothing else. With the ledger
    off, reporting it as on would label the arm by a capability the run has
    not got."""
    settings.configure({"hypothesis_ledger": False})
    assert features.enabled("verdict_validator") is False
    assert features.arm_config()["verdict_validator"] is False
    assert any("verdict_validator" in m for m in features.conflicts())


def test_a_prerequisite_that_is_on_changes_nothing():
    settings.configure({"verdict_validator": False})
    assert features.enabled("hypothesis_ledger") is True
    assert features.enabled("verdict_validator") is False
    assert features.conflicts() == []


def test_every_declared_prerequisite_is_a_feature():
    for f in features.FEATURES:
        for r in f.requires:
            assert r in features.FEATURE_KEYS, (f.key, r)
            assert r != f.key


def test_the_pipeline_and_reproduction_knobs_are_read_through_the_registry(
        tmp_path):
    """Both used to be read as get_bool(key, True) literals at three sites, so
    a changed registry default would have drifted from them silently."""
    import inspect

    from adda._src.nodes import orchestration, reproduction_gate
    from adda._src.runtime import agent_runtime

    for mod in (orchestration, reproduction_gate, agent_runtime):
        src = inspect.getsource(mod)
        for key in ("pipeline_deliverable", "reproduction_gate"):
            assert f'get_bool("{key}"' not in src.replace("\n", "").replace(
                " ", ""), (mod.__name__, key)


# --- inline gates: [[if key]]on[[else]]off[[/if]] --------------------------


def test_gate_keeps_on_branch_byte_for_byte_and_drops_else():
    s = "a [[if hypothesis_ledger]]ON  x[[else]]OFF[[/if]] z"
    assert features.resolve_gates(s) == "a ON  x z"


def test_gate_off_takes_else_or_nothing():
    settings.configure({"hypothesis_ledger": False})
    assert features.resolve_gates("a [[if hypothesis_ledger]]ON[[else]]OFF[[/if]] z") == "a OFF z"
    assert features.resolve_gates("a [[if hypothesis_ledger]]ON[[/if]] z") == "a  z"


def test_gates_nest_and_dead_outer_kills_inner():
    s = "[[if pipeline_deliverable]]P[[if reproduction_gate]]R[[else]]r[[/if]][[/if]]"
    assert features.resolve_gates(s) == "PR"
    settings.configure({"reproduction_gate": False})
    assert features.resolve_gates(s) == "Pr"
    settings.configure({"pipeline_deliverable": False})
    assert features.resolve_gates(s) == ""


@pytest.mark.parametrize(
    "bad",
    ["[[if hypothesis_ledger]]x", "x[[/if]]", "x[[else]]y", "[[if nope_key]]x[[/if]]"],
)
def test_gate_errors_are_loud(bad):
    with pytest.raises((ValueError, KeyError)):
        features.resolve_gates(bad)


def test_feature_tagged_chapter_hidden_while_feature_off():
    from adda._src.knowledge.kb import KnowledgeBase

    kb = KnowledgeBase.load()
    assert kb.get("pipeline-reproduces-from-store") is not None
    settings.configure({"pipeline_deliverable": False})
    assert kb.get("pipeline-reproduces-from-store") is None
    assert all(e.id != "pipeline-reproduces-from-store" for e in kb.entries)


def test_node_gate_follows_the_graph_and_clears_with_configure():
    s = "a[[if node:critic]]C[[else]]n[[/if]]b"
    assert features.resolve_gates(s) == "aCb"          # no graph built: present
    settings.set_graph_nodes({"strategizer"})
    assert features.resolve_gates(s) == "anb"
    settings.set_graph_nodes({"strategizer", "critic"})
    assert features.resolve_gates(s) == "aCb"
    settings.set_graph_nodes({"strategizer"})
    settings.configure(None)
    assert features.resolve_gates(s) == "aCb"


def test_handbook_chapters_do_not_mention_a_critic_that_is_not_in_the_graph():
    from adda._src.knowledge.kb import KnowledgeBase

    kb = KnowledgeBase.load()
    present = {e.id: e.render().lower() for e in kb.entries}
    assert "the critic checks" in present["pipeline-building-patterns"]
    assert any("critic" in t for t in present.values())

    settings.set_graph_nodes({"strategizer", "implementer"})
    try:
        absent = {e.id: e.render().lower() for e in kb.entries}
        assert {i for i, t in absent.items() if "critic" in t} == set()
        assert "[[if" not in "".join(absent.values())
        assert kb.get("pipeline-building-patterns").render()
    finally:
        settings.configure(None)
