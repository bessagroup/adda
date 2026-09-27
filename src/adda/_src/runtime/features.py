"""What a feature IS: one runtime knob, the tools it owns, the prompt it owns.

Those three used to be wired independently, so turning a feature off left two
thirds of it running. The tools are gated on the agent's static ``tools``
frozenset, never on whether the backing object exists, and the system prompt is
one literal string per role. Disable the hypothesis ledger and the agent is
still commanded that ``hypotheses.json`` "is your canonical scientific record",
still sees all five tools published as AUTHORITATIVE, calls one, and gets
``"ERROR: hypothesis ledger not available in this run."`` — which the runtime
counts as an ``ERROR_RETURN`` diagnostic, the one KPI whose target is zero. The
run measures an agent confused by broken tools, not an agent without a ledger.

Exactly one feature already did this properly: ``pipeline_deliverable`` gates
its gate, its tools, its prompt injection and its seeded milestone on one knob.
It took three rounds to get there — ``internal/BACKLOG.md`` #27, #28 and #30
are successive entries of "the flag didn't actually turn it off". This module
makes that shape structural instead of repeating it by hand for each feature.

A feature declares its knob, the tool names it owns and the prompt sections it
owns; the tool catalog and the assembled prompt are both built from the live
set. ``tests/test_features.py`` fails if a declared section tag does not exist
in the prompt that claims to own it, so a renamed tag cannot silently stop
being stripped.

PERVASIVE features are the honest caveat. A feature is pervasive when its
CONCEPT appears outside the sections it owns — the strategizer's whole
scientific method is written in terms of hypotheses, across
``<role>``, ``<operating_principles>`` and
``<failure_modes_to_avoid>``. Disabling such a feature removes its tools and its
own sections but cannot remove the idea, so the arm is a PARTIAL ablation and
must be reported as one. Stripping those sections too would not be an ablation
— it would be a different agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .settings import get_bool

__all__ = [
    "Feature",
    "NOTEBOOK_TOOLS",
    "FEATURES",
    "FEATURE_KEYS",
    "enabled",
    "disabled_tool_names",
    "strip_disabled_sections",
    "by_key",
]


@dataclass(frozen=True)
class Feature:
    """One switchable capability and everything it owns."""

    #: runtime knob name; must be in settings.KNOWN_KEYS and documented
    key: str
    #: value when the knob is unset
    default: bool
    #: tool names that exist only because this feature does
    tools: frozenset[str] = field(default_factory=frozenset)
    #: prompt section tags (``<tag>…</tag>``) this feature owns outright
    sections: tuple[str, ...] = ()
    #: named RUNTIME behaviours this feature owns — a capability with no tool
    #: and no prompt surface, which the runtime consults by key.
    #:
    #: The registry was built when every feature had a tool or a prompt
    #: section, and the invariant "a feature owns one of those two" followed
    #: from the sample rather than from the idea. The verdict validator owns
    #: neither: it is a judge that reviews each HypothesisUpdate and appends a
    #: concern. An agent run whose verdicts are second-guessed is as different
    #: from one whose are not as an agent missing a tool, so calling it a mere
    #: setting would put it outside the ablation registry — precisely where it
    #: must not be.
    behaviours: tuple[str, ...] = ()
    #: True when the feature's CONCEPT also appears outside its own sections,
    #: so disabling it is a partial ablation. See the module docstring.
    pervasive: bool = False


#: Notebook-authoring surface. Done is deliberately excluded — a run still has
#: to be able to close. So is WriteDeliverable: it writes the study's declared
#: EXTRA deliverables (e.g. replicate.py), which the Done() gate requires
#: whether or not there is a notebook, so stripping it with the notebook would
#: deadlock that arm.
NOTEBOOK_TOOLS = frozenset({"WriteCell", "ShowNotebook", "RunNotebook"})

FEATURES: tuple[Feature, ...] = (
    Feature(
        key="hypothesis_ledger",
        default=True,
        tools=frozenset({
            "HypothesisPropose", "HypothesisUpdate", "HypothesisList",
        }),
        sections=("hypothesis_ledger",),
        # The Popperian workflow IS the strategizer's operating model: it is
        # argued in <role>, enforced in <operating_principles>
        # ("SCOPE EACH DELEGATION TO ONE HYPOTHESIS") and resolved in
        # the worked examples in <failure_modes_to_avoid>. Those are the agent's scientific method, not
        # ledger documentation, so they stay.
        pervasive=True,
    ),
    Feature(
        key="milestones_enabled",
        default=True,
        tools=frozenset({
            "MilestoneList", "MilestoneSet",
        }),
    ),
    Feature(
        key="science_monitor",
        default=True,
        # No tools: the monitor speaks by injecting notices. Its prompt block
        # is self-contained, which makes this the cleanest arm of the set.
        sections=("science_monitor",),
    ),
    Feature(
        key="f3dasm_api",
        default=True,
        tools=frozenset({"ConsultF3dasm"}),
        sections=("f3dasm_api_lookup",),
        # The tag is f3dasm_api_LOOKUP, not f3dasm_api: <f3dasm_api> is the
        # fixed excerpt — the canonical imports, the Domain surface and
        # F3DASM_CORE_IDIOMS, which CI executes against the installed f3dasm.
        # Reusing the name would make this arm strip the excerpt too, so it
        # would measure "no cheat sheet AND no lookup" while claiming to
        # measure the lookup.
        #
        # Not pervasive: f3dasm is named throughout both prompts, but nothing
        # outside this section tells the agent to CONSULT it. Off removes the
        # instruction and the tool together, leaving an agent that writes
        # f3dasm from the excerpt and memory — the state before this tool
        # existed, and so an honest control arm.
    ),
    Feature(
        key="doe_playbook",
        default=True,
        # No tools — it is pure method prior: the space-filling recipe, the
        # eval-budget arithmetic and the surrogate-guided exploit loop, roughly
        # 625 tokens on every implementer call. It used to sit inside
        # <f3dasm_api>, which conflated three unrelated things: f3dasm API
        # facts (now the excerpt plus the lookup), the adda oracle contract
        # (<oracle_contract>, run substrate and NOT ablatable — an agent
        # without it writes unledgered evaluations rather than a worse loop),
        # and this. Separating them is what makes either arm interpretable.
        #
        # The hypothesis it tests is the interesting one for a small model:
        # whether an explicit method prior substitutes for capability. Expect
        # a large model to lose little and a 27B-class model to lose a lot.
        sections=("doe_playbook",),
    ),
    Feature(
        key="verdict_validator",
        default=True,
        # No tools and no prompt section: it owns a runtime behaviour. The
        # agent is never told a judge is reading its verdicts, which is the
        # point — it must not write for the judge. Advisory by construction:
        # it annotates a HypothesisUpdate, never blocks one, so the arm
        # measures whether being second-guessed changes the science rather
        # than whether a gate stopped the run.
        behaviours=("verdict_validator",),
    ),
    Feature(
        key="pipeline_deliverable",
        default=True,
        tools=NOTEBOOK_TOOLS,
        # Its prompt contribution is an INJECTION (notebook_deliverable_spec),
        # already gated at the injection site rather than carried as a section
        # of the role prompt.
    ),
    Feature(
        key="reproduction_gate",
        default=True,
        # No tools of its own (RunNotebook/WriteCell etc. are pipeline_
        # deliverable's) and no static <section> (its prompt contribution —
        # nodes/reproduction_gate.py::gate_contract() — is an INJECTION,
        # gated at the injection site, same as pipeline_deliverable's). Off:
        # _reproduction_gate() returns None unconditionally (Done()'s gate
        # never runs, RunNotebook(gate=True) always reports a pass) and the
        # ORACLE_GOLD_STATE milestone (epistemics/milestones.py) is not
        # seeded — its only reason to exist is this gate's store-row
        # precondition. Distinct from pipeline_deliverable: that knob decides
        # whether a notebook is REQUIRED at all; this one decides whether an
        # authored notebook must additionally prove it reproduces.
        behaviours=("reproduction_gate_check",),
    ),
)

FEATURE_KEYS: frozenset[str] = frozenset(f.key for f in FEATURES)


def by_key(key: str) -> Feature | None:
    return next((f for f in FEATURES if f.key == key), None)


def enabled(key: str) -> bool:
    """Whether the feature named ``key`` is on for this run."""
    f = by_key(key)
    if f is None:
        raise KeyError(f"unknown feature {key!r}; known: {sorted(FEATURE_KEYS)}")
    return get_bool(f.key, f.default)


def disabled_tool_names() -> frozenset[str]:
    """Tool names to withhold: every tool owned by a disabled feature.

    Withholding is the point. Leaving a dead tool registered does not produce
    an agent without the feature — it produces an agent that keeps calling a
    tool that errors.
    """
    out: set[str] = set()
    for f in FEATURES:
        if not get_bool(f.key, f.default):
            out |= f.tools
    return frozenset(out)


def strip_disabled_sections(prompt: str) -> str:
    """Remove ``<tag>…</tag>`` for every section owned by a disabled feature.

    Non-greedy per tag and anchored on the exact tag name, so neighbouring
    sections are untouched. A tag that is absent is a no-op here — the drift
    test is what makes its absence loud.
    """
    for f in FEATURES:
        if get_bool(f.key, f.default):
            continue
        for tag in f.sections:
            prompt = re.sub(
                rf"<{re.escape(tag)}>.*?</{re.escape(tag)}>\s*",
                "", prompt, flags=re.DOTALL,
            )
    return prompt
