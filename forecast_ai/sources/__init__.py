from typing import List, Dict
from .base import BaseSource
from .news import NewsSource
from .rss import RssSource
from .twitter import TwitterSource
from .reddit import RedditSource
from .blockchain import BlockchainSource
from .kalshi import KalshiSource
from .tavily_search import TavilySearchSource
from .falcon import FalconSource
from .bravado import BravadoSource
from .mihari import MihariSource
from .fred import FredSource
from ..models.evidence import Evidence
from ..config import ForecastConfig
from .cache import SourceCache
from ..kalshi.client import KalshiClient
from ..polymarket.gamma import GammaClient
from ..polymarket.clob import ClobClient

import os
import asyncio
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

class SourceManager:
    def __init__(self, config: ForecastConfig, provider_manager=None):
        self.config = config
        self.provider_manager = provider_manager
        self.cache = SourceCache()
        self.kalshi_client = KalshiClient(
            base_url=config.kalshi.api_base_url,
            api_key=getattr(config.kalshi, "api_key", "") or None,
        )
        self.gamma_client = GammaClient(base_url=config.polymarket.gamma_api_url)
        self.clob_client = ClobClient(base_url=config.polymarket.clob_api_url)
        self.falcon_source = None
        if getattr(config.falcon, "enabled", False) and getattr(config.falcon, "api_token", ""):
            self.falcon_source = FalconSource(
                api_token=config.falcon.api_token,
                api_url=config.falcon.api_url,
                timeout_seconds=config.falcon.timeout_seconds,
                market_insights_agent_id=config.falcon.market_insights_agent_id,
                kalshi_markets_agent_id=config.falcon.kalshi_markets_agent_id,
                social_pulse_agent_id=config.falcon.social_pulse_agent_id,
                social_enabled=config.falcon.social_enabled,
                falcon_score_agent_id=config.falcon.falcon_score_agent_id,
                polymarket_trades_agent_id=config.falcon.polymarket_trades_agent_id,
                smart_money_enabled=config.falcon.smart_money_enabled,
            )
        self.bravado_source = None
        if getattr(config.bravado, "enabled", False):
            self.bravado_source = BravadoSource(
                api_token=config.bravado.api_token,
                api_url=config.bravado.api_url,
                timeout_seconds=config.bravado.timeout_seconds,
                leaderboard_window=config.bravado.leaderboard_window,
                scan_limit=config.bravado.scan_limit,
                min_trades=config.bravado.min_trades,
            )
        
        news_key = getattr(config.sources, "news_api_key", "") or os.getenv("NEWS_API_KEY", "")
        twitter_token = getattr(config.sources, "twitter_bearer_token", "") or os.getenv("TWITTER_BEARER_TOKEN", "")
        polygonscan_key = getattr(config.sources, "polygonscan_key", "") or os.getenv("POLYGONSCAN_API_KEY", "")

        self.sources: Dict[str, BaseSource] = {
            "news": NewsSource(api_key=news_key),
            "rss": RssSource(),
            "twitter": TwitterSource(bearer_token=twitter_token),
            "reddit": RedditSource(),
            "blockchain": BlockchainSource(polygonscan_key=polygonscan_key),
            "kalshi": KalshiSource(api_base_url=config.kalshi.api_base_url),
        }
        tavily_key = getattr(config.tavily, "api_key", "") or os.getenv("TAVILY_API_KEY", "")
        if getattr(config.tavily, "enabled", False) or os.getenv("TAVILY_ENABLED", "").lower() in ("true", "1", "yes"):
            self.sources["tavily"] = TavilySearchSource(
                api_key=tavily_key,
                enabled=True
            )
        if getattr(config.mihari, "enabled", False):
            self.sources["mihari"] = MihariSource(
                api_url=config.mihari.api_url,
                enabled=True,
                timeout_seconds=config.mihari.timeout_seconds,
                max_symbols=config.mihari.max_symbols,
            )
        if getattr(config.fred, "enabled", False) and getattr(config.fred, "api_key", ""):
            self.sources["fred"] = FredSource(
                api_key=config.fred.api_key,
                api_url=config.fred.api_url,
                enabled=True,
                timeout_seconds=config.fred.timeout_seconds,
            )

    async def _fetch_single_source(self, name: str, source: BaseSource, query: str, limit: int) -> List[Evidence]:
        # Check Cache first
        cache_ttl = int(getattr(source, "cache_ttl_seconds", 3600))
        cached = self.cache.get(name, query, ttl_seconds=cache_ttl)
        if cached is not None:
            return cached

        try:
            results = await asyncio.wait_for(source.fetch(query, limit=limit), timeout=15.0)
            if results:
                self.cache.set(name, query, results)
            return results
        except Exception as e:
            logger.warning(f"[SourceManager] Source '{name}' fetch failed or timed out: {e}")
            return []

    async def gather_evidence(
        self,
        query: str,
        limit: int = 5,
        market_id: Optional[str] = None,
        venue: Optional[str] = None,
    ) -> List[Evidence]:
        """
        Gathers evidence from all configured and enabled sources.
        Relies on the Caching layer to prevent redundant API calls.
        """
        tasks = []
        if market_id and market_id != "custom_market":
            market_evidence = await self.gather_market_evidence(
                market_id=market_id,
                venue=venue,
                limit=limit,
            )
        else:
            market_evidence = []
        for name, source in self.sources.items():
            tasks.append(
                asyncio.create_task(self._fetch_single_source(name, source, query, limit))
            )
                
        if not tasks:
            logger.warning("[SourceManager] No sources configured for gathering.")
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_evidence = list(market_evidence)
        for res in results:
            if isinstance(res, list):
                all_evidence.extend(res)
                
        # Optional: Synthesis Layer (if provider_manager is attached)
        if self.provider_manager and all_evidence:
            all_evidence = await self.synthesize_evidence(query, all_evidence)

        return all_evidence

    async def gather_market_evidence(
        self,
        market_id: str,
        venue: Optional[str] = None,
        limit: int = 5,
    ) -> List[Evidence]:
        """Fetch the exact selected market and its live orderbook context."""
        venue_name = (venue or "").lower()
        evidence: List[Evidence] = []

        async def fetch_polymarket() -> List[Evidence]:
            event = await self.gamma_client.fetch_event_by_slug(market_id)
            markets = event.markets if event and event.markets else []
            if not markets:
                market = await self.gamma_client.fetch_market_by_slug(market_id)
                if market:
                    markets = [market]
            if not markets:
                return []

            selected = [m for m in markets if m.active and not m.closed]
            if not selected:
                return []

            outcome_parts = []
            orderbook_parts = []
            orderbook_metadata = []
            for market in selected[:limit]:
                prices = market.outcome_prices
                if prices:
                    outcome_parts.append(
                        f"{market.question or market.slug}: "
                        + ", ".join(f"{p:.4f}" for p in prices)
                    )
                for token in market.tokens[:2]:
                    token_id = token.get("token_id") or token.get("tokenId")
                    if not token_id:
                        continue
                    book = await self.clob_client.fetch_order_book(token_id)
                    if not book:
                        continue
                    best_bid = book.bids[0].price if book.bids else None
                    best_ask = book.asks[0].price if book.asks else None
                    orderbook_parts.append(
                        f"{market.question or market.slug} {token.get('outcome', 'outcome')}: "
                        f"bid={best_bid}, ask={best_ask}, spread={book.spread:.4f}"
                    )
                    orderbook_metadata.append({
                        "token_id": token_id,
                        "outcome": token.get("outcome"),
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread": book.spread,
                    })

            first = selected[0]
            display_title = first.question or (event.title if event else first.slug)
            content = (
                f"Polymarket selected market/event: {display_title}. "
                f"Active={first.active}, Closed={first.closed}, End={first.end_date_iso}, "
                f"Volume={first.volume}, Liquidity={first.liquidity}. "
                f"Outcome prices: {'; '.join(outcome_parts) or 'unavailable'}. "
                f"CLOB order book: {'; '.join(orderbook_parts) or 'unavailable'}."
            )
            metadata = {
                "market_id": market_id,
                "condition_id": first.condition_id,
                "venue": "Polymarket",
                "volume": first.volume,
                "liquidity": first.liquidity,
                "expiration_time": first.end_date_iso,
                "outcomes": [
                    {"label": token.get("outcome"), "price": price}
                    for market in selected[:limit]
                    for token, price in zip(market.tokens, market.outcome_prices)
                ],
                "order_books": orderbook_metadata,
            }
            return [Evidence(
                source_name="polymarket",
                content=content,
                title=first.question or (event.title if event else first.slug),
                url=f"https://polymarket.com/event/{event.slug}" if event else "",
                relevance_score=1.0,
                metadata=metadata,
            )]

        async def fetch_kalshi() -> List[Evidence]:
            market = await self.kalshi_client.fetch_market_by_ticker(market_id.upper())
            if not market or market.status not in ("open", "active"):
                return []
            orderbook = await self.kalshi_client.fetch_orderbook(market.ticker)
            orderbook_text = ""
            if orderbook:
                orderbook_text = (
                    f" Orderbook midpoint={orderbook.midpoint}, spread={orderbook.spread}, "
                    f"YES bids={len(orderbook.yes_bids)}, YES asks={len(orderbook.yes_asks)}."
                )
            content = (
                f"Kalshi selected market: {market.title}. Status={market.status}, "
                f"Last price={market.last_price}, YES bid/ask={market.yes_bid}/{market.yes_ask}, "
                f"Volume={market.volume}, Open interest={market.open_interest}, "
                f"Expiration={market.expiration_time}.{orderbook_text}"
            )
            return [Evidence(
                source_name="kalshi",
                content=content,
                title=market.title,
                url=f"https://kalshi.com/markets/{market.ticker}",
                relevance_score=1.0,
                metadata={
                    "market_id": market.ticker,
                    "venue": "Kalshi",
                    "current_price": market.last_price,
                    "volume": market.volume,
                    "open_interest": market.open_interest,
                    "yes_bid": market.yes_bid,
                    "yes_ask": market.yes_ask,
                    "spread": orderbook.spread if orderbook else None,
                    "expiration_time": market.expiration_time,
                },
            )]

        async def with_falcon(base_evidence: List[Evidence]) -> List[Evidence]:
            if not base_evidence or self.falcon_source is None:
                return base_evidence
            social_query = (base_evidence[0].title or market_id).strip()
            cache_key = (
                f"{venue_name or 'auto'}:{market_id}:"
                f"topic={social_query.lower()[:120]}:"
                f"social={self.falcon_source.social_enabled}:"
                f"smart={self.falcon_source.smart_money_enabled}"
            )
            cached = self.cache.get("falcon", cache_key, ttl_seconds=300)
            if cached is not None:
                return base_evidence + cached
            try:
                market_metadata = base_evidence[0].metadata or {}
                condition_id = str(market_metadata.get("condition_id") or "").strip() or None
                partner_evidence = await self.falcon_source.fetch_market_intelligence(
                    market_id=market_id,
                    venue=venue,
                    condition_id=condition_id,
                    social_query=social_query,
                    limit=25,
                )
                if partner_evidence:
                    self.cache.set("falcon", cache_key, partner_evidence)
                return base_evidence + partner_evidence
            except Exception as exc:
                logger.warning("[SourceManager] Falcon partner intelligence unavailable: %s", exc)
                return base_evidence + [Evidence(
                    source_name="falcon_status",
                    content="Falcon partner intelligence was unavailable for this analysis.",
                    relevance_score=0.0,
                    metadata={
                        "provider": "Falcon",
                        "source_type": "status",
                        "status": "unavailable",
                        "partner": True,
                    },
                )]

        async def with_bravado(base_evidence: List[Evidence]) -> List[Evidence]:
            if not base_evidence or self.bravado_source is None:
                return base_evidence
            market_metadata = base_evidence[0].metadata or {}
            if str(market_metadata.get("venue") or venue or "").lower() != "polymarket":
                return base_evidence
            question = (base_evidence[0].title or market_id).strip()
            condition_id = str(market_metadata.get("condition_id") or "").strip() or None
            cache_key = f"{market_id}:{condition_id or ''}:{question.lower()[:120]}"
            cached = self.cache.get("bravado", cache_key, ttl_seconds=900)
            if cached is not None:
                return base_evidence + cached
            try:
                partner_evidence = await self.bravado_source.fetch_market_trader_intelligence(
                    market_id=market_id,
                    question=question,
                    condition_id=condition_id,
                )
                if partner_evidence:
                    self.cache.set("bravado", cache_key, partner_evidence)
                return base_evidence + partner_evidence
            except Exception as exc:
                logger.warning("[SourceManager] Bravado trader intelligence unavailable: %s", exc)
                return base_evidence + [Evidence(
                    source_name="bravado_status",
                    content="Bravado trader intelligence was unavailable for this analysis.",
                    relevance_score=0.0,
                    metadata={
                        "provider": "Bravado",
                        "source_type": "trader_status",
                        "status": "unavailable",
                        "partner": True,
                    },
                )]

        async def with_partner_intelligence(base_evidence: List[Evidence]) -> List[Evidence]:
            return await with_bravado(await with_falcon(base_evidence))

        if "polymarket" in venue_name:
            return await with_partner_intelligence(await fetch_polymarket())
        if "kalshi" in venue_name or "robinhood" in venue_name:
            return await with_partner_intelligence(await fetch_kalshi())

        # Custom/legacy callers may omit the venue. Resolve deterministically by
        # trying Polymarket slug resolution first, then Kalshi ticker resolution.
        evidence = await fetch_polymarket()
        if evidence:
            return await with_partner_intelligence(evidence)
        return await with_partner_intelligence(await fetch_kalshi())

    async def synthesize_evidence(self, query: str, evidence: List[Evidence]) -> List[Evidence]:
        """
        Uses the default LLM to deduplicate and synthesize the gathered evidence into one master document.
        """
        if not evidence:
            return []
            
        raw_text = "\n\n".join([f"[{e.source_name}] {e.title}\n{e.content}" for e in evidence])
        
        sys_prompt = "You are a Research Synthesizer. Deduplicate, verify, and summarize the provided search results into a clean, highly factual Markdown report. Include specific numbers, dates, and preserve all critical context."
        user_prompt = f"Query: {query}\n\nRaw Search Results:\n{raw_text}\n\nProvide the synthesized summary."
        
        try:
            summary = await self.provider_manager.generate_with_fallback(
                primary_name=self.config.default_provider,
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                max_tokens=2000
            )
            
            # Keep the original evidence so agent-specific source routing and
            # citation links are not destroyed by the synthesis step.
            return evidence + [
                Evidence(
                    source_name="Smart Synthesizer",
                    content=summary,
                    relevance_score=1.0,
                    title=f"Synthesized Research: {query[:50]}",
                    url=""
                )
            ]
        except Exception as e:
            logger.error(f"[SourceManager] Synthesis failed: {e}")
            return evidence  # Fallback to raw evidence

__all__ = [
    "BaseSource",
    "NewsSource",
    "RssSource",
    "TwitterSource",
    "RedditSource",
    "BlockchainSource",
    "KalshiSource",
    "TavilySearchSource",
    "FalconSource",
    "BravadoSource",
    "MihariSource",
    "FredSource",
    "SourceManager",
]
