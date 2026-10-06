"""Central run-knob accessor — config.yaml is the source of truth.

Run KNOBS (debug, recursion_limit, idle timeouts, retry, backstop, …) live in
the ``runtime:`` block of a study's ``config.yaml``. Environment variables are
override/secrets only. Resolution precedence, per knob:

    explicit argument  >  config.yaml  >  default

The explicit tier is what a caller passes as ``AgenticRun(runtime={...})``.
There is no environment tier: a knob lives in ``config.yaml`` or is passed
explicitly, so the run's recorded condition is the one it ran under. A stale
``F3DASM_<KEY>`` exported in a shell used to beat every caller silently, which
for a sweep means an arm that ran under a label it did not have. It is now an
error: :func:`reject_stale_env` runs when an ``AgenticRun`` is constructed.

``AgenticRun`` calls :func:`configure` once at run start with the parsed
``runtime`` mapping. Read sites call :func:`get_bool` / :func:`get_int` /
:func:`get_float` / :func:`get_str`. Until ``configure`` runs (e.g. in a unit
test that constructs a node directly), only the default applies.
"""

from __future__ import annotations

import os
import threading

__all__ = [
    "KNOWN_KEYS",
    "SECRET_KEYS",
    "configure",
    "reject_stale_env",
    "get_bool",
    "resolved",
    "get_int",
    "get_float",
    "get_str",
]

# Every knob the codebase reads through this module. Its purpose is to make a
# typo LOUD: a misspelled key in a study's `runtime:` block would otherwise
# resolve to its default and the run would proceed as if the setting had been
# honoured, which is the classic silent-config failure. configure() warns on
# anything not listed here.
#
# tests/test_settings_contract.py greps the source for every get_*("key") call
# and fails if one is missing from this set, so the list cannot drift behind
# the code that reads it.
KNOWN_KEYS: frozenset[str] = frozenset({
    "citation_weighting",
    "context_policy",
    "context_window",
    "debug",
    "delegate_cutoff_multiple",
    "doe_playbook",
    "f3dasm_api",
    "followup_wait_s",
    "hypothesis_ledger",
    "launch",
    "llm_max_buffer_mb",
    "llm_metadata_fetch",
    "allow_arm_drift",
    "bash_timeout_s",
    "budget_notes",
    "delegation_contract",
    "reprompt_unfinished",
    "llm_metadata_timeout_s",
    "llm_quantization",
    "llm_retry_base",
    "llm_retry_max",
    "llm_stream_idle_timeout",
    "llm_tool_idle_timeout",
    "max_awake_nodes",
    "max_consecutive_errors",
    "max_output_tokens",
    "milestones_enabled",
    "peer_interaction",
    "peer_message_wait_s",
    "pipeline_deliverable",
    "recursion_limit",
    "reproduction_gate",
    "resume_close_with_retrospectives",
    "retrieval_mode",
    "run_backstop_multiple",
    "science_monitor",
    "stop_grace_s",
    "thinking_display",
    "verdict_validator",
})

#: Secrets are never run knobs: the viewer commits config.yaml to git, so a
#: secret written there lands in history. Maps the refused key to the
#: environment variable that carries it.
SECRET_KEYS: dict[str, str] = {
    "semantic_scholar_api_key": "SEMANTIC_SCHOLAR_API_KEY",
}


def secret_key_error(key: str) -> str:
    return (f"runtime.{key} is a secret and cannot live in config.yaml (the "
            f"viewer commits that file to git). Remove it and set the "
            f"environment variable {SECRET_KEYS[key]} instead.")


#: Names that set a run setting held outside ``runtime:`` (top-level
#: ``config.yaml`` keys), refused the same way.
_REFUSED_ENV = frozenset({"F3DASM_MEM_CAP"})

#: Endpoint variables, refused for the same reason: the endpoint is
#: ``base_url`` in config.yaml. API keys stay in the environment.
_REFUSED_ENDPOINT_ENV = frozenset(
    {"VLLM_BASE_URL", "OLLAMA_BASE_URL", "OPENROUTER_BASE_URL"})

_lock = threading.Lock()
_config: dict = {}
_explicit: dict = {}
_graph_nodes: frozenset | None = None

_TRUE = {"1", "true", "yes", "on"}


