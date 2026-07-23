import json

from agentmesh.domain.pricing import UsagePriceCatalog


def test_versioned_price_catalog_quotes_integer_micros() -> None:
    catalog = UsagePriceCatalog(
        json.dumps(
            {
                "openai:model-a": {
                    "currency": "USD",
                    "version": "operator-2026-07-23",
                    "micros_per_million_tokens": {
                        "input_tokens": 2_000_000,
                        "output_tokens": 8_000_000,
                    },
                }
            }
        )
    )

    quote = catalog.quote(
        provider="openai",
        model="model-a",
        usage={"input_tokens": 500_000, "output_tokens": 250_000},
    )

    assert quote is not None
    assert quote.cost_details_micros == {
        "input_tokens": 1_000_000,
        "output_tokens": 2_000_000,
        "total": 3_000_000,
    }
    assert quote.pricing_version == "operator-2026-07-23"
    assert catalog.quote(provider="openai", model="unknown", usage={}) is None
