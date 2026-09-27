"""Labelled queries for an agent-facing index of adda's OWN source.

WHY THIS FILE EXISTS, AND WHY IT EXISTS FIRST
    The proposal is a lookup tool over adda's source — the same shape as
    ``knowledge/f3dasm_api.py``, pointed at this package instead of f3dasm.
    Written after the tool, a query set measures whatever the tool already
    does. Written before it, it is the thing the tool has to satisfy. The
    alias map in ``f3dasm_api`` is the cautionary case: 84 hand-written
    English->f3dasm pairs, added once the misses were known, which is an
    answer fitted to its own test.

    So these 45 queries are written against the QUESTION, not against any
    implementation. Nothing here may be edited to accommodate a retriever.
    Fixing a label because a tool missed it is the failure mode this file is
    supposed to prevent.

THE BASELINE IS RIPGREP, NOT NOTHING
    A coding agent with a shell already reads a 31k-line repo. The honest
    control is ``rg -i <the query's own words> src/``, scored the same way.
    A tier where the index does not beat that control is a tier where the
    index should not ship.

THE HELD-OUT SPLIT
    ``DEVELOPMENT`` (31) may be looked at while building. ``HELD_OUT`` (14)
    may not be read, reasoned about, or scored until the design is frozen.
    They are stratified by tier so the held-out set is not accidentally the
    easy third.

WHAT A GOLD ANSWER IS
    The file(s) a correct answer must name. Where more than one is listed,
    ANY of them at rank 1 counts as a hit: "where does a run decide it is
    over" is answered by ``runtime/terminal.py`` alone, but "how does it stop
    an agent inventing results" is genuinely two places and a retriever that
    names either has answered it.

    Paths are repository-relative. ``tests/test_adda_source_queries.py``
    asserts every one of them still exists, so a rename cannot rot this set
    into a silently-passing one.
"""
from __future__ import annotations

S = "src/adda/_src/"

#: An agent already holds the identifier and wants the definition. This tier
#: is the CONTROL, not the target: ripgrep is excellent at exact names and is
#: expected to win here. An index that loses badly on this tier is broken;
#: one that merely ties has cost nothing.
NAME = [
    ("n01", "AgenticRun", [S + "runtime/agent_runtime.py"]),
    ("n02", "what does compact_to_budget do",
     [S + "backends/context_compaction.py"]),
    ("n03", "get_evaluator", [S + "evaluation/oracle_resolution.py"]),
    ("n04", "HypothesisLedger", [S + "epistemics/hypothesis_ledger.py"]),
    ("n05", "ScienceMonitor rules", [S + "epistemics/science_monitor.py"]),
    ("n06", "build_graph", [S + "runtime/graph_builder.py"]),
    ("n07", "ConsultF3dasm", [S + "knowledge/f3dasm_api.py"]),
    ("n08", "Telemetry", [S + "infra/telemetry.py"]),
]

#: English, from someone who does not yet share adda's vocabulary. This is
#: the tier the whole proposal rests on: nobody greps "backstop" for "stop it
#: when it costs too much", and nobody greps "operator_channel" for "ask a
#: human". If the index does not win here it wins nowhere.
CONCEPT = [
    ("c01", "how do I stop a run that is burning money",
     [S + "runtime/terminal.py", S + "nodes/orchestration.py"]),
    ("c02", "where does a run decide it is over",
     [S + "runtime/terminal.py"]),
    ("c03", "what keeps the conversation from overflowing the model's context",
     [S + "backends/context_budget.py", S + "backends/context_compaction.py"]),
    ("c04", "how does the system stop an agent inventing results",
     [S + "epistemics/science_monitor.py", S + "nodes/critic_gate.py"]),
    ("c05", "where does a worker ask a human a question",
     [S + "infra/operator_channel.py"]),
    ("c06", "how do I watch a run live in a browser",
     [S + "viewer/app.py"]),
    ("c07", "what makes one agent hand work to another",
     [S + "nodes/tools/routing/delegation.py"]),
    ("c08", "where is the text the model actually reads",
     [S + "prompts/agent_prompts.py", S + "agents/strategizer.py"]),
    ("c09", "where is the evaluation recorded so it cannot be faked",
     [S + "evaluation/instrumented.py", S + "evaluation/oracle_resolution.py"]),
    ("c10", "what happens when the model returns something unparseable",
     [S + "nodes/parsing.py"]),
    ("c11", "how are papers found and read",
     [S + "literature/literature_corpus.py", S + "agents/literature.py"]),
    ("c12", "where is the cost of a run recorded",
     [S + "infra/telemetry.py"]),
    ("c13", "how does it run on a cluster",
     [S + "infra/slurm_llm.py"]),
    ("c14", "what turns the agents' output into a notebook that runs",
     [S + "evaluation/notebook_exec.py"]),
]

#: The agent is staring at an error and wants the code that produced it. The
#: query is a string it did not choose, which is what makes this tier
#: different from CONCEPT: some of these words ARE in the source, so the
#: interesting failures are the ones where the message is assembled far from
#: the decision that caused it.
ERROR = [
    ("e01", "ModuleNotFoundError: No module named 'f3dasm'",
     [S + "knowledge/f3dasm_api.py"]),
    ("e02", "the run stopped at the recursion limit",
     [S + "runtime/terminal.py", S + "runtime/settings.py"]),
    ("e03", "the report says UNGATED RUN, what does that mean",
     [S + "runtime/terminal.py"]),
    ("e04", "ERROR_RETURN diagnostics keep appearing in the log",
     [S + "nodes/orchestration.py"]),
    ("e05", "the local server rejected the request for context length",
     [S + "backends/context_budget.py"]),
    ("e06", "the viewer page loads but shows no run data at all",
     [S + "viewer/app.py"]),
    ("e07", "unknown configuration key",
     [S + "runtime/settings.py"]),
]

