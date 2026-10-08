import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forecast_ai.api import routes
from forecast_ai.models.evidence import Evidence
from forecast_ai.polymarket.models import BookLevel, PolymarketMarket, PolymarketEvent
from forecast_ai.services.live_markets import LiveMarketService, depth_quote
from forecast_ai.services.forecast_review import evidence_audit, execution_check, review_forecast


def test_depth_uses_sorted_asks_and_exposes_partial_fill():
    quote = depth_quote([BookLevel(.8, 10), BookLevel(.4, 10)], 8)
    assert quote["best_ask"] == .4
    assert quote["full_depth"] is True
    assert quote["average_price"] == pytest.approx(8 / 15, abs=1e-6)
    partial = depth_quote([BookLevel(.5, 2)], 10)
    assert partial["full_depth"] is False and partial["filled_notional"] == 1
    assert depth_quote([BookLevel(float("nan"), 100)], 10)["average_price"] is None


def test_unknown_fees_never_produce_a_verified_net_edge():
    quote = {"status": "live", "yes": {"average_price": .4, "full_depth": True},
             "no": {"average_price": .6, "full_depth": True}}
    check = execution_check(.7, quote)
    assert check["side"] == "YES"
    assert check["gross_edge"] == pytest.approx(.3)
    assert check["net_edge"] is None
    assert "fees_unverified" in check["flags"]


def test_audit_counts_domains_not_duplicate_links():
    ev = [Evidence("news", "content", url="https://a.test/1")] * 3
    result = evidence_audit(ev, [SimpleNamespace(citations=[{"url": "https://invented.test"}])])
    assert result["domain_count"] == 1 and result["duplicate_urls"] == 2
    assert result["status"] == "concern" and "unmatched_citations" in result["flags"]


@pytest.mark.asyncio
async def test_event_with_multiple_contracts_requires_selection():
    gamma = AsyncMock()
    gamma.fetch_market_by_slug.return_value = None
    gamma.fetch_event_by_slug.return_value = SimpleNamespace(markets=[1, 2])
    service = LiveMarketService(gamma, AsyncMock(), AsyncMock())
    assert (await service.snapshot("event-name", "Polymarket"))["status"] == "select_contract"


@pytest.mark.asyncio
async def test_quote_cache_coalesces_and_keeps_notional_separate():
    service = LiveMarketService(AsyncMock(), AsyncMock(), AsyncMock())
    service._fetch = AsyncMock(return_value={"status": "live"})
    await asyncio.gather(*(service.snapshot("contract", "Kalshi", 100) for _ in range(4)))
    assert service._fetch.await_count == 1
    await service.snapshot("contract", "Kalshi", 200)
    assert service._fetch.await_count == 2


@pytest.mark.asyncio
async def test_decimal_strike_ticker_is_allowed_but_path_traversal_is_not():
    service = LiveMarketService(AsyncMock(), AsyncMock(), AsyncMock())
    service._fetch = AsyncMock(return_value={"status": "live"})
    ticker = "KXTEMPMIAH-26OCT0812-T88.99"
    assert (await service.snapshot(ticker, "Kalshi"))["status"] == "live"
    service._fetch.assert_awaited_once_with(ticker, "kalshi", 100)
    for invalid in ("..", "../secret", "contract?other=1", "contract/extra"):
        assert (await service.snapshot(invalid, "Kalshi"))["status"] == "unsupported"


@pytest.mark.asyncio
async def test_non_yes_no_market_is_not_silently_priced():
    gamma = AsyncMock()
    gamma.fetch_market_by_slug.return_value = SimpleNamespace(active=True, closed=False, tokens=[{"outcome": "Team A", "token_id": "1"}, {"outcome": "Team B", "token_id": "2"}])
    service = LiveMarketService(gamma, AsyncMock(), AsyncMock())
    assert (await service.snapshot("sports-contract", "Polymarket"))["status"] == "unsupported_outcomes"


@pytest.mark.asyncio
async def test_reviews_do_not_mutate_consensus_and_reject_invented_sources():
    prediction = SimpleNamespace(agent_name="news", probability=.6, reasoning="reason", counter_signals=[], citations=[])
    result = SimpleNamespace(probability=.6, individual_predictions=[prediction])
    provider = SimpleNamespace(generate_with_fallback=AsyncMock(return_value=json.dumps({"status": "checked", "summary": "Reviewed", "evidence_ids": ["E999"]})))
    review = await review_forecast(result, [Evidence("news", "data", url="https://source.test")], "Question", {}, provider, SimpleNamespace(default_provider="openai"))
    assert result.probability == .6
    assert review["status"] == "incomplete"
    assert review["checks"][0]["status"] == "unavailable"
    assert review["checks"][2]["status"] == "insufficient_evidence"


def test_live_api_requires_key_and_bounds_batch_and_notional(monkeypatch):
    fake = SimpleNamespace(config=SimpleNamespace(server=SimpleNamespace(api_key="test-only")), live_markets=SimpleNamespace(snapshot=AsyncMock(return_value={"status": "live"})))
    monkeypatch.setattr(routes, "_pipeline", fake)
    app = FastAPI(); app.include_router(routes.router)
    with TestClient(app) as client:
        payload = {"markets": [{"market_id": "contract", "venue": "kalshi"}]}
        assert client.post("/markets/live", json=payload).status_code == 401
        headers = {"x-api-key": "test-only"}
        assert client.post("/markets/live", json=payload, headers=headers).status_code == 200
        assert client.post("/markets/live", json={"markets": [{"market_id": "KXTEMPMIAH-26OCT0812-T88.99", "venue": "kalshi"}]}, headers=headers).status_code == 200
        assert client.post("/markets/live", json={**payload, "notional": -1}, headers=headers).status_code == 422
        assert client.post("/markets/live", json={"markets": payload["markets"] * 13}, headers=headers).status_code == 422
        assert client.post("/markets/live", json={"markets": [{"market_id": "../secret", "venue": "kalshi"}]}, headers=headers).status_code == 422
