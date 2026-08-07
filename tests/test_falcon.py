from unittest.mock import AsyncMock, patch

import pytest

from forecast_ai.config import ForecastConfig
from forecast_ai.config_store import ConfigStore
from forecast_ai.sources.falcon import FalconError, FalconSource, get_falcon_runtime_status


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
            condition_id="0xcondition",
        )

    payload = post.await_args.kwargs["json"]
    assert payload["agent_id"] == 575
    assert payload["params"] == {"condition_id": "0xcondition"}
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
    monkeypatch.setenv("FALCON_SMART_MONEY_ENABLED", "true")

    config: ForecastConfig = ConfigStore().load_config()

    assert config.falcon.api_token == "falcon-env-token"
    assert config.falcon.enabled is True
    assert config.falcon.social_enabled is True
    assert config.falcon.smart_money_enabled is True


@pytest.mark.asyncio
async def test_falcon_smart_money_joins_ranked_wallets_to_selected_market_trades():
    source = FalconSource(
        api_token="falcon-test-token",
        api_url="https://retriever.falconapi.net/api/v2/semantic/retrieve/parameterized",
        smart_money_enabled=True,
    )
    source._retrieve = AsyncMock(side_effect=[
        {"data": {"results": [{"liquidity": 1000}]}},
        {"data": {"results": [{"wallet": "0xabc", "h_score": 90}]}},
        {"data": {"results": [{"wallet_proxy": "0xabc", "side": "BUY"}]}},
    ])

    evidence = await source.fetch_market_intelligence(
        "fed-rate-cut",
        "Polymarket",
        condition_id="0xcondition",
    )

    smart_money = next(item for item in evidence if item.source_name == "falcon_smart_money")
    assert smart_money.metadata["status"] == "active"
    assert smart_money.metadata["signals"]["smart_money_wallet_count"] == 1
    assert source._retrieve.await_args_list[1].args[0] == 584
    assert source._retrieve.await_args_list[2].args[0] == 556


@pytest.mark.asyncio
async def test_optional_falcon_layers_fail_independently_from_market_insights():
    source = FalconSource(
        api_token="falcon-test-token",
        api_url="https://retriever.falconapi.net/api/v2/semantic/retrieve/parameterized",
        smart_money_enabled=True,
        social_enabled=True,
    )
    source._retrieve = AsyncMock(side_effect=[
        {"data": {"results": [{"liquidity": 1000}]}},
        FalconError(400, "smart money parameters rejected"),
        FalconError(400, "social parameters rejected"),
    ])

    evidence = await source.fetch_market_intelligence(
        "fed-rate-cut",
        "Polymarket",
        condition_id="0xcondition",
    )

    assert evidence[0].source_name == "falcon_market_intelligence"
    assert evidence[0].metadata["status"] == "active"
    assert any(item.source_name == "falcon_smart_money_status" for item in evidence)
    assert any(item.source_name == "falcon_social_status" for item in evidence)


@pytest.mark.asyncio
async def test_social_pulse_uses_documented_keyword_format():
    source = FalconSource(
        api_token="falcon-test-token",
        api_url="https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized",
        social_enabled=True,
    )
    source._retrieve = AsyncMock(side_effect=[
        {"data": {"results": [{"liquidity": 1000}]}},
        {"data": {"results": [{"acceleration": 1.7, "tweet_count": 120}]}},
    ])

    evidence = await source.fetch_market_intelligence(
        "will-nato-eu-troops-fight-in-ukraine-before-december-2026",
        "Polymarket",
        condition_id="0xcondition",
    )

    social_call = source._retrieve.await_args_list[1]
    assert social_call.args[0] == 585
    assert social_call.args[1]["keywords"] == "{nato,troops,fight,ukraine,2026}"
    assert social_call.args[1]["hours_back"] == "24"
    social = next(item for item in evidence if item.source_name == "falcon_social_pulse")
    assert social.metadata["signals"]["acceleration"] == 1.7
