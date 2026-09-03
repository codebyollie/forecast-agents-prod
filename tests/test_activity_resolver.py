from unittest.mock import AsyncMock, patch

import httpx
import pytest

from forecast_ai.services.activity_resolver import resolve_website_activities


@pytest.mark.asyncio
async def test_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("FORECAST_ACTIVITY_CRON_SECRET", raising=False)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        assert await resolve_website_activities() is False
        post.assert_not_called()


@pytest.mark.asyncio
async def test_callback_uses_private_header(monkeypatch):
    url = "https://forai.tech/api/activities/resolve"
    monkeypatch.setenv("FORECAST_ACTIVITY_RESOLVER_URL", url)
    monkeypatch.setenv("FORECAST_ACTIVITY_CRON_SECRET", "test-only-secret")
    response = httpx.Response(200, request=httpx.Request("POST", url))
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response) as post:
        assert await resolve_website_activities() is True
        post.assert_awaited_once_with(url, headers={"x-forecast-cron-secret": "test-only-secret"})


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://forai.tech/api/activities/resolve", "https://forai.tech/other", "https://user:pass@forai.tech/api/activities/resolve"])
async def test_rejects_invalid_endpoint(monkeypatch, url):
    monkeypatch.setenv("FORECAST_ACTIVITY_RESOLVER_URL", url)
    monkeypatch.setenv("FORECAST_ACTIVITY_CRON_SECRET", "test-only-secret")
    with pytest.raises(ValueError):
        await resolve_website_activities()
