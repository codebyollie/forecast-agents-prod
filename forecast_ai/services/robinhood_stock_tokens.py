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

    async def asset(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return one canonical Robinhood Stock Token asset by its token symbol."""
        normalized = symbol.strip().upper()
        if not normalized:
            return None
        assets = await self.assets()
        for item in assets:
            if str(item.get("tokenSymbol") or "").upper() == normalized:
                return item
        return None

    @staticmethod
    def display_name(asset: Dict[str, Any]) -> str:
        """Remove the API's product suffix without changing its canonical name."""
        raw = str(asset.get("tokenName") or asset.get("tokenSymbol") or "Stock Token")
        return raw.replace(" • Robinhood Token", "").strip()

    @classmethod
    def public_asset(cls, asset: Dict[str, Any], quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return a browser-safe, normalized Stock Token payload."""
        deployments = asset.get("deployments") if isinstance(asset.get("deployments"), list) else []
        primary_deployment = deployments[0] if deployments else {}
        quote = quote or {}
        return {
            "symbol": str(asset.get("tokenSymbol") or "").upper(),
            "name": cls.display_name(asset),
            "canonical_name": asset.get("tokenName"),
            "status": asset.get("status"),
            "logo_url": asset.get("logoUrl"),
            "current_multiplier": asset.get("currentMultiplier"),
            "pending_multiplier": asset.get("pendingMultiplier") or None,
            "token_decimals": asset.get("tokenDecimals"),
            "isin": asset.get("isin"),
            "contract_address": primary_deployment.get("contractAddress"),
            "chain_id": primary_deployment.get("chainId"),
            "network_name": primary_deployment.get("networkName") or "Robinhood Chain",
            "trading_capabilities": asset.get("tradingCapabilities") or {},
            "quote": {
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "currency": quote.get("currency") or "USD",
                "daily_high": quote.get("dailyHigh"),
                "daily_low": quote.get("dailyLow"),
                "daily_trading_volume": quote.get("dailyTradingVolume"),
                "is_trading_halt": quote.get("isTradingHalt"),
                "generated_at": quote.get("generatedAt"),
            },
        }

    @staticmethod
    def related_symbols(question: str) -> List[str]:
        lowered = question.lower()
        symbols: List[str] = []
        for keyword, candidates in _THEME_SYMBOLS.items():
            pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
            if re.search(pattern, lowered):
                symbols.extend(candidates)
        return list(dict.fromkeys(symbols))[:5]

    async def matching_symbols(self, question: str, limit: int = 5) -> List[str]:
        """Match explicit Stock Token names/tickers first, then known thematic baskets.

        This lets an RWA asset brief for any catalog symbol carry its own
        context into the Onchain, SEC and Mihari layers without pretending that
        a semantic theme match is a direct relationship.
        """
        lowered = question.lower()
        assets = await self.assets()
        explicit: List[str] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("tokenSymbol") or "").upper().strip()
            name = self.display_name(asset).lower()
            ticker_match = len(symbol) >= 2 and bool(
                re.search(rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])", lowered)
            )
            name_match = len(name) >= 4 and bool(
                re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered)
            )
            if symbol and (ticker_match or name_match):
                explicit.append(symbol)
        thematic = self.related_symbols(question)
        return list(dict.fromkeys([*explicit, *thematic]))[:max(1, limit)]

    async def related_assets(self, question: str) -> List[Dict[str, Any]]:
        assets = await self.assets()
        requested = await self.matching_symbols(question)
        if not requested:
            return []
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
