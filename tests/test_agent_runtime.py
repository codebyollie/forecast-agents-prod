import pytest

from forecast_ai.models.evidence import Evidence
from forecast_ai.pipelines.forecast import (
    filter_runtime_evidence,
    normalize_agent_runtime,
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