#: "Am I allowed to?" — the tier no grep can serve, because the answer is a
#: RULE about a symbol rather than the symbol. This is the differentiator
#: claim in one list: if the index cannot answer these, it is a search engine
#: with extra steps.
INVARIANT = [
    ("i01", "can I call the objective function directly instead of get_evaluator",
     [S + "knowledge/entries/0001-evaluate-through-get-evaluator.md",
      S + "evaluation/oracle_resolution.py"]),
    ("i02", "is it ok to import from adda._src",
     ["src/adda/__init__.py"]),
    ("i03", "can a surrogate model's prediction count as a result",
     [S + "knowledge/entries/0002-surrogates-and-analysis-are-off-ledger.md"]),
    ("i04", "must every delegation cite a hypothesis",
     [S + "knowledge/entries/0003-hypotheses-are-bounded-claims.md",
      S + "nodes/tools/routing/delegation.py"]),
    ("i05", "can one delegation run two experiments",
     [S + "knowledge/entries/0004-one-delegation-one-experiment.md"]),
    ("i06", "does the headline number have to reproduce",
     [S + "knowledge/entries/0005-replicate-reproduces-from-store.md",
      S + "nodes/reproduction_gate.py"]),
    ("i07", "can I add a tool without touching the prompt",
     [S + "runtime/features.py"]),
    ("i08", "is InstrumentedDataGenerator part of the public API",
     ["src/adda/__init__.py", S + "evaluation/instrumented.py"]),
]

#: "How do I turn this off / point it somewhere else." Every answer is a
#: declared knob, so this tier doubles as a drift check: a knob that no query
#: can reach is a knob nobody can find.
KNOB = [
    ("k01", "how do I turn off the hypothesis ledger",
     [S + "runtime/features.py", S + "runtime/settings.py"]),
    ("k02", "how do I use a local model instead of Claude",
     [S + "backends/registry.py", S + "backends/openai_compatible.py",
      S + "backends/ollama.py"]),
    ("k03", "where do I set the context window",
     [S + "runtime/settings.py"]),
    ("k04", "how do I change which nodes are in the graph",
     [S + "runtime/graph_builder.py", S + "backends/base.py"]),
    ("k05", "can I cap how many tokens the model emits",
     [S + "backends/context_budget.py", S + "runtime/settings.py"]),
    ("k06", "how do I switch from trimming to compaction",
     [S + "runtime/settings.py", S + "backends/context_compaction.py"]),
    ("k07", "how do I point it at Abaqus documentation",
     [S + "knowledge/abaqus/__init__.py"]),
    ("k08", "how do I make the critic stricter",
     [S + "epistemics/verdict_validator.py", S + "nodes/critic_gate.py"]),
    ("k09", "can I remove the checklist it has to clear before it runs anything",
     [S + "runtime/features.py", S + "epistemics/milestones.py"]),
    ("k10", "how do I stop it producing a notebook",
     [S + "runtime/features.py", S + "evaluation/notebook_exec.py"]),
    ("k11", "where does it get its design-of-experiments recipe",
     [S + "agents/implementer.py"]),
    ("k12", "how do I stop it requiring the notebook to reproduce",
     [S + "runtime/features.py", S + "nodes/reproduction_gate.py"]),
]

#: Which query is the way to ASK FOR each declared feature knob.
#:
#: Declared rather than derived, because the obvious derivation — does the
#: knob's own name appear in the query — rewards exactly the queries this set
#: exists to avoid. Nobody types "milestones_enabled"; they ask what is
#: blocking the first experiment. ``test_adda_source_queries`` asserts this
#: map covers ``features.FEATURES``, so a new knob cannot ship without a
#: question a person would really ask for it.
KNOB_COVERAGE = {
    "hypothesis_ledger": "k01",
    "milestones_enabled": "k09",
    "science_monitor": "c04",
    "f3dasm_api": "n07",
    "doe_playbook": "k11",
    "verdict_validator": "k08",
    "pipeline_deliverable": "k10",
    "reproduction_gate": "k12",
}

TIERS = {"name": NAME, "concept": CONCEPT, "error": ERROR,
         "invariant": INVARIANT, "knob": KNOB}

ALL = [(tier, qid, q, gold)
       for tier, rows in TIERS.items() for qid, q, gold in rows]

#: Frozen split. One third held out, stratified by tier so the held-out set
#: is not accidentally the easy third. The ids are listed literally rather
#: than computed, because a hash-based split silently reshuffles the moment a
#: query is added and the held-out set stops being held out.
HELD_OUT_IDS = frozenset({
    "n03", "n07",                                   # 2 of 8 name
    "c02", "c05", "c09", "c13",                     # 4 of 14 concept
    "e03", "e05",                                   # 2 of 7 error
    "i02", "i05", "i08",                            # 3 of 8 invariant
    "k02", "k05", "k07",                            # 3 of 12 knob
})

DEVELOPMENT = [r for r in ALL if r[1] not in HELD_OUT_IDS]
HELD_OUT = [r for r in ALL if r[1] in HELD_OUT_IDS]
