"""Cross-market Outcome Graph for selected markets and Robinhood RWA assets."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Set

from .market_search import MarketSearchService
from .robinhood_stock_tokens import RobinhoodStockTokenClient


_STOP_WORDS = {
    "will", "the", "a", "an", "by", "before", "after", "on", "in", "of",
    "to", "and", "or", "is", "are", "be", "this", "that", "market",
}


def _tokens(value: str) -> Set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return round(len(left_tokens & right_tokens) / len(left_tokens | right_tokens), 4)


class OutcomeGraphService:
    def __init__(
        self,
        search_service: MarketSearchService,
        stock_tokens: Optional[RobinhoodStockTokenClient] = None,
    ):
        self.search_service = search_service
        self.stock_tokens = stock_tokens

    async def build(
        self,
        question: str,
        selected_market_id: str,
        selected_venue: Optional[str],
        selected_probability: Optional[float],
    ) -> Dict[str, Any]:
        market_task = asyncio.create_task(self.search_service.search_markets(question, limit=20))
        asset_task = (
            asyncio.create_task(self.stock_tokens.related_assets(question))
            if self.stock_tokens is not None
            else None
        )
        markets_result, assets_result = await asyncio.gather(
            market_task,
            asset_task if asset_task is not None else asyncio.sleep(0, result=[]),
            return_exceptions=True,
        )
        markets = markets_result if isinstance(markets_result, list) else []
        assets = assets_result if isinstance(assets_result, list) else []

        related: List[Dict[str, Any]] = []
        for market in markets:
            market_id = str(market.get("market_id") or "")
            if market_id.lower() == selected_market_id.lower():
                continue
            match_score = similarity(question, str(market.get("question") or ""))
            if match_score < 0.12:
                continue
            try:
                current_probability = float(market.get("current_price"))
            except (TypeError, ValueError):
                current_probability = None
            gap = None
            if current_probability is not None and selected_probability is not None:
                gap = round(current_probability - selected_probability, 4)
            related.append({
                "market_id": market_id,
                "question": market.get("question"),
                "venue": market.get("venue"),
                "probability": current_probability,
                "price_gap": gap,
                "similarity": match_score,
                "volume": market.get("volume"),
                "end_date": market.get("end_date"),
            })

        related.sort(key=lambda item: (item["similarity"], item.get("volume") or 0), reverse=True)
        selected_venue_name = (selected_venue or "").lower()
        counterparts = [
            item for item in related
            if str(item.get("venue") or "").lower() != selected_venue_name
        ][:3]
        return {
            "version": "1.0",
            "selected": {
                "market_id": selected_market_id,
                "venue": selected_venue,
                "probability": selected_probability,
            },
            "counterpart_markets": counterparts,
            "related_markets": related[:6],
            "rwa_assets": assets,
            "matching_method": "deterministic_title_similarity",
        }
