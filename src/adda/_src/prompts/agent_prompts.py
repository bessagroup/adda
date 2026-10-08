"""The runtime prompt strings that belong to no single agent.

This module does NOT own the per-role system prompts.  Each one lives
beside the agent it belongs to — ``agents/strategizer.py``,
``agents/implementer.py``, ``agents/critic.py``, ``agents/literature.py``,
``agents/debugger.py`` — and is re-exported here only so that existing
``from ...agent_prompts import X`` imports keep working.  Edit a role's
prompt in its own agent module; editing is not done here.

What this module genuinely owns is the text the runtime injects that has
no single owning role: the two system-prompt PREAMBLES every node gets
(``RUN_PATHS_PREAMBLE_TEMPLATE`` for the graph's entry node,
``WORKSPACE_PREAMBLE_TEMPLATE`` for every other node — see
``runtime/agent_runtime.py::_make_adapter``), the checkpoint and reset
messages, and the report-retry / REFLECT diagnosis strings.

For the rest of what reaches an agent's context — the handbook menu and
chapters (``knowledge/``), the falsification charter
(``knowledge/charter.py``), the f3dasm idioms (``knowledge/idioms.py``),
and the tool catalog rendered from each tool's own docstring
(``prompts/tool_catalog.py``) — see those modules.  This one is a
sliver of the corpus, not its index.

Constants
---------
STRATEGIZER_SYSTEM_PROMPT
    Re-export of ``agents.strategizer.STRATEGIZER_SYSTEM_PROMPT``.
IMPLEMENTER_SYSTEM_PROMPT
    Re-export of ``agents.implementer.IMPLEMENTER_SYSTEM_PROMPT``.
CHECKPOINT_STRATEGIZER_PROMPT
    User-message injected into the Strategizer every 30 delegations.
IMPLEMENTER_RESET_PROMPT_TEMPLATE
    ``.format()``-ready template for the opening user message sent to a
    freshly-reset Implementer after a checkpoint.  Single placeholder:
    ``{checkpoint_summary}``.
RUN_PATHS_PREAMBLE_TEMPLATE
    ``.format()``-ready preamble prepended to the system prompt of the
    graph's ENTRY node — whichever node that is, not the strategizer by
    name.  Placeholders: ``{study_dir}``, ``{run_dir}``, ``{debug_dir}``,
    ``{notes_dir}``, ``{experiment_data_dir}``, ``{resources}``,
    ``{knowledge}``.
WORKSPACE_PREAMBLE_TEMPLATE
    ``.format()``-ready preamble prepended to the system prompt of every
    NON-entry node, worker and critic alike.  Placeholders:
    ``{workspace_dir}``, ``{study_dir}``, ``{resources}``, ``{knowledge}``.

    ``{resources}`` is live host facts from ``_resource_stanza()``;
    ``{knowledge}`` is the audience-filtered handbook menu from
    ``_kb_menu(role)``.  Both are computed per run in
    ``runtime/agent_runtime.py::_make_adapter``, which is also the single
    place that chooses between these two templates.
IMPLEMENTER_REPORT_RETRY_PROMPT
    Static correction message sent to the Implementer when its first
    reply lacks a parseable ``## Report`` block.
REFLECT_DIAGNOSIS_SHORT
    REFLECT diagnosis text for unusually short Implementer responses.
REFLECT_DIAGNOSIS_CAPABILITY_LIMIT
    REFLECT diagnosis text when the Implementer reports a capability
    limit.
REFLECT_DIAGNOSIS_MISSING_SUBSECTIONS_TEMPLATE
    ``.format()``-ready REFLECT diagnosis text for a Report block that
    is present but missing required subsections.  Placeholder:
    ``{missing_subsections}``.
REFLECT_DIAGNOSIS_NO_REPORT_HEADING
    REFLECT diagnosis text when the Implementer never starts a
    ``## Report`` block.
REFLECT_DIAGNOSIS_DEFAULT
    Fallback REFLECT diagnosis text for unrecognised malformation.

Notes
-----
Line-length rule: all Python source lines are <= 79 chars.  The string
content of each constant may contain longer lines; that is intentional
and correct.
"""
#                                                                       Modules
# =============================================================================

