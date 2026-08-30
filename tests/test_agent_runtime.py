from datetime import datetime, timezone

import pytest

from forecast_ai.models.evidence import Evidence
from forecast_ai.pipelines.forecast import (
    filter_runtime_evidence,
    normalize_agent_runtime,
    resolve_forecast_closes_at,
    runtime_modules_for_agent,
)


def test_custom_runtime_selects_all_seven_agents():
    runtime = normalize_agent_runtime({
        "id": "agent-123",
        "name": "My Swarm",
        "mode": "custom",
        "template_id": "forecast-swarm",
        "config": {
            "models": {"news": "openai"},
            "modules": {"news": ["newsrss", "tavily"]},
            "category": "Politics",
        },
    })

    assert runtime is not None
    assert runtime["selected_agents"] == ["news", "social", "reddit", "research", "macro", "onchain", "market"]
    assert runtime_modules_for_agent(runtime, "news") == {"newsrss", "tavily"}


def test_specialist_runtime_maps_to_real_backend_agent():
    runtime = normalize_agent_runtime({
        "id": "agent-456",
        "name": "Evidence Desk",
        "mode": "specialist",
        "template_id": "evidence",
        "config": {"modules": {"evidence": ["factsai", "tavily"]}},
    })

    assert runtime is not None
    assert runtime["selected_agents"] == ["research"]
    assert runtime_modules_for_agent(runtime, "research") == {"factsai", "tavily"}


def test_runtime_rejects_unknown_specialist():
    with pytest.raises(ValueError):
        normalize_agent_runtime({
            "id": "agent-789",
            "name": "Unknown",
            "mode": "specialist",
            "template_id": "anything",
            "config": {},
        })


def test_runtime_module_filter_keeps_only_selected_intelligence():
    evidence = [
        Evidence(source_name="FactsAI Deep Research", content="facts", metadata={"provider": "FactsAI"}),
        Evidence(source_name="Tavily Search", content="web", metadata={"provider": "Tavily"}),
        Evidence(source_name="polymarket", content="market"),
    ]

    filtered = filter_runtime_evidence(evidence, {"factsai"})
    assert [item.content for item in filtered] == ["facts"]


def test_rwa_horizon_becomes_proof_resolution_timestamp():
    started_at = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    assert resolve_forecast_closes_at(None, "rwa", 1, now=started_at) == "2026-08-28T12:00:00+00:00"
    assert resolve_forecast_closes_at(None, "rwa", 7, now=started_at) == "2026-09-03T12:00:00+00:00"
    assert resolve_forecast_closes_at(None, "rwa", 30, now=started_at) == "2026-09-26T12:00:00+00:00"


def test_rwa_resolution_rejects_unsupported_horizon():
    with pytest.raises(ValueError, match="24-hour, 7, 30, 90, or 180-day"):
        resolve_forecast_closes_at(None, "rwa", 14)
