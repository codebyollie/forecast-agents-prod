"""
Forecast Pipeline.

Coordinates evidence gathering, agent predictions, consensus aggregation, and memory logging.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from ..config import ForecastConfig
from ..models.forecast import ForecastResult
from ..models.evidence import Evidence
from ..sources import SourceManager
from ..providers import ProviderManager, ProviderError
from ..consensus import ConsensusEngine
from ..memory import MemoryStore
from ..agents import NewsAgent, SocialAgent, RedditAgent, ResearchAgent, MacroAgent, OnchainAgent, MarketAgent
from ..services.opportunity_radar import build_opportunity_radar
from ..services.market_search import MarketSearchService
from ..services.outcome_graph import OutcomeGraphService
from ..services.robinhood_stock_tokens import RobinhoodStockTokenClient
from ..proof.ledger import build_forecast_envelope

logger = logging.getLogger(__name__)

class ForecastPipeline:
    def __init__(self, config: ForecastConfig, memory_store: Optional[MemoryStore] = None):
        self.config = config
        self.provider_manager = ProviderManager(config)
        self.source_manager = SourceManager(config, provider_manager=self.provider_manager)
        self.consensus_engine = ConsensusEngine(config)
        self.memory_store = memory_store or MemoryStore(config)
        market_search = MarketSearchService(
            kalshi_base_url=config.kalshi.api_base_url,
            gamma_api_url=config.polymarket.gamma_api_url,
        )
        stock_tokens = None
        if config.robinhood_chain.stock_tokens_enabled:
            stock_tokens = RobinhoodStockTokenClient(config.robinhood_chain.stock_token_api_url)
        self.outcome_graph = OutcomeGraphService(market_search, stock_tokens)
        self._init_agents()

    def _init_agents(self):
        self.agents = {}
        agent_classes = {
            "news": NewsAgent,
            "social": SocialAgent,
            "reddit": RedditAgent,
            "research": ResearchAgent,
            "macro": MacroAgent,
            "onchain": OnchainAgent,
            "market": MarketAgent
        }

        for name, a_cfg in self.config.agents.items():
            if a_cfg.enabled and name in agent_classes:
                try:
                    primary_name = a_cfg.provider or self.config.default_provider
                    provider = self.provider_manager.get_provider(primary_name)
                    self.agents[name] = agent_classes[name](
                        name=name,
                        provider=provider,
                        config=self.config,
                        provider_manager=self.provider_manager,
                        primary_provider_name=primary_name
                    )
                except Exception as e:
                    logger.warning(f"[ForecastPipeline] Could not initialize agent '{name}': {e}")

    async def run_forecast(
        self,
        question: str,
        market_id: str = "custom_market",
        is_public_feed: bool = False,
        model_override: Optional[str] = None,
        facts_key: Optional[str] = None,
        venue: Optional[str] = None,
        category: Optional[str] = None,
        market_closes_at: Optional[str] = None,
    ) -> ForecastResult:
        """
        Orchestrates full forecasting process.
        """
        # 1. Gather evidence
        evidence = await self.source_manager.gather_evidence(
            question,
            market_id=market_id,
            venue=venue,
        )

        # FactsAI is a paid partner source. Fetch it once per forecast and share
        # the same verified research with News, Research, and Macro agents.
        # This replaces the previous three independent calls per Swarm run.
        facts_enabled = getattr(self.config.facts_ai, "enabled", False)
        facts_api_key = facts_key or getattr(self.config.facts_ai, "api_key", "")
        if facts_enabled and facts_api_key:
            from ..sources.facts_ai import FactsAISource

            facts_cache_key = f"shared:{question.strip()}"
            cached_facts = self.source_manager.cache.get("facts_ai", facts_cache_key, ttl_seconds=900)
            if cached_facts is not None:
                evidence.extend(cached_facts)
            else:
                facts_source = FactsAISource(
                    api_key=facts_api_key,
                    api_url=self.config.facts_ai.api_url,
                    query_max_length=self.config.facts_ai.query_max_length,
                )
                partner_evidence = []
                try:
                    facts_result = await facts_source.fetch_deep_research(
                        f"Primary sources, verified reporting, and macro context relevant to: {question}"
                    )
                    answer = str(facts_result.get("answer") or "").strip()
                    if answer:
                        partner_evidence.append(Evidence(
                            source_name="FactsAI Deep Research",
                            content=answer,
                            relevance_score=0.97,
                            title=f"FactsAI Synthesis: {question[:60]}",
                            url="https://factsai.org",
                            metadata={
                                "provider": "FactsAI",
                                "source_type": "summary",
                                "status": "active",
                                "partner": True,
                            },
                        ))
                    for citation in facts_result.get("citations", []):
                        title = str(citation.get("title") or "FactsAI source")
                        url = str(citation.get("url") or "")
                        partner_evidence.append(Evidence(
                            source_name="FactsAI Citation",
                            content=f"FactsAI verified source: {title}",
                            relevance_score=0.92,
                            title=title,
                            url=url,
                            metadata={
                                "provider": "FactsAI",
                                "source_type": "research",
                                "status": "active",
                                "partner": True,
                            },
                        ))
                    if not answer and not facts_result.get("citations"):
                        partner_evidence.append(Evidence(
                            source_name="FactsAI Status",
                            content="FactsAI completed the request but returned no research.",
                            relevance_score=0.0,
                            metadata={
                                "provider": "FactsAI",
                                "source_type": "status",
                                "status": "empty",
                                "partner": True,
                            },
                        ))
                    evidence.extend(partner_evidence)
                    if partner_evidence:
                        self.source_manager.cache.set("facts_ai", facts_cache_key, partner_evidence)
                except Exception as exc:
                    logger.warning("[ForecastPipeline] FactsAI unavailable: %s", exc)
                    evidence.append(Evidence(
                        source_name="FactsAI Status",
                        content="FactsAI was unavailable; standard web research fallback was used.",
                        relevance_score=0.0,
                        metadata={
                            "provider": "FactsAI",
                            "source_type": "status",
                            "status": "unavailable",
                            "partner": True,
                            "error": str(exc)[:240],
                        },
                    ))

        # 2. Query active agents in parallel
        active_agents = list(self.agents.values())
        predictions = []

        agent_source_map = {
            "news": ["news", "rss", "facts_ai", "factsai", "tavily"],
            "social": ["twitter", "social"],
            "reddit": ["reddit"],
            "research": ["facts_ai", "factsai", "arxiv", "research", "tavily"],
            "macro": ["macro", "cme", "fred", "news", "rss", "facts_ai", "factsai", "tavily"],
            "onchain": ["blockchain", "onchain", "polygonscan"],
            "market": ["kalshi", "polymarket", "market", "robinhood", "falcon"]
        }

        async def _query_agent(agent):
            allowed = agent_source_map.get(agent.name.lower(), [agent.name.lower()])
            agent_evidence = [e for e in evidence if any(s in e.source_name.lower() for s in allowed)]
            if not agent_evidence and agent.name.lower() not in ("social", "reddit"):
                agent_evidence = evidence
            try:
                return await agent.forecast(question, agent_evidence, is_public_feed=is_public_feed, model_override=model_override, facts_key=facts_key)
            except ProviderError as pe:
                logger.error(f"[ForecastPipeline] Agent '{agent.name}' failed after provider fallbacks: {pe}")
                return None
            except Exception as e:
                logger.error(f"[ForecastPipeline] Unexpected error querying agent '{agent.name}': {e}")
                return None

        if active_agents:
            raw_predictions = await asyncio.gather(*[_query_agent(a) for a in active_agents])
            predictions = [p for p in raw_predictions if p is not None]

        if not predictions:
            raise ProviderError("pipeline", f"All active agents failed to generate predictions for '{question}'")

        # 3. Apply Consensus Engine
        reputations = self.memory_store.get_agent_reputations()
        result = await self.consensus_engine.aggregate_predictions(market_id, predictions, reputations)
        if not market_closes_at:
            selected_market = next((item for item in evidence if item.source_name in ("kalshi", "polymarket")), None)
            if selected_market:
                market_closes_at = (selected_market.metadata or {}).get("expiration_time")
        result.metadata["question"] = question
        result.metadata["venue"] = venue
        result.metadata["category"] = category or "Other"
        result.metadata["market_closes_at"] = market_closes_at
        
        # 4. Attach model_used metadata reflecting reality
        result.metadata["model_used"] = model_override or getattr(self.config, "default_model", "gpt-4o")
        result.metadata["market_context"] = [
            {
                "source": item.source_name,
                "title": item.title,
                "url": item.url,
                "metadata": item.metadata,
            }
            for item in evidence
            if item.source_name in ("kalshi", "polymarket")
        ]
        result.metadata["opportunity_radar"] = build_opportunity_radar(
            result=result,
            evidence=evidence,
            question=question,
            venue=venue,
        )
        result.metadata["outcome_graph"] = {
            "version": "1.0",
            "selected": {"market_id": market_id, "venue": venue},
            "counterpart_markets": [],
            "related_markets": [],
            "rwa_assets": [],
            "status": "not_applicable" if market_id == "custom_market" else "unavailable",
        }
        if market_id != "custom_market":
            try:
                market_probability = result.metadata["opportunity_radar"].get("market", {}).get("probability")
                result.metadata["outcome_graph"] = await self.outcome_graph.build(
                    question=question,
                    selected_market_id=market_id,
                    selected_venue=venue,
                    selected_probability=market_probability,
                )
                result.metadata["outcome_graph"]["status"] = "active"
            except Exception as exc:
                logger.warning("[ForecastPipeline] Outcome Graph unavailable: %s", exc)
                result.metadata["outcome_graph"]["message"] = "Related market intelligence was unavailable."

        proof = build_forecast_envelope(
            result=result,
            question=question,
            venue=venue,
            category=category,
            market_closes_at=market_closes_at,
        )
        proof["chain_id"] = self.config.robinhood_chain.chain_id
        proof["contract_address"] = self.config.robinhood_chain.registry_address or None
        if not self.config.robinhood_chain.proof_enabled or not self.config.robinhood_chain.registry_address:
            proof["status"] = "pending_onchain"
        result.metadata["proof"] = proof
        result.metadata["opportunity_radar"]["proof"] = {
            key: proof.get(key)
            for key in (
                "status", "network", "chain_id", "forecast_id", "payload_hash",
                "contract_address", "transaction_hash",
            )
        }

        # 5. Save to Memory (skip saving private forecast store if public feed, handled separately)
        if not is_public_feed:
            self.memory_store.save_forecast(result)

        return result

    async def resolve_due_forecasts(self) -> Dict[str, Any]:
        """Resolve due binary markets from official venue data without an LLM call."""
        now = datetime.now(timezone.utc)
        grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
        for entry in self.memory_store.list_forecasts():
            if entry.get("resolution"):
                continue
            closes_at = entry.get("market_closes_at")
            if not closes_at:
                continue
            try:
                close_time = datetime.fromisoformat(str(closes_at).replace("Z", "+00:00"))
                if close_time.tzinfo is None:
                    close_time = close_time.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if close_time > now:
                continue
            key = (str(entry.get("market_id") or ""), str(entry.get("venue") or ""))
            grouped[key] = entry

        checked = 0
        resolved_count = 0
        unresolved = []
        for (market_id, venue), entry in grouped.items():
            checked += 1
            outcome = None
            source_url = None
            try:
                if "kalshi" in venue.lower() or "robinhood" in venue.lower():
                    market = await self.source_manager.kalshi_client.fetch_market_by_ticker(market_id.upper())
                    result = str(market.result or "").lower() if market else ""
                    if result in ("yes", "y", "1"):
                        outcome = 1
                    elif result in ("no", "n", "0"):
                        outcome = 0
                    source_url = f"https://kalshi.com/markets/{market_id}"
                else:
                    market = await self.source_manager.gamma_client.fetch_market_by_slug(market_id)
                    if market is None:
                        event = await self.source_manager.gamma_client.fetch_event_by_slug(market_id)
                        if event and len(event.markets) == 1:
                            market = event.markets[0]
                    if market and market.closed and market.outcome_prices:
                        winner_index = max(range(len(market.outcome_prices)), key=market.outcome_prices.__getitem__)
                        if market.outcome_prices[winner_index] >= 0.99 and winner_index < len(market.tokens):
                            winner = str(market.tokens[winner_index].get("outcome") or "").lower()
                            if winner == "yes":
                                outcome = 1
                            elif winner == "no":
                                outcome = 0
                    source_url = f"https://polymarket.com/event/{market_id}"
            except Exception as exc:
                logger.warning("[ForecastPipeline] Resolution check failed for %s: %s", market_id, exc)

            if outcome is None:
                unresolved.append({"market_id": market_id, "venue": venue})
                continue
            resolved = self.memory_store.resolve_market_forecasts(
                market_id=market_id,
                outcome=outcome,
                resolution_source=source_url or "official venue data",
                resolved_at=now.isoformat(),
            )
            resolved_count += len(resolved)

        return {
            "checked_markets": checked,
            "resolved_forecasts": resolved_count,
            "still_unresolved": unresolved,
        }