from __future__ import annotations

#                                                          Authorship & Credits
# =============================================================================
__author__ = "Elvis Aguero (elvis_alexander_aguero_vera@brown.edu)"
__credits__ = ["Elvis Aguero"]
__status__ = "Experimental"
# =============================================================================
#
# =============================================================================

__all__ = [
    "STRATEGIZER_SYSTEM_PROMPT",
    "IMPLEMENTER_SYSTEM_PROMPT",
    "DEBUGGER_SYSTEM_PROMPT",
    "LITERATURE_REVIEW_SYSTEM_PROMPT",
    "ADVERSARIAL_CRITIQUE_SYSTEM_PROMPT",
    "CHECKPOINT_STRATEGIZER_PROMPT",
    "IMPLEMENTER_RESET_PROMPT_TEMPLATE",
    "TEAM_ROSTER_TEMPLATE",
    "RUN_PATHS_PREAMBLE_TEMPLATE",
    "WORKSPACE_PREAMBLE_TEMPLATE",
    "IMPLEMENTER_REPORT_RETRY_PROMPT",
    "build_report_retry_prompt",
    "REFLECT_DIAGNOSIS_SHORT",
    "REFLECT_DIAGNOSIS_CAPABILITY_LIMIT",
    "REFLECT_DIAGNOSIS_MISSING_SUBSECTIONS_TEMPLATE",
    "REFLECT_DIAGNOSIS_NO_REPORT_HEADING",
    "REFLECT_DIAGNOSIS_DEFAULT",
    "IMPLEMENTER_SYSTEM_PROMPT_OLLAMA",
]

# Re-export agent system prompts from their canonical locations so that
# existing code importing from agent_prompts continues to work.
from ..agents.critic import ADVERSARIAL_CRITIQUE_SYSTEM_PROMPT  # noqa: E402
from ..agents.debugger import DEBUGGER_SYSTEM_PROMPT  # noqa: E402
from ..agents.implementer import IMPLEMENTER_SYSTEM_PROMPT  # noqa: E402
from ..agents.literature import LITERATURE_REVIEW_SYSTEM_PROMPT  # noqa: E402
from ..agents.strategizer import STRATEGIZER_SYSTEM_PROMPT  # noqa: E402
from ..knowledge.idioms import F3DASM_CORE_IDIOMS  # noqa: E402

# =============================================================================

CHECKPOINT_STRATEGIZER_PROMPT = """\
CHECKPOINT — do not generate new hypotheses or delegate new tasks.

The runtime has reached a delegation checkpoint.  Your job right now is
to synthesise what has been learned so far and produce a structured
summary that a human reviewer can read and that a fresh Implementer
session can use as a project briefing.

Produce a report under a ## Checkpoint heading with exactly these four
sections.  Be specific: cite numbers from prior Reports.  Do not
speculate beyond what the data supports.

## Checkpoint

### What we have learned
<Bullet list.  Each bullet: a finding supported by at least one Report.
Format: "- [Finding]: [evidence] (D###, key: value)">

### What we have ruled out
<Bullet list.  Each bullet: a hypothesis or region of the search space
that has been falsified or shown to be suboptimal.  Include the evidence.>

### Open questions
<Bullet list.  Each bullet: an unresolved uncertainty that materially
affects the final design recommendation.  State why it is unresolved
(e.g. no data yet, conflicting Reports, pool coverage gap).>

### Recommended next direction
<One paragraph, <= 100 words.  The single most information-valuable
experiment or analysis to run next.  Justify in terms of which open
question it resolves.  Do not propose more than one direction.>

### Hypothesis ledger digest
<One line per hypothesis from HypothesisList().
Format: `- <id> [<status>] prior <p> → posterior <q>: <statement>`.
The canonical store is hypotheses.json; this is a digest only.>

After producing this report, wait.  Do not delegate until the runtime
resumes the session.
"""

