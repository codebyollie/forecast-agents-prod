import pytest

from forecast_ai.sources.bravado import BravadoSource


@pytest.mark.asyncio
async def test_bravado_matches_ranked_non_bot_positions(monkeypatch):
    source = BravadoSource(
        api_token="test-token",
        leaderboard_window="30d",
        scan_limit=3,
        min_trades=10,
    )
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        if path == "leaderboard":
            return {
                "results": [
                    {
                        "trader": "1111111111111111111111111111111111111111",
                        "rank": 1,
                        "username": "alpha",
                        "win_rate": "0.72",
                        "total_pnl": "25000",
                        "trades": 80,
                        "active_positions": 2,
                        "is_mm_bot": False,
                    },
                    {
                        "trader": "0x2222222222222222222222222222222222222222",
                        "rank": 2,
                        "active_positions": 10,
                        "is_mm_bot": True,
                    },
                ]
            }
        return {
            "positions": [{
                "market_id": "0xcondition",
                "question": "Will the Fed cut rates?",
                "outcome": "Yes",
                "value_usdc": "12500.50",
                "shares": "15000",
            }]
        }

    monkeypatch.setattr(source, "_get", fake_get)
    evidence = await source.fetch_market_trader_intelligence(
        market_id="market-slug",
        condition_id="0xcondition",
        question="Will the Fed cut rates?",
    )

    assert len(evidence) == 1
    signals = evidence[0].metadata["signals"]
    assert signals["smart_money_wallet_count"] == 1
    assert signals["smart_money_direction"] == "YES"
    assert signals["smart_money_position_value_usdc"] == 12500.5
    assert signals["smart_money_average_win_rate"] == 72.0
    assert signals["smart_money_wallets"] == ["0x1111111111111111111111111111111111111111"]
    assert calls[0][0] == "leaderboard"
    assert calls[0][1]["exclude_bots"] == "true"
    assert calls[0][1]["limit"] == 50
    assert "basis" not in calls[0][1]
    assert "income" not in calls[0][1]
    assert len([path for path, _ in calls if "/positions/active" in path]) == 1
    positions_call = next(call for call in calls if "/positions/active" in call[0])
    assert positions_call[1] == {"limit": 500, "offset": 0}


@pytest.mark.asyncio
async def test_bravado_reports_no_match_without_inventing_a_signal(monkeypatch):
    source = BravadoSource(api_token="test-token", scan_limit=1)

    async def fake_get(path, params=None):
        if path == "leaderboard":
            return {"results": [{
                "trader": "0x1111111111111111111111111111111111111111",
                "rank": 1,
                "active_positions": 1,
                "is_mm_bot": False,
            }]}
        return {"positions": []}

    monkeypatch.setattr(source, "_get", fake_get)
    evidence = await source.fetch_market_trader_intelligence("market", "Question")

    assert evidence[0].metadata["signals"]["smart_money_direction"] == "NO_MATCH"
    assert evidence[0].metadata["signals"]["smart_money_wallet_count"] == 0
    assert evidence[0].relevance_score < 0.5


@pytest.mark.asyncio
async def test_bravado_skips_wallets_without_active_positions(monkeypatch):
    source = BravadoSource(api_token="test-token", scan_limit=2)
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        if path == "leaderboard":
            return {"results": [
                {
                    "trader": "0x1111111111111111111111111111111111111111",
                    "active_positions": 0,
                    "is_mm_bot": False,
                },
                {
                    "trader": "0x2222222222222222222222222222222222222222",
                    "active_positions": 3,
                    "is_mm_bot": False,
                },
            ]}
        return {"positions": []}

    monkeypatch.setattr(source, "_get", fake_get)
    evidence = await source.fetch_market_trader_intelligence("market", "Question")

    position_calls = [path for path, _ in calls if "/positions/active" in path]
    assert position_calls == [
        "traders/0x2222222222222222222222222222222222222222/positions/active"
    ]
    assert evidence[0].metadata["signals"]["leaderboard_wallets_considered"] == 2
    assert evidence[0].metadata["signals"]["leaderboard_wallets_scanned"] == 1


@pytest.mark.asyncio
async def test_bravado_collects_paginated_active_positions(monkeypatch):
    source = BravadoSource(api_token="test-token")
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        if params["offset"] == 0:
            return {
                "positions": [{"market_id": f"market-{index}"} for index in range(500)],
                "total": 501,
            }
        return {
            "positions": [{"market_id": "target", "question": "Target question"}],
            "total": 501,
        }

    monkeypatch.setattr(source, "_get", fake_get)
    payload = await source._active_positions(
        "0x1111111111111111111111111111111111111111"
    )

    assert len(payload["positions"]) == 501
    assert calls == [
        (
            "traders/0x1111111111111111111111111111111111111111/positions/active",
            {"limit": 500, "offset": 0},
        ),
        (
            "traders/0x1111111111111111111111111111111111111111/positions/active",
            {"limit": 500, "offset": 500},
        ),
    ]
