"""
Memory Store for Forecast AI.

Stores historical forecasts, agent reputations, and evidence databases to improve future predictions.
"""

import json
import os
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

from ..models.forecast import ForecastResult
from ..config import ForecastConfig
from ..proof.ledger import calculate_brier_score

class MemoryStore:
    def __init__(self, config: ForecastConfig):
        self.config = config
        self.store_dir = Path(config.memory.store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        
        self.forecasts_file = self.store_dir / "forecasts.json"
        self.reputation_file = self.store_dir / "reputation.json"
        self.evidence_file = self.store_dir / "evidence.json"

        self._init_files()

    def _init_files(self):
        for f in [self.forecasts_file, self.reputation_file, self.evidence_file]:
            if not f.exists():
                with open(f, "w", encoding="utf-8") as file_handle:
                    initial_data = {} if f == self.reputation_file else []
                    json.dump(initial_data, file_handle)

    def _load_json(self, path: Path) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {} if path == self.reputation_file else []

    def _save_json(self, path: Path, data: Any):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception:
            pass

    def save_forecast(self, result: ForecastResult):
        forecasts = self._load_json(self.forecasts_file)
        
        # Serialize ForecastResult
        runtime = result.metadata.get("agent_runtime") or {}
        namespace = runtime.get("id")
        entry = {
            "forecast_id": result.metadata.get("proof", {}).get("forecast_id"),
            "payload_hash": result.metadata.get("proof", {}).get("payload_hash"),
            "question": result.metadata.get("question"),
            "venue": result.metadata.get("venue"),
            "category": result.metadata.get("category", "Other"),
            "market_closes_at": result.metadata.get("market_closes_at"),
            "market_id": result.market_id,
            "probability": result.probability,
            "confidence": {
                "score": result.confidence.score,
                "factors": result.confidence.factors,
                "warnings": result.confidence.warnings
            },
            "timestamp": result.timestamp.isoformat(),
            "predictions": [
                {
                    "agent_name": f"{namespace}:{p.agent_name}" if namespace else p.agent_name,
                    "role": p.agent_name,
                    "probability": p.probability,
                    "confidence": p.confidence.score,
                    "reasoning": p.reasoning
                } for p in result.individual_predictions
            ],
            "metadata": result.metadata
        }

        forecast_id = entry.get("forecast_id")
        if forecast_id and any(item.get("forecast_id") == forecast_id for item in forecasts):
            return
        forecasts.append(entry)
        # Cap limit
        max_entries = self.config.memory.max_history_entries
        if len(forecasts) > max_entries:
            forecasts = forecasts[-max_entries:]
            
        self._save_json(self.forecasts_file, forecasts)

    def get_agent_reputations(self) -> Dict[str, float]:
        return self._load_json(self.reputation_file)

    def get_agent_reputation(self, agent_name: str) -> float:
        rep = self.get_agent_reputations()
        return rep.get(agent_name, self.config.consensus.default_agent_weight)

    def update_agent_reputation(self, agent_name: str, outcome_correct: bool, error_delta: float):
        if not self.config.memory.enable_reputation_updates:
            return

        reps = self.get_agent_reputations()
        current = reps.get(agent_name, self.config.consensus.default_agent_weight)

        alpha = self.config.consensus.calibration_alpha
        if outcome_correct:
            # Boost reputation
            new_rep = current + alpha * (2.0 - current) * (1.0 - error_delta)
        else:
            # Penalty
            new_rep = current - alpha * current * error_delta

        reps[agent_name] = max(0.1, min(2.0, new_rep))
        self._save_json(self.reputation_file, reps)
        
    def list_forecasts(self) -> List[Dict[str, Any]]:
        return self._load_json(self.forecasts_file)

    def resolve_market_forecasts(
        self,
        market_id: str,
        outcome: int,
        resolution_source: str,
        resolved_at: str,
    ) -> List[Dict[str, Any]]:
        if outcome not in (0, 1):
            raise ValueError("Outcome must be 0 or 1.")
        forecasts = self._load_json(self.forecasts_file)
        resolved: List[Dict[str, Any]] = []
        for entry in forecasts:
            if entry.get("market_id") != market_id or entry.get("resolution"):
                continue
            self._apply_resolution(entry, outcome, resolution_source, resolved_at)
            resolved.append(entry)
        self._save_json(self.forecasts_file, forecasts)
        return resolved

    def resolve_forecast(
        self,
        forecast_id: str,
        outcome: int,
        resolution_source: str,
        resolved_at: str,
        resolution_details: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Resolve one immutable forecast without affecting later RWA runs."""
        if outcome not in (0, 1):
            raise ValueError("Outcome must be 0 or 1.")
        forecasts = self._load_json(self.forecasts_file)
        resolved = None
        for entry in forecasts:
            if entry.get("forecast_id") != forecast_id or entry.get("resolution"):
                continue
            self._apply_resolution(
                entry,
                outcome,
                resolution_source,
                resolved_at,
                resolution_details=resolution_details,
            )
            resolved = entry
            break
        self._save_json(self.forecasts_file, forecasts)
        return resolved

    def _apply_resolution(
        self,
        entry: Dict[str, Any],
        outcome: int,
        resolution_source: str,
        resolved_at: str,
        *,
        resolution_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        consensus_brier = calculate_brier_score(entry.get("probability", 0.5), outcome)
        agent_scores = []
        for prediction in entry.get("predictions", []):
            agent_brier = calculate_brier_score(prediction.get("probability", 0.5), outcome)
            prediction["brier_score"] = agent_brier
            agent_scores.append({
                "agent_id": prediction.get("agent_name"),
                "probability": prediction.get("probability"),
                "brier_score": agent_brier,
            })
            self.apply_agent_brier_score(prediction.get("agent_name", "unknown"), agent_brier)
        entry["resolution"] = {
            **(resolution_details or {}),
            "status": "resolved_offchain",
            "outcome": outcome,
            "resolved_at": resolved_at,
            "source": resolution_source,
            "consensus_brier_score": consensus_brier,
            "agent_scores": agent_scores,
        }
        proof = entry.setdefault("metadata", {}).setdefault("proof", {})
        if proof.get("status") != "resolved_onchain":
            proof["status"] = "resolved_offchain"

    def apply_agent_brier_score(self, agent_name: str, brier_score: float) -> None:
        if not self.config.memory.enable_reputation_updates:
            return
        reps = self.get_agent_reputations()
        current = float(reps.get(agent_name, self.config.consensus.default_agent_weight))
        target = max(0.1, min(2.0, 2.0 * (1.0 - float(brier_score))))
        alpha = self.config.consensus.calibration_alpha
        reps[agent_name] = round(current + alpha * (target - current), 6)
        self._save_json(self.reputation_file, reps)

    def get_track_record(self) -> Dict[str, Any]:
        forecasts = self._load_json(self.forecasts_file)
        resolved = [entry for entry in forecasts if entry.get("resolution")]
        consensus_scores = [
            float(entry["resolution"]["consensus_brier_score"])
            for entry in resolved
        ]
        agents: Dict[str, Dict[str, Any]] = {}
        categories: Dict[str, Dict[str, Any]] = {}
        for entry in resolved:
            category = str(entry.get("category") or "Other")
            category_stat = categories.setdefault(category, {"resolved": 0, "brier_total": 0.0})
            category_stat["resolved"] += 1
            category_stat["brier_total"] += float(entry["resolution"]["consensus_brier_score"])
            for score in entry["resolution"].get("agent_scores", []):
                agent_id = str(score.get("agent_id") or "unknown")
                stat = agents.setdefault(agent_id, {"resolved": 0, "brier_total": 0.0})
                stat["resolved"] += 1
                stat["brier_total"] += float(score.get("brier_score", 0.0))

        for stats in (agents, categories):
            for value in stats.values():
                value["average_brier_score"] = round(value.pop("brier_total") / value["resolved"], 6)

        return {
            "total_forecasts": len(forecasts),
            "resolved_forecasts": len(resolved),
            "pending_forecasts": len(forecasts) - len(resolved),
            "average_brier_score": round(sum(consensus_scores) / len(consensus_scores), 6) if consensus_scores else None,
            "agent_reputations": self.get_agent_reputations(),
            "agents": agents,
            "categories": categories,
        }
