"""The viewer serves ANY study (spec 15 §6a): no study's vocabulary may appear
in the shipped viewer, its screenshot tool, or the tests that exercise it.

The denylist is every distinctive term from the studies in ``studies/`` (their
declared output columns and names) plus the benchmark suite's physics, columns
and namespaces. Everything study-specific must come from a study's config or
the run's own records.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

_BENCHMARK_TERMS = (
    "sigma", "riks", "coil", "prefilter", "bessa", "supercompress", "freeform",
    "abaqus", "ratio_", "slenderness", "buckl", "imperfection", "matlab",
    "kinematic", "bioreactor", "locomotion", "hybridml", "pass bar", "lb_coil",
    "mcs_", "mls_", "energy_absorbed", "mesh", "p_cr",
)
_GENERIC = {"status", "note", "feasible", "converged", "output", "input"}

_SCANNED = [
    *sorted((ROOT / "src/adda/_src/viewer").rglob("*")),
    ROOT / "internal/tools/viewer_shots.py",
    ROOT / "tests/test_viewer_ui.py",
    ROOT / "tests/test_objective_declaration.py",
    ROOT / "internal/specs/15-viewer-design.md",
]
_SUFFIXES = {".py", ".js", ".css", ".html", ".md"}


def _study_terms() -> set[str]:
    terms = set()
    for cfg in (ROOT / "studies").glob("*/config.yaml"):
        terms.add(cfg.parent.name.lower())
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        ev = data.get("evaluator") or {}
        cols = [*(ev.get("output_names") or []),
                *((ev.get("lookup") or {}).get("output_columns") or []),
                *(ev.get("provenance") or {})]
        obj = data.get("objective") or {}
        cols += [obj.get("column"), obj.get("feasible"), *(data.get("funnel") or [])]
        terms.update(c.lower() for c in cols if isinstance(c, str))
    return {t for t in terms if len(t) >= 4 and t not in _GENERIC}


def _hits(text: str, terms) -> list[str]:
    low = text.lower()
    return sorted({t for t in terms if re.search(r"(?<![a-z])" + re.escape(t), low)})


def test_the_viewer_carries_no_study_vocabulary():
    terms = set(_BENCHMARK_TERMS) | _study_terms()
    found = {}
    for path in _SCANNED:
        if path.suffix not in _SUFFIXES or not path.is_file():
            continue
        hit = _hits(path.read_text(encoding="utf-8", errors="ignore"), terms)
        if hit:
            found[str(path.relative_to(ROOT))] = hit
    assert not found, (
        "study vocabulary in the viewer; move it to a study's config.yaml "
        f"declaration or the run's records: {found}")


def test_the_denylist_catches_what_it_is_for():
    assert _hits("x Bessa", _BENCHMARK_TERMS) == ["bessa"]
    assert _hits("sigma_peak", _BENCHMARK_TERMS) == ["sigma"]
    assert _hits("flex-wrap; align-items: baseline", _BENCHMARK_TERMS) == []
