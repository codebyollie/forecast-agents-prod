"""Canonical forecast envelopes used by the offchain ledger and onchain registry."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from ..models.forecast import ForecastResult


def calculate_brier_score(probability: float, outcome: int) -> float:
    if outcome not in (0, 1):
        raise ValueError("Outcome must be 0 or 1.")
    probability = max(0.0, min(1.0, float(probability)))
    return round((probability - float(outcome)) ** 2, 6)


def calculate_commitment_brier_scores(
    commitments: Iterable[Dict[str, Any]], outcome: int
) -> Dict[str, Any]:
    """Calculate consensus and specialist scores from immutable commitments."""
    if outcome not in (0, 1):
        raise ValueError("Outcome must be 0 or 1.")
    consensus_score = None
    agent_scores = []
    for commitment in commitments:
        if not isinstance(commitment, dict):
            continue
        probability = max(
            0.0,
            min(1.0, float(commitment.get("probability_bps") or 0) / 10_000),
        )
        agent_name = str(commitment.get("agent_name") or "unknown")
        score = calculate_brier_score(probability, outcome)
        score_row = {
            "agent_id": agent_name,
            "probability": round(probability, 6),
            "brier_score": score,
        }
        if agent_name == "consensus" or agent_name.endswith(":consensus"):
            consensus_score = score
        else:
            agent_scores.append(score_row)
    return {
        "consensus_brier_score": consensus_score,
        "agent_scores": agent_scores,
    }


def _canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return f"0x{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def build_resolution_hash(market_id: str, outcome: int, source: str, resolved_at: str) -> str:
    if outcome not in (0, 1):
        raise ValueError("Outcome must be 0 or 1.")
    payload = {
        "schema": "forecast-ai-resolution-v1",
        "market_id": market_id,
        "outcome": outcome,
        "source": source,
        "resolved_at": resolved_at,
    }
    return _digest(_canonical_json(payload))


def build_rwa_resolution_hash(
    forecast_id: str,
    asset_symbol: str,
    outcome: int,
    reference_price: float,
    final_price: float,
    source: str,
    source_timestamp: str,
    resolved_at: str,
) -> str:
    """Commit the auditable RWA pricing inputs behind the binary outcome."""
    if outcome not in (0, 1):
        raise ValueError("Outcome must be 0 or 1.")
    payload = {
        "schema": "forecast-ai-rwa-resolution-v1",
        "forecast_id": forecast_id,
        "asset_symbol": asset_symbol.upper(),
        "outcome": outcome,
        "reference_price": round(float(reference_price), 8),
        "final_price": round(float(final_price), 8),
        "source": source,
        "source_timestamp": source_timestamp,
        "resolved_at": resolved_at,
    }
    return _digest(_canonical_json(payload))


def _unix_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def build_forecast_envelope(
    result: ForecastResult,
    question: str,
    venue: str | None,
    category: str | None,
    market_closes_at: str | None,
    *,
    proof_enabled: bool = False,
    chain_id: int = 4663,
    contract_address: str | None = None,
    explorer_url: str | None = None,
    agent_namespace: str | None = None,
    agent_identity: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    payload = {
        "schema": "forecast-ai-proof-v1",
        "question": question.strip(),
        "market_id": result.market_id,
        "venue": venue or None,
        "category": category or "Other",
        "market_closes_at": market_closes_at or None,
        "created_at": result.timestamp.isoformat(),
        "consensus_probability": round(float(result.probability), 6),
        "confidence": round(float(result.confidence.score), 6),
        "agent_predictions": [
            {
                "agent_id": f"{agent_namespace}:{prediction.agent_name}" if agent_namespace else prediction.agent_name,
                **({"role": prediction.agent_name} if agent_namespace else {}),
                "probability": round(float(prediction.probability), 6),
                "confidence": round(float(prediction.confidence.score), 6),
            }
            for prediction in sorted(result.individual_predictions, key=lambda item: item.agent_name)
        ],
    }
    if str(result.metadata.get("analysis_mode") or "").lower() == "rwa":
        payload["resolution_spec"] = {
            "type": "rwa_price_direction",
            "asset_symbol": str(result.metadata.get("asset_symbol") or "").upper(),
            "reference_price": round(float(result.metadata.get("reference_price") or 0), 8),
            "reference_price_source": result.metadata.get("reference_price_source"),
            "reference_price_source_timestamp": result.metadata.get("reference_price_source_timestamp"),
            "forecast_horizon_days": int(result.metadata.get("forecast_horizon_days") or 0),
            "resolves_at": market_closes_at,
            "condition": "final_midpoint_greater_than_reference_midpoint",
        }
    if agent_identity:
        payload["agent_identity"] = agent_identity
    canonical = _canonical_json(payload)
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload_hash_hex = f"0x{payload_hash}"
    closes_at_unix = _unix_timestamp(market_closes_at)
    created_at_unix = int(result.timestamp.timestamp())
    queue_eligible = bool(
        proof_enabled
        and contract_address
        and closes_at_unix
        and closes_at_unix > created_at_unix
    )
    if not proof_enabled or not contract_address:
        proof_status = "disabled"
    elif not closes_at_unix or closes_at_unix <= created_at_unix:
        proof_status = "not_eligible"
    else:
        proof_status = "pending_onchain"
    probability_rows = [
        (f"{agent_namespace}:consensus" if agent_namespace else "consensus", float(result.probability)),
        *[
            (f"{agent_namespace}:{prediction.agent_name}" if agent_namespace else prediction.agent_name, float(prediction.probability))
            for prediction in result.individual_predictions
        ],
    ]
    commitments = [
        {
            "forecast_id": _digest(f"{payload_hash}:{agent_id}"),
            "payload_hash": payload_hash_hex,
            "market_id_hash": _digest(result.market_id),
            "agent_id": _digest(agent_id),
            "agent_name": agent_id,
            "category_id": _digest(category or "Other"),
            "probability_bps": round(max(0.0, min(1.0, probability)) * 10_000),
            "closes_at": closes_at_unix,
        }
        for agent_id, probability in probability_rows
    ]
    return {
        "forecast_id": payload_hash,
        "payload_hash": payload_hash_hex,
        "hash_algorithm": "sha256",
        "payload": payload,
        "status": proof_status,
        "network": "Robinhood Chain",
        "chain_id": int(chain_id),
        "contract_address": contract_address or None,
        "explorer_url": explorer_url.rstrip("/") if explorer_url else None,
        "transaction_hash": None,
        "queue_eligible": queue_eligible,
        "onchain_commitments": commitments,
    }
