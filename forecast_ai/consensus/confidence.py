"""
Consensus Confidence Scoring.
"""

from typing import List, Dict
from ..models.prediction import Prediction
from ..models.confidence import ConfidenceScore

class ConsensusConfidenceCalculator:
    def calculate_confidence(
        self,
        predictions: List[Prediction],
        agent_weights: Dict[str, float],
        warnings: List[str],
        correlation_penalty: float = 0.0,
    ) -> ConfidenceScore:
        """
        Combines confidence scores of all predictions.
        Reduces confidence if conflicts are detected or evidence is sparse.
        """
        if not predictions:
            return ConfidenceScore(score=0.0, warnings=["No predictions to analyze."])

        total_weight = 0.0
        confidence_sum = 0.0

        for p in predictions:
            agent_weight = agent_weights.get(p.agent_name, 1.0)
            confidence_sum += p.confidence.score * agent_weight
            total_weight += agent_weight

        avg_confidence = confidence_sum / total_weight if total_weight > 0 else 0.5

        # Volatility / conflict penalty
        penalty = 0.0
        for w in warnings:
            if "anomaly" in w.lower() or "conflict" in w.lower():
                penalty += 0.15

        correlation_penalty = max(0.0, min(0.12, correlation_penalty))
        final_score = max(0.1, min(1.0, avg_confidence - penalty - correlation_penalty))
        
        # Accumulate all warnings
        all_warnings = list(warnings)
        for p in predictions:
            all_warnings.extend(p.confidence.warnings)

        # Unique warnings
        unique_warnings = list(set(all_warnings))

        return ConfidenceScore(
            score=final_score,
            factors={
                "base_average_confidence": avg_confidence,
                "conflict_penalty": penalty,
                "source_correlation_penalty": correlation_penalty,
            },
            warnings=unique_warnings
        )
