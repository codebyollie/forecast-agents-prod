"""Read-only Robinhood Stock Token intelligence for RWA impact cards."""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timezone
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
    def __init__(
        self,
        base_url: str = "https://api.robinhood.com/rhj",
        timeout_seconds: float = 15.0,
        *,
        retry_attempts: int = 3,
        last_good_ttl_seconds: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = max(1, retry_attempts)
        self.last_good_ttl_seconds = max(0.0, last_good_ttl_seconds)
        self._assets: List[Dict[str, Any]] = []
        self._assets_at = 0.0
        self._quotes: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._last_good_prices: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def _get_json(self, path: str) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        async with httpx.AsyncClient() as client:
            for attempt in range(self.retry_attempts):
                try:
                    response = await client.get(
                        f"{self.base_url}/{path.lstrip('/')}",
                        headers={"Accept": "application/json"},
                        timeout=self.timeout_seconds,
                    )
                    response.raise_for_status()
                    data = response.json()
                    return data if isinstance(data, dict) else {}
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code != 429 and exc.response.status_code < 500:
                        raise
                except (httpx.TransportError, ValueError) as exc:
                    last_error = exc
                if attempt + 1 < self.retry_attempts:
                    await asyncio.sleep(0.2 * (2**attempt))
        if last_error:
            raise last_error
        return {}

    async def assets(self) -> List[Dict[str, Any]]:
        if self._assets and time.time() - self._assets_at < 86_400:
            return self._assets
        data = await self._get_json("assets")
        assets = data.get("assets")
        self._assets = assets if isinstance(assets, list) else []
        self._assets_at = time.time()
        return self._assets

    async def quote(self, symbol: str, *, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        normalized = symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9.-]{1,20}", normalized):
            return None
        cached = self._quotes.get(normalized)
        if not force_refresh and cached and time.time() - cached[0] < 15:
            return cached[1]
        data = await self._get_json(f"prices/{normalized}")
        quotes = data.get("quotes")
        quote = quotes[0] if isinstance(quotes, list) and quotes else None
        if isinstance(quote, dict):
            self._quotes[normalized] = (time.time(), quote)
            return quote
        return None

    async def resolution_price(
        self,
        symbol: str,
        not_before: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Return the first verifiable midpoint observed at or after a horizon.

        A stale pre-horizon quote is never used. If the market is closed, the
        forecast remains pending until Robinhood publishes a newer quote.
        """
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        price = await self.current_price(symbol)
        if not price:
            return None
        source_time = datetime.fromisoformat(str(price["generated_at"]).replace("Z", "+00:00"))
        if source_time.tzinfo is None:
            source_time = source_time.replace(tzinfo=timezone.utc)
        return price if source_time >= not_before else None

    async def current_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return an official midpoint, falling back briefly to a verified quote."""
        normalized = symbol.strip().upper()
        try:
            quote = await self.quote(normalized, force_refresh=True)
        except Exception:
            quote = None
        price = self._normalize_price(normalized, quote)
        if price:
            price["quote_status"] = "live"
            price["cache_age_seconds"] = 0
            self._last_good_prices[normalized] = (time.time(), dict(price))
            return price

        cached = self._last_good_prices.get(normalized)
        if not cached:
            return None
        cache_age = max(0.0, time.time() - cached[0])
        if self.last_good_ttl_seconds <= 0 or cache_age > self.last_good_ttl_seconds:
            return None
        fallback = dict(cached[1])
        fallback["quote_status"] = "last_known_good"
        fallback["cache_age_seconds"] = round(cache_age, 3)
        return fallback

    def _normalize_price(
        self,
        symbol: str,
        quote: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Validate a Robinhood quote before it can enter the trusted cache."""
        if not quote:
            return None
        generated_at = str(quote.get("generatedAt") or quote.get("generated_at") or "").strip()
        if not generated_at:
            return None
        try:
            source_time = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if source_time.tzinfo is None:
            source_time = source_time.replace(tzinfo=timezone.utc)
        try:
            bid = float(quote.get("bid"))
            ask = float(quote.get("ask"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0 or ask < bid:
            return None
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "midpoint": round((bid + ask) / 2, 8),
            "currency": str(quote.get("currency") or "USD"),
            "generated_at": source_time.isoformat(),
            "source": f"{self.base_url}/prices/{symbol.upper()}",
        }

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
                "daily_high": quote.get("dailyHigh") or quote.get("daily_high"),
                "daily_low": quote.get("dailyLow") or quote.get("daily_low"),
                "daily_trading_volume": quote.get("dailyTradingVolume") or quote.get("daily_trading_volume"),
                "is_trading_halt": quote.get("isTradingHalt") if "isTradingHalt" in quote else quote.get("is_trading_halt"),
                "generated_at": quote.get("generatedAt") or quote.get("generated_at"),
                "status": quote.get("quote_status") or ("live" if quote else "unavailable"),
                "cache_age_seconds": quote.get("cache_age_seconds"),
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
