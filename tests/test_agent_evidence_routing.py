import pytest

from forecast_ai.agents.market import MarketAgent
from forecast_ai.config import ForecastConfig
from forecast_ai.models.evidence import Evidence
from forecast_ai.pipelines.forecast import _route_evidence_for_agent
from forecast_ai.providers.base import BaseProvider


class DummyProvider(BaseProvider):
    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
        return '{"probability": 0.55, "confidence": 0.7, "reasoning": "Market evidence reviewed.", "warnings": []}'


def _evidence(source_name: str, source_type: str = "", provider: str = "") -> Evidence:
    return Evidence(
        source_name=source_name,
        content=source_name,
        metadata={"source_type": source_type, "provider": provider},
    )


def test_falcon_layers_are_routed_to_their_specialist_agents_only():
    market = _evidence("polymarket")
    falcon_market = _evidence("falcon_market_intelligence", "market_intelligence", "Falcon")
    falcon_social = _evidence("falcon_social_pulse", "social_intelligence", "Falcon")
    falcon_smart_money = _evidence("falcon_smart_money", "smart_money", "Falcon")
    evidence = [market, falcon_market, falcon_social, falcon_smart_money]

    assert _route_evidence_for_agent("social", evidence) == [falcon_social]
    assert _route_evidence_for_agent("market", evidence) == [
        market,
        falcon_market,
        falcon_smart_money,
    ]
    assert _route_evidence_for_agent("onchain", evidence) == [market]


def test_empty_specialist_route_falls_back_to_market_context_not_partner_payloads():
    market = _evidence("kalshi")
    falcon_social = _evidence("falcon_social_pulse", "social_intelligence", "Falcon")

    assert _route_evidence_for_agent("onchain", [market, falcon_social]) == [market]


@pytest.mark.asyncio
async def test_falcon_active_status_wins_when_optional_layer_is_unavailable():
    agent = MarketAgent(name="market", provider=DummyProvider(), config=ForecastConfig())
    evidence = [
        Evidence(
            source_name="falcon_market_intelligence",
            content="Falcon market intelligence is available.",
            metadata={"provider": "Falcon", "source_type": "market_intelligence", "status": "active"},
        ),
        Evidence(
            source_name="falcon_smart_money_status",
            content="Falcon Smart Money was unavailable for this analysis.",
            metadata={"provider": "Falcon", "source_type": "smart_money_status", "status": "unavailable"},
        ),
    ]

    prediction = await agent.forecast("Will the market resolve yes?", evidence)

    assert prediction.provider_statuses["Falcon"] == "active"
    assert "Falcon Market Intelligence" in prediction.provider_insights
