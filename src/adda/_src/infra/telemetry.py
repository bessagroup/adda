"""Separable, analysis-ready LLM-call telemetry.

This subsystem is deliberately *additive* and *off the decision path*: it
records one row per LLM call and merges them into a ``summary.json`` for
post-hoc analysis (ablations — "where did the budget go; does the machinery
improve outcomes").  A telemetry write must NEVER break a run, so every
``record_call`` swallows its own errors.

Layout (under ``<run>/debug/telemetry/``):
  - ``calls.<pid>.jsonl``  — one JSON row per LLM call, one file per process.
    Per-PID files keep parallel worker *processes* from corrupting a shared
    file; within a process an instance lock serialises the daemon worker
    threads' appends.
  - ``summary.json``       — written by :meth:`Telemetry.merge`, with totals
    and breakdowns by role / phase / model.

A row carries the same token fields the run already accumulates
(``adapter.last_usage``) plus ``role`` / ``model`` / ``phase`` /
``delegation_id`` / ``ts`` so each call is attributable.

Token schema (the same for every backend)
-----------------------------------------
Each backend maps its own numbers into four disjoint counts, so a total is
their plain sum and a number means the same thing whichever backend produced
it:

  - ``fresh_input`` -- prompt tokens the model read at full price (not served
    from, and not written to, a cache);
  - ``cache_read``  -- prompt tokens served from a cache;
  - ``cache_write`` -- prompt tokens written to a cache;
  - ``output``      -- generated tokens.

The older ``input_tokens`` field is NOT comparable across backends: Claude
reports it without cache tokens, an openai-compatible server (vllm, ollama,
openrouter) reports the whole re-sent prompt with cache tokens included. It
stays on the row for continuity. A row without the four fields above was
written before the schema existed; read it as "legacy, not comparable".
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

_PRICES_PATH = Path(__file__).with_name("model_prices.yaml")
_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")
_prices_cache: dict | None = None
_warned_models: set[str] = set()


def _load_prices() -> dict:
    global _prices_cache
    if _prices_cache is None:
        import yaml
        _prices_cache = (
            yaml.safe_load(_PRICES_PATH.read_text(encoding="utf-8")) or {}
        ).get("models") or {}
    return _prices_cache


def compute_cost_usd(model: Optional[str], usage: dict) -> Optional[float]:
    """Cost of one call from its exact token counts and the model's list price
    (``model_prices.yaml``). None for a model with no entry, with a one-time
    warning -- an unpriced call is unknown, never free."""
    entry = _load_prices().get(_SNAPSHOT_SUFFIX.sub("", model or ""))
    if entry is None:
        if model not in _warned_models:
            _warned_models.add(model)
            _log.warning(
                "telemetry: no price for model %r in %s; cost_usd_computed "
                "is None for its calls", model, _PRICES_PATH.name)
        return None
    if has_normalized_usage(usage):
        # input_tokens may include cache tokens (openai-compatible); the
        # disjoint schema fields never do.
        fresh, read, out = (
            usage["fresh_input"], usage["cache_read"], usage["output"])
        cc_total = usage["cache_write"]
    else:
        fresh = usage.get("input_tokens") or 0
        read = usage.get("cache_read_input_tokens") or 0
        out = usage.get("output_tokens") or 0
        cc_total = usage.get("cache_creation_input_tokens") or 0
    cc_1h = usage.get("cache_creation_1h_tokens")
    cc_5m = usage.get("cache_creation_5m_tokens")
    if cc_1h is None and cc_5m is None:
        cc_1h, cc_5m = cc_total, 0
    else:
        cc_1h, cc_5m = cc_1h or 0, cc_5m or 0
        cc_1h += max(cc_total - cc_1h - cc_5m, 0)
    return (
        fresh * entry["input"]
        + out * entry["output"]
        + read * entry["cache_read"]
        + cc_1h * entry["cache_write_1h"]
        + cc_5m * entry["cache_write_5m"]
    ) / 1e6


# The backend-independent token schema (see the module docstring).
NORMALIZED_FIELDS = ("fresh_input", "cache_read", "cache_write", "output")


def normalized_usage(
    *, fresh_input: int, cache_read: int, cache_write: int, output: int,
) -> dict:
    """The four schema fields, as non-negative ints. Each backend calls this
    with its own mapping; ``fresh_input + cache_read + cache_write + output``
    is then the call's total."""
    return {
        "fresh_input": max(int(fresh_input or 0), 0),
        "cache_read": max(int(cache_read or 0), 0),
        "cache_write": max(int(cache_write or 0), 0),
        "output": max(int(output or 0), 0),
    }


