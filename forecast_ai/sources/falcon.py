"""Falcon API partner-intelligence connector.

The connector intentionally uses Falcon's unified parameterized endpoint and is
only called for an explicitly selected market. This keeps partner API usage
predictable and prevents browse/search traffic from consuming credits.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional

import httpx

from .base import BaseSource
from ..models.evidence import Evidence


_RUNTIME_STATUS: Dict[str, Any] = {
    "status": "not_checked",
    "last_checked_at": None,
    "message": None,
}


def get_falcon_runtime_status() -> Dict[str, Any]:
    return dict(_RUNTIME_STATUS)


def _record_status(status: str, message: Optional[str] = None) -> None:
    _RUNTIME_STATUS.update({
        "status": status,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
    })


class FalconError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Falcon Error {status_code}: {message}")


class FalconSource(BaseSource):
    def __init__(
        self,
        api_token: str,
        api_url: str,
        timeout_seconds: float = 30.0,
        market_insights_agent_id: int = 575,
        kalshi_markets_agent_id: int = 565,
        social_pulse_agent_id: int = 585,
        social_enabled: bool = False,
    ):
        self.api_token = api_token
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.market_insights_agent_id = market_insights_agent_id
        self.kalshi_markets_agent_id = kalshi_markets_agent_id
        self.social_pulse_agent_id = social_pulse_agent_id
        self.social_enabled = social_enabled

    async def _retrieve(self, agent_id: int, params: Dict[str, Any], limit: int = 25) -> Dict[str, Any]:
        if not self.api_token:
            raise FalconError(401, "Falcon API token is not configured.")

        payload = {
            "agent_id": agent_id,
            "params": params,
            "pagination": {"limit": limit, "offset": 0},
            "formatter_config": {"format_type": "raw"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        _record_status("requested")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
        except Exception as exc:
            _record_status("unavailable", str(exc)[:240])
            raise FalconError(503, f"Network error calling Falcon: {exc}") from exc

        if response.status_code != 200:
            try:
                body = response.json()
                detail = body.get("detail") or body.get("error") or body.get("message") or str(body)
            except Exception:
                detail = response.text[:240]
            status = "unauthorized" if response.status_code in (401, 403) else "unavailable"
            _record_status(status, str(detail)[:240])
            raise FalconError(response.status_code, str(detail))

        try:
            data = response.json()
        except Exception as exc:
            _record_status("unavailable", "Falcon returned invalid JSON.")
            raise FalconError(502, "Falcon returned invalid JSON.") from exc

        if not isinstance(data, dict):
            _record_status("unavailable", "Falcon returned a non-object response.")
            raise FalconError(502, "Falcon returned a non-object response.")
        if data.get("success") is False:
            detail = str(data.get("error") or data.get("message") or "Falcon request failed.")
            _record_status("unavailable", detail[:240])
            raise FalconError(502, detail)

        _record_status("active")
        return data

    @staticmethod
    def _content(label: str, payload: Dict[str, Any]) -> str:
        data = payload.get("data", payload)
        return f"{label}: {json.dumps(data, ensure_ascii=False, default=str)[:7000]}"

    async def fetch_market_intelligence(
        self,
        market_id: str,
        venue: Optional[str] = None,
        limit: int = 25,
    ) -> List[Evidence]:
        venue_name = (venue or "").lower()
        if not market_id or market_id == "custom_market":
            return []

        if "kalshi" in venue_name or "robinhood" in venue_name:
            agent_id = self.kalshi_markets_agent_id
            params = {"ticker": market_id.upper(), "status": "open"}
            label = "Falcon Kalshi market intelligence"
        else:
            agent_id = self.market_insights_agent_id
            params = {"market_slug": market_id}
            label = "Falcon Polymarket market intelligence"

        results: List[Evidence] = []
        payload = await self._retrieve(agent_id, params, limit=limit)
        results.append(Evidence(
            source_name="falcon_market_intelligence",
            content=self._content(label, payload),
            relevance_score=0.98,
            title=label,
            url="https://api.polymarketanalytics.com/",
            metadata={
                "provider": "Falcon",
                "source_type": "market_intelligence",
                "status": "active",
                "partner": True,
                "agent_id": agent_id,
                "market_id": market_id,
                "venue": venue or "Polymarket",
            },
        ))

        if self.social_enabled and "kalshi" not in venue_name and "robinhood" not in venue_name:
            social_payload = await self._retrieve(
                self.social_pulse_agent_id,
                {"market_slug": market_id, "window": "24h"},
                limit=limit,
            )
            results.append(Evidence(
                source_name="falcon_social_pulse",
                content=self._content("Falcon Social Pulse", social_payload),
                relevance_score=0.92,
                title="Falcon Social Pulse",
                url="https://api.polymarketanalytics.com/",
                metadata={
                    "provider": "Falcon",
                    "source_type": "social_intelligence",
                    "status": "active",
                    "partner": True,
                    "agent_id": self.social_pulse_agent_id,
                    "market_id": market_id,
                },
            ))
        return results

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        # Generic queries are deliberately disabled to control partner API cost.
        return []
