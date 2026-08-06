"""
Forecast Pipeline.

Coordinates evidence gathering, agent predictions, consensus aggregation, and memory logging.
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional
from ..config import ForecastConfig
from ..models.forecast import ForecastResult
from ..models.evidence import Evidence
from ..sources import SourceManager
from ..providers import ProviderManager, ProviderError
from ..consensus import ConsensusEngine
from ..memory import MemoryStore
from ..agents import NewsAgent, SocialAgent, RedditAgent, ResearchAgent, MacroAgent, OnchainAgent, MarketAgent

logger = logging.getLogger(__name__)

class ForecastPipeline:
    def __init__(self, config: ForecastConfig, memory_store: Optional[MemoryStore] = None):
        self.config = config
        self.provider_manager = ProviderManager(config)
        self.source_manager = SourceManager(config, provider_manager=self.provider_manager)
        self.consensus_engine = ConsensusEngine(config)
        self.memory_store = memory_store or MemoryStore(config)
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

        # 5. Save to Memory (skip saving private forecast store if public feed, handled separately)
        if not is_public_feed:
            self.memory_store.save_forecast(result)

        return result