def has_normalized_usage(row: dict) -> bool:
    """True for a row written under the schema; False for a legacy row."""
    return all(isinstance(row.get(f), (int, float)) for f in NORMALIZED_FIELDS)


# Token fields copied verbatim from adapter.last_usage.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    # The SDK's cache-write TTL split (0 when a backend reports none).
    "cache_creation_1h_tokens",
    "cache_creation_5m_tokens",
)

# How one turn's model calls were shaped; not every backend reports them.
_CALL_SHAPE_FIELDS = ("n_calls", "first_call_input", "max_call_input")


def call_shape(call_inputs: list[int]) -> dict:
    """The per-call shape of a turn from each model call's WHOLE prompt size
    (cached tokens included, whatever the backend's own ``input_tokens``
    means). Both backends call this, so the three fields mean the same."""
    return {
        "n_calls": len(call_inputs),
        "first_call_input": call_inputs[0] if call_inputs else 0,
        "max_call_input": max(call_inputs, default=0),
    }


class Telemetry:
    """Per-run telemetry writer.  One instance per orchestrating node."""

    def __init__(self, debug_dir: Any) -> None:
        self._dir = Path(debug_dir) / "telemetry"
        self._lock = threading.Lock()
        self._path = self._dir / f"calls.{os.getpid()}.jsonl"

    # -- recording -----------------------------------------------------------

    def record_call(
        self,
        *,
        role: Optional[str],
        model: Optional[str],
        phase: Optional[str],
        delegation_id: Optional[str],
        usage: Optional[dict],
    ) -> None:
        """Append one row for a single LLM call.

        Never raises into the caller: a telemetry failure must not break the
        run.  ``usage`` is ``adapter.last_usage`` (may be empty / partial /
        have ``total_cost_usd=None`` under ollama — all tolerated).
        """
        try:
            usage = usage or {}
            row = {
                "role": role,
                "model": model,
                "phase": phase,
                "delegation_id": delegation_id,
                "ts": time.time(),
            }
            for f in _TOKEN_FIELDS:
                row[f] = usage.get(f, 0) or 0
            # The schema fields are copied only when the backend reported
            # them: inventing zeros would make a legacy row look measured.
            if has_normalized_usage(usage):
                for f in NORMALIZED_FIELDS:
                    row[f] = int(usage[f])
            # Per-call shape of a turn, only where the backend reports it
            # (openai-compatible): input_tokens is a sum over the turn's calls.
            for f in _CALL_SHAPE_FIELDS:
                if f in usage:
                    row[f] = int(usage[f] or 0)
            # cost is the one field that stays None under ollama (never faked)
            row["total_cost_usd"] = usage.get("total_cost_usd")
            # Computed from exact tokens x config price; separate from the
            # SDK's figure above, which it never overwrites.
            try:
                row["cost_usd_computed"] = compute_cost_usd(model, row)
            except Exception:  # noqa: BLE001
                row["cost_usd_computed"] = None
            self._append_row(row)
        except Exception:  # noqa: BLE001 — telemetry is best-effort
            pass

    def _append_row(self, row: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)

    # -- aggregation ---------------------------------------------------------

    @staticmethod
    def merge(debug_dir: Any) -> dict:
        """Union all ``calls.*.jsonl`` files → ``summary.json``; return it.

        Robust to a missing telemetry dir (returns an empty summary) and to
        malformed lines (skipped).  Breakdowns by role / phase / model each
        partition the totals.
        """
        tdir = Path(debug_dir) / "telemetry"
        rows: list[dict] = []
        if tdir.is_dir():
            for f in sorted(tdir.glob("calls.*.jsonl")):
                try:
                    for line in f.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except (ValueError, json.JSONDecodeError):
                            continue
                except OSError:
                    continue

        def _bucket() -> dict:
            return {
                "calls": 0,
                # How many of those calls reported a cost at all. Open-weight
                # and self-hosted backends report none, so a bucket can have
                # calls > 0 and cost_calls == 0 — see _finish.
                "cost_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_creation_1h_tokens": 0,
                "cache_creation_5m_tokens": 0,
                # The backend-independent schema, summed over the calls that
                # carry it; `legacy_calls` counts the rest (not comparable).
                "normalized_calls": 0,
                "legacy_calls": 0,
                "fresh_input": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
                "total_cost_usd": 0.0,
                "computed_cost_calls": 0,
                "cost_usd_computed": 0.0,
            }

        def _add(b: dict, r: dict) -> None:
            b["calls"] += 1
            for f in _TOKEN_FIELDS:
                b[f] += int(r.get(f, 0) or 0)
            if has_normalized_usage(r):
                b["normalized_calls"] += 1
                for f in NORMALIZED_FIELDS:
                    b[f] += int(r[f])
            else:
                b["legacy_calls"] += 1
            cost = r.get("total_cost_usd")
            if cost is not None:
                b["cost_calls"] += 1
                b["total_cost_usd"] += cost
            comp = r.get("cost_usd_computed")
            if comp is not None:
                b["computed_cost_calls"] += 1
                b["cost_usd_computed"] += comp

        def _finish(b: dict) -> dict:
            """Unpriced calls leave the cost UNKNOWN, not zero.

            ``record_call`` is careful never to fake a cost (ollama and every
            self-hosted backend return None), but summing Nones as 0.0 undid
            that here: a whole run on an open-weight model reported
            ``total_cost_usd: 0.0``, indistinguishable from a run that
            genuinely cost nothing. An ablation comparing arms on cost has to
            be able to tell "free" from "not measured".
            """
            if b["cost_calls"] == 0:
                b["total_cost_usd"] = None
            if b["computed_cost_calls"] == 0:
                b["cost_usd_computed"] = None
            return b

        totals = _bucket()
        by_role: dict = {}
        by_phase: dict = {}
        by_model: dict = {}
        tss = []
        for r in rows:
            _add(totals, r)
            _add(by_role.setdefault(r.get("role"), _bucket()), r)
            _add(by_phase.setdefault(r.get("phase"), _bucket()), r)
            _add(by_model.setdefault(r.get("model"), _bucket()), r)
            ts = r.get("ts")
            if isinstance(ts, (int, float)):
                tss.append(ts)

        # Legacy and NOT comparable across backends (see the module
        # docstring); kept so older readers keep working.
        totals["total_tokens"] = (
            totals["input_tokens"] + totals["output_tokens"]
        )
        # Comparable: fresh + cache_read + cache_write + output, over the
        # normalized calls only (`legacy_calls` says how many were left out).
        totals["tokens_total"] = sum(totals[f] for f in NORMALIZED_FIELDS)
        totals["wall_time_s"] = (max(tss) - min(tss)) if len(tss) >= 2 else 0.0

        summary = {
            "totals": _finish(totals),
            "by_role": {k: _finish(v) for k, v in by_role.items()},
            "by_phase": {k: _finish(v) for k, v in by_phase.items()},
            "by_model": {k: _finish(v) for k, v in by_model.items()},
        }
        try:
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "summary.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        return summary
