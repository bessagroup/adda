"""Central run-knob accessor — config.yaml is the source of truth.

Run KNOBS (debug, recursion_limit, idle timeouts, retry, backstop, …) live in
the ``runtime:`` block of a study's ``config.yaml``. Environment variables are
override/secrets only. Resolution precedence, per knob:

    explicit argument  >  env var (F3DASM_<UPPER_KEY>)  >  config.yaml  >  default

The explicit tier is what a caller passes as ``AgenticRun(runtime={...})``. It
outranks the environment because the docs already say that channel "is for
secrets and one-off overrides — it is not where a study's settings belong": a
stale ``F3DASM_MILESTONES_ENABLED`` left exported in a shell used to beat every
caller silently, which for a sweep means an arm that ran under a label it did
not have. An explicit argument is the most deliberate statement of intent
there is, so it wins.

``AgenticRun`` calls :func:`configure` once at run start with the parsed
``runtime`` mapping. Read sites call :func:`get_bool` / :func:`get_int` /
:func:`get_float` / :func:`get_str`. Until ``configure`` runs (e.g. in a unit
test that constructs a node directly), only env + default apply — identical to
the old ``os.environ.get`` behaviour, so the migration is backward compatible.
"""

from __future__ import annotations

import os
import threading

__all__ = [
    "KNOWN_KEYS",
    "configure",
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
    "llm_max_buffer_mb",
    "llm_metadata_fetch",
    "llm_metadata_timeout_s",
    "llm_quantization",
    "llm_retry_base",
    "llm_retry_max",
    "llm_stream_idle_timeout",
    "llm_tool_idle_timeout",
    "max_consecutive_errors",
    "max_output_tokens",
    "milestones_enabled",
    "peer_interaction",
    "peer_message_wait_s",
    "pipeline_deliverable",
    "recursion_limit",
    "reproduction_gate",
    "retrieval_mode",
    "run_backstop_multiple",
    "science_monitor",
    "semantic_scholar_api_key",
    "thinking_display",
    "verdict_validator",
})

_lock = threading.Lock()
_config: dict = {}
_explicit: dict = {}

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
    global _config, _explicit
    cfg = dict(config or {})
    exp = dict(explicit or {})

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
    """Resolved raw value: explicit > env > config.yaml > None."""
    with _lock:
        if key in _explicit:
            return _explicit[key]
    env = os.environ.get("F3DASM_" + key.upper())
    if env is not None:
        return env
    with _lock:
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
