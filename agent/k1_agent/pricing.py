"""
Token counts to US dollars, from pricing.yaml (rates per million tokens).

    prices = Pricing.load()
    prices.cost("claude-opus-5-5", Usage(input=12000, output=800))   # -> 0.064
"""
import datetime
import os

import yaml

from k1_agent.providers.base import Usage

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PRICING = os.path.join(AGENT_DIR, "pricing.yaml")
RATE_KEYS = ("input", "output", "cache_read", "cache_write")


class Pricing:

    def __init__(self, models: dict, checked=None):
        self.models = models
        self.checked = checked

    @classmethod
    def load(cls, path: str | None = None) -> "Pricing":
        with open(path or DEFAULT_PRICING, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data.get("models") or {}, data.get("checked"))

    def row(self, model: str | None) -> dict | None:
        """The price row for a model id; a dated id (gpt-5.5-2026-04-23) matches the longest prefix."""
        if not model:
            return None
        if model in self.models:
            return self.models[model]
        matches = [key for key in self.models if model.startswith(key + "-")]
        return self.models[max(matches, key=len)] if matches else None

    def rates(self, model: str | None, prompt_tokens: int = 0, on: datetime.date | None = None) -> dict | None:
        row = self.row(model)
        if row is None:
            return None
        rates = {k: float(row.get(k, 0.0)) for k in RATE_KEYS}
        on = on or datetime.date.today()
        for change in sorted(row.get("changes") or [], key=lambda c: str(c["from"])):
            start = change["from"]
            start = start if isinstance(start, datetime.date) else datetime.date.fromisoformat(str(start))
            if on >= start:
                rates.update({k: float(v) for k, v in change.items() if k in RATE_KEYS})
        long = row.get("long_context")
        if long and prompt_tokens > long["above"]:
            rates.update({k: float(v) for k, v in long.items() if k in RATE_KEYS})
        return rates

    def cost(self, model: str | None, usage: Usage | None, on: datetime.date | None = None) -> float | None:
        """Dollars for one call, or None when the model has no price row."""
        if usage is None:
            return None
        rates = self.rates(model, usage.prompt_tokens, on)
        if rates is None:
            return None
        return (usage.input * rates["input"] + usage.output * rates["output"] +
                usage.cache_read * rates["cache_read"] + usage.cache_write * rates["cache_write"]) / 1e6
