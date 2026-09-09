"""Live read-only smoke test for Kalshi and Polymarket market discovery."""

import asyncio

from forecast_ai.services.market_search import MarketSearchService


async def _check_venue(service: MarketSearchService, venue: str) -> int:
    response = await service.browse_markets(venue=venue, page=1, page_size=10, sort="trending")
    markets = response.get("results", [])
    if not markets:
        raise RuntimeError(f"{venue} returned no active markets")

    for market in markets:
        price = market.get("current_price")
        outcomes = market.get("outcomes") or []
        if price is None and not outcomes:
            raise RuntimeError(f"{venue} market {market.get('market_id')} has no live price or outcomes")
        if price is not None and not 0 <= float(price) <= 1:
            raise RuntimeError(f"{venue} market {market.get('market_id')} returned invalid price {price}")
        if "volume_24h" not in market:
            raise RuntimeError(f"{venue} market {market.get('market_id')} has no 24-hour activity field")

    print(f"{venue}: ok ({len(markets)} active markets)")
    return len(markets)


async def main() -> None:
    service = MarketSearchService()
    await asyncio.gather(
        _check_venue(service, "kalshi"),
        _check_venue(service, "polymarket"),
    )


if __name__ == "__main__":
    asyncio.run(main())