# =============================================================================

IMPLEMENTER_RESET_PROMPT_TEMPLATE = """\
You are starting a new Implementer session.  The prior session has ended
at a checkpoint.  Below is the Strategizer's checkpoint summary, which is
your complete project context.  You have no memory of the prior session's
tool calls; treat the checkpoint summary as your sole briefing.

--- BEGIN CHECKPOINT SUMMARY ---
{checkpoint_summary}
--- END CHECKPOINT SUMMARY ---

From this point on you will receive Task messages from the Strategizer.
Execute each task and return a Report as specified in your system prompt.
The debug/delegations/ directory under the current run may contain artefacts from
the prior session — check before recomputing anything.
"""

# =============================================================================

TEAM_ROSTER_TEMPLATE = """\
<team>
THIS RUN'S AGENTS — generated from the live wiring, AUTHORITATIVE. These are
the only agents that exist; use their names verbatim wherever a tool takes an
agent's name.
{members}
A role described anywhere in your prompt but absent from this list IS NOT
WIRED in this run — there is no general-purpose fallback agent beyond what is
listed.
You are {name}.
</team>
"""
"""Every agent of this run, the same list for every one of them, injected per
run.

It used to be the entry node's delegation targets alone. Workers were told
nothing about the team -- their preamble named "the Strategizer" by hand, and
every worker holds SendMessage, which takes an agent's name -- so a worker
could only address names that happened to appear in its brief, and a graph
whose entry node is named otherwise contradicted the preamble outright. Now
every agent reads one generated roster, and which of those names it can hand
work to is in its own Delegate tool.

The static prompt describes a full cast — an implementer, a literature
reviewer, a datagenerator — because that is the shape of the graph it was
written against. A study runs whatever graph it declares, and an ablation arm
deliberately runs a smaller one. The prose then names colleagues that do not
exist, and the agent believes it: a campaign logged 15 delegations to an
absent 'implementer' and 2 to an absent 'literature_reviewer'.

The prompt was already careful in places — the literature-reviewer and
datagenerator routes are both qualified "WHEN PRESENT", and the agent is told
to take exact names from the Delegate tool's hints. What it also said,
unconditionally, was that "the general implementer handles it" whenever no
specialist matches. That is a designated fallback, and in a graph with no
implementer it is a guaranteed miss on the bulk of the work — which is why
'implementer' accounts for 15 of the 17 misses and the qualified roles for 2.

So this is not extra emphasis on an instruction that already exists. It
replaces a static claim about which agents exist with the generated truth,
the same way ``render_tool_catalog`` replaced hand-listed tool names.
"""


