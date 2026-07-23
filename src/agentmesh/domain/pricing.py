from __future__ import annotations

import json
from dataclasses import dataclass

from agentmesh.domain.errors import InvalidTaskInput


@dataclass(frozen=True)
class PriceQuote:
    cost_details_micros: dict[str, int]
    currency: str
    pricing_version: str


class UsagePriceCatalog:
    """Versioned operator-supplied rates; no mutable vendor price is hard-coded."""

    def __init__(self, catalog_json: str = "{}") -> None:
        try:
            raw = json.loads(catalog_json or "{}")
            if not isinstance(raw, dict):
                raise TypeError
            self._catalog = raw
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidTaskInput("usage_price_catalog_json must be an object") from exc

    def quote(
        self, *, provider: str, model: str, usage: dict[str, int]
    ) -> PriceQuote | None:
        entry = self._catalog.get(f"{provider}:{model}")
        if entry is None:
            return None
        try:
            currency = str(entry["currency"]).upper()
            version = str(entry["version"])
            rates = entry["micros_per_million_tokens"]
            if len(currency) != 3 or not version or not isinstance(rates, dict):
                raise ValueError
            costs: dict[str, int] = {}
            for bucket in ("input_tokens", "output_tokens"):
                tokens = usage.get(bucket, 0)
                rate = rates.get(bucket, 0)
                if (
                    isinstance(tokens, bool)
                    or not isinstance(tokens, int)
                    or tokens < 0
                    or isinstance(rate, bool)
                    or not isinstance(rate, int)
                    or rate < 0
                ):
                    raise ValueError
                costs[bucket] = (tokens * rate + 999_999) // 1_000_000
            costs["total"] = sum(costs.values())
            return PriceQuote(costs, currency, version)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidTaskInput(
                f"Price catalog entry '{provider}:{model}' is invalid"
            ) from exc
