"""Perigon structured news and event-context source."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import httpx

from .base import BaseSource
from ..models.evidence import Evidence


logger = logging.getLogger(__name__)


class PerigonSource(BaseSource):
    """Use Perigon articles, with optional story-cluster context."""

    cache_ttl_seconds = 900

    def __init__(
        self,
        api_key: str = "",
        api_url: str = "https://api.perigon.io",
        enabled: bool = False,
        timeout_seconds: float = 20.0,
        stories_enabled: bool = False,
    ):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.stories_enabled = stories_enabled

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.api_url}/{path.lstrip('/')}",
                headers={"Accept": "application/json", "x-api-key": self.api_key},
                params=params,
                timeout=self.timeout_seconds,
            )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        if not self.enabled or not self.api_key:
            return []

        # `q`, `size` and `sortBy` are supported by Perigon's Articles API.
        # Keep the result set tight: the specialist agents need evidence, not a
        # whole news feed, and SourceManager caches this result for 15 minutes.
        article_params = {
            "q": query,
            "size": max(1, min(limit, 10)),
            "sortBy": "date",
            "showReprints": "false",
            "excludeLabel": "Paid News",
        }
        calls = [self._get("v1/articles/all", article_params)]
        if self.stories_enabled:
            calls.append(self._get("v1/stories/all", {
                "q": query,
                "size": 3,
                "sortBy": "date",
            }))

        try:
            responses = await asyncio.gather(*calls, return_exceptions=True)
        except Exception as exc:
            logger.warning("[PerigonSource] Request scheduling failed: %s", exc)
            return []

        article_response = responses[0] if responses and isinstance(responses[0], dict) else {}
        articles = article_response.get("articles") or article_response.get("data") or []
        evidence: List[Evidence] = []
        if isinstance(articles, list):
            for article in articles[:limit]:
                if not isinstance(article, dict):
                    continue
                title = str(article.get("title") or "Perigon news result")
                description = str(article.get("description") or article.get("summary") or article.get("content") or "")
                if not description:
                    continue
                source = article.get("source") if isinstance(article.get("source"), dict) else {}
                evidence.append(Evidence(
                    source_name="Perigon News",
                    content=description[:5000],
                    relevance_score=float(article.get("relevance") or 0.85),
                    title=title,
                    url=str(article.get("url") or ""),
                    metadata={
                        "provider": "Perigon",
                        "source_type": "structured_news",
                        "published_at": article.get("pubDate") or article.get("publishedAt"),
                        "author": article.get("authorsByline") or article.get("author"),
                        "publisher": source.get("domain") or source.get("name") or article.get("source"),
                        "sentiment": article.get("sentiment"),
                        "entities": article.get("entities") or [],
                    },
                ))

        if self.stories_enabled and len(responses) > 1 and isinstance(responses[1], dict):
            stories = responses[1].get("stories") or responses[1].get("data") or []
            if isinstance(stories, list):
                for story in stories[:3]:
                    if not isinstance(story, dict):
                        continue
                    summary = str(story.get("summary") or story.get("description") or "")
                    if not summary:
                        continue
                    evidence.append(Evidence(
                        source_name="Perigon Story",
                        content=summary[:5000],
                        relevance_score=0.9,
                        title=str(story.get("title") or "Perigon event cluster"),
                        url=str(story.get("url") or ""),
                        metadata={
                            "provider": "Perigon",
                            "source_type": "event_cluster",
                            "article_count": story.get("articleCount") or story.get("count"),
                            "published_at": story.get("pubDate") or story.get("publishedAt"),
                        },
                    ))
        return evidence
