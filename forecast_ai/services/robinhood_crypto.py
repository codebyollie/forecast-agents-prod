"""Curated Robinhood Chain coin intelligence backed by live DEX pools.

This is intentionally separate from Robinhood Crypto brokerage listings. The
catalog ranks native Robinhood Chain ecosystem tokens from GeckoTerminal pool
data and keeps contract addresses attached to every quote. Stock Tokens,
stablecoins, wrapped gas assets, and leveraged products are excluded.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx


EXCLUDED_SYMBOLS = {"USDG", "WETH", "ETH"}
EXCLUDED_NAME_FRAGMENTS = ("robinhood token", "stock token")


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nested_number(value: Any, key: str) -> Optional[float]:
    return _number(value.get(key)) if isinstance(value, dict) else None


class RobinhoodCryptoClient:
    """Read-only Top 10 catalog for native Robinhood Chain ecosystem tokens."""

    def __init__(self, base_url: str = "https://api.geckoterminal.com/api/v2", ttl_seconds: int = 300):
        self.base_url = base_url.rstrip("/")
        self.ttl_seconds = max(30, ttl_seconds)
        self._market_cache: tuple[float, List[Dict[str, Any]]] = (0.0, [])
        self._last_refresh_at = 0.0
        self._last_good: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _address(resource_id: str) -> str:
        value = resource_id.split("_", 1)[-1]
        return value.lower() if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) else ""

    @staticmethod
    def _is_ecosystem_coin(token: Dict[str, Any]) -> bool:
        symbol = str(token.get("symbol") or "").upper()
        name = str(token.get("name") or "").lower()
        if not symbol or symbol in EXCLUDED_SYMBOLS:
            return False
        if any(fragment in name for fragment in EXCLUDED_NAME_FRAGMENTS):
            return False
        if re.search(r"x\d+[ls]\b", symbol.lower()) or " leveraged" in name:
            return False
        return bool(token.get("contract_address"))

    @staticmethod
    def risk_snapshot(asset: Dict[str, Any]) -> Dict[str, Any]:
        """Return deterministic market-quality metrics without implying token safety."""
        liquidity = max(0.0, _number(asset.get("liquidity")) or 0.0)
        volume = max(0.0, _number(asset.get("volume_24h")) or 0.0)
        market_cap = max(0.0, _number(asset.get("market_cap")) or 0.0)
        change = _number(asset.get("change_24h"))
        liquidity_to_cap = liquidity / market_cap if market_cap > 0 else None
        turnover = volume / market_cap if market_cap > 0 else None
        volume_to_liquidity = volume / liquidity if liquidity > 0 else None
        flags: List[str] = []
        score = 100
        if liquidity < 25_000:
            score -= 35
            flags.append("very_low_liquidity")
        elif liquidity < 100_000:
            score -= 18
            flags.append("low_liquidity")
        if liquidity_to_cap is not None and liquidity_to_cap < 0.01:
            score -= 20
            flags.append("thin_liquidity_vs_market_cap")
        elif liquidity_to_cap is not None and liquidity_to_cap < 0.05:
            score -= 10
            flags.append("limited_liquidity_depth")
        if volume_to_liquidity is not None and volume_to_liquidity > 3:
            score -= 15
            flags.append("high_turnover_vs_liquidity")
        if change is not None and abs(change) > 50:
            score -= 20
            flags.append("extreme_24h_move")
        elif change is not None and abs(change) > 25:
            score -= 10
            flags.append("high_24h_volatility")
        if str(asset.get("quote_status") or "live") != "live":
            score -= 15
            flags.append("stale_quote_fallback")
        score = max(0, min(100, score))
        return {
            "market_quality_score": score,
            "risk_level": "high" if score < 50 else "medium" if score < 75 else "lower",
            "flags": flags,
            "liquidity_to_market_cap_pct": liquidity_to_cap * 100 if liquidity_to_cap is not None else None,
            "turnover_24h_pct": turnover * 100 if turnover is not None else None,
            "volume_to_liquidity": volume_to_liquidity,
            "absolute_change_24h_pct": abs(change) if change is not None else None,
            "methodology": "Market structure only; contract, issuer and smart-contract risks require separate review.",
        }

    def _pool_candidates(self, payload: Dict[str, Any], observed_at: str) -> List[Dict[str, Any]]:
        pools = payload.get("data") if isinstance(payload, dict) else None
        included = payload.get("included") if isinstance(payload, dict) else None
        if not isinstance(pools, list) or not isinstance(included, list):
            raise ValueError("GeckoTerminal pool response is incomplete.")
        token_map: Dict[str, Dict[str, Any]] = {}
        for resource in included:
            if not isinstance(resource, dict) or resource.get("type") != "token":
                continue
            attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
            resource_id = str(resource.get("id") or "")
            token_map[resource_id] = {
                "symbol": str(attributes.get("symbol") or "").upper(),
                "name": str(attributes.get("name") or ""),
                "contract_address": self._address(resource_id),
                "image_url": attributes.get("image_url") or attributes.get("image_url_small"),
            }
        rows: List[Dict[str, Any]] = []
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            attributes = pool.get("attributes") if isinstance(pool.get("attributes"), dict) else {}
            relationships = pool.get("relationships") if isinstance(pool.get("relationships"), dict) else {}
            base_id = str((((relationships.get("base_token") or {}).get("data") or {}).get("id") or ""))
            quote_id = str((((relationships.get("quote_token") or {}).get("data") or {}).get("id") or ""))
            base = dict(token_map.get(base_id) or {})
            quote = dict(token_map.get(quote_id) or {})
            token = base if self._is_ecosystem_coin(base) else quote if self._is_ecosystem_coin(quote) else None
            if not token:
                continue
            using_base = token.get("contract_address") == base.get("contract_address")
            price = _number(attributes.get("base_token_price_usd" if using_base else "quote_token_price_usd"))
            if not price or price <= 0:
                continue
            liquidity = _number(attributes.get("reserve_in_usd")) or 0.0
            volume = _nested_number(attributes.get("volume_usd"), "h24") or 0.0
            change = _nested_number(attributes.get("price_change_percentage"), "h24")
            market_cap = _number(attributes.get("market_cap_usd"))
            fdv = _number(attributes.get("fdv_usd"))
            contract = str(token["contract_address"])
            row = {
                **token,
                "price": price,
                "market_cap": market_cap or fdv,
                "fdv": fdv,
                "volume_24h": volume,
                "liquidity": liquidity,
                "change_24h": change,
                "pool_address": self._address(str(pool.get("id") or "")),
                "quote_symbol": str((quote if using_base else base).get("symbol") or ""),
                "quote_status": "live",
                "generated_at": observed_at,
                "source": f"https://www.geckoterminal.com/robinhood/tokens/{contract}",
                "network_name": "Robinhood Chain",
                "chain_id": 4663,
                "risk_tier": "experimental",
            }
            row["risk"] = self.risk_snapshot(row)
            rows.append(row)
        return rows

    async def _fetch_pools(self) -> List[Dict[str, Any]]:
        headers = {"Accept": "application/json;version=20230203"}
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                observed_at = datetime.now(timezone.utc).isoformat()
                discovered: List[Dict[str, Any]] = []
                async with httpx.AsyncClient(timeout=18.0) as client:
                    for page in range(1, 4):
                        response = await client.get(
                            f"{self.base_url}/networks/robinhood/pools",
                            params={"page": str(page), "include": "base_token,quote_token"},
                            headers=headers,
                        )
                        response.raise_for_status()
                        discovered.extend(self._pool_candidates(response.json(), observed_at))

                # A repeated ticker with multiple contracts is ambiguous and unsafe
                # in a symbol-addressed product. Keep only its strongest pool.
                candidates: Dict[str, Dict[str, Any]] = {}
                for row in discovered:
                    key = str(row["symbol"]).upper()
                    previous = candidates.get(key)
                    if previous is None or (
                        float(row.get("liquidity") or 0), float(row.get("volume_24h") or 0)
                    ) > (
                        float(previous.get("liquidity") or 0), float(previous.get("volume_24h") or 0)
                    ):
                        candidates[key] = row
                ranked = sorted(
                    candidates.values(),
                    key=lambda item: (
                        float(item.get("market_cap") or 0),
                        float(item.get("liquidity") or 0),
                        float(item.get("volume_24h") or 0),
                    ),
                    reverse=True,
                )[:10]
                if len(ranked) < 10:
                    raise ValueError("Fewer than ten eligible Robinhood Chain coin pools were returned.")
                for rank, item in enumerate(ranked, 1):
                    item["rank"] = rank
                    self._last_good[str(item["contract_address"])] = dict(item)
                return ranked
            except Exception as exc:  # pragma: no cover - transport failures vary
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
        raise RuntimeError("Robinhood Chain market data is temporarily unavailable.") from last_error

    async def markets(self, *, force: bool = False) -> List[Dict[str, Any]]:
        expires_at, cached = self._market_cache
        # Multiple due forecasts can resolve in the same scheduler pass. A
        # short grace window prevents each symbol from spending three public
        # API calls while preserving a fresh post-horizon timestamp.
        if force and cached and time.time() - self._last_refresh_at < 15:
            return [dict(item) for item in cached]
        if not force and cached and time.time() < expires_at:
            return [dict(item) for item in cached]
        async with self._lock:
            expires_at, cached = self._market_cache
            if not force and cached and time.time() < expires_at:
                return [dict(item) for item in cached]
            try:
                result = await self._fetch_pools()
                self._market_cache = (time.time() + self.ttl_seconds, result)
                self._last_refresh_at = time.time()
                return [dict(item) for item in result]
            except Exception:
                if cached:
                    return [{**item, "quote_status": "last_known_good"} for item in cached]
                if self._last_good:
                    rows = sorted(self._last_good.values(), key=lambda item: int(item.get("rank") or 99))[:10]
                    return [{**item, "quote_status": "last_known_good"} for item in rows]
                raise

    async def asset(self, symbol: str) -> Optional[Dict[str, Any]]:
        normalized = symbol.strip().upper()
        rows = await self.markets()
        return next((dict(item) for item in rows if item.get("symbol") == normalized), None)

    async def current_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        normalized = symbol.strip().upper()
        rows = await self.markets(force=True)
        asset = next((item for item in rows if item.get("symbol") == normalized), None)
        if not asset or not asset.get("price"):
            return None
        return {
            "midpoint": asset["price"],
            "currency": "USD",
            "generated_at": asset["generated_at"],
            "source": asset["source"],
            "quote_status": asset.get("quote_status") or "live",
            "contract_address": asset["contract_address"],
        }

    async def resolution_price(self, symbol: str, not_before: datetime) -> Optional[Dict[str, Any]]:
        quote = await self.current_price(symbol)
        if not quote:
            return None
        timestamp = datetime.fromisoformat(str(quote["generated_at"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp < not_before:
            return None
        return quote
