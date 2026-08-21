"""FRED macroeconomic series source.

Only macro-relevant questions issue a request, so the source does not add
unrelated economic data to politics, entertainment or technology markets.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List

import httpx

from .base import BaseSource
from ..models.evidence import Evidence


logger = logging.getLogger(__name__)

_SERIES_BY_TOPIC: Dict[str, tuple[str, str]] = {
    "fed": ("FEDFUNDS", "Effective Federal Funds Rate"),
    "interest rate": ("FEDFUNDS", "Effective Federal Funds Rate"),
    "rate cut": ("FEDFUNDS", "Effective Federal Funds Rate"),
    "rate hike": ("FEDFUNDS", "Effective Federal Funds Rate"),
    "inflation": ("CPIAUCSL", "Consumer Price Index for All Urban Consumers"),
    "cpi": ("CPIAUCSL", "Consumer Price Index for All Urban Consumers"),
    "unemployment": ("UNRATE", "Unemployment Rate"),
    "jobs": ("PAYEMS", "All Employees, Total Nonfarm"),
    "payroll": ("PAYEMS", "All Employees, Total Nonfarm"),
    "gdp": ("GDP", "Gross Domestic Product"),
    "recession": ("T10Y2Y", "10-Year Treasury Constant Maturity Minus 2-Year Treasury"),
    "yield": ("DGS10", "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity"),
    "treasury": ("DGS10", "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity"),
}


class FredSource(BaseSource):
    cache_ttl_seconds = 3600

    def __init__(
        self,
        api_key: str = "",
        api_url: str = "https://api.stlouisfed.org/fred",
        enabled: bool = False,
        timeout_seconds: float = 12.0,
    ):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def relevant_series(query: str) -> List[tuple[str, str]]:
        lowered = query.lower()
        series = [value for keyword, value in _SERIES_BY_TOPIC.items() if keyword in lowered]
        return list(dict.fromkeys(series))[:3]

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        if not self.enabled or not self.api_key:
            return []
        requested = self.relevant_series(query)
        if not requested:
            return []

        async def fetch_series(series_id: str, series_name: str) -> Evidence | None:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"{self.api_url}/series/observations",
                        params={
                            "series_id": series_id,
                            "api_key": self.api_key,
                            "file_type": "json",
                            "sort_order": "desc",
                            "limit": 2,
                        },
                        headers={"Accept": "application/json"},
                        timeout=self.timeout_seconds,
                    )
                response.raise_for_status()
                observations = response.json().get("observations") or []
                latest = next(
                    (item for item in observations if isinstance(item, dict) and item.get("value") not in (None, ".")),
                    None,
                )
                if not isinstance(latest, dict):
                    return None
                value = latest.get("value")
                date = latest.get("date")
                return Evidence(
                    source_name="FRED Macro Data",
                    content=f"FRED official macro series: {series_name} ({series_id}) was {value} on {date}.",
                    relevance_score=0.92,
                    title=f"{series_name}: {value}",
                    url=f"https://fred.stlouisfed.org/series/{series_id}",
                    metadata={
                        "provider": "FRED",
                        "source_type": "macro_data",
                        "series_id": series_id,
                        "series_name": series_name,
                        "observation_date": date,
                        "observation_value": value,
                    },
                )
            except Exception as exc:
                logger.warning("[FredSource] %s request failed: %s", series_id, exc)
                return None

        responses = await asyncio.gather(
            *[fetch_series(series_id, series_name) for series_id, series_name in requested]
        )
        return [item for item in responses if item is not None][:limit]
