"""Exa deep-web research source for the Research agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from .base import BaseSource
from ..models.evidence import Evidence


logger = logging.getLogger(__name__)


class ExaSource(BaseSource):
    """Search Exa and return cited, extracted research evidence."""

    cache_ttl_seconds = 900

    def __init__(
        self,
        api_key: str = "",
        api_url: str = "https://api.exa.ai",
        enabled: bool = False,
        timeout_seconds: float = 25.0,
        search_type: str = "auto",
    ):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.search_type = search_type

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        if not self.enabled or not self.api_key:
            return []

        payload: Dict[str, Any] = {
            "query": query,
            "type": self.search_type,
            "numResults": max(1, min(limit, 10)),
            "contents": {
                "highlights": {"maxCharacters": 1200},
            },
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/search",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "x-api-key": self.api_key,
                    },
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("[ExaSource] Search request failed: %s", exc)
            return []

        results = data.get("results") if isinstance(data, dict) else []
        if not isinstance(results, list):
            return []

        evidence: List[Evidence] = []
        for result in results[:limit]:
            if not isinstance(result, dict):
                continue
            highlights = result.get("highlights")
            highlight_text = " ".join(str(item) for item in highlights if item) if isinstance(highlights, list) else ""
            content = str(result.get("summary") or highlight_text or result.get("text") or "")
            if not content:
                continue
            evidence.append(Evidence(
                source_name="Exa Deep Research",
                content=content[:5000],
                relevance_score=float(result.get("score") or 0.85),
                title=str(result.get("title") or "Exa research result"),
                url=str(result.get("url") or ""),
                metadata={
                    "provider": "Exa",
                    "source_type": "deep_research",
                    "published_at": result.get("publishedDate"),
                    "author": result.get("author"),
                    "request_id": data.get("requestId") if isinstance(data, dict) else None,
                    "search_type": self.search_type,
                },
            ))
        return evidence
