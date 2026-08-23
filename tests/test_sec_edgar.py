from unittest.mock import AsyncMock, Mock, patch

import pytest

from forecast_ai.sources.sec_edgar import SecEdgarSource


@pytest.mark.asyncio
async def test_sec_edgar_returns_recent_material_filing_for_rwa_symbol():
    source = SecEdgarSource(enabled=True, user_agent="Forecast AI contact@forai.tech", lookback_days=365)
    source.stock_tokens.matching_symbols = AsyncMock(return_value=["NVDA"])
    ticker_response = Mock()
    ticker_response.raise_for_status = Mock()
    ticker_response.json.return_value = {"0": {"ticker": "NVDA", "cik_str": 1045810}}
    filing_response = Mock()
    filing_response.raise_for_status = Mock()
    filing_response.json.return_value = {
        "name": "NVIDIA CORP",
        "filings": {"recent": {
            "form": ["8-K", "4"],
            "filingDate": ["2026-08-01", "2026-07-28"],
            "accessionNumber": ["0001045810-26-000001", "0001045810-26-000002"],
            "primaryDocument": ["earnings.htm", "xslF345X01/form4.xml"],
            "primaryDocDescription": ["Current report", "Statement of changes in beneficial ownership"],
        }},
    }

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=[ticker_response, filing_response])):
        evidence = await source.fetch("Will AI spending rise this year?")

    assert len(evidence) == 1
    assert evidence[0].metadata["provider"] == "SEC EDGAR"
    assert evidence[0].metadata["symbol"] == "NVDA"
    assert evidence[0].metadata["filings"][0]["form"] == "8-K"
    assert evidence[0].url.endswith("/earnings.htm")
