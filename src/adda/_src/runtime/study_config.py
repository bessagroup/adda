"""The keys a study's ``config.yaml`` may hold at the top level.

adda reads exactly these. ``study:`` is reserved for the study's own use (its
run scripts read it); adda never interprets it. Any other key is an error, so a
typo cannot be silently ignored.
"""

from __future__ import annotations

import difflib
from typing import Any

#: Top-level keys adda reads.
TOP_KEYS = frozenset({
    "backend", "model", "base_url", "budget", "budget_usd", "eval_budget",
    "mem_cap", "budget_clock", "token_budget", "watchdog_wall_s",
    "required_deliverables", "review_statement", "runtime", "nodes",
    "evaluator", "objective", "funnel", "training_data", "llm_slurm",
})

#: Reserved for the study; adda never reads inside it.
STUDY_KEY = "study"


def validate_top_level(cfg: Any) -> list[str]:
    """Errors for unknown top-level keys and a malformed ``base_url``/``study``."""
    if not isinstance(cfg, dict):
        return ["config.yaml must be a mapping of settings"]
    errors = []
    for key in sorted(set(cfg) - TOP_KEYS - {STUDY_KEY}, key=str):
        close = difflib.get_close_matches(str(key), TOP_KEYS, n=1)
        errors.append(f"{key} is not a known config.yaml key"
                      + (f" (did you mean {close[0]}?)" if close else "")
                      + f"; put study-specific settings under `{STUDY_KEY}:`")
    if STUDY_KEY in cfg and not isinstance(cfg[STUDY_KEY], dict):
        errors.append(f"`{STUDY_KEY}:` must be a mapping")
    if "base_url" in cfg and not (
            isinstance(cfg["base_url"], str) and cfg["base_url"]):
        errors.append("base_url must be a non-empty string")
    return errors
