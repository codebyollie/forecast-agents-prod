from forecast_ai.models.confidence import ConfidenceScore
from forecast_ai.models.evidence import Evidence
from forecast_ai.models.forecast import ForecastResult, ReasoningTrace
from forecast_ai.services.opportunity_radar import build_opportunity_radar


def _result(probability=0.58, confidence=0.76):
    return ForecastResult(
        market_id="fed-rate-cut",
        probability=probability,
        confidence=ConfidenceScore(score=confidence),
        reasoning_trace=ReasoningTrace(),
    )


def test_radar_calculates_real_edge_and_never_claims_onchain_proof():
    evidence = [Evidence(
        source_name="polymarket",
        content="selected market",
        title="Fed rate cut",
        url="https://polymarket.com/event/fed-rate-cut",
        metadata={
            "venue": "Polymarket",
            "outcomes": [{"label": "Yes", "price": 0.42}],
            "volume": 500_000,
            "liquidity": 150_000,
            "order_books": [{"spread": 0.02}],
        },
    )]

    radar = build_opportunity_radar(_result(), evidence, "Fed rate cut?", "Polymarket")

    assert radar["forecast"]["edge"] == 0.16
    assert radar["market"]["probability"] == 0.42
    assert radar["risk"]["level"] == "low"
    assert radar["proof"]["status"] == "pending"
    assert radar["score"] > 0


def test_radar_surfaces_falcon_social_and_partner_provenance():
    evidence = [
        Evidence(
            source_name="kalshi",
            content="selected market",
            metadata={"venue": "Kalshi", "current_price": 45, "spread": 0.1},
        ),
        Evidence(
            source_name="falcon_social_pulse",
            content="partner signal",
            metadata={
                "provider": "Falcon",
                "partner": True,
                "status": "active",
                "source_type": "social_intelligence",
                "signals": {"narrative_trend": "bullish", "sentiment_score": 0.73},
            },
        ),
        Evidence(
            source_name="facts_ai",
            content="research",
            metadata={"provider": "FactsAI", "partner": True, "status": "active"},
        ),
    ]

    radar = build_opportunity_radar(_result(), evidence, "Question", "Kalshi")

    assert radar["market"]["probability"] == 0.45
    assert radar["signals"]["social"]["trend"] == "bullish"
    assert radar["risk"]["flags"] == ["wide_spread"]
    assert {item["provider"] for item in radar["providers"]} == {"FactsAI", "Falcon"}