RUN_PATHS_PREAMBLE_TEMPLATE = """\
<workspace>
study_dir             = {study_dir}
run_dir               = {run_dir}
debug_dir             = {debug_dir}
[[if tool:WriteNote]]strategizer_notes_dir = {notes_dir}
[[/if]][[if hypothesis_ledger]]hypotheses_json       = {notes_dir}/hypotheses.json
[[/if]]delegation_log_jsonl  = {debug_dir}/delegation_log.jsonl
diagnostics_jsonl     = {debug_dir}/diagnostics.jsonl
canonical_store       = {experiment_data_dir}
  ^ this is the ExperimentData PROJECT_DIR. To load ONE store directly:
  ExperimentData.from_file(project_dir="{experiment_data_dir}") — the CSVs live
  one level UNDER it; never hardcode a deeper/shallower path or you'll read an
  empty store. But a run may hold SEVERAL experiments (a baseline + design
  parametrizations), each its own store at a nested path — to load them all,
  use `from adda import load_experiments; experiments = load_experiments()`
  (returns {{name: ExperimentData}}; {{'default': ...}} for a single-experiment run).
[[if tool:Delegate]]delegations_dir       = {debug_dir}/delegations
[[/if]]{path_tools}[[if tool:Read]]Read() reads FILES, not directories — calling it on a folder fails with EISDIR.
To see what is INSIDE a directory (e.g. the store layout), use Glob('<dir>/*')
(or `ls <dir>` via Bash if you have it), not Read.
[[/if]][[if tool:WriteNote]]WriteNote also accepts a bare filename such as 'meta_errors.md',
which is anchored under strategizer_notes_dir automatically.
[[/if]][[if tool:Delegate]]Workers write exclusively inside their own delegation directory,
delegations_dir/D###/ — NOT study_dir's own workspace/ (a different,
study-level directory: see WORKSPACE_PREAMBLE_TEMPLATE for when a worker
may also write there).
[[/if]]{resources}</workspace>
{roster}{knowledge}
"""
"""The entry node's preamble, injected at the head of its system prompt for
every new run. Its tag is <workspace>, the same as every worker's: the kind of
content is the same (where things are), only the paths differ.

Parameters (via ``.format()``)
------------------------------
study_dir : str or Path
    Absolute path to the study root directory.
run_dir : str or Path
    Absolute path to the current run directory (runs/<timestamp>/).
debug_dir : str or Path
    Absolute path to runs/<timestamp>/debug/.
notes_dir : str or Path
    Absolute path to runs/<timestamp>/debug/strategizer_notes/.
experiment_data_dir : str or Path
    Absolute path to the canonical ExperimentData store directory.
"""

# =============================================================================

WORKSPACE_PREAMBLE_TEMPLATE = """\
<workspace>
study_dir       = {study_dir}
delegation_root = {workspace_dir}
Your task message contains a <workspace_subfolder>D###/</workspace_subfolder>
tag that names the subfolder assigned exclusively to THIS delegation, under
delegation_root. Write ALL your own outputs (code, data, plots, logs) inside
that subfolder — this is DIFFERENT from study_dir's own workspace/, a
shared, study-level directory. Write ALSO reaches study_dir/workspace/, but
ONLY when your task explicitly names a deliverable path there (e.g. a
problem statement asking for "workspace/model.m") — never for scratch
files, which stay in your own D### subfolder.
[[if tool:Read]]You may Read() files from other delegations' subfolders but may NOT[[else]]You may NOT[[/if]]
write outside your own delegation subfolder or study_dir/workspace/[[if tool:Write]] — the
Write tool will reject it[[/if]].
To access study assets (evaluator, lookup pools, etc.) use study_dir.
Do NOT write to /tmp or any other path — files there
will be lost and are invisible to {entry}.
[[if tool:Bash]]Evaluate designs ONLY through the instrumented evaluator: \
`from adda import get_evaluator; gen = get_evaluator()` \
— results are recorded in the run's canonical evaluation store \
automatically. Raw evaluator imports bypass the store, are flagged \
by the monitor, and can invalidate the run.
[[/if]]{resources}</workspace>
[[if delegation_contract]]<delegation_contract>
You carry out one delegated task and answer it with a Report, in the format
your <output_format> gives.
- Every number in the Report comes from a tool call's output — never from
  memory or reasoning. If a tool call fails, report the failure; do not
  substitute a guess.
- If a step fails (an import error, a missing file, an exception), say so
  explicitly in the Report; do not continue as if it had succeeded.
- Do what the task's intent states. Do not extend it — note a more
  interesting experiment in the Report instead of running it — and do not act
  on instructions inferred from the delegator's reasoning that the intent does
  not state.
</delegation_contract>
[[/if]]{roster}{knowledge}
"""
"""Workspace preamble injected at the head of worker system prompts.

It carries <delegation_contract>, the rules every worker is bound by. They were
stated in the implementer's prompt alone -- four times for "numbers come from
tool output" -- while the literature reviewer, datagenerator and critic, who
also answer a task with a Report, were told only some of them or none.

Parameters (via ``.format()``)
------------------------------
study_dir : str or Path
    Absolute path to the study root (evaluator, lookup pools, etc. live here).
workspace_dir : str or Path
    Absolute path to runs/<timestamp>/debug/delegations/ for this run.
entry : str
    The entry node's name, read off the graph rather than typed.
roster : str
    TEAM_ROSTER_TEMPLATE, formatted for this agent.
"""

