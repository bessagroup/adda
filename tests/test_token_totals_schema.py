"""The run's in-memory token totals and the run report use the token schema.

`input_tokens` is cache-inclusive on openai-compatible backends and
cache-exclusive on Claude, so the totals also carry the disjoint schema
(fresh_input / cache_read / cache_write / output) and say how many calls had
none of it. A mixed run is "legacy, not comparable", never a blend.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from adda._src.nodes.recording import RecordingMixin
from adda._src.runtime.agent_runtime import (
    _comparable_tokens, _normalized_token_rows, _token_line)


def _node():
    return SimpleNamespace(
        _token_totals={"input_tokens": 0, "output_tokens": 0,
                       "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0,
                       "total_cost_usd": 0.0},
        _registry_lock=threading.Lock(), _cost_observed=False)


def _norm(fresh, read, write, out):
    return {"input_tokens": fresh, "output_tokens": out,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
            "fresh_input": fresh, "cache_read": read, "cache_write": write,
            "output": out, "total_cost_usd": None}


def test_totals_sum_the_schema_and_count_legacy_calls():
    n = _node()
    RecordingMixin._accumulate_usage(n, _norm(10, 200, 30, 5))
    RecordingMixin._accumulate_usage(n, _norm(1, 2, 3, 4))
    t = n._token_totals
    assert (t["fresh_input"], t["cache_read"], t["cache_write"],
            t["output"]) == (11, 202, 33, 9)
    assert t["normalized_calls"] == 2 and t.get("legacy_calls", 0) == 0
    RecordingMixin._accumulate_usage(
        n, {"input_tokens": 99, "output_tokens": 1})
    assert t["legacy_calls"] == 1 and t["fresh_input"] == 11


def test_a_checkpoint_without_the_schema_keys_still_accumulates():
    """token_totals persist in the checkpoint; an older one lacks the keys."""
    n = _node()
    RecordingMixin._accumulate_usage(n, _norm(1, 2, 3, 4))
    assert n._token_totals["normalized_calls"] == 1


def test_report_uses_the_schema_when_every_call_has_it():
    t = {"normalized_calls": 2, "legacy_calls": 0, "fresh_input": 11,
         "cache_read": 202, "cache_write": 33, "output": 9,
         "input_tokens": 5000, "output_tokens": 9}
    assert _comparable_tokens(t) is not None
    assert "fresh 11 / cache read 202 / cache write 33 / output 9" == _token_line(t)
    rows = _normalized_token_rows(t, 5009)
    assert "| total_tokens | 255 |" in rows


def test_report_calls_a_mixed_or_old_run_legacy_not_a_blend():
    mixed = {"normalized_calls": 2, "legacy_calls": 1, "fresh_input": 11}
    old = {"input_tokens": 100, "output_tokens": 5}
    for t in (mixed, old):
        assert _comparable_tokens(t) is None
        assert "legacy" in _token_line(t)
        rows = _normalized_token_rows(t, 105)
        assert "| total_tokens | 105 |" in rows
        assert "legacy, not comparable" in rows
