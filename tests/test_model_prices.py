"""cost_usd_computed: exact tokens x the explicit price table (model_prices.yaml).

Recorded alongside the SDK's own cost, never merged into it. The table is
validated against the SDK's cost on calls it DID price."""
from __future__ import annotations

import json
import logging

import pytest

from adda._src.infra import telemetry
from adda._src.infra.telemetry import Telemetry, compute_cost_usd

# (input, output, cache_read, cache_creation, SDK total_cost_usd), claude-haiku-4-5,
# taken from the priced worker/critic calls of run 20260928T141126.
_HAIKU_SDK_PRICED = [
    (50, 2690, 97036, 20785, 0.0657466),
    (10, 1357, 0, 17560, 0.045331),
    (82, 5491, 244762, 23122, 0.0993522),
    (10, 6777, 14142, 3671, 0.0463302),
    (298, 24169, 1360505, 37119, 0.3324955),
    (42, 20959, 141840, 40757, 0.205234),
    (82, 6610, 258023, 18789, 0.1023223),
    (90, 10204, 295263, 20736, 0.1279043),
]


def _u(i, o, cr, cc):
    return {"input_tokens": i, "output_tokens": o,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}


def test_table_reproduces_the_sdks_own_cost_on_priced_calls():
    sdk_total = computed_total = 0.0
    for i, o, cr, cc, sdk in _HAIKU_SDK_PRICED:
        c = compute_cost_usd("claude-haiku-4-5-20251001", _u(i, o, cr, cc))
        assert c == pytest.approx(sdk, rel=0.10)
        sdk_total += sdk
        computed_total += c
    assert computed_total == pytest.approx(sdk_total, rel=0.05)


def test_snapshot_suffix_resolves_but_a_different_model_does_not():
    u = _u(1_000_000, 0, 0, 0)
    assert compute_cost_usd("claude-haiku-4-5-20251001", u) == pytest.approx(1.0)
    assert compute_cost_usd("claude-sonnet-5-5", u) == pytest.approx(2.0)
    assert compute_cost_usd("claude-sonnet-5", u) == pytest.approx(2.0)
    # claude-sonnet-5-1 must not silently inherit claude-sonnet-5's price
    assert compute_cost_usd("claude-sonnet-5-1", u) is None


def test_opus_cache_read_is_the_pages_0_05x_not_0_1x():
    c = compute_cost_usd("claude-opus-5-5", _u(0, 0, 1_000_000, 0))
    assert c == pytest.approx(0.20)


def test_unknown_model_is_none_with_a_warning_never_zero(caplog):
    telemetry._warned_models.discard("mystery-model")
    with caplog.at_level(logging.WARNING):
        assert compute_cost_usd("mystery-model", _u(10, 10, 10, 10)) is None
    assert "mystery-model" in caplog.text


def test_rows_and_summary_carry_computed_next_to_the_sdk_cost(tmp_path):
    t = Telemetry(tmp_path)
    t.record_call(role="critic", model="claude-haiku-4-5-20251001",
                  phase="p", delegation_id="d",
                  usage={**_u(10, 1357, 0, 17560), "total_cost_usd": 0.045331})
    t.record_call(role="strategizer", model="claude-haiku-4-5-20251001",
                  phase="strategizer_turn", delegation_id=None,
                  usage={**_u(100, 5000, 200_000, 30_000),
                         "total_cost_usd": None})
    rows = [json.loads(line) for f in (tmp_path / "telemetry").glob("calls.*")
            for line in f.read_text().splitlines()]
    assert rows[0]["total_cost_usd"] == 0.045331          # SDK figure untouched
    assert rows[0]["cost_usd_computed"] == pytest.approx(0.045331, rel=0.10)
    assert rows[1]["total_cost_usd"] is None              # still unknown
    assert rows[1]["cost_usd_computed"] > 0               # but now computable
    tot = Telemetry.merge(tmp_path)["totals"]
    assert tot["total_cost_usd"] == 0.045331              # SDK-only, unmerged
    assert tot["cost_usd_computed"] == pytest.approx(
        rows[0]["cost_usd_computed"] + rows[1]["cost_usd_computed"])
    assert tot["computed_cost_calls"] == 2
