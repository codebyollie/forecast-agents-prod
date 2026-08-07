"""Falcon API partner-intelligence connector.

The connector intentionally uses Falcon's unified parameterized endpoint and is
only called for an explicitly selected market. This keeps partner API usage
predictable and prevents browse/search traffic from consuming credits.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time
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
        falcon_score_agent_id: int = 584,
        polymarket_trades_agent_id: int = 556,
        smart_money_enabled: bool = False,
    ):
        self.api_token = api_token
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.market_insights_agent_id = market_insights_agent_id
        self.kalshi_markets_agent_id = kalshi_markets_agent_id
        self.social_pulse_agent_id = social_pulse_agent_id
        self.social_enabled = social_enabled
        self.falcon_score_agent_id = falcon_score_agent_id
        self.polymarket_trades_agent_id = polymarket_trades_agent_id
        self.smart_money_enabled = smart_money_enabled
        self._leaderboard_payload: Optional[Dict[str, Any]] = None
        self._leaderboard_cached_at = 0.0

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

    @staticmethod
    def _signal_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Extract stable, UI-safe signal fields without assuming one response shape."""
        wanted = {
            "liquidity",
            "volume",
            "volume_total",
            "concentration",
            "holder_concentration",
            "sentiment_score",
            "mention_volume",
            "narrative_trend",
            "price_sentiment_divergence",
            "acceleration",
            "author_diversity_pct",
            "pct_last_1h",
            "pct_last_6h",
            "tweet_count",
            "like_count",
            "retweet_count",
            "reply_count",
            "falcon_score",
            "win_rate",
            "roi",
            "total_pnl",
        }
        snapshot: Dict[str, Any] = {}

        def visit(value: Any) -> None:
            if len(snapshot) >= 20:
                return
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = str(key).lower()
                    if normalized in wanted and normalized not in snapshot:
                        if isinstance(nested, (str, int, float, bool)) or nested is None:
                            snapshot[normalized] = nested
                    visit(nested)
            elif isinstance(value, list):
                for nested in value[:10]:
                    visit(nested)

        visit(payload.get("data", payload))
        return snapshot

    @staticmethod
    def _records(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        data: Any = payload.get("data", payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("results", "items", "data", "trades", "leaderboard"):
                nested = data.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
        return []

    @staticmethod
    def _wallet(record: Dict[str, Any]) -> str:
        for key in ("wallet", "proxy_wallet", "wallet_proxy", "wallet_address"):
            value = str(record.get(key) or "").lower()
            if value.startswith("0x"):
                return value
        return ""

    @staticmethod
    def _social_terms(query: str) -> List[str]:
        stop_words = {
            "will", "the", "and", "for", "with", "from", "into", "before",
            "after", "this", "that", "market", "december", "january", "february",
            "march", "april", "may", "june", "july", "august", "september",
            "october", "november", "next", "called", "call", "being", "be", "by",
        }
        short_topic_terms = {"ai", "eu", "uk", "us"}
        tokens: List[str] = []
        for token in re.findall(r"[A-Za-z0-9]+", query.replace("-", " ")):
            normalized = token.lower()
            if (
                (len(normalized) < 3 and normalized not in short_topic_terms)
                or normalized in stop_words
                or normalized in tokens
            ):
                continue
            tokens.append(normalized)
        return tokens[:8] or ["prediction"]

    @classmethod
    def _social_keywords(cls, query: str) -> str:
        return "{" + ",".join(cls._social_terms(query)) + "}"

    @classmethod
    def _social_content(cls, payload: Dict[str, Any], query: str) -> str:
        """Return a readable, topic-checked Social Pulse summary."""
        records = cls._records(payload)
        query_terms = {
            term for term in cls._social_terms(query)
            if not term.isdigit() and term != "prediction"
        }
        text_fields = ("content", "text", "title", "body", "tweet")
        searchable_records: List[tuple[Dict[str, Any], str]] = []
        relevant_records: List[tuple[Dict[str, Any], str]] = []

        for record in records:
            text = " ".join(
                str(record.get(field) or "").strip()
                for field in text_fields
                if record.get(field)
            ).strip()
            if not text:
                continue
            searchable_records.append((record, text))
            text_tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
            if query_terms.intersection(text_tokens):
                relevant_records.append((record, text))

        # Falcon occasionally returns a valid but unrelated trending narrative.
        # Never send that content into the forecasting agents.
        if searchable_records and not relevant_records:
            raise FalconError(422, "Falcon Social Pulse returned no topic-relevant records.")

        signals = cls._signal_snapshot(payload)
        metrics = []
        metric_labels = (
            ("tweet_count", "posts"),
            ("acceleration", "acceleration"),
            ("author_diversity_pct", "unique authors %"),
            ("pct_last_1h", "last 1h %"),
            ("pct_last_6h", "last 6h %"),
        )
        for key, label in metric_labels:
            value = signals.get(key)
            if value is not None:
                metrics.append(f"{label}: {value}")

        excerpts = []
        for _, text in relevant_records[:3]:
            compact = re.sub(r"\s+", " ", text).strip()
            excerpts.append(compact[:320])

        summary = f'Falcon Social Pulse for "{query}": '
        summary += "; ".join(metrics) if metrics else "topic-relevant social data returned"
        if excerpts:
            summary += ". Relevant posts: " + " | ".join(excerpts)
        return summary[:4000]

    async def _smart_money(self, market_id: str) -> Optional[Evidence]:
        if self._leaderboard_payload is None or time.time() - self._leaderboard_cached_at > 900:
            self._leaderboard_payload = await self._retrieve(
                self.falcon_score_agent_id,
                {
                    "min_win_rate_15d": "0.45",
                    "max_win_rate_15d": "0.95",
                    "min_roi_15d": "0",
                    "min_total_trades_15d": "30",
                    "max_total_trades_15d": "5000",
                    "min_pnl_15d": "5000",
                    "sort_by": "roi",
                },
                limit=25,
            )
            self._leaderboard_cached_at = time.time()
        trades_payload = await self._retrieve(
            self.polymarket_trades_agent_id,
            {"market_slug": market_id},
            limit=100,
        )
        leaders = {
            self._wallet(record): record
            for record in self._records(self._leaderboard_payload)
            if self._wallet(record)
        }
        matched = []
        for trade in self._records(trades_payload):
            wallet = self._wallet(trade)
            if wallet and wallet in leaders:
                matched.append({
                    "wallet": wallet,
                    "leader": leaders[wallet],
                    "trade": trade,
                })
            if len(matched) >= 10:
                break
        return Evidence(
            source_name="falcon_smart_money",
            content=f"Falcon Smart Money: {json.dumps(matched, ensure_ascii=False, default=str)[:7000]}",
            relevance_score=0.95,
            title="Falcon Smart Money",
            url="https://api.polymarketanalytics.com/",
            metadata={
                "provider": "Falcon",
                "source_type": "smart_money",
                "status": "active" if matched else "empty",
                "partner": True,
                "agent_ids": [self.falcon_score_agent_id, self.polymarket_trades_agent_id],
                "market_id": market_id,
                "signals": {
                    "smart_money_wallet_count": len(matched),
                    "smart_money_wallets": [item["wallet"] for item in matched],
                },
            },
        )

    async def fetch_market_intelligence(
        self,
        market_id: str,
        venue: Optional[str] = None,
        condition_id: Optional[str] = None,
        social_query: Optional[str] = None,
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
            if not condition_id:
                raise FalconError(422, "Polymarket condition_id is required for Falcon Market Insights.")
            params = {"condition_id": condition_id}
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
                "condition_id": condition_id,
                "venue": venue or "Polymarket",
                "signals": self._signal_snapshot(payload),
            },
        ))

        if self.smart_money_enabled and "kalshi" not in venue_name and "robinhood" not in venue_name:
            try:
                smart_money = await self._smart_money(market_id)
                if smart_money is not None:
                    results.append(smart_money)
            except Exception as exc:
                results.append(Evidence(
                    source_name="falcon_smart_money_status",
                    content="Falcon Smart Money was unavailable for this analysis.",
                    relevance_score=0.0,
                    metadata={
                        "provider": "Falcon",
                        "source_type": "smart_money_status",
                        "status": "unavailable",
                        "message": str(exc)[:240],
                        "partner": True,
                    },
                ))

        if self.social_enabled and "kalshi" not in venue_name and "robinhood" not in venue_name:
            try:
                topic = (social_query or market_id).strip()
                social_payload = await self._retrieve(
                    self.social_pulse_agent_id,
                    {"keywords": self._social_keywords(topic), "hours_back": "24"},
                    limit=limit,
                )
                results.append(Evidence(
                    source_name="falcon_social_pulse",
                    content=self._social_content(social_payload, topic),
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
                        "query": topic,
                        "signals": self._signal_snapshot(social_payload),
                    },
                ))
            except Exception as exc:
                results.append(Evidence(
                    source_name="falcon_social_status",
                    content="Falcon Social Pulse was unavailable for this analysis.",
                    relevance_score=0.0,
                    metadata={
                        "provider": "Falcon",
                        "source_type": "social_status",
                        "status": "unavailable",
                        "message": str(exc)[:240],
                        "partner": True,
                    },
                ))
        return results

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        # Generic queries are deliberately disabled to control partner API cost.
        return []
