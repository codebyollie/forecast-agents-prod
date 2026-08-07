"""Canonical forecast envelopes used by the offchain ledger and onchain registry."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from ..models.forecast import ForecastResult


def calculate_brier_score(probability: float, outcome: int) -> float:
    if outcome not in (0, 1):
        raise ValueError("Outcome must be 0 or 1.")
    probability = max(0.0, min(1.0, float(probability)))
    return round((probability - float(outcome)) ** 2, 6)


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
                "agent_id": prediction.agent_name,
                "probability": round(float(prediction.probability), 6),
                "confidence": round(float(prediction.confidence.score), 6),
            }
            for prediction in sorted(result.individual_predictions, key=lambda item: item.agent_name)
        ],
    }
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
        ("consensus", float(result.probability)),
        *[(prediction.agent_name, float(prediction.probability)) for prediction in result.individual_predictions],
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
