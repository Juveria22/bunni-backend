"""
spend accounting

the cap is only as good as the arithmetic under it, so these pin the pricing
rather than the plumbing
"""

import pytest

from services import budget
from services.budget import (
    MICROS_PER_USD,
    MONTHLY_BUDGET_MICROS,
    SMS_SEGMENT_USD,
    price_model_call,
    price_sms,
)


def test_budget_ceiling_is_fifteen_dollars_by_default():
    assert MONTHLY_BUDGET_MICROS == 15 * MICROS_PER_USD


def test_sonnet_input_and_output_priced_separately():
    # 1M input at $3 plus 1M output at $15
    micros = price_model_call("claude-sonnet-4-5", 1_000_000, 1_000_000)
    assert micros == pytest.approx(18 * MICROS_PER_USD, rel=1e-6)


def test_haiku_is_cheaper_than_sonnet_for_identical_usage():
    sonnet = price_model_call("claude-sonnet-4-5", 10_000, 1_000)
    haiku = price_model_call("claude-haiku-4-5", 10_000, 1_000)
    assert haiku < sonnet


def test_dated_model_ids_still_match_their_family():
    """the classifier is pinned to a dated haiku id"""
    dated = price_model_call("claude-haiku-4-5-20251001", 10_000, 1_000)
    alias = price_model_call("claude-haiku-4-5", 10_000, 1_000)
    assert dated == alias


def test_cache_reads_are_a_tenth_of_full_input():
    full = price_model_call("claude-sonnet-4-5", 1_000_000, 0)
    cached = price_model_call("claude-sonnet-4-5", 0, 0, cache_read_tokens=1_000_000)
    assert cached == pytest.approx(full * 0.1, rel=1e-6)


def test_cache_writes_cost_more_than_plain_input():
    full = price_model_call("claude-sonnet-4-5", 1_000_000, 0)
    written = price_model_call("claude-sonnet-4-5", 0, 0, cache_write_tokens=1_000_000)
    assert written == pytest.approx(full * 1.25, rel=1e-6)


def test_unknown_model_bills_at_the_dearer_tier():
    """
    a model swap should be able to stop us early, never overspend quietly
    """
    unknown = price_model_call("claude-something-new", 100_000, 10_000)
    haiku = price_model_call("claude-haiku-4-5", 100_000, 10_000)
    assert unknown >= haiku


def test_zero_usage_costs_nothing():
    assert price_model_call("claude-sonnet-4-5", 0, 0) == 0
    assert price_sms(0) == 0


def test_sms_priced_per_segment():
    assert price_sms(1) == round(SMS_SEGMENT_USD * MICROS_PER_USD)
    assert price_sms(3) == 3 * price_sms(1)


def test_a_two_segment_reply_costs_twice_a_one_segment_reply():
    assert price_sms(2) == 2 * price_sms(1)


async def test_over_budget_flips_exactly_at_the_ceiling(monkeypatch):
    async def spend(value):
        monkeypatch.setattr(budget, "_cached_spend", value)
        monkeypatch.setattr(budget, "_cached_at", float("inf"))
        return await budget.over_budget()

    assert await spend(MONTHLY_BUDGET_MICROS - 1) is False
    assert await spend(MONTHLY_BUDGET_MICROS) is True
    assert await spend(MONTHLY_BUDGET_MICROS * 2) is True


async def test_fresh_month_is_not_over_budget(monkeypatch):
    monkeypatch.setattr(budget, "_cached_spend", 0)
    monkeypatch.setattr(budget, "_cached_at", float("inf"))
    assert await budget.over_budget() is False


def test_period_key_rolls_with_the_calendar_month():
    key = budget._key()
    assert key.startswith("spend:")
    # spend:YYYY-MM
    assert len(key) == len("spend:2026-08")
