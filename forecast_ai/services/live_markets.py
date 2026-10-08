"""Read-only, bounded market snapshots. Never substitute a last trade for a fill."""
from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timezone
from typing import Any


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def valid_levels(levels: list) -> list[tuple[float, float]]:
    result = []
    for level in levels:
        price, size = number(level.price), number(level.size)
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            result.append((price, size))
    return result


def depth_quote(asks: list, notional: float) -> dict:
    """Sweep visible asks for a dollar notional, including partial-depth disclosure."""
    remaining, cost, shares = notional, 0.0, 0.0
    levels = sorted(valid_levels(asks))
    for price, size in levels:
        spent = min(remaining, price * size)
        shares += spent / price
        cost += spent
        remaining -= spent
        if remaining < 1e-8:
            break
    average = cost / shares if shares else None
    return {
        "best_ask": levels[0][0] if levels else None,
        "average_price": round(average, 6) if average is not None else None,
        "filled_notional": round(cost, 4),
        "requested_notional": notional,
        "full_depth": remaining < 1e-8,
        "slippage": round(average - levels[0][0], 6) if average is not None else None,
    }


class LiveMarketService:
    def __init__(self, gamma, clob, kalshi):
        self.gamma, self.clob, self.kalshi = gamma, clob, kalshi
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.slots = asyncio.Semaphore(4)

    async def snapshot(self, market_id: str, venue: str, notional: float = 100) -> dict:
        venue = venue.lower()
        if venue not in {"polymarket", "kalshi"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", market_id):
            return {"market_id": market_id, "venue": venue, "status": "unsupported"}
        key = (venue, market_id, notional)
        # Prune both cache and locks together; retain locks while a request holds them.
        now = time.monotonic()
        for old_key in list(self.cache):
            if now - self.cache[old_key][0] > 60 and not self.locks[old_key].locked():
                self.cache.pop(old_key, None)
                self.locks.pop(old_key, None)
        if key not in self.locks and len(self.locks) >= 500:
            return {"market_id": market_id, "venue": venue, "status": "busy"}
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 20:
                return cached[1]
            async with self.slots:
                try:
                    result = await asyncio.wait_for(self._fetch(market_id, venue, notional), timeout=18)
                except Exception:
                    result = {"market_id": market_id, "venue": venue, "status": "unavailable"}
            result["observed_at"] = datetime.now(timezone.utc).isoformat()
            result["expires_after_seconds"] = 60
            self.cache[key] = (time.monotonic(), result)
            return result

    async def _fetch(self, market_id: str, venue: str, notional: float) -> dict:
        output = {"market_id": market_id, "venue": venue, "fee_status": "unknown", "fee_per_share": None}
        if venue == "kalshi":
            market = await self.kalshi.fetch_market_by_ticker(market_id.upper())
            if market is None:
                return {**output, "status": "unavailable"}
            if market.status not in {"active", "open"}:
                return {**output, "status": "closed", "outcome": market.result}
            book = await self.kalshi.fetch_orderbook(market.ticker)
            bids = valid_levels(book.yes_bids) if book else []
            asks = valid_levels(book.yes_asks) if book else []
            output.update({
                "question": market.title, "closes_at": market.expiration_time,
                "rules": market.raw_data.get("rules_primary", ""),
                "rules_secondary": market.raw_data.get("rules_secondary", ""),
                "url": f"https://kalshi.com/markets/{market.ticker}",
                "yes": depth_quote(book.yes_asks if book else [], notional),
                "no": depth_quote(book.no_asks if book else [], notional),
            })
        else:
            market = await (self.gamma.fetch_market(market_id) if market_id.isdigit()
                            else self.gamma.fetch_market_by_slug(market_id))
            if market is None:
                event = await self.gamma.fetch_event_by_slug(market_id)
                choices = event.markets if event else []
                # Multi-contract events must never be priced as their first child.
                if len(choices) != 1:
                    return {**output, "status": "select_contract" if choices else "unavailable"}
                market = choices[0]
            if market.closed or not market.active:
                return {**output, "status": "closed"}
            tokens = {str(token.get("outcome", "")).lower(): token.get("token_id") for token in market.tokens}
            if not tokens.get("yes") or not tokens.get("no"):
                return {**output, "status": "unsupported_outcomes"}
            yes, no = await asyncio.gather(self.clob.fetch_order_book(tokens["yes"]), self.clob.fetch_order_book(tokens["no"]))
            bids, asks = valid_levels(yes.bids) if yes else [], valid_levels(yes.asks) if yes else []
            output.update({
                "question": market.question, "closes_at": market.end_date_iso,
                "rules": market.raw_data.get("description", ""),
                "resolution_source": market.resolution_source,
                "url": f"https://polymarket.com/market/{market.slug}",
                "yes": depth_quote(yes.asks if yes else [], notional),
                "no": depth_quote(no.asks if no else [], notional),
            })
        bid = max((p for p, _ in bids), default=None)
        ask = min((p for p, _ in asks), default=None)
        crossed = bid is not None and ask is not None and bid > ask
        output.update({
            "status": "invalid_book" if crossed else "live" if bid is not None and ask is not None else "no_book",
            "probability": round((bid + ask) / 2, 6) if bid is not None and ask is not None and not crossed else None,
            "best_bid": bid, "best_ask": ask,
            "spread": round(ask - bid, 6) if bid is not None and ask is not None and not crossed else None,
        })
        if crossed:
            output.pop("yes", None)
            output.pop("no", None)
        return output
