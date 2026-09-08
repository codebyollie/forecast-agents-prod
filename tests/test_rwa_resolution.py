import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from forecast_ai.pipelines.forecast import ForecastPipeline
from forecast_ai.services.robinhood_stock_tokens import RobinhoodStockTokenClient


@pytest.mark.asyncio
async def test_resolution_price_rejects_a_stale_pre_horizon_quote():
    client = RobinhoodStockTokenClient()
    client.quote = AsyncMock(return_value={
        "bid": "99",
        "ask": "101",
        "currency": "USD",
        "generatedAt": "2026-09-01T11:59:59Z",
    })

    result = await client.resolution_price(
        "NVDA", datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    )

    assert result is None
    client.quote.assert_awaited_once_with("NVDA", force_refresh=True)


@pytest.mark.asyncio
async def test_resolution_price_uses_verified_post_horizon_midpoint():
    client = RobinhoodStockTokenClient("https://api.robinhood.com/rhj")
    client.quote = AsyncMock(return_value={
        "bid": "104.20",
        "ask": "104.40",
        "currency": "USD",
        "generatedAt": "2026-09-01T12:00:01Z",
    })

    result = await client.resolution_price(
        "NVDA", datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    )

    assert result == {
        "symbol": "NVDA",
        "bid": 104.2,
        "ask": 104.4,
        "midpoint": 104.3,
        "currency": "USD",
        "generated_at": "2026-09-01T12:00:01+00:00",
        "source": "https://api.robinhood.com/rhj/prices/NVDA",
        "quote_status": "live",
        "cache_age_seconds": 0,
    }


@pytest.mark.asyncio
async def test_current_price_uses_recent_last_known_good_when_live_quote_fails():
    client = RobinhoodStockTokenClient(last_good_ttl_seconds=300)
    client.quote = AsyncMock(side_effect=[{
        "bid": "99",
        "ask": "101",
        "currency": "USD",
        "generatedAt": "2026-09-01T12:00:01Z",
    }, RuntimeError("temporary upstream failure")])

    live = await client.current_price("NVDA")
    fallback = await client.current_price("NVDA")

    assert live["quote_status"] == "live"
    assert fallback["midpoint"] == 100.0
    assert fallback["quote_status"] == "last_known_good"
    assert fallback["generated_at"] == "2026-09-01T12:00:01+00:00"


@pytest.mark.asyncio
async def test_current_price_rejects_expired_last_known_good_quote():
    client = RobinhoodStockTokenClient(last_good_ttl_seconds=0)
    client.quote = AsyncMock(side_effect=[{
        "bid": "99",
        "ask": "101",
        "currency": "USD",
        "generatedAt": "2026-09-01T12:00:01Z",
    }, RuntimeError("temporary upstream failure")])

    await client.current_price("NVDA")
    await asyncio.sleep(0)

    assert await client.current_price("NVDA") is None


@pytest.mark.asyncio
async def test_rwa_runs_with_same_market_id_resolve_by_forecast_id():
    due_at = "2026-09-01T12:00:00+00:00"
    rows = [
        {
            "id": "row-1",
            "analysis_id": "analysis-1",
            "forecast_id": "forecast-1",
            "market_id": "rwa-nvda-30d",
            "venue": "Robinhood Chain",
            "closes_at": due_at,
            "commitments": [
                {"agent_name": "agent-x:consensus", "probability_bps": 8000},
                {"agent_name": "agent-x:market", "probability_bps": 7000},
            ],
        },
        {
            "id": "row-2",
            "analysis_id": "analysis-2",
            "forecast_id": "forecast-2",
            "market_id": "rwa-nvda-30d",
            "venue": "Robinhood Chain",
            "closes_at": due_at,
            "commitments": [
                {"agent_name": "consensus", "probability_bps": 4000},
                {"agent_name": "research", "probability_bps": 3000},
            ],
        },
    ]
    analyses = {
        "analysis-1": {"analysis_mode": "rwa", "asset_symbol": "NVDA", "reference_price": 100.0},
        "analysis-2": {"analysis_mode": "rwa", "asset_symbol": "NVDA", "reference_price": 200.0},
    }
    outbox = Mock(configured=True)
    outbox.list_due_markets = AsyncMock(return_value=rows)
    outbox.get_analysis_results = AsyncMock(return_value=analyses)
    outbox.update_analysis_resolution = AsyncMock()
    outbox.queue_forecast_resolution = AsyncMock()
    stock_tokens = Mock()
    stock_tokens.resolution_price = AsyncMock(return_value={
        "midpoint": 150.0,
        "bid": 149.0,
        "ask": 151.0,
        "currency": "USD",
        "generated_at": "2026-09-01T12:00:01+00:00",
        "source": "https://api.robinhood.com/rhj/prices/NVDA",
    })
    memory = Mock()
    memory.list_forecasts.return_value = []
    memory.resolve_forecast.return_value = None

    pipeline = ForecastPipeline.__new__(ForecastPipeline)
    pipeline.memory_store = memory
    pipeline.proof_outbox = outbox
    pipeline.stock_tokens = stock_tokens

    result = await pipeline.resolve_due_forecasts(
        now=datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    )

    assert result["resolved_rwa_forecasts"] == 2
    assert result["queued_onchain"] == 2
    assert outbox.queue_forecast_resolution.await_args_list[0].args[:2] == ("forecast-1", 1)
    assert outbox.queue_forecast_resolution.await_args_list[1].args[:2] == ("forecast-2", 0)
    first_details = outbox.update_analysis_resolution.await_args_list[0].args[2]
    assert first_details["consensus_brier_score"] == 0.04
    assert first_details["agent_scores"][0]["brier_score"] == 0.09
    assert first_details["final_price"] == 150.0
    assert first_details["reference_price"] == 100.0