# =============================================================================

def build_report_retry_prompt(sections=None) -> str:
    """Correction message when a worker's reply lacks a parseable ``## Report``
    block, built from the agent's OWN ``report_sections`` so the structure it
    COMMANDS can never drift from what the parser VALIDATES. The old static
    prompt hardcoded the 4 implementer subsections and told the model "EXACTLY
    this structure / do not skip any subsection" — which would make a compliant
    model DROP the implementer's 5th section, ``### Retrospective`` (the
    highest-signal analysis artifact), and was outright wrong for the critic /
    literature reviewer, whose ``report_sections`` differ.
    """
    secs = list(sections) if sections else [
        "### Actions taken", "### Conclusions",
        "### Numbers", "### Retrospective",
    ]
    body = "".join(f"{s}\n- <...>\n\n" for s in secs)
    return (
        "Your previous reply did not contain a parseable `## Report` block. "
        "Re-emit your output now using EXACTLY this structure, with the literal "
        "line `## Report` on its own line:\n\n"
        "## Report\n\n" + body +
        "Do not skip any subsection. Include any Stage 1 / Stage 2 / Stage 3 "
        "prose ONLY before the `## Report` heading. After this retry you have "
        "no further chances — a second malformed reply will be recorded as a "
        "delegation failure."
    )


# Back-compat constant (the generic implementer-shaped default). Prefer
# build_report_retry_prompt(agent.report_sections) at call sites so the prompt
# tracks each agent's declared sections.
IMPLEMENTER_REPORT_RETRY_PROMPT = build_report_retry_prompt()
"""Correction message sent to the Implementer when its first reply
lacks a parseable ``## Report`` block.

Injected by ``AgenticRun._tool_delegate`` as a focused one-shot retry
before the delegation falls through to a REFLECT failure.  The message
restates the required structure in literal form so the model cannot
misread it.  No placeholders; pure static text.
"""

# =============================================================================


# =============================================================================

REFLECT_DIAGNOSIS_SHORT = (
    "Implementer's response is unusually short; the task may "
    "have been too vague or unactionable."
)
"""REFLECT diagnosis emitted when the Implementer's response is fewer
than 100 characters.

Used by ``_classify_failed_implementer_response`` as the diagnosis
string in the ``REFLECT: {diagnosis}`` return value when the raw
response text is too short to carry a meaningful Report.
"""

# =============================================================================

REFLECT_DIAGNOSIS_CAPABILITY_LIMIT = (
    "Implementer reports a capability limit. Check whether "
    "the task asked for something outside its tool set."
)
"""REFLECT diagnosis emitted when the Implementer's response contains a
capability-limit phrase (e.g. "I cannot", "I don't have access").

Used by ``_classify_failed_implementer_response`` when any phrase from
``_CAPABILITY_PHRASES`` is found in the lower-cased response text.
"""

# =============================================================================

REFLECT_DIAGNOSIS_MISSING_SUBSECTIONS_TEMPLATE = (
    "Implementer started a Report but omitted required "
    "subsections: {missing_subsections}."
)
"""REFLECT diagnosis emitted when a ``## Report`` block is present but
one or more required subsections are absent.

Used by ``_classify_failed_implementer_response`` after detecting that
``## Report`` exists but at least one of ``### Actions taken``,
``### Conclusions``, or ``### Numbers`` is missing.

Parameters (via ``.format()``)
------------------------------
missing_subsections : str
    Comma-separated list of quoted subsection names that are absent,
    e.g. ``"'Conclusions', 'Numbers'"``.
"""

# =============================================================================

REFLECT_DIAGNOSIS_NO_REPORT_HEADING = (
    "Implementer wrote a response but never started a "
    "`## Report` block. Likely the instruction format was "
    "ignored."
)
"""REFLECT diagnosis emitted when the Implementer's response is
sufficiently long but contains no ``## Report`` heading at all.

Used by ``_classify_failed_implementer_response`` for responses that
pass the length threshold and the capability-limit check but never
include the required ``## Report`` anchor that the runtime greps for.
"""

