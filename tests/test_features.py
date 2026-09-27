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

    ``peer_interaction`` is the one deliberate, TEMPORARY exception
    (spec 12, internal/specs/12-peer-interaction.md): its default is False
    while it is being built across several commits, precisely so a run
    with no ablate config stays byte-identical to today WHILE it lands --
    "today" just doesn't have SendMessage yet either. The migration-sweep
    commit that flips this default to True is also the one that makes
    "byte-identical to today" mean something new; this assertion updates
    then, not before.
    """
    assert features.disabled_tool_names() == frozenset({"SendMessage"})
    prompt = _prompt_for(None)
    assert features.strip_disabled_sections(prompt) == prompt


def test_a_disabled_feature_takes_its_tools_with_it():
    settings.configure({"hypothesis_ledger": False})

    withheld = features.disabled_tool_names()

    assert "HypothesisPropose" in withheld
    assert "HypothesisList" in withheld
    # another feature's tools are untouched
    assert "MilestoneList" not in withheld


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
