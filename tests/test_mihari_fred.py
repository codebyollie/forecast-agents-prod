from unittest.mock import AsyncMock, Mock, patch

import pytest

from forecast_ai.config import ForecastConfig
from forecast_ai.config_store import ConfigStore
from forecast_ai.sources.fred import FredSource
from forecast_ai.sources.mihari import MihariSource


@pytest.mark.asyncio
async def test_mihari_returns_rwa_context_for_matching_theme_symbols():
    source = MihariSource(enabled=True)
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {
        "data": {
            "signals": [{
                "asset": {
                    "symbol": "NVDA",
                    "multiplier": {"current": "1.0", "state": "stable"},
                    "price": {"bid": "200", "ask": "201"},
                    "multiplierAdjustedQuote": {"midpoint": "200.5", "currency": "USD"},
                },
                "event": None,
                "risk": {"level": "low", "attention": "clear", "summary": "No active corporate action."},
            }]
        }
    }

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)) as get:
        evidence = await source.fetch("Will AI spending rise this year?")

    assert get.await_args.kwargs["params"]["symbols"] == "NVDA,MSFT,GOOGL,META"
    assert evidence[0].source_name == "Mihari RWA Intelligence"
    assert evidence[0].metadata["provider"] == "Mihari"
    assert evidence[0].metadata["source_type"] == "rwa_intelligence"
    assert evidence[0].metadata["symbol"] == "NVDA"


@pytest.mark.asyncio
async def test_fred_only_queries_series_for_macro_relevant_questions():
    source = FredSource(api_key="fred-test", enabled=True)
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"observations": [{"date": "2026-08-01", "value": "3.75"}]}

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)) as get:
        evidence = await source.fetch("Will the Fed cut interest rates by December?")

    assert get.await_count == 1
    assert {call.kwargs["params"]["series_id"] for call in get.await_args_list} == {"FEDFUNDS"}
    assert evidence[0].metadata["provider"] == "FRED"
    assert evidence[0].metadata["source_type"] == "macro_data"

    assert await source.fetch("Will a new AI device launch?") == []


def test_mihari_and_fred_config_env_overrides(monkeypatch):
    monkeypatch.setenv("MIHARI_ENABLED", "true")
    monkeypatch.setenv("MIHARI_MAX_SYMBOLS", "3")
    monkeypatch.setenv("FRED_ENABLED", "true")
    monkeypatch.setenv("FRED_API_KEY", "fred-env-key")

    config: ForecastConfig = ConfigStore().load_config()

    assert config.mihari.enabled is True
    assert config.mihari.max_symbols == 3
    assert config.fred.enabled is True
    assert config.fred.api_key == "fred-env-key"
