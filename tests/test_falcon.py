from unittest.mock import AsyncMock, patch

import pytest

from forecast_ai.config import ForecastConfig
from forecast_ai.config_store import ConfigStore
from forecast_ai.sources.falcon import FalconSource, get_falcon_runtime_status


@pytest.mark.asyncio
async def test_falcon_market_intelligence_uses_unified_endpoint_contract():
    source = FalconSource(
        api_token="falcon-test-token",
        api_url="https://retriever.falconapi.net/api/v2/semantic/retrieve/parameterized",
    )
    response = AsyncMock()
    response.status_code = 200
    response.json = lambda: {"data": {"results": [{"liquidity": 1000, "trend": "up"}]}}

    with patch("httpx.AsyncClient.post", return_value=response) as post:
        evidence = await source.fetch_market_intelligence(
            market_id="fed-rate-cut-2026",
            venue="Polymarket",
        )

    payload = post.await_args.kwargs["json"]
    assert payload["agent_id"] == 575
    assert payload["params"] == {"market_slug": "fed-rate-cut-2026"}
    assert payload["formatter_config"] == {"format_type": "raw"}
    assert evidence[0].metadata["provider"] == "Falcon"
    assert evidence[0].metadata["status"] == "active"
    assert get_falcon_runtime_status()["status"] == "active"


@pytest.mark.asyncio
async def test_falcon_kalshi_uses_market_agent_without_social_call():
    source = FalconSource(
        api_token="falcon-test-token",
        api_url="https://retriever.falconapi.net/api/v2/semantic/retrieve/parameterized",
        social_enabled=True,
    )
    response = AsyncMock()
    response.status_code = 200
    response.json = lambda: {"data": {"results": [{"ticker": "KXFED"}]}}

    with patch("httpx.AsyncClient.post", return_value=response) as post:
        evidence = await source.fetch_market_intelligence("KXFED", "Kalshi")

    assert post.await_count == 1
    assert post.await_args.kwargs["json"]["agent_id"] == 565
    assert post.await_args.kwargs["json"]["params"]["ticker"] == "KXFED"
    assert len(evidence) == 1


def test_falcon_config_env_overrides(monkeypatch):
    monkeypatch.setenv("FALCON_API_TOKEN", "falcon-env-token")
    monkeypatch.setenv("FALCON_ENABLED", "true")
    monkeypatch.setenv("FALCON_SOCIAL_ENABLED", "true")

    config: ForecastConfig = ConfigStore().load_config()

    assert config.falcon.api_token == "falcon-env-token"
    assert config.falcon.enabled is True
    assert config.falcon.social_enabled is True
