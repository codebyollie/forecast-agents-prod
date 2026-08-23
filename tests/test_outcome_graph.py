import pytest

from forecast_ai.services.outcome_graph import OutcomeGraphService, similarity
from forecast_ai.services.robinhood_stock_tokens import RobinhoodStockTokenClient


class FakeSearch:
    async def search_markets(self, question, limit=20):
        return [
            {
                "market_id": "selected",
                "question": question,
                "venue": "Polymarket",
                "current_price": 0.42,
            },
            {
                "market_id": "KXFED-CUT",
                "question": "Will the Fed cut interest rates before December?",
                "venue": "Kalshi",
                "current_price": 0.49,
                "volume": 120_000,
            },
            {
                "market_id": "unrelated",
                "question": "Will a football team win?",
                "venue": "Kalshi",
                "current_price": 0.7,
            },
        ]


class FakeStockTokens:
    async def related_assets(self, question):
        return [{"symbol": "TLT", "relationship": "theme_match"}]


def test_similarity_is_deterministic():
    assert similarity("Will the Fed cut rates?", "Fed interest rate cut") > 0.3
    assert similarity("Fed rate cut", "Football winner") == 0


@pytest.mark.asyncio
async def test_outcome_graph_returns_cross_venue_gap_and_rwa_assets():
    graph = await OutcomeGraphService(FakeSearch(), FakeStockTokens()).build(
        question="Will the Fed cut interest rates before December?",
        selected_market_id="selected",
        selected_venue="Polymarket",
        selected_probability=0.42,
    )

    assert graph["counterpart_markets"][0]["market_id"] == "KXFED-CUT"
    assert graph["counterpart_markets"][0]["price_gap"] == 0.07
    assert graph["rwa_assets"][0]["symbol"] == "TLT"


def test_robinhood_theme_mapping_is_bounded_and_deduplicated():
    symbols = RobinhoodStockTokenClient.related_symbols("Fed rate and inflation outlook")
    assert symbols[0] == "SPY"
    assert len(symbols) <= 5
    assert len(symbols) == len(set(symbols))


def test_rwa_theme_matching_uses_whole_words():
    assert RobinhoodStockTokenClient.related_symbols(
        "NATO/EU troops fighting in Ukraine by December 31, 2026?"
    ) == []
    assert RobinhoodStockTokenClient.related_symbols("Will AI investment grow?") == [
        "NVDA", "MSFT", "GOOGL", "META"
    ]


def test_stock_token_public_asset_hides_unneeded_upstream_shape():
    asset = {
        "tokenSymbol": "NVDA",
        "tokenName": "NVIDIA • Robinhood Token",
        "status": "ASSET_STATUS_ACTIVE",
        "logoUrl": "https://example.com/nvda.png",
        "currentMultiplier": "1.0",
        "deployments": [{"contractAddress": "0xabc", "chainId": 4663, "networkName": "Robinhood Chain"}],
    }
    public = RobinhoodStockTokenClient.public_asset(asset, {"bid": "100", "ask": "101"})
    assert public["name"] == "NVIDIA"
    assert public["contract_address"] == "0xabc"
    assert public["quote"]["ask"] == "101"


@pytest.mark.asyncio
async def test_stock_token_matching_uses_explicit_ticker_for_any_catalog_asset(monkeypatch):
    client = RobinhoodStockTokenClient()

    async def fake_assets():
        return [
            {"tokenSymbol": "PFE", "tokenName": "Pfizer • Robinhood Token"},
            {"tokenSymbol": "NVDA", "tokenName": "NVIDIA • Robinhood Token"},
        ]

    monkeypatch.setattr(client, "assets", fake_assets)
    assert await client.matching_symbols("What could affect Pfizer (PFE) over the next quarter?") == ["PFE"]
