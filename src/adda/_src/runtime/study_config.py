"""The keys a study's ``config.yaml`` may hold at the top level.

adda reads exactly these. ``study:`` is reserved for the study's own use (its
run scripts read it); adda never interprets it. Any other key is an error, so a
typo cannot be silently ignored.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

#: Top-level keys adda reads.
TOP_KEYS = frozenset({
    "backend", "model", "base_url", "budget", "budget_usd", "eval_budget",
    "mem_cap", "token_budget", "watchdog_wall_s",
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
    errors += _deliverable_errors(cfg.get("required_deliverables"))
    if "base_url" in cfg and not (
            isinstance(cfg["base_url"], str) and cfg["base_url"]):
        errors.append("base_url must be a non-empty string")
    return errors


def deliverable_name(entry: Any) -> str:
    """The bare filename of a ``required_deliverables`` entry: a string, or a
    mapping with ``path``."""
    return Path(entry["path"] if isinstance(entry, dict) else entry).name


def deliverable_json_keys(entry: Any) -> list[str] | None:
    """The top-level keys a mapping entry requires its JSON file to hold."""
    return list(entry["json_keys"]) if (
        isinstance(entry, dict) and "json_keys" in entry) else None


def _deliverable_errors(entries: Any) -> list[str]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        return ["required_deliverables must be a list"]
    errors = []
    for e in entries:
        if isinstance(e, str):
            continue
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)
                and e["path"] and set(e) <= {"path", "json_keys"}):
            errors.append(
                "required_deliverables entries are a filename or a mapping "
                f"with `path` and optional `json_keys`; got {e!r}")
            continue
        keys = e.get("json_keys")
        if "json_keys" in e and not (
                isinstance(keys, list) and keys
                and all(isinstance(k, str) for k in keys)):
            errors.append(
                f"required_deliverables {e['path']}: json_keys must be a "
                "non-empty list of key names")
    return errors


def deliverable_format_errors(study_dir: Path, entries: list) -> list[str]:
    """Why each mapping entry with ``json_keys`` is not in its declared format.

    The file must hold a JSON object whose top-level keys are exactly the
    declared ones. A plain filename entry has no format and is never reported.
    """
    problems = []
    for e in entries:
        keys = deliverable_json_keys(e)
        if keys is None:
            continue
        name = deliverable_name(e)
        path = Path(study_dir) / name
        if not path.exists():
            problems.append(f"{name}: the file does not exist")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{name}: not valid JSON ({exc})")
            continue
        if not isinstance(data, dict):
            problems.append(
                f"{name}: the top level must be a JSON object with the keys "
                f"{keys}, not {type(data).__name__}")
            continue
        missing = [k for k in keys if k not in data]
        extra = [k for k in data if k not in keys]
        if missing or extra:
            problems.append(
                f"{name}: top-level keys must be exactly {keys}; "
                f"missing {missing}, extra {extra}")
    return problems