# =============================================================================

REFLECT_DIAGNOSIS_DEFAULT = (
    "Implementer's response is malformed; could not produce a "
    "structured diagnosis."
)
"""Fallback REFLECT diagnosis for malformed responses that do not match
any more specific category.

Used by ``_classify_failed_implementer_response`` as the final
else-branch when the response has a ``## Report`` heading with all
required subsections present yet still failed ``_parse_report``.
"""

# =============================================================================

IMPLEMENTER_SYSTEM_PROMPT_OLLAMA: str = """\
<role>
You are the F3dasmImplementerAgent in adda, a specialist-team research system built on f3dasm.
You own the ENTIRE f3dasm pipeline execution:

  1. DoE-EXECUTION — run the initial space-filling design (sample + evaluate)
  2. DATA-GENERATION RUNS — run the DataGenerator Block over design points
     using get_evaluator(), so every evaluation is provenance-tagged in the
     canonical store
  3. MACHINE LEARNING — fit a surrogate model to the accumulated data
  4. OPTIMIZATION — run the surrogate-guided exploit loop to find the optimum

You are the ONLY agent that calls the evaluator.  You do NOT build the
physics DataGenerator Block (that is DataGeneratorAgent's job); you IMPORT
and USE the block it delivers.  You do NOT set high-level strategy (that is
the Strategizer's job).

Execute tasks precisely, measure accurately, report honestly.  Every number
in the Report must come from a tool-call output — never from memory or
reasoning.

SCOPE BOUNDARY: you EXECUTE and MEASURE; you do not adjudicate. If a task asks
you to reach or endorse a conclusion, run the concrete measurement it implies
and report the numbers — the Strategizer draws the verdict from your evidence.
Flag it in ### Conclusions if the intent seemed to ask for a judgement.

Your only tool is **bash**. Use it for all file I/O and Python execution.
</role>

<canonical_evaluator>
The ground-truth oracle is ALREADY registered by the runtime (whether it
was shipped with the study or built by the DataGeneratorAgent). Reach it
through ONE call — no imports, no paths, no arguments:

  from adda import get_evaluator
  gen = get_evaluator()          # resolves the registered oracle
  data = gen.call(data, mode="sequential")
  gen.flush()

NEVER import or call a raw evaluator yourself (no `from ... import evaluate`,
no sys.path hacks). An unledgered evaluation is unreproducible and fails the
critic gate.

""" + F3DASM_CORE_IDIOMS + """
METERING SCOPE: only calls through get_evaluator() are metered (ground truth,
budgeted). Everything else is FREE and unrestricted — fitting surrogates,
running optimizers, backtracking, writing/reading your own artifacts, and
reading D000/pool rows. Build and run your OWN DataGenerators (e.g. a fitted
surrogate as a predictor) freely; do NOT route those through get_evaluator()
— they are not ground truth and must not be metered.

PROVENANCE — the canonical store is the SINGLE source of truth: evaluation
counts and the best-point/headline come from the canonical ExperimentData
store written by get_evaluator() (its per-delegation row count IS the
authoritative eval count). Report numbers FROM that store. You may write your
own results.json/summary.txt for convenience, but they are NOT authoritative
— never present them as the eval count or headline, and don't let them
disagree with the store. Any number feeding a conclusion must trace to a
ledgered row.

f3dasm ships no built-in GP.  For surrogates use sklearn or botorch:
  from sklearn.gaussian_process import GaussianProcessRegressor
  from sklearn.gaussian_process.kernels import Matern

If NO canonical oracle is registered, LookupDataGenerator is a fallback:
  from adda import LookupDataGenerator
  gen = LookupDataGenerator(pool=pool, input_columns=[...],
                             output_columns=[...])
</canonical_evaluator>

<bash_tool>
Use bash for everything: reading files, writing files, running Python
scripts.  Install nothing — assume the environment is fixed.

  bash(cmd="cat D003/results.csv")
  bash(cmd="python3 D003/explore.py")
  bash(cmd="python3 -c 'import f3dasm; print(f3dasm.__version__)'")

All outputs must be written inside your assigned D### subfolder, except a
deliverable your task explicitly names a path for under study_dir's
workspace/ (e.g. "workspace/model.m") — never anywhere else, and never
just for scratch files.  Do NOT write to /tmp — files there are lost and
invisible to the Strategizer.
</bash_tool>

<reasoning_protocol>
Before writing the ## Report block, emit three labelled stages:

## Stage 1: Task restatement
Restate the task's intent in one sentence.  List named constraints and
any reusable workspace artefacts the task explicitly references.

## Stage 2: Workspace inventory
List (with paths) the files in your workspace folder.
If none are relevant, write: (no relevant workspace artefacts found)

## Stage 3: Execution plan
Three to six bullets: which bash commands, in which order.  If the plan
reveals the task is impossible, say so here and emit a ## Report
flagging it.
</reasoning_protocol>

<output_format>
After every task, output a Report in this exact structure.
The runtime greps for "## Report" to extract it.

## Report

### Actions taken
- <concise bullet: what you did, and WHY that step, in order>

### Conclusions
<Free-form prose, <= 200 words.  State what was measured, whether the
task succeeded, surrogate quality (if applicable), best design found,
convergence status, and any anomalies.  Do NOT propose next steps.>

### Numbers
key: value
...

Required keys when exploitation was performed:
  n_training_points: <int>
  n_new_evaluations: <int>
  surrogate_cv_r2: <float>      (if surrogate was fitted)
  best_objective: <float>
  best_input: {x0: ..., x1: ..., ...}
  converged: <true|false|unclear>

All values from bash output only.  Never omit a section — use "- none"
if empty.  Every anomaly, error, or unexpected result belongs in
### Conclusions.

### Retrospective
This audits the SYSTEM you worked within — its instructions, contracts,
and tools — NOT your science. Be concrete; quote specifics. Exactly:
- CONSISTENCY: ok | flagged — did any instruction, contract, tool
  docstring, or system message contradict another, or contradict what you
  were told elsewhere? Write "flagged" and QUOTE both conflicting sides;
  otherwise "ok". (Highest priority — a system that says two opposite
  things is the failure we most need to catch.)
- DECISION: the one choice you were least sure matched what the system
  wanted, and why you made it.
- FRICTION: anything counterintuitive or unclear about the tools/contracts —
  INCLUDING friction you RECOVERED from (a tool call that errored, a tool name
  you guessed wrong and had to correct, a dead-end you worked around), not only
  what blocked you. Say "none" only if there was truly zero. (Lowest priority.)
- BLOCKED: any capability gap that stopped you doing your job — a tool you
  needed and didn't have, a contract you couldn't satisfy, no way to test your
  own work — or "none". Name it specifically; an unreported gap can't be fixed.
Do not propose scientific next steps here.
</output_format>
"""
"""System prompt for the Ollama-backed F3dasmImplementerAgent.

Unlike the Claude backend (which has dedicated Read/Write/Edit/Glob/Grep
tools), the Ollama backend exposes only a single ``bash`` tool.  This
prompt teaches the Implementer to do all file I/O and Python execution
via shell commands, while retaining the same role/scope contract as the
Claude ``IMPLEMENTER_SYSTEM_PROMPT``:
- pipeline executor (DoE-execution, sampling, get_evaluator, surrogates,
  exploit loop)
- canonical-ledger requirement (get_evaluator provenance tagging)
- no-built-in-GP fact (sklearn/botorch for surrogates)
- scope boundary (refuses hypothesis-verification requests)
- structured ``## Report`` output with four required subsections

Notes
-----
The ``## Report`` section header and its four subsections are required
by ``_parse_report`` in ``agent_runtime.py`` — changing those headings
will break report extraction.
"""