def configure(config: dict | None, explicit: dict | None = None) -> None:
    """Install this run's knobs.

    ``config`` is the study's ``runtime:`` block; ``explicit`` is what a caller
    passed programmatically and outranks both it and the environment.

    Replaces any prior mapping (each run installs its own). Pass ``None`` or an
    empty dict to clear (e.g. between tests).

    An unknown key is treated differently depending on where it came from. From
    ``config.yaml`` it warns: a stale block should not make a study
    unstartable, and that leniency is deliberate. From ``explicit`` it RAISES —
    a caller that misspells a knob is not asking for the default, and for a
    sweep a typo'd override means the baseline runs under an arm's label and
    reports as a null result."""
    global _config, _explicit, _graph_nodes
    cfg = dict(config or {})
    exp = dict(explicit or {})

    leaked = sorted((set(cfg) | set(exp)) & set(SECRET_KEYS))
    if leaked:
        raise ValueError(" ".join(secret_key_error(k) for k in leaked))
    bad = sorted(set(exp) - KNOWN_KEYS)
    if bad:
        raise ValueError(
            f"unknown runtime knob(s) {', '.join(repr(k) for k in bad)}. "
            f"Known knobs: {', '.join(sorted(KNOWN_KEYS))}"
        )

    unknown = sorted(set(cfg) - KNOWN_KEYS)
    if unknown:
        # A warning, not an error: an unknown knob is far more often a typo
        # than a reason to refuse to run, and refusing would make a stale
        # config block a study you cannot start.
        import logging
        logging.getLogger(__name__).warning(
            "config.yaml runtime: has unrecognised key(s) %s — ignored. "
            "Known knobs: %s",
            ", ".join(repr(k) for k in unknown),
            ", ".join(sorted(KNOWN_KEYS)),
        )
    with _lock:
        _config = cfg
        _explicit = exp
        _graph_nodes = None


def reject_stale_env() -> None:
    """Raise if the environment carries ``F3DASM_<KNOWN_KEY>``.

    The environment is not a settings channel, so such a variable is a stale
    export that would otherwise be ignored without a word. Only names that are
    a knob's own (``F3DASM_`` + the upper-cased key), or ``F3DASM_MEM_CAP``
    (the hard memory cap, ``mem_cap`` in config.yaml), count; the variables adda
    itself hands to its subprocesses (``F3DASM_NAMESPACE``,
    ``F3DASM_DELEGATION_ID``, ``F3DASM_RUN_CONFIG``, ``F3DASM_CANONICAL_STORE``,
    ``F3DASM_DEDUP_SCOPE``, …) are not knobs and are left alone."""
    names = {f"F3DASM_{k.upper()}" for k in KNOWN_KEYS} | _REFUSED_ENV
    stale = sorted(names & os.environ.keys())
    endpoint = sorted(_REFUSED_ENDPOINT_ENV & os.environ.keys())
    if endpoint:
        raise ValueError(
            f"environment variable(s) {', '.join(endpoint)} would set an "
            f"endpoint. The environment is not a settings channel: unset them "
            f"and put `base_url: <url>` in config.yaml (top level, or under "
            f"`nodes.<node>`). API keys stay in the environment.")
    if stale:
        raise ValueError(
            f"environment variable(s) {', '.join(stale)} would set a run knob. "
            f"The environment is not a settings channel: unset them and put the "
            f"value under `runtime:` in config.yaml (or pass "
            f"`AgenticRun(runtime={{...}})`).")


def set_graph_nodes(nodes) -> None:
    """Record which nodes this run's graph contains (``build_graph`` calls it
    once). ``configure`` clears it, so it is always a property of the run that
    installed the knobs."""
    global _graph_nodes
    with _lock:
        _graph_nodes = frozenset(nodes)


def graph_nodes() -> frozenset | None:
    """The live graph's node names, or ``None`` before any graph is built
    (every node is then assumed present: the default topology)."""
    return _graph_nodes


def resolved() -> dict:
    """Every knob this run actually runs with, after precedence is applied.

    The condition a run was executed under, recorded from the run itself
    rather than asserted by whatever launched it."""
    out = {}
    for key in sorted(KNOWN_KEYS):
        v = _raw(key)
        if v is not None:
            out[key] = v
    return out


def _raw(key: str):
    """Resolved raw value: explicit > config.yaml > None."""
    with _lock:
        if key in _explicit:
            return _explicit[key]
        return _config.get(key)


def _is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def get_bool(key: str, default: bool) -> bool:
    v = _raw(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _TRUE


def get_int(key: str, default: int) -> int:
    v = _raw(key)
    if _is_blank(v):
        return default
    # tolerate float-ish strings ("12", "12.0") and real yaml numbers
    return int(float(v))


def get_float(key: str, default: float) -> float:
    v = _raw(key)
    if _is_blank(v):
        return default
    return float(v)


def get_str(key: str, default: str) -> str:
    v = _raw(key)
    return default if v is None else str(v)
