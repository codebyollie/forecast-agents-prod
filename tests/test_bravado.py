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
                        "is_mm_bot": False,
                    },
                    {
                        "trader": "0x2222222222222222222222222222222222222222",
                        "rank": 2,
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
    assert len([path for path, _ in calls if "/positions/active" in path]) == 1


@pytest.mark.asyncio
async def test_bravado_reports_no_match_without_inventing_a_signal(monkeypatch):
    source = BravadoSource(api_token="test-token", scan_limit=1)

    async def fake_get(path, params=None):
        if path == "leaderboard":
            return {"results": [{
                "trader": "0x1111111111111111111111111111111111111111",
                "rank": 1,
                "is_mm_bot": False,
            }]}
        return {"positions": []}

    monkeypatch.setattr(source, "_get", fake_get)
    evidence = await source.fetch_market_trader_intelligence("market", "Question")

    assert evidence[0].metadata["signals"]["smart_money_direction"] == "NO_MATCH"
    assert evidence[0].metadata["signals"]["smart_money_wallet_count"] == 0
    assert evidence[0].relevance_score < 0.5
