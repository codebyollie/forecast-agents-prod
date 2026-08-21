from unittest.mock import AsyncMock, Mock, patch

import pytest

from forecast_ai.config import ForecastConfig
from forecast_ai.config_store import ConfigStore
from forecast_ai.sources.exa import ExaSource
from forecast_ai.sources.perigon import PerigonSource


@pytest.mark.asyncio
async def test_perigon_returns_structured_article_evidence():
    source = PerigonSource(api_key="perigon-test", enabled=True)
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {
        "articles": [{
            "title": "Central bank decision",
            "description": "The central bank announced its latest policy decision.",
            "url": "https://news.example/central-bank",
            "pubDate": "2026-08-21T12:00:00Z",
            "authorsByline": "A. Reporter",
            "source": {"domain": "news.example"},
            "sentiment": {"positive": 0.4, "negative": 0.1},
        }]
    }

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)) as get:
        evidence = await source.fetch("Will the Fed cut rates?", limit=3)

    assert get.await_args.kwargs["params"]["q"] == "Will the Fed cut rates?"
    assert get.await_args.kwargs["params"]["size"] == 3
    assert evidence[0].source_name == "Perigon News"
    assert evidence[0].metadata["provider"] == "Perigon"
    assert evidence[0].metadata["publisher"] == "news.example"


@pytest.mark.asyncio
async def test_exa_returns_cited_deep_research_evidence():
    source = ExaSource(api_key="exa-test", enabled=True)
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {
        "requestId": "exa-request",
        "results": [{
            "title": "Official policy release",
            "url": "https://official.example/release",
            "highlights": ["A relevant official development."],
            "publishedDate": "2026-08-21T12:00:00Z",
            "author": "Official office",
        }],
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post:
        evidence = await source.fetch("What is the policy timeline?", limit=3)

    payload = post.await_args.kwargs["json"]
    assert payload["query"] == "What is the policy timeline?"
    assert payload["type"] == "auto"
    assert payload["contents"]["highlights"]["maxCharacters"] == 1200
    assert evidence[0].source_name == "Exa Deep Research"
    assert evidence[0].metadata["provider"] == "Exa"
    assert evidence[0].url == "https://official.example/release"


def test_perigon_and_exa_config_env_overrides(monkeypatch):
    monkeypatch.setenv("PERIGON_ENABLED", "true")
    monkeypatch.setenv("PERIGON_API_KEY", "perigon-env-key")
    monkeypatch.setenv("PERIGON_STORIES_ENABLED", "true")
    monkeypatch.setenv("EXA_ENABLED", "true")
    monkeypatch.setenv("EXA_API_KEY", "exa-env-key")
    monkeypatch.setenv("EXA_SEARCH_TYPE", "fast")

    config: ForecastConfig = ConfigStore().load_config()

    assert config.perigon.enabled is True
    assert config.perigon.api_key == "perigon-env-key"
    assert config.perigon.stories_enabled is True
    assert config.exa.enabled is True
    assert config.exa.api_key == "exa-env-key"
    assert config.exa.search_type == "fast"
