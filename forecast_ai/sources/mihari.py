"""Mihari intelligence for Robinhood Stock Token corporate-action context.

Mihari is a read-only, public intelligence API.  It is deliberately routed to
the Onchain agent rather than the probability consensus: corporate actions,
token multipliers and quote integrity describe RWA execution context, not
independent evidence about a prediction-market outcome.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from .base import BaseSource
from ..models.evidence import Evidence
from ..services.robinhood_stock_tokens import RobinhoodStockTokenClient


logger = logging.getLogger(__name__)


class MihariSource(BaseSource):
    """Fetch public Robinhood Stock Token risk and corporate-action signals."""

    cache_ttl_seconds = 300

    def __init__(
        self,
        api_url: str = "https://mihari.pro/api/v1",
        enabled: bool = False,
        timeout_seconds: float = 12.0,
        max_symbols: int = 5,
    ):
        self.api_url = api_url.rstrip("/")
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_symbols = max(1, min(max_symbols, 10))

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        if not self.enabled:
            return []

        symbols = RobinhoodStockTokenClient.related_symbols(query)[: self.max_symbols]
        if not symbols:
            return []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/risk-feed",
                    params={"symbols": ",".join(symbols)},
                    headers={"Accept": "application/json"},
                    timeout=self.timeout_seconds,
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("[MihariSource] Risk-feed request failed: %s", exc)
            return []

        data = payload.get("data") if isinstance(payload, dict) else {}
        signals = data.get("signals") if isinstance(data, dict) else []
        if not isinstance(signals, list):
            return []

        evidence: List[Evidence] = []
        for signal in signals[:limit]:
            if not isinstance(signal, dict):
                continue
            asset = signal.get("asset") if isinstance(signal.get("asset"), dict) else {}
            event = signal.get("event") if isinstance(signal.get("event"), dict) else {}
            risk = signal.get("risk") if isinstance(signal.get("risk"), dict) else {}
            symbol = str(asset.get("symbol") or event.get("symbol") or "RWA").upper()
            summary = str(
                risk.get("summary")
                or event.get("summary")
                or "No current official corporate-action record matched this Stock Token."
            )
            multiplier = asset.get("multiplier") if isinstance(asset.get("multiplier"), dict) else {}
            adjusted_quote = (
                asset.get("multiplierAdjustedQuote")
                if isinstance(asset.get("multiplierAdjustedQuote"), dict)
                else {}
            )
            price = asset.get("price") if isinstance(asset.get("price"), dict) else {}
            event_type = event.get("type")
            event_status = event.get("status")
            content_parts = [
                f"Mihari Robinhood Stock Token context for {symbol}: {summary}",
                f"Risk level={risk.get('level') or 'unknown'}, attention={risk.get('attention') or 'unknown'}.",
                f"Multiplier={multiplier.get('current') or 'unavailable'} ({multiplier.get('state') or 'unknown'}).",
            ]
            if event_type:
                content_parts.append(f"Official corporate action: {event_type} ({event_status or 'status unavailable'}).")
            if adjusted_quote.get("midpoint") is not None:
                content_parts.append(f"Multiplier-adjusted midpoint={adjusted_quote.get('midpoint')} {adjusted_quote.get('currency') or 'USD'}.")

            evidence.append(Evidence(
                source_name="Mihari RWA Intelligence",
                content=" ".join(content_parts),
                relevance_score=0.9 if event else 0.65,
                title=f"{symbol} Robinhood Stock Token context",
                url="https://mihari.pro/docs#intelligence-api",
                metadata={
                    "provider": "Mihari",
                    "source_type": "rwa_intelligence",
                    "symbol": symbol,
                    "event_type": event_type,
                    "event_status": event_status,
                    "risk_level": risk.get("level"),
                    "risk_attention": risk.get("attention"),
                    "risk_confidence": risk.get("confidence"),
                    "risk_summary": risk.get("summary"),
                    "current_multiplier": multiplier.get("current"),
                    "multiplier_state": multiplier.get("state"),
                    "adjusted_midpoint": adjusted_quote.get("midpoint"),
                    "bid": price.get("bid"),
                    "ask": price.get("ask"),
                    "event": event,
                    "partner": True,
                },
            ))
        return evidence
