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
