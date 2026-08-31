from unittest.mock import AsyncMock, Mock, patch

import pytest

from forecast_ai.proof.outbox import SupabaseProofOutbox


@pytest.mark.asyncio
async def test_durable_track_record_uses_only_verified_resolutions():
    response = Mock(status_code=200)
    response.json.return_value = [
        {
            "forecast_id": "one",
            "category": "Politics",
            "status": "verified",
            "resolution_status": "verified",
            "outcome": True,
            "commitments": [
                {"agent_name": "studio-1:consensus", "probability_bps": 6000},
                {"agent_name": "studio-1:market", "probability_bps": 7000},
            ],
        },
        {
            "forecast_id": "two",
            "category": "Crypto",
            "status": "submitted",
            "resolution_status": None,
            "outcome": None,
            "commitments": [],
        },
    ]
    outbox = SupabaseProofOutbox(
        "https://example.supabase.co",
        "service-key",
        chain_id=46630,
        registry_address="0xABC",
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response) as get:
        result = await outbox.get_track_record()

    assert result["source"] == "onchain_outbox"
    assert result["resolved_forecasts"] == 1
    assert result["pending_forecasts"] == 0
    assert result["average_brier_score"] == 0.16
    assert result["agents"]["studio-1:market"]["average_brier_score"] == 0.09
    assert result["categories"]["Politics"]["average_brier_score"] == 0.16
    assert get.await_args.kwargs["params"]["chain_id"] == "eq.46630"
    assert get.await_args.kwargs["params"]["registry_address"] == "eq.0xabc"


@pytest.mark.asyncio
async def test_due_market_query_is_network_scoped():
    response = Mock(status_code=200)
    response.json.return_value = []
    outbox = SupabaseProofOutbox(
        "https://example.supabase.co",
        "service-key",
        chain_id=4663,
        registry_address="0xDEF",
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response) as get:
        assert await outbox.list_due_markets() == []

    params = get.await_args.kwargs["params"]
    assert params["status"] == "eq.verified"
    assert params["resolution_status"] == "is.null"
    assert params["chain_id"] == "eq.4663"
    assert params["registry_address"] == "eq.0xdef"
    assert "forecast_id" in params["select"]
    assert "analysis_id" in params["select"]


@pytest.mark.asyncio
async def test_verified_resolution_updates_result_and_agent_scores():
    analysis_response = Mock(status_code=200)
    analysis_response.json.return_value = [{
        "result": {
            "analysis_mode": "rwa",
            "resolution": {"resolution_type": "rwa_price", "final_price": 110.0},
            "proof": {"status": "verified_onchain"},
        }
    }]
    patch_response = Mock(status_code=204, text="")
    row = {
        "id": "row-1",
        "analysis_id": "analysis-1",
        "outcome": True,
        "commitments": [
            {"agent_name": "studio:consensus", "probability_bps": 8000},
            {"agent_name": "studio:market", "probability_bps": 7000},
        ],
    }
    outbox = SupabaseProofOutbox("https://example.supabase.co", "service-key")

    with (
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=analysis_response),
        patch("httpx.AsyncClient.patch", new_callable=AsyncMock, return_value=patch_response) as patch_request,
    ):
        await outbox.mark_resolution_verified(row, "0xtx", 123)

    result = patch_request.await_args_list[1].kwargs["json"]["result"]
    assert result["proof"]["status"] == "resolved_onchain"
    assert result["resolution"]["status"] == "resolved_onchain"
    assert result["resolution"]["consensus_brier_score"] == 0.04
    assert result["resolution"]["agent_scores"][0]["brier_score"] == 0.09
