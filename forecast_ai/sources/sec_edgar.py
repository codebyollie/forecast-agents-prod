"""Official SEC EDGAR filing context for Robinhood Stock Token underliers.

The source deliberately reports only recent, material filing types. It does
not infer a directional trade signal from a filing; agents receive the dated,
citable primary-source context and decide whether it matters to the market.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .base import BaseSource
from ..models.evidence import Evidence
from ..services.robinhood_stock_tokens import RobinhoodStockTokenClient


logger = logging.getLogger(__name__)

_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
_MATERIAL_FORMS = {
    "8-K": "material current report",
    "10-Q": "quarterly report",
    "10-K": "annual report",
    "4": "insider ownership filing",
}


class SecEdgarSource(BaseSource):
    """Fetch recent official company filings for mapped Robinhood Stock Tokens."""

    cache_ttl_seconds = 900

    def __init__(
        self,
        api_url: str = "https://data.sec.gov",
        user_agent: str = "",
        enabled: bool = False,
        timeout_seconds: float = 12.0,
        max_symbols: int = 3,
        lookback_days: int = 90,
    ):
        self.api_url = api_url.rstrip("/")
        self.user_agent = user_agent.strip()
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_symbols = max(1, min(max_symbols, 5))
        self.lookback_days = max(1, min(lookback_days, 365))
        self._tickers: Dict[str, str] = {}
        self._tickers_loaded_at = 0.0

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Accept": "application/json", "User-Agent": self.user_agent}

    async def _get_json(self, url: str) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self._headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def _ticker_to_cik(self) -> Dict[str, str]:
        if self._tickers and time.time() - self._tickers_loaded_at < 86_400:
            return self._tickers
        payload = await self._get_json(_TICKER_URL)
        mapping: Dict[str, str] = {}
        for item in payload.values():
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").upper().strip()
            cik = item.get("cik_str")
            if ticker and cik is not None:
                mapping[ticker] = str(cik).zfill(10)
        self._tickers = mapping
        self._tickers_loaded_at = time.time()
        return mapping

    @staticmethod
    def _filing_url(cik: str, accession: str, primary_document: str) -> str:
        accession_without_dashes = accession.replace("-", "")
        cik_without_padding = str(int(cik))
        if primary_document:
            return f"https://www.sec.gov/Archives/edgar/data/{cik_without_padding}/{accession_without_dashes}/{primary_document}"
        return f"https://www.sec.gov/Archives/edgar/data/{cik_without_padding}/{accession_without_dashes}/"

    async def _filings_for_symbol(self, symbol: str, cik: str) -> Optional[Evidence]:
        payload = await self._get_json(f"{self.api_url}/submissions/CIK{cik}.json")
        recent = payload.get("filings", {}).get("recent", {}) if isinstance(payload.get("filings"), dict) else {}
        if not isinstance(recent, dict):
            return None

        forms = recent.get("form") or []
        filing_dates = recent.get("filingDate") or []
        accessions = recent.get("accessionNumber") or []
        documents = recent.get("primaryDocument") or []
        descriptions = recent.get("primaryDocDescription") or []
        cutoff = date.today() - timedelta(days=self.lookback_days)
        filings: List[Dict[str, str]] = []
        for index, form in enumerate(forms):
            form_value = str(form or "")
            if form_value not in _MATERIAL_FORMS:
                continue
            filing_date = str(filing_dates[index] if index < len(filing_dates) else "")
            try:
                if date.fromisoformat(filing_date) < cutoff:
                    continue
            except ValueError:
                continue
            accession = str(accessions[index] if index < len(accessions) else "")
            primary_document = str(documents[index] if index < len(documents) else "")
            if not accession:
                continue
            filings.append({
                "form": form_value,
                "filing_date": filing_date,
                "description": str(descriptions[index] if index < len(descriptions) else ""),
                "url": self._filing_url(cik, accession, primary_document),
            })
            if len(filings) == 3:
                break
        if not filings:
            return None

        company = str(payload.get("name") or symbol)
        events = "; ".join(
            f"{item['form']} ({_MATERIAL_FORMS[item['form']]}) filed {item['filing_date']}"
            for item in filings
        )
        return Evidence(
            source_name="SEC EDGAR Corporate Events",
            content=(
                f"Official SEC EDGAR filing context for {symbol} ({company}): {events}. "
                "These are primary-source corporate events, not directional trading recommendations."
            ),
            relevance_score=0.88,
            title=f"{symbol}: recent SEC corporate events",
            url=filings[0]["url"],
            metadata={
                "provider": "SEC EDGAR",
                "source_type": "rwa_corporate_event",
                "symbol": symbol,
                "cik": cik,
                "filings": filings,
                "partner": False,
                "status": "active",
            },
        )

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        if not self.enabled or not self.user_agent:
            return []
        symbols = RobinhoodStockTokenClient.related_symbols(query)[: self.max_symbols]
        if not symbols:
            return []
        try:
            ticker_map = await self._ticker_to_cik()
        except Exception as exc:
            logger.warning("[SecEdgarSource] Ticker mapping request failed: %s", exc)
            return []

        async def fetch_one(symbol: str) -> Optional[Evidence]:
            cik = ticker_map.get(symbol)
            if not cik:
                return None
            try:
                return await self._filings_for_symbol(symbol, cik)
            except Exception as exc:
                logger.warning("[SecEdgarSource] %s filing request failed: %s", symbol, exc)
                return None

        results = await asyncio.gather(*[fetch_one(symbol) for symbol in symbols])
        return [item for item in results if item is not None][:limit]
