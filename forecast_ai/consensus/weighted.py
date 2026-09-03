"""
Weighted Consensus Formulas.
"""

from statistics import median
from typing import List, Dict
from ..models.prediction import Prediction

class WeightedConsensusCalculator:
    def calculate_weighted_probability(
        self,
        predictions: List[Prediction],
        agent_weights: Dict[str, float],
        influence_cap_multiple: float = 1.75,
    ) -> float:
        """
        Calculates consensus probability using weighted combination of predictions,
        incorporating both agent reliability weight and prediction confidence.
        """
        if not predictions:
            return 0.5

        weighted_rows = []

        for p in predictions:
            # Combined weight = agent reliability * confidence score
            agent_weight = agent_weights.get(p.agent_name, 1.0)
            conf = p.confidence.score
            combined_weight = agent_weight * (0.2 + 0.8 * conf)

            weighted_rows.append((p, combined_weight))

        positive_weights = [weight for _, weight in weighted_rows if weight > 0]
        cap = median(positive_weights) * max(1.0, influence_cap_multiple) if positive_weights else 0.0
        capped_rows = [(prediction, min(weight, cap)) for prediction, weight in weighted_rows]
        total_weight = sum(weight for _, weight in capped_rows)
        weighted_sum = sum(prediction.probability * weight for prediction, weight in capped_rows)

        if total_weight == 0.0:
            return 0.5

        return weighted_sum / total_weight
