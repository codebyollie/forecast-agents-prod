from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from forecast_ai.services.robinhood_crypto import RobinhoodCryptoClient


def _coin(symbol: str = "PONS", price: float = 0.000042):
    return {
        "rank": 1,
        "symbol": symbol,
        "name": "Pons",
        "contract_address": "0x39dbed3a2bd333467115de45665cc57f813c4571",
        "price": price,
        "market_cap": 1_000_000,
        "volume_24h": 100_000,
        "liquidity": 250_000,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://www.geckoterminal.com/robinhood/tokens/0x39dbed3a2bd333467115de45665cc57f813c4571",
        "quote_status": "live",
    }


def test_catalog_excludes_stock_tokens_and_wrapped_quote_assets():
    assert RobinhoodCryptoClient._is_ecosystem_coin(_coin()) is True
    assert RobinhoodCryptoClient._is_ecosystem_coin({**_coin(), "symbol": "WETH"}) is False
    assert RobinhoodCryptoClient._is_ecosystem_coin({**_coin(), "name": "NVIDIA Robinhood Token"}) is False


def test_coin_risk_snapshot_exposes_liquidity_and_turnover_quality():
    healthy = RobinhoodCryptoClient.risk_snapshot(_coin())
    assert healthy["market_quality_score"] == 100
    assert healthy["risk_level"] == "lower"
    assert healthy["liquidity_to_market_cap_pct"] == 25
    assert healthy["turnover_24h_pct"] == 10

    thin = RobinhoodCryptoClient.risk_snapshot({
        **_coin(),
        "liquidity": 5_000,
        "market_cap": 5_000_000,
        "volume_24h": 50_000,
        "change_24h": 70,
        "quote_status": "last_known_good",
    })
    assert thin["risk_level"] == "high"
    assert "very_low_liquidity" in thin["flags"]
    assert "extreme_24h_move" in thin["flags"]
    assert "stale_quote_fallback" in thin["flags"]


@pytest.mark.asyncio
async def test_catalog_caches_live_top_ten_and_falls_back_to_last_good():
    client = RobinhoodCryptoClient(ttl_seconds=30)
    rows = [_coin()]
    client._fetch_pools = AsyncMock(return_value=rows)

    first = await client.markets()
    second = await client.markets()

    assert first[0]["symbol"] == "PONS"
    assert client._fetch_pools.await_count == 1
    client._market_cache = (0, first)
    client._fetch_pools = AsyncMock(side_effect=RuntimeError("temporary provider failure"))
    fallback = await client.markets()
    assert fallback[0]["quote_status"] == "last_known_good"


@pytest.mark.asyncio
async def test_forced_quotes_share_a_short_refresh_window():
    client = RobinhoodCryptoClient(ttl_seconds=30)
    client._fetch_pools = AsyncMock(return_value=[_coin()])

    await client.markets(force=True)
    await client.markets(force=True)

    assert client._fetch_pools.await_count == 1


@pytest.mark.asyncio
async def test_coin_resolution_requires_a_post_horizon_quote():
    client = RobinhoodCryptoClient()
    quote = _coin()
    client._fetch_pools = AsyncMock(return_value=[quote])

    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert await client.resolution_price("PONS", future) is None

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    resolved = await client.resolution_price("PONS", past)
    assert resolved is not None
    assert resolved["midpoint"] == quote["price"]
