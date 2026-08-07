from forecast_ai.models.evidence import Evidence
from forecast_ai.pipelines.forecast import _route_evidence_for_agent


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
