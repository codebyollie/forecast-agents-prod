import pytest

from forecast_ai.agents.market import MarketAgent
from forecast_ai.agents.research import ResearchAgent
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


def test_bravado_trader_intelligence_is_routed_only_to_market_agent():
    market = _evidence("polymarket")
    bravado = _evidence("bravado_trader_intelligence", "trader_intelligence", "Bravado")

    assert _route_evidence_for_agent("market", [market, bravado]) == [market, bravado]
    assert _route_evidence_for_agent("social", [market, bravado]) == [market]
    assert _route_evidence_for_agent("onchain", [market, bravado]) == [market]


def test_mihari_rwa_intelligence_is_routed_only_to_onchain_agent():
    market = _evidence("polymarket")
    mihari = _evidence("Mihari RWA Intelligence", "rwa_intelligence", "Mihari")

    assert _route_evidence_for_agent("onchain", [market, mihari]) == [mihari]
    assert _route_evidence_for_agent("market", [market, mihari]) == [market]
    assert _route_evidence_for_agent("macro", [market, mihari]) == [market]


def test_perigon_and_exa_are_routed_to_their_specialist_roles():
    market = _evidence("polymarket")
    perigon = _evidence("Perigon News", "structured_news", "Perigon")
    exa = _evidence("Exa Deep Research", "deep_research", "Exa")

    assert _route_evidence_for_agent("news", [market, perigon, exa]) == [perigon]
    assert _route_evidence_for_agent("research", [market, perigon, exa]) == [exa]
    assert _route_evidence_for_agent("market", [market, perigon, exa]) == [market]


def test_sec_edgar_is_routed_only_to_the_onchain_agent():
    market = _evidence("polymarket")
    sec = _evidence("SEC EDGAR Corporate Events", "rwa_corporate_event", "SEC EDGAR")

    assert _route_evidence_for_agent("onchain", [market, sec]) == [sec]
    assert _route_evidence_for_agent("market", [market, sec]) == [market]


def test_runtime_modules_keep_platform_layers_separate():
    from forecast_ai.pipelines.forecast import filter_runtime_evidence

    evidence = [
        _evidence("Perigon News", "structured_news", "Perigon"),
        _evidence("Exa Deep Research", "deep_research", "Exa"),
        _evidence("FRED Macro Data", "macro_data", "FRED"),
        _evidence("SEC EDGAR Corporate Events", "rwa_corporate_event", "SEC EDGAR"),
    ]
    assert filter_runtime_evidence(evidence, {"perigon-news", "rwa-intelligence"}) == [evidence[0], evidence[3]]


def test_rwa_mode_source_allowlist_excludes_mihari():
    from forecast_ai.pipelines.forecast import RWA_ALLOWED_SOURCES

    assert "mihari" not in RWA_ALLOWED_SOURCES
    assert {"sec_edgar", "fred", "perigon", "exa", "tavily", "twitter"}.issubset(RWA_ALLOWED_SOURCES)


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

    assert prediction.provider_statuses["Falcon"] == "used"
    assert "Falcon Market Intelligence" in prediction.provider_insights


@pytest.mark.asyncio
async def test_perigon_and_exa_are_exposed_as_active_research_providers():
    agent = ResearchAgent(name="research", provider=DummyProvider(), config=ForecastConfig())
    evidence = [
        Evidence(
            source_name="Perigon News",
            content="A structured news item.",
            title="Perigon item",
            url="https://news.example/item",
            metadata={"provider": "Perigon", "source_type": "structured_news"},
        ),
        Evidence(
            source_name="Exa Deep Research",
            content="A deep research finding.",
            title="Exa finding",
            url="https://research.example/finding",
            metadata={"provider": "Exa", "source_type": "deep_research"},
        ),
    ]

    prediction = await agent.forecast("Will the event happen?", evidence)

    assert prediction.provider_statuses["Perigon"] == "used"
    assert prediction.provider_statuses["Exa"] == "used"
    assert "Perigon Structured News" in prediction.provider_insights
    assert "Exa Deep Research" in prediction.provider_insights
