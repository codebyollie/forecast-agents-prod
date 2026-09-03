from forecast_ai.intelligence.evidence_correlation import analyze_evidence_dependencies
from forecast_ai.intelligence.provider_status import normalize_provider_statuses
from forecast_ai.models.confidence import ConfidenceScore
from forecast_ai.models.evidence import Evidence
from forecast_ai.models.prediction import Prediction


def prediction(name: str, url: str, content: str = "Material evidence about the forecast and its likely outcome.") -> Prediction:
    return Prediction(
        agent_name=name,
        probability=0.7,
        confidence=ConfidenceScore(score=0.8),
        reasoning="Evidence-backed view.",
        evidence_used=[Evidence(source_name="test", content=content, url=url, relevance_score=0.9)],
        citations=[{"title": "Source", "url": url}],
    )


def test_shared_url_is_clustered_and_downweighted():
    report = analyze_evidence_dependencies([
        prediction("news", "https://example.com/story?utm_source=x"),
        prediction("research", "https://www.example.com/story"),
        prediction("market", "https://market.example/prices"),
    ])

    assert report["unique_source_count"] == 2
    assert report["shared_source_count"] == 1
    assert report["agent_weight_multipliers"]["news"] < 1.0
    assert report["agent_weight_multipliers"]["research"] < 1.0
    assert report["agent_weight_multipliers"]["market"] == 1.0
    assert report["confidence_penalty"] > 0


def test_distinct_sources_keep_full_independence():
    report = analyze_evidence_dependencies([
        prediction("news", "https://news.example/a"),
        prediction("social", "https://social.example/post/1"),
    ])
    assert report["shared_source_count"] == 0
    assert report["agent_weight_multipliers"] == {"news": 1.0, "social": 1.0}
    assert report["confidence_penalty"] == 0.0


def test_provider_states_are_stable_for_the_ui():
    assert normalize_provider_statuses({
        "Tavily": "active",
        "FactsAI": "empty",
        "Bravado": "unavailable",
        "Falcon": "requested",
    }) == {
        "Tavily": "used",
        "FactsAI": "irrelevant",
        "Bravado": "unavailable",
        "Falcon": "failed",
    }
