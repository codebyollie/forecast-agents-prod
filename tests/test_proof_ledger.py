from pathlib import Path

from forecast_ai.config import ForecastConfig
from forecast_ai.memory.store import MemoryStore
from forecast_ai.models.confidence import ConfidenceScore
from forecast_ai.models.forecast import ForecastResult, ReasoningTrace
from forecast_ai.models.prediction import Prediction
from forecast_ai.proof.ledger import build_forecast_envelope, build_resolution_hash, calculate_brier_score


def _forecast():
    prediction = Prediction(
        agent_name="research",
        probability=0.7,
        confidence=ConfidenceScore(score=0.8),
        reasoning="Evidence",
    )
    return ForecastResult(
        market_id="market-1",
        probability=0.6,
        confidence=ConfidenceScore(score=0.75),
        reasoning_trace=ReasoningTrace(),
        individual_predictions=[prediction],
    )


def test_brier_score_uses_probability_not_binary_accuracy():
    assert calculate_brier_score(0.8, 1) == 0.04
    assert calculate_brier_score(0.8, 0) == 0.64


def test_resolution_hash_is_deterministic_and_outcome_specific():
    first = build_resolution_hash("market-1", 1, "official", "2030-01-02T00:00:00Z")
    second = build_resolution_hash("market-1", 1, "official", "2030-01-02T00:00:00Z")
    no_outcome = build_resolution_hash("market-1", 0, "official", "2030-01-02T00:00:00Z")
    assert first == second
    assert first.startswith("0x") and len(first) == 66
    assert first != no_outcome


def test_envelope_hash_is_deterministic():
    result = _forecast()
    first = build_forecast_envelope(result, "Question?", "Polymarket", "Politics", "2030-01-01")
    second = build_forecast_envelope(result, "Question?", "Polymarket", "Politics", "2030-01-01")
    assert first["payload_hash"] == second["payload_hash"]
    assert first["status"] == "pending_onchain"
    assert len(first["onchain_commitments"]) == 2
    assert first["onchain_commitments"][0]["agent_name"] == "consensus"
    assert first["onchain_commitments"][0]["probability_bps"] == 6000


def test_resolution_creates_real_track_record(tmp_path: Path):
    config = ForecastConfig()
    config.memory.store_dir = str(tmp_path)
    store = MemoryStore(config)
    result = _forecast()
    proof = build_forecast_envelope(result, "Question?", "Polymarket", "Politics", "2030-01-01")
    result.metadata.update({
        "proof": proof,
        "question": "Question?",
        "venue": "Polymarket",
        "category": "Politics",
        "market_closes_at": "2030-01-01",
    })
    store.save_forecast(result)

    resolved = store.resolve_market_forecasts("market-1", 1, "official market", "2030-01-02T00:00:00Z")
    track_record = store.get_track_record()

    assert len(resolved) == 1
    assert resolved[0]["resolution"]["consensus_brier_score"] == 0.16
    assert resolved[0]["resolution"]["agent_scores"][0]["brier_score"] == 0.09
    assert track_record["resolved_forecasts"] == 1
    assert track_record["average_brier_score"] == 0.16
    assert track_record["agents"]["research"]["average_brier_score"] == 0.09
