"""Read-only Robinhood Stock Token intelligence for RWA impact cards."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx


_THEME_SYMBOLS = {
    "fed": ("SPY", "QQQ", "TLT", "JPM", "BAC"),
    "rate": ("SPY", "QQQ", "TLT", "JPM", "BAC"),
    "inflation": ("SPY", "TLT", "GLD", "XOM"),
    "economy": ("SPY", "QQQ", "JPM", "BAC"),
    "recession": ("SPY", "TLT", "JPM", "BAC"),
    "ai": ("NVDA", "MSFT", "GOOGL", "META"),
    "artificial intelligence": ("NVDA", "MSFT", "GOOGL", "META"),
    "bitcoin": ("COIN", "MSTR"),
    "crypto": ("COIN", "MSTR"),
    "oil": ("XOM", "CVX"),
    "energy": ("XOM", "CVX"),
}


class RobinhoodStockTokenClient:
    def __init__(self, base_url: str = "https://api.robinhood.com/rhj", timeout_seconds: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._assets: List[Dict[str, Any]] = []
        self._assets_at = 0.0
        self._quotes: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def _get_json(self, path: str) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/{path.lstrip('/')}",
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def assets(self) -> List[Dict[str, Any]]:
        if self._assets and time.time() - self._assets_at < 86_400:
            return self._assets
        data = await self._get_json("assets")
        assets = data.get("assets")
        self._assets = assets if isinstance(assets, list) else []
        self._assets_at = time.time()
        return self._assets

    async def quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        normalized = symbol.upper()
        cached = self._quotes.get(normalized)
        if cached and time.time() - cached[0] < 15:
            return cached[1]
        data = await self._get_json(f"prices/{normalized}")
        quotes = data.get("quotes")
        quote = quotes[0] if isinstance(quotes, list) and quotes else None
        if isinstance(quote, dict):
            self._quotes[normalized] = (time.time(), quote)
            return quote
        return None

    @staticmethod
    def related_symbols(question: str) -> List[str]:
        lowered = question.lower()
        symbols: List[str] = []
        for keyword, candidates in _THEME_SYMBOLS.items():
            pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
            if re.search(pattern, lowered):
                symbols.extend(candidates)
        return list(dict.fromkeys(symbols))[:5]

    async def related_assets(self, question: str) -> List[Dict[str, Any]]:
        requested = self.related_symbols(question)
        if not requested:
            return []
        assets = await self.assets()
        by_symbol = {
            str(item.get("tokenSymbol") or "").upper(): item
            for item in assets
            if isinstance(item, dict)
        }
        available = [(symbol, by_symbol.get(symbol)) for symbol in requested if by_symbol.get(symbol)]

        async def safe_quote(symbol: str) -> Optional[Dict[str, Any]]:
            try:
                return await self.quote(symbol)
            except Exception:
                return None

        quotes = await asyncio.gather(*[safe_quote(symbol) for symbol, _ in available])
        related: List[Dict[str, Any]] = []
        for (symbol, asset), quote in zip(available, quotes):
            related.append({
                "symbol": symbol,
                "name": asset.get("tokenName"),
                "status": asset.get("status"),
                "logo_url": asset.get("logoUrl"),
                "current_multiplier": asset.get("currentMultiplier"),
                "deployments": asset.get("deployments") or [],
                "bid": quote.get("bid") if quote else None,
                "ask": quote.get("ask") if quote else None,
                "daily_trading_volume": quote.get("dailyTradingVolume") if quote else None,
                "generated_at": quote.get("generatedAt") if quote else None,
                "relationship": "theme_match",
            })
        return related
