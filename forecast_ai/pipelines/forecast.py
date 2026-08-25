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
from ..proof.ledger import build_forecast_envelope, build_resolution_hash
from ..proof.outbox import SupabaseProofOutbox

logger = logging.getLogger(__name__)

AGENT_IDS = ("news", "social", "reddit", "research", "macro", "onchain", "market")
SPECIALIST_AGENT_MAP = {
    "evidence": "research",
    "market-scout": "market",
    "risk-challenger": "macro",
}

# The RWA vertical deliberately does not use Mihari. It combines primary
# corporate records, macro data, live research and social context instead.
# Bravado and Falcon are added by SourceManager when a real linked market is
# selected, where their market identifiers can be verified.
RWA_ALLOWED_SOURCES = {
    "news", "rss", "twitter", "reddit", "blockchain", "kalshi",
    "tavily", "sec_edgar", "fred", "perigon", "exa",
}


def normalize_agent_runtime(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate the server-supplied Agent Studio runtime before using it."""
    if not isinstance(value, dict):
        return None
    agent_id = str(value.get("id") or "").strip()
    name = str(value.get("name") or "").strip()[:80]
    mode = str(value.get("mode") or "").strip().lower()
    template_id = str(value.get("template_id") or "").strip().lower()
    config = value.get("config") if isinstance(value.get("config"), dict) else {}
    raw_modules = config.get("modules") if isinstance(config.get("modules"), dict) else {}
    raw_models = config.get("models") if isinstance(config.get("models"), dict) else {}
    if not agent_id or not name or mode not in {"custom", "specialist"}:
        raise ValueError("Invalid Agent Studio runtime identity.")
    if mode == "custom" and template_id != "forecast-swarm":
        raise ValueError("Invalid custom Swarm template.")
    if mode == "specialist" and template_id not in SPECIALIST_AGENT_MAP:
        raise ValueError("Unsupported standalone specialist template.")

    selected_agents = list(AGENT_IDS) if mode == "custom" else [SPECIALIST_AGENT_MAP[template_id]]
    modules = {
        str(key): [str(item) for item in items if isinstance(item, str)]
        for key, items in raw_modules.items()
        if isinstance(items, list)
    }
    models = {
        str(key): str(model)
        for key, model in raw_models.items()
        if isinstance(model, str)
    }
    return {
        "id": agent_id,
        "name": name,
        "mode": mode,
        "template_id": template_id,
        "category": str(config.get("category") or "General")[:80],
        "selected_agents": selected_agents,
        "modules": modules,
        "models": models,
    }


def runtime_modules_for_agent(runtime: Optional[Dict[str, Any]], agent_name: str) -> Optional[set[str]]:
    if not runtime:
        return None
    module_key = runtime["template_id"] if runtime["mode"] == "specialist" else agent_name
    configured = runtime.get("modules", {}).get(module_key)
    return set(configured) if isinstance(configured, list) else set()


def filter_runtime_evidence(evidence: List[Evidence], enabled_modules: Optional[set[str]]) -> List[Evidence]:
    """Enforce Agent Studio module choices without trusting client labels."""
    if enabled_modules is None:
        return evidence
    filtered: List[Evidence] = []
    for item in evidence:
        metadata = item.metadata or {}
        provider = str(metadata.get("provider") or "").lower()
        source_type = str(metadata.get("source_type") or "").lower()
        source_name = item.source_name.lower()
        module = None
        if provider == "factsai" or "factsai" in source_name or "facts_ai" in source_name:
            module = "factsai"
        elif provider == "tavily" or "tavily" in source_name:
            module = "tavily"
        elif provider == "exa" or "exa" in source_name:
            module = "exa-research"
        elif provider == "perigon" or "perigon" in source_name:
            module = "perigon-news"
        elif provider == "fred" or "fred" in source_name:
            module = "fred-macro"
        elif provider == "falcon" or source_name.startswith("falcon"):
            module = "falcon-social" if source_type.startswith("social") else "falcon-market"
        elif "news" in source_name or "rss" in source_name:
            module = "newsrss"
        elif "blockchain" in source_name or "onchain" in source_name:
            module = "chain-data"
        elif provider in {"mihari", "sec edgar"} or "mihari" in source_name or "sec edgar" in source_name:
            # RWA intelligence is platform-managed and applies only to the
            # onchain specialist when explicitly enabled in Agent Studio.
            module = "rwa-intelligence"
        elif source_name in {"polymarket", "kalshi"}:
            module = "markets"
        elif provider == "bravado" or source_name.startswith("bravado"):
            # Bravado is platform-managed market intelligence, not a user-selectable partner module.
            module = "markets"
        if module is None or module in enabled_modules:
            filtered.append(item)
    return filtered


def _route_evidence_for_agent(agent_name: str, evidence: List[Evidence]) -> List[Evidence]:
    """Give each Swarm node only the evidence relevant to its specialty."""
    normalized_agent = agent_name.lower().strip()
    routed: List[Evidence] = []

    source_patterns = {
        "news": ("news", "rss", "perigon", "facts_ai", "factsai", "tavily"),
        "social": ("twitter", "social"),
        "reddit": ("reddit",),
        "research": ("facts_ai", "factsai", "arxiv", "research", "tavily", "exa"),
        "macro": ("macro", "cme", "fred", "news", "rss", "facts_ai", "factsai", "tavily"),
        "onchain": ("blockchain", "onchain", "polygonscan", "mihari", "sec edgar", "rwa"),
        "market": ("kalshi", "polymarket", "market", "robinhood"),
    }
    falcon_types = {
        "social": {"social_intelligence", "social_status"},
        "market": {
            "market_intelligence",
            "smart_money",
            "smart_money_status",
            "status",
        },
    }
    bravado_types = {
        "market": {"trader_intelligence", "trader_status"},
    }

    for item in evidence:
        metadata = item.metadata or {}
        provider = str(metadata.get("provider") or "").lower().strip()
        source_type = str(metadata.get("source_type") or "").lower().strip()
        source_name = item.source_name.lower()

        if provider == "falcon" or source_name.startswith("falcon"):
            if source_type in falcon_types.get(normalized_agent, set()):
                routed.append(item)
            continue

        if provider == "bravado" or source_name.startswith("bravado"):
            if source_type in bravado_types.get(normalized_agent, set()):
                routed.append(item)
            continue

        patterns = source_patterns.get(normalized_agent, (normalized_agent,))
        if any(pattern in source_name for pattern in patterns):
            routed.append(item)

    if routed:
        return routed

    # A selected market is safe shared context when a specialist has no
    # dedicated source. Do not fall back to every partner payload.
    return [
        item for item in evidence
        if item.source_name.lower() in {"polymarket", "kalshi"}
    ]

class ForecastPipeline:
    def __init__(self, config: ForecastConfig, memory_store: Optional[MemoryStore] = None):
        self.config = config
        self.provider_manager = ProviderManager(config)
        self.source_manager = SourceManager(config, provider_manager=self.provider_manager)
        self.consensus_engine = ConsensusEngine(config)
        self.memory_store = memory_store or MemoryStore(config)
        self.proof_outbox = SupabaseProofOutbox(
            config.robinhood_chain.supabase_url,
            config.robinhood_chain.supabase_service_role_key,
            chain_id=config.robinhood_chain.chain_id,
            registry_address=config.robinhood_chain.registry_address,
        )
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
        agent_runtime: Optional[Dict[str, Any]] = None,
        analysis_mode: Optional[str] = None,
        context_market_id: Optional[str] = None,
        context_venue: Optional[str] = None,
    ) -> ForecastResult:
        """
        Orchestrates full forecasting process.
        """
        runtime = normalize_agent_runtime(agent_runtime)
        normalized_mode = str(analysis_mode or "").strip().lower()
        if normalized_mode not in {"", "rwa"}:
            raise ValueError("Unsupported analysis mode.")

        # 1. Gather evidence
        evidence_market_id = context_market_id or market_id
        evidence_venue = context_venue or venue
        evidence = await self.source_manager.gather_evidence(
            question,
            market_id=evidence_market_id,
            venue=evidence_venue,
            allowed_sources=RWA_ALLOWED_SOURCES if normalized_mode == "rwa" else None,
        )

        # FactsAI is a paid partner source. Fetch it once per forecast and share
        # the same verified research with News, Research, and Macro agents.
        # This replaces the previous three independent calls per Swarm run.
        runtime_uses_facts = runtime is None or any(
            "factsai" in (runtime_modules_for_agent(runtime, agent_name) or set())
            for agent_name in runtime["selected_agents"]
        )
        facts_enabled = getattr(self.config.facts_ai, "enabled", False) and runtime_uses_facts
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
        selected_agent_ids = set(runtime["selected_agents"]) if runtime else set(self.agents)
        active_agents = [agent for name, agent in self.agents.items() if name in selected_agent_ids]
        predictions = []

        async def _query_agent(agent):
            enabled_modules = runtime_modules_for_agent(runtime, agent.name)
            agent_evidence = _route_evidence_for_agent(agent.name, evidence)
            agent_evidence = filter_runtime_evidence(
                agent_evidence,
                enabled_modules,
            )
            try:
                return await agent.forecast(
                    question,
                    agent_evidence,
                    is_public_feed=is_public_feed,
                    model_override=model_override,
                    facts_key=facts_key,
                    enabled_modules=enabled_modules,
                )
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
        # An RWA outlook can use a prediction market as supporting evidence,
        # but that market's resolution must never become the Stock Token
        # forecast's resolution condition.
        if not market_closes_at and normalized_mode != "rwa":
            selected_market = next((item for item in evidence if item.source_name in ("kalshi", "polymarket")), None)
            if selected_market:
                market_closes_at = (selected_market.metadata or {}).get("expiration_time")
        result.metadata["question"] = question
        result.metadata["venue"] = venue
        result.metadata["category"] = category or "Other"
        result.metadata["market_closes_at"] = market_closes_at
        if normalized_mode:
            result.metadata["analysis_mode"] = normalized_mode
        if context_market_id:
            result.metadata["context_market"] = {
                "market_id": context_market_id,
                "venue": context_venue,
                "role": "supplementary_evidence",
            }
        if runtime:
            result.metadata["agent_runtime"] = {
                "id": runtime["id"],
                "name": runtime["name"],
                "mode": runtime["mode"],
                "template_id": runtime["template_id"],
                "category": runtime["category"],
                "selected_agents": runtime["selected_agents"],
                "models": runtime["models"],
                "modules": runtime["modules"],
            }
        
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
            proof_enabled=self.config.robinhood_chain.proof_enabled,
            chain_id=self.config.robinhood_chain.chain_id,
            contract_address=self.config.robinhood_chain.registry_address or None,
            explorer_url=self.config.robinhood_chain.explorer_url,
            agent_namespace=runtime["id"] if runtime else None,
            agent_identity=result.metadata.get("agent_runtime"),
        )
        result.metadata["proof"] = proof
        result.metadata["opportunity_radar"]["proof"] = {
            key: proof.get(key)
            for key in (
                "status", "network", "chain_id", "forecast_id", "payload_hash",
                "contract_address", "explorer_url", "transaction_hash", "queue_eligible",
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

        # Supabase is the durable source of truth and survives Railway restarts.
        for entry in await self.proof_outbox.list_due_markets():
            key = (str(entry.get("market_id") or ""), str(entry.get("venue") or ""))
            if key[0]:
                grouped[key] = entry

        checked = 0
        resolved_count = 0
        unresolved = []
        queued_onchain = 0
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
            await self.queue_onchain_resolution(
                market_id,
                outcome,
                source_url or "official venue data",
                now.isoformat(),
                venue=venue,
            )
            queued_onchain += 1
            resolved_count += len(resolved)

        return {
            "checked_markets": checked,
            "resolved_forecasts": resolved_count,
            "queued_onchain_markets": queued_onchain,
            "still_unresolved": unresolved,
        }

    async def queue_onchain_resolution(
        self,
        market_id: str,
        outcome: int,
        source: str,
        resolved_at: str,
        venue: str | None = None,
    ) -> None:
        if not self.proof_outbox.configured:
            return
        resolution_hash = build_resolution_hash(market_id, outcome, source, resolved_at)
        await self.proof_outbox.queue_resolution(
            market_id, outcome, resolution_hash, venue=venue
        )
