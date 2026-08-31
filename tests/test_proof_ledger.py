from pathlib import Path

from forecast_ai.config import ForecastConfig
from forecast_ai.memory.store import MemoryStore
from forecast_ai.models.confidence import ConfidenceScore
from forecast_ai.models.forecast import ForecastResult, ReasoningTrace
from forecast_ai.models.prediction import Prediction
from forecast_ai.proof.ledger import (
    build_forecast_envelope,
    build_resolution_hash,
    build_rwa_resolution_hash,
    calculate_brier_score,
    calculate_commitment_brier_scores,
)


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


def test_commitment_scores_separate_namespaced_consensus_from_agents():
    scores = calculate_commitment_brier_scores([
        {"agent_name": "studio-1:consensus", "probability_bps": 8000},
        {"agent_name": "studio-1:research", "probability_bps": 7000},
    ], 1)

    assert scores["consensus_brier_score"] == 0.04
    assert scores["agent_scores"] == [{
        "agent_id": "studio-1:research",
        "probability": 0.7,
        "brier_score": 0.09,
    }]


def test_resolution_hash_is_deterministic_and_outcome_specific():
    first = build_resolution_hash("market-1", 1, "official", "2030-01-02T00:00:00Z")
    second = build_resolution_hash("market-1", 1, "official", "2030-01-02T00:00:00Z")
    no_outcome = build_resolution_hash("market-1", 0, "official", "2030-01-02T00:00:00Z")
    assert first == second
    assert first.startswith("0x") and len(first) == 66
    assert first != no_outcome


def test_rwa_resolution_hash_commits_reference_and_final_prices():
    first = build_rwa_resolution_hash(
        "forecast-1", "NVDA", 1, 100, 110, "Robinhood", "2030-01-02T00:00:01Z", "2030-01-02T00:01:00Z"
    )
    same = build_rwa_resolution_hash(
        "forecast-1", "NVDA", 1, 100, 110, "Robinhood", "2030-01-02T00:00:01Z", "2030-01-02T00:01:00Z"
    )
    changed_price = build_rwa_resolution_hash(
        "forecast-1", "NVDA", 1, 100, 111, "Robinhood", "2030-01-02T00:00:01Z", "2030-01-02T00:01:00Z"
    )

    assert first == same
    assert first != changed_price


def test_envelope_hash_is_deterministic():
    result = _forecast()
    options = {
        "proof_enabled": True,
        "chain_id": 46630,
        "contract_address": "0x1111111111111111111111111111111111111111",
        "explorer_url": "https://explorer.testnet.chain.robinhood.com/",
    }
    first = build_forecast_envelope(result, "Question?", "Polymarket", "Politics", "2030-01-01", **options)
    second = build_forecast_envelope(result, "Question?", "Polymarket", "Politics", "2030-01-01", **options)
    assert first["payload_hash"] == second["payload_hash"]
    assert first["status"] == "pending_onchain"
    assert first["queue_eligible"] is True
    assert first["chain_id"] == 46630
    assert first["explorer_url"] == "https://explorer.testnet.chain.robinhood.com"
    assert len(first["onchain_commitments"]) == 2
    assert first["onchain_commitments"][0]["agent_name"] == "consensus"
    assert first["onchain_commitments"][0]["probability_bps"] == 6000


def test_rwa_envelope_commits_server_captured_resolution_spec():
    result = _forecast()
    result.metadata.update({
        "analysis_mode": "rwa",
        "asset_symbol": "NVDA",
        "reference_price": 104.3,
        "reference_price_source": "https://api.robinhood.com/rhj/prices/NVDA",
        "reference_price_source_timestamp": "2030-01-01T12:00:00Z",
        "forecast_horizon_days": 30,
    })

    proof = build_forecast_envelope(
        result,
        "Will NVDA be higher?",
        "Robinhood Chain",
        "RWA",
        "2030-01-31T12:00:00Z",
    )

    assert proof["payload"]["resolution_spec"] == {
        "type": "rwa_price_direction",
        "asset_symbol": "NVDA",
        "reference_price": 104.3,
        "reference_price_source": "https://api.robinhood.com/rhj/prices/NVDA",
        "reference_price_source_timestamp": "2030-01-01T12:00:00Z",
        "forecast_horizon_days": 30,
        "resolves_at": "2030-01-31T12:00:00Z",
        "condition": "final_midpoint_greater_than_reference_midpoint",
    }


def test_envelope_is_not_queued_without_a_deployed_registry():
    proof = build_forecast_envelope(
        _forecast(), "Question?", "Polymarket", "Politics", "2030-01-01"
    )
    assert proof["status"] == "disabled"
    assert proof["queue_eligible"] is False


def test_agent_studio_identity_namespaces_onchain_commitments():
    proof = build_forecast_envelope(
        _forecast(),
        "Question?",
        "Polymarket",
        "Politics",
        "2030-01-01",
        proof_enabled=True,
        contract_address="0x1111111111111111111111111111111111111111",
        agent_namespace="studio-agent-id",
        agent_identity={"id": "studio-agent-id", "name": "My Agent", "mode": "specialist"},
    )

    assert proof["payload"]["agent_identity"]["id"] == "studio-agent-id"
    assert proof["payload"]["agent_predictions"][0]["agent_id"] == "studio-agent-id:research"
    assert proof["onchain_commitments"][0]["agent_name"] == "studio-agent-id:consensus"
    assert proof["onchain_commitments"][1]["agent_name"] == "studio-agent-id:research"


def test_envelope_is_not_queued_without_a_future_close_time():
    proof = build_forecast_envelope(
        _forecast(),
        "Question?",
        "Polymarket",
        "Politics",
        None,
        proof_enabled=True,
        contract_address="0x1111111111111111111111111111111111111111",
    )
    assert proof["status"] == "not_eligible"
    assert proof["queue_eligible"] is False


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
