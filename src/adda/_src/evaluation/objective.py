"""The study's declared objective: one place that says what "best" and
"feasible" mean, read by the run ledger and the viewer alike.

``config.yaml``::

    objective:
      column: sigma_peak
      direction: max          # max | min
      feasible: feasible      # optional 0/1 column; absent = finite rule only
      lines:                  # optional reference lines on the viewer's chart
        - {value: 0.1122, label: "1x pass bar"}
      unit_label: {divide_by: 0.1122, label: "x Bessa"}   # optional axis scaling

Nothing here is inferred: a study that declares no objective is reported as
undeclared and its rows are judged by the finite rule alone.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

_KEYS = {"column", "direction", "feasible", "lines", "unit_label"}
_DIRECTIONS = ("max", "min")
# f3dasm marks failed designs with a large-magnitude sentinel; mirrors
# nodes/tools/routing/store._INFEASIBLE_SENTINEL_MAG.
SENTINEL_MAG = 1e8


def parse_objective(
    raw: Any, known_columns: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Validate the ``objective:`` block; None when the study declares none.

    ``known_columns`` is what the oracle is declared to produce; when given,
    an unknown ``column`` or ``feasible`` is refused. A study whose oracle is
    only registered mid-run passes None and is not checked here.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"objective: must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - _KEYS
    if unknown:
        raise ValueError(
            f"objective: unknown key(s) {sorted(unknown)}; allowed {sorted(_KEYS)}")
    column = raw.get("column")
    if not isinstance(column, str) or not column:
        raise ValueError("objective.column is required (the output column to optimise)")
    direction = raw.get("direction")
    if direction not in _DIRECTIONS:
        raise ValueError(
            f"objective.direction is required and must be one of {_DIRECTIONS}, "
            f"got {direction!r}")
    feasible = raw.get("feasible")
    if feasible is not None and (not isinstance(feasible, str) or not feasible):
        raise ValueError("objective.feasible must be a column name when given")
    if known_columns is not None:
        known = list(known_columns)
        for key, col in (("column", column), ("feasible", feasible)):
            if col is not None and col not in known:
                raise ValueError(
                    f"objective.{key} {col!r} is not a column this study's "
                    f"oracle produces (declared: {known}); fix config.yaml")
    out: dict[str, Any] = {"column": column, "direction": direction}
    if feasible is not None:
        out["feasible"] = feasible
    if raw.get("lines") is not None:
        out["lines"] = _parse_lines(raw["lines"])
    if raw.get("unit_label") is not None:
        out["unit_label"] = _parse_unit_label(raw["unit_label"])
    return out


def _finite(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _parse_lines(raw: Any) -> list[dict[str, Any]]:
    """Reference lines for the chart, in the objective column's own units."""
    if not isinstance(raw, list):
        raise ValueError("objective.lines must be a list of {value, label}")
    out = []
    for i, ln in enumerate(raw):
        if not isinstance(ln, Mapping) or set(ln) != {"value", "label"}:
            raise ValueError(
                f"objective.lines[{i}] must be a mapping with exactly "
                f"'value' and 'label'")
        if not _finite(ln["value"]):
            raise ValueError(f"objective.lines[{i}].value must be a finite number")
        if not isinstance(ln["label"], str) or not ln["label"].strip():
            raise ValueError(f"objective.lines[{i}].label must be a non-empty string")
        out.append({"value": ln["value"], "label": ln["label"]})
    return out


def _parse_unit_label(raw: Any) -> dict[str, Any]:
    """Display-only axis scaling: shown value = raw value / divide_by."""
    if not isinstance(raw, Mapping) or set(raw) != {"divide_by", "label"}:
        raise ValueError(
            "objective.unit_label must be a mapping with exactly "
            "'divide_by' and 'label'")
    if not _finite(raw["divide_by"]) or raw["divide_by"] <= 0:
        raise ValueError("objective.unit_label.divide_by must be a positive number")
    if not isinstance(raw["label"], str) or not raw["label"].strip():
        raise ValueError("objective.unit_label.label must be a non-empty string")
    return {"divide_by": raw["divide_by"], "label": raw["label"]}


def missing_columns(
    objective: Mapping[str, Any] | None, produced: Iterable[str],
) -> list[tuple[str, str]]:
    """The declared ``(key, column)`` pairs an oracle producing ``produced``
    would not supply; empty when no objective is declared."""
    if not objective:
        return []
    have = set(produced)
    return [(k, objective[k]) for k in ("column", "feasible")
            if objective.get(k) and objective[k] not in have]


def label(objective: Mapping[str, Any] | None) -> str:
    return "undeclared" if not objective else (
        f"{objective['column']}:{objective['direction']}"
        + (f":feasible={objective['feasible']}" if objective.get("feasible") else ""))


def _number(v: Any) -> float | None:
    if v is True or v == "True":
        return 1.0
    if v is False or v == "False":
        return 0.0
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def objective_values(
    rows: list[Mapping[str, Any]], objective: Mapping[str, Any] | None,
    default_column: str,
) -> list[float | None]:
    """Per row: the objective value if the row counts, else None.

    A row counts when its objective is finite and below the sentinel
    magnitude and, if the study declares a ``feasible`` column, that column is
    1. A finite value on an infeasible row does not count.
    """
    col = objective["column"] if objective else default_column
    feas = objective.get("feasible") if objective else None
    out: list[float | None] = []
    for r in rows:
        v = _number(r.get(col))
        if v is None or abs(v) >= SENTINEL_MAG:
            out.append(None)
        elif feas is not None and _number(r.get(feas)) != 1.0:
            out.append(None)
        else:
            out.append(v)
    return out


def best_so_far(
    values: list[float | None], objective: Mapping[str, Any] | None,
) -> dict[str, list[float | None]]:
    """Running best per row. Declared: ``best`` in the declared direction.
    Undeclared: ``min`` and ``max`` both (the direction is not ours to guess)."""
    def run(pick) -> list[float | None]:
        cur: float | None = None
        res: list[float | None] = []
        for v in values:
            if v is not None:
                cur = v if cur is None else pick(cur, v)
            res.append(cur)
        return res
    if objective:
        return {"best": run(max if objective["direction"] == "max" else min)}
    return {"min": run(min), "max": run(max)}
