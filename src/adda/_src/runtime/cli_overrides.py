"""``--set key=value`` for the command-line entry points.

An ablation campaign needs N runs of one committed study under different
runtime arms without editing the study. ``AgenticRun(runtime=...)`` is that
override path in Python; this is the same path from the command line, so the
arm is explicit (it outranks env and config.yaml), is validated against
``settings.KNOWN_KEYS`` before anything launches, and lands in the run's
``run_config.json`` like any other explicit knob.
"""
from __future__ import annotations

import argparse

import yaml

from .settings import KNOWN_KEYS


def set_item(text: str) -> tuple[str, object]:
    """Parse one ``key=value``; the value is read as YAML (``false`` -> bool,
    ``5`` -> int, ``auto`` -> str), the same grammar config.yaml uses."""
    key, sep, raw = text.partition("=")
    key = key.strip()
    if not sep or not key:
        raise argparse.ArgumentTypeError(
            f"expected KEY=VALUE, got {text!r}")
    if key not in KNOWN_KEYS:
        raise argparse.ArgumentTypeError(
            f"unknown runtime knob {key!r}. "
            f"Known knobs: {', '.join(sorted(KNOWN_KEYS))}")
    try:
        return key, yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise argparse.ArgumentTypeError(
            f"cannot read the value of {key!r}: {exc}") from exc


def add_set_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        type=set_item,
        default=[],
        metavar="KEY=VALUE",
        help="Override a runtime knob for this run (repeatable), e.g. "
             "--set hypothesis_ledger=false. Outranks env and config.yaml; "
             "an unknown knob is an error.",
    )


def as_runtime(items: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in items:
        if key in out:
            raise ValueError(f"--set {key} given twice")
        out[key] = value
    return out


def format_set(items: list[tuple[str, object]]) -> list[str]:
    """Back to argv form, for a launcher that forwards the overrides."""
    out: list[str] = []
    for key, value in items:
        out += ["--set", f"{key}={yaml.safe_dump(value).splitlines()[0]}"]
    return out
