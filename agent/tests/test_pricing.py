import datetime

from k1_agent.pricing import Pricing
from k1_agent.providers.base import Usage

PRICES = Pricing({
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
    "gpt-5.5": {"input": 5.0, "output": 30.0, "cache_read": 0.5, "cache_write": 0.0,
                "long_context": {"above": 272000, "input": 10.0, "output": 45.0}},
    "gemini-4-argon": {"input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 0.0,
                       "changes": [{"from": datetime.date(2026, 12, 1), "input": 4.0, "output": 20.0}]},
})


def test_cost_adds_every_kind_of_token():
    usage = Usage(input=10_000, output=1_000, cache_read=100_000, cache_write=2_000)
    # 10k*4 + 1k*20 + 100k*0.2 + 2k*5 = 40000 + 20000 + 20000 + 10000 = 90000 $/M -> $0.09
    assert abs(PRICES.cost("claude-opus-5-5", usage) - 0.09) < 1e-12


def test_dated_model_ids_match_by_prefix_and_unknown_models_are_none():
    assert PRICES.row("gpt-5.5-2026-04-23") is PRICES.models["gpt-5.5"]
    assert PRICES.cost("some-other-model", Usage(input=1)) is None
    assert PRICES.cost("claude-opus-5-5", None) is None


def test_long_context_and_dated_price_changes():
    big = Usage(input=300_000)
    assert abs(PRICES.cost("gpt-5.5", big) - 3.0) < 1e-12               # 300k * $10/M
    small = Usage(input=1_000_000)
    assert PRICES.cost("gemini-4-argon", small, on=datetime.date(2026, 11, 30)) == 2.0
    assert PRICES.cost("gemini-4-argon", small, on=datetime.date(2026, 12, 1)) == 4.0


def test_shipped_pricing_file_loads():
    prices = Pricing.load()
    assert prices.cost("claude-sonnet-5-5", Usage(input=1_000_000, output=1_000_000)) == 12.0
