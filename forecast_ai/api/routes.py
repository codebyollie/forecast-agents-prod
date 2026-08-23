"""
API Routes for Forecast AI API Server.
"""

import os
import time
import secrets
import asyncio
import logging
import re
from fastapi import APIRouter, HTTPException, Depends, Request, Query
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from ..pipelines.forecast import ForecastPipeline
from ..polymarket.gamma import GammaClient
from ..services.market_search import MarketSearchService
from ..services.robinhood_stock_tokens import RobinhoodStockTokenClient
from ..proof.publisher import ProofPublisher, get_proof_publisher_status

router = APIRouter()
logger = logging.getLogger(__name__)

# Global reference to pipeline, will be set during server init
_pipeline: Optional[ForecastPipeline] = None
_proof_publisher: Optional[ProofPublisher] = None
_stock_tokens: Optional[RobinhoodStockTokenClient] = None

_IP_RATE_LIMITS: Dict[str, List[float]] = {}
MAX_PER_HOUR = max(1, int(os.getenv("PUBLIC_RATE_LIMIT_PER_HOUR", "50")))
WINDOW_SECONDS = 3600.0

def check_ip_rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    timestamps = _IP_RATE_LIMITS.setdefault(ip, [])
    _IP_RATE_LIMITS[ip] = [t for t in timestamps if now - t < WINDOW_SECONDS]
    if len(_IP_RATE_LIMITS[ip]) >= MAX_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Maximum {MAX_PER_HOUR} requests per hour per IP."
        )
    _IP_RATE_LIMITS[ip].append(now)

def require_server_api_key(request: Request) -> bool:
    """Protect the private production deployment while keeping OSS self-hosting easy."""
    expected = ""
    if _pipeline is not None:
        expected = getattr(_pipeline.config.server, "api_key", "") or ""
    if not expected:
        return False

    provided = request.headers.get("x-api-key", "")
    authorization = request.headers.get("authorization", "")
    if not provided and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid agents API key.")
    return True

def enforce_request_access(request: Request) -> None:
    """Authenticate production service calls and rate-limit only public OSS traffic."""
    is_authenticated_service = require_server_api_key(request)
    if not is_authenticated_service:
        check_ip_rate_limit(request)

class PredictionRequest(BaseModel):
    question: str
    market_id: str = "custom_market"
    model_override: Optional[str] = None
    facts_key: Optional[str] = None
    venue: Optional[str] = None
    source_venue: Optional[str] = None
    category: Optional[str] = None
    market_closes_at: Optional[str] = None
    agent_runtime: Optional[Dict[str, Any]] = None
    analysis_mode: Optional[str] = None

class CalibrateRequest(BaseModel):
    agent_name: str
    outcome_correct: bool
    error_delta: float

class ResolveForecastRequest(BaseModel):
    market_id: str
    outcome: int
    resolution_source: str
    resolved_at: str

def get_pipeline() -> ForecastPipeline:
    if _pipeline is None:
        raise HTTPException(status_code=500, detail="Forecast pipeline is not initialized.")
    return _pipeline

def get_search_service(pipeline: ForecastPipeline = Depends(get_pipeline)) -> MarketSearchService:
    return MarketSearchService(
        kalshi_base_url=pipeline.config.kalshi.api_base_url,
        gamma_api_url=pipeline.config.polymarket.gamma_api_url
    )


def get_stock_token_client(pipeline: ForecastPipeline = Depends(get_pipeline)) -> RobinhoodStockTokenClient:
    """Return the shared read-only Robinhood Stock Token client."""
    global _stock_tokens
    if not getattr(pipeline.config.robinhood_chain, "stock_tokens_enabled", False):
        raise HTTPException(status_code=503, detail="Robinhood Stock Token intelligence is disabled.")
    configured_url = getattr(pipeline.config.robinhood_chain, "stock_token_api_url", "")
    if _stock_tokens is None or _stock_tokens.base_url != configured_url.rstrip("/"):
        _stock_tokens = RobinhoodStockTokenClient(base_url=configured_url)
    return _stock_tokens


def _rwa_market_matches(asset: Dict[str, Any], markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only transparent text matches. An empty result is better than a guess."""
    symbol = str(asset.get("tokenSymbol") or "").lower()
    name = RobinhoodStockTokenClient.display_name(asset).lower()
    meaningful_name_parts = [part for part in name.replace("-", " ").split() if len(part) > 3]
    matches: List[Dict[str, Any]] = []
    for market in markets:
        question = str(market.get("question") or market.get("title") or "")
        lowered = question.lower()
        if symbol and re.search(rf"(?<![a-z0-9]){re.escape(symbol)}(?![a-z0-9])", lowered):
            matches.append(market)
        elif meaningful_name_parts and sum(part in lowered for part in meaningful_name_parts) >= min(2, len(meaningful_name_parts)):
            matches.append(market)
    return matches[:6]


_RWA_EVENT_TOPICS: Dict[str, Dict[str, str]] = {
    # These are intentionally broad, transparent event themes. They are not
    # presented as direct company-market matches.
    "JNJ": {"query": "FDA drug approval healthcare", "reason": "Healthcare and drug-approval exposure", "terms": "fda|drug|healthcare"},
    "LLY": {"query": "FDA drug approval healthcare", "reason": "Healthcare and drug-approval exposure", "terms": "fda|drug|healthcare"},
    "PFE": {"query": "FDA drug approval healthcare", "reason": "Healthcare and drug-approval exposure", "terms": "fda|drug|healthcare"},
    "MRNA": {"query": "FDA drug approval healthcare", "reason": "Healthcare and drug-approval exposure", "terms": "fda|drug|healthcare"},
    "UNH": {"query": "healthcare policy Medicare", "reason": "Healthcare policy exposure", "terms": "healthcare|medicare"},
    "IONQ": {"query": "quantum computing artificial intelligence", "reason": "Quantum-computing and AI exposure", "terms": "quantum"},
    "RGTI": {"query": "quantum computing artificial intelligence", "reason": "Quantum-computing and AI exposure", "terms": "quantum"},
    "QBTS": {"query": "quantum computing artificial intelligence", "reason": "Quantum-computing and AI exposure", "terms": "quantum"},
    "NVDA": {"query": "artificial intelligence semiconductors", "reason": "AI and semiconductor exposure", "terms": "semiconductor|chip"},
    "AMD": {"query": "artificial intelligence semiconductors", "reason": "AI and semiconductor exposure", "terms": "semiconductor|chip"},
    "AVGO": {"query": "artificial intelligence semiconductors", "reason": "AI and semiconductor exposure", "terms": "semiconductor|chip"},
    "INTC": {"query": "artificial intelligence semiconductors", "reason": "AI and semiconductor exposure", "terms": "semiconductor|chip"},
    "KLAC": {"query": "semiconductors tariffs China", "reason": "Semiconductor-cycle and trade-policy exposure", "terms": "semiconductor|chip|tariff"},
    "ASML": {"query": "semiconductors China export controls", "reason": "Semiconductor-cycle and trade-policy exposure", "terms": "semiconductor|chip|export controls"},
    "MU": {"query": "semiconductors artificial intelligence", "reason": "AI and semiconductor exposure", "terms": "semiconductor|chip"},
    "JBL": {"query": "semiconductors tariffs manufacturing", "reason": "Electronics manufacturing and trade exposure", "terms": "semiconductor|chip|tariff|manufacturing"},
    "AAPL": {"query": "Apple tariffs China", "reason": "Consumer hardware and China exposure", "terms": "apple|tariff"},
    "TSLA": {"query": "electric vehicles tariffs China", "reason": "Electric-vehicle and trade exposure", "terms": "electric vehicle|tesla|tariff"},
    "RIVN": {"query": "electric vehicles tariffs", "reason": "Electric-vehicle exposure", "terms": "electric vehicle|rivian|tariff"},
    "JOBY": {"query": "aviation FAA electric aircraft", "reason": "Aviation and regulatory exposure", "terms": "aviation|faa|aircraft|joby"},
    "BA": {"query": "Boeing FAA aviation", "reason": "Aviation and regulatory exposure", "terms": "aviation|faa|aircraft|boeing"},
    "COIN": {"query": "bitcoin cryptocurrency regulation", "reason": "Crypto-market and regulatory exposure", "terms": "bitcoin|crypto|cryptocurrency"},
    "MSTR": {"query": "bitcoin cryptocurrency", "reason": "Bitcoin exposure", "terms": "bitcoin|crypto|cryptocurrency"},
    "IREN": {"query": "bitcoin cryptocurrency mining", "reason": "Bitcoin-mining exposure", "terms": "bitcoin|crypto|cryptocurrency|mining"},
    "MARA": {"query": "bitcoin cryptocurrency mining", "reason": "Bitcoin-mining exposure", "terms": "bitcoin|crypto|cryptocurrency|mining"},
    "RIOT": {"query": "bitcoin cryptocurrency mining", "reason": "Bitcoin-mining exposure", "terms": "bitcoin|crypto|cryptocurrency|mining"},
    "XOM": {"query": "oil OPEC energy", "reason": "Oil and energy exposure", "terms": "oil|opec|energy"},
    "CVX": {"query": "oil OPEC energy", "reason": "Oil and energy exposure", "terms": "oil|opec|energy"},
    "JPM": {"query": "Federal Reserve interest rates banking", "reason": "Interest-rate and banking exposure", "terms": "federal reserve|interest rate|banking"},
    "BAC": {"query": "Federal Reserve interest rates banking", "reason": "Interest-rate and banking exposure", "terms": "federal reserve|interest rate|banking"},
    "GS": {"query": "Federal Reserve interest rates banking", "reason": "Interest-rate and banking exposure", "terms": "federal reserve|interest rate|banking"},
}


def _rwa_related_event_topic(asset: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return a clearly labelled thematic search, never a claimed direct link."""
    symbol = str(asset.get("tokenSymbol") or "").upper()
    return _RWA_EVENT_TOPICS.get(symbol)


def _rwa_related_market_candidates(
    markets: List[Dict[str, Any]],
    direct_market_ids: set[str],
    topic: Optional[Dict[str, str]],
) -> List[Dict[str, Any]]:
    if not topic:
        return []
    candidates: List[Dict[str, Any]] = []
    seen = set(direct_market_ids)
    terms = [term.strip().lower() for term in str(topic.get("terms") or "").split("|") if term.strip()]
    for market in markets:
        market_id = str(market.get("market_id") or market.get("slug") or "")
        if not market_id or market_id in seen:
            continue
        question = str(market.get("question") or market.get("title") or "").lower()
        if terms and not any(term in question for term in terms):
            continue
        candidate = dict(market)
        candidate["match_type"] = "thematic_candidate"
        candidate["match_reason"] = topic["reason"]
        candidates.append(candidate)
        seen.add(market_id)
        if len(candidates) >= 6:
            break
    return candidates

AGENT_METADATA = [
    {"id": "news", "name": "News Agent", "icon": "ti-news", "color": "blue"},
    {"id": "social", "name": "Social Agent", "icon": "ti-message-circle", "color": "pink"},
    {"id": "reddit", "name": "Reddit Agent", "icon": "ti-brand-reddit", "color": "coral"},
    {"id": "research", "name": "Research Agent", "icon": "ti-microscope", "color": "purple"},
    {"id": "macro", "name": "Macro Agent", "icon": "ti-building-bank", "color": "green"},
    {"id": "onchain", "name": "On-Chain Agent", "icon": "ti-link", "color": "teal"},
    {"id": "market", "name": "Market Agent", "icon": "ti-chart-candle", "color": "amber"}
]

@router.get("/healthz")
async def healthz():
    return {"status": "ok", "message": "Forecast AI API Server active."}

@router.get("/integrations/status")
async def integrations_status(
    request: Request,
    pipeline: ForecastPipeline = Depends(get_pipeline),
):
    """Return safe configuration and runtime health without exposing secrets."""
    enforce_request_access(request)
    from ..sources.facts_ai import get_facts_ai_runtime_status
    from ..sources.falcon import get_falcon_runtime_status
    from ..sources.bravado import get_bravado_runtime_status

    facts_runtime = get_facts_ai_runtime_status()
    falcon_runtime = get_falcon_runtime_status()
    bravado_runtime = get_bravado_runtime_status()
    facts_configured = bool(getattr(pipeline.config.facts_ai, "api_key", ""))
    falcon_configured = bool(getattr(pipeline.config.falcon, "api_token", ""))
    bravado_configured = bool(
        getattr(pipeline.config.bravado, "enabled", False)
        and getattr(pipeline.config.bravado, "api_url", "")
    )

    return {
        "facts_ai": {
            "enabled": bool(getattr(pipeline.config.facts_ai, "enabled", False)),
            "configured": facts_configured,
            **facts_runtime,
        },
        "falcon": {
            "enabled": bool(getattr(pipeline.config.falcon, "enabled", False)),
            "configured": falcon_configured,
            "social_enabled": bool(getattr(pipeline.config.falcon, "social_enabled", False)),
            "smart_money_enabled": bool(getattr(pipeline.config.falcon, "smart_money_enabled", False)),
            **falcon_runtime,
        },
        "bravado": {
            "enabled": bool(getattr(pipeline.config.bravado, "enabled", False)),
            "configured": bravado_configured,
            "authenticated": bool(getattr(pipeline.config.bravado, "api_token", "")),
            "leaderboard_window": getattr(pipeline.config.bravado, "leaderboard_window", "30d"),
            "scan_limit": getattr(pipeline.config.bravado, "scan_limit", 12),
            **bravado_runtime,
        },
        "tavily": {
            "enabled": bool(getattr(pipeline.config.tavily, "enabled", False)),
            "configured": bool(getattr(pipeline.config.tavily, "api_key", "")),
            "status": "configured" if (
                getattr(pipeline.config.tavily, "enabled", False)
                and getattr(pipeline.config.tavily, "api_key", "")
            ) else "disabled",
            "last_checked_at": None,
            "message": None,
        },
    }

@router.get("/agents/meta")
async def get_agents_metadata():
    """
    Returns static metadata for all 7 agents,
    including Tabler icon identifiers and brand color pairings.
    """
    return AGENT_METADATA

@router.get("/markets/browse")
async def browse_markets_route(
    request: Request,
    venue: str = Query("all", description="Venue filter: kalshi, polymarket, or all"),
    category: Optional[str] = Query(None, description="Category filter"),
    sort: str = Query("volume", description="Sort by: volume, ending_soon, newest"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(24, ge=1, le=50, description="Page size"),
    q: Optional[str] = Query(None, description="Optional keyword search"),
    search_service: MarketSearchService = Depends(get_search_service)
) -> Dict[str, Any]:
    """
    GET /markets/browse
    Full browse endpoint for markets with pagination, sorting, and unified categories.
    """
    enforce_request_access(request)
    return await search_service.browse_markets(
        venue=venue.lower(),
        category=category,
        sort=sort.lower(),
        page=page,
        page_size=page_size,
        q=q
    )

@router.get("/markets/search")
async def search_markets(
    request: Request,
    q: Optional[str] = Query(None, description="Query text to search across prediction markets"),
    limit: int = Query(10, ge=1, le=50),
    search_service: MarketSearchService = Depends(get_search_service)
) -> List[Dict[str, Any]]:
    """
    GET /markets/search?q=<query>
    Searches open prediction markets on Kalshi & Polymarket matching the query text.
    IP-based rate limited.
    """
    enforce_request_access(request)
    return await search_service.search_markets(query=q, limit=limit)


@router.get("/rwa/assets")
async def browse_rwa_assets(
    request: Request,
    q: Optional[str] = Query(None, description="Stock Token symbol or company name"),
    limit: int = Query(48, ge=1, le=250),
    offset: int = Query(0, ge=0),
    stock_tokens: RobinhoodStockTokenClient = Depends(get_stock_token_client),
) -> Dict[str, Any]:
    """Browse the canonical Robinhood Chain Stock Token catalog without quotes."""
    enforce_request_access(request)
    assets = await stock_tokens.assets()
    query = (q or "").strip().lower()
    visible = [
        asset for asset in assets
        if isinstance(asset, dict)
        and (not query or query in str(asset.get("tokenSymbol") or "").lower() or query in RobinhoodStockTokenClient.display_name(asset).lower())
    ]
    visible.sort(key=lambda item: str(item.get("tokenSymbol") or ""))
    page = visible[offset: offset + limit]
    return {
        "assets": [RobinhoodStockTokenClient.public_asset(asset) for asset in page],
        "total": len(visible),
        "offset": offset,
        "limit": limit,
        "source": "Robinhood Chain Stock Token APIs",
    }


@router.get("/rwa/assets/{symbol}")
async def rwa_asset_detail(
    symbol: str,
    request: Request,
    stock_tokens: RobinhoodStockTokenClient = Depends(get_stock_token_client),
    search_service: MarketSearchService = Depends(get_search_service),
) -> Dict[str, Any]:
    """Return one Stock Token with its live quote and transparently matched market context."""
    enforce_request_access(request)
    asset = await stock_tokens.asset(symbol)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Stock Token '{symbol.upper()}' was not found.")

    display_name = RobinhoodStockTokenClient.display_name(asset)
    normalized_symbol = str(asset.get("tokenSymbol") or symbol).upper()
    warnings: List[str] = []

    async def safe_quote() -> Optional[Dict[str, Any]]:
        try:
            return await asyncio.wait_for(stock_tokens.quote(normalized_symbol), timeout=12)
        except Exception as exc:
            logger.warning("RWA quote unavailable for %s: %s", normalized_symbol, exc)
            warnings.append("Live Robinhood quote is temporarily unavailable.")
            return None

    async def safe_market_search(query: str, label: str) -> List[Dict[str, Any]]:
        try:
            return await asyncio.wait_for(search_service.search_markets(query=query, limit=20), timeout=12)
        except Exception as exc:
            logger.warning("RWA %s market search unavailable for %s: %s", label, normalized_symbol, exc)
            warnings.append("Prediction-market context is temporarily unavailable.")
            return []

    topic = _rwa_related_event_topic(asset)
    direct_query = f"{display_name} {normalized_symbol}"
    quote_task = asyncio.create_task(safe_quote())
    direct_task = asyncio.create_task(safe_market_search(direct_query, "direct"))
    topic_task = asyncio.create_task(safe_market_search(topic["query"], "thematic")) if topic else None
    quote, direct_results = await asyncio.gather(quote_task, direct_task)
    thematic_results = await topic_task if topic_task else []
    direct_markets = _rwa_market_matches(asset, direct_results)
    direct_market_ids = {str(market.get("market_id") or market.get("slug") or "") for market in direct_markets}
    related_markets = _rwa_related_market_candidates(thematic_results, direct_market_ids, topic)
    return {
        "asset": RobinhoodStockTokenClient.public_asset(asset, quote),
        "prediction_markets": direct_markets,
        "related_prediction_markets": related_markets,
        "market_match_method": "direct symbol or company-name text match",
        "related_market_method": "thematic candidate, not a direct company-market match" if topic else None,
        "warnings": list(dict.fromkeys(warnings)),
        "source": "Robinhood Chain Stock Token APIs",
    }

@router.post("/predict")
async def predict(
    request: Request,
    req: PredictionRequest, 
    pipeline: ForecastPipeline = Depends(get_pipeline)
):
    enforce_request_access(request)
    try:
        selected_venue = req.source_venue or req.venue
        result = await pipeline.run_forecast(
            question=req.question, 
            market_id=req.market_id,
            is_public_feed=False,
            model_override=req.model_override,
            # FactsAI credentials are deployment secrets, never caller input.
            facts_key=None,
            venue=selected_venue,
            category=req.category,
            market_closes_at=req.market_closes_at,
            agent_runtime=req.agent_runtime,
            analysis_mode=req.analysis_mode,
        )
        agent_breakdown = [
            {
                "id": p.agent_name,
                "name": p.agent_name,
                "agent": p.agent_name,
                "probability": p.probability,
                "confidence": p.confidence.score,
                "reasoning": p.reasoning,
                "summary": p.summary,
                "key_drivers": p.key_drivers,
                "counter_signals": p.counter_signals,
                "uncertainties": p.uncertainties,
                "watch_next": p.watch_next,
                "warnings": p.confidence.warnings,
                "citations": p.citations,
                "providers": p.research_providers,
                "provider_insights": p.provider_insights,
                "provider_statuses": p.provider_statuses,
            }
            for p in result.individual_predictions
        ]
        return {
            "question": req.question,
            "market_id": result.market_id,
            "venue": selected_venue,
            "source_venue": selected_venue,
            "probability": result.probability,
            "recommendation": "YES" if result.probability >= 0.5 else "NO",
            "confidence": {
                "score": result.confidence.score,
                "warnings": result.confidence.warnings
            },
            "reasoning": result.metadata.get("summary_reasoning", ""),
            "reasoning_trace": {
                "agent_contributions": result.reasoning_trace.agent_contributions,
                "aggregation_steps": result.reasoning_trace.aggregation_steps,
                "conflicts_resolved": result.reasoning_trace.conflicts_resolved,
            },
            "market_context": result.metadata.get("market_context", []),
            "opportunity_radar": result.metadata.get("opportunity_radar", {}),
            "outcome_graph": result.metadata.get("outcome_graph", {}),
            "proof": result.metadata.get("proof", {}),
            "agent_runtime": result.metadata.get("agent_runtime"),
            "analysis_mode": result.metadata.get("analysis_mode"),
            "timestamp": result.timestamp.isoformat(),
            "agent_breakdown": agent_breakdown,
            "individual_predictions": agent_breakdown,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/forecasts")
async def get_forecasts(request: Request, pipeline: ForecastPipeline = Depends(get_pipeline)):
    require_server_api_key(request)
    try:
        return pipeline.memory_store.list_forecasts()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stats")
async def get_stats(request: Request, pipeline: ForecastPipeline = Depends(get_pipeline)):
    require_server_api_key(request)
    try:
        forecasts = pipeline.memory_store.list_forecasts()
        reputations = pipeline.memory_store.get_agent_reputations()
        
        avg_conf = 0.0
        if forecasts:
            avg_conf = sum(f.get("confidence", {}).get("score", 0.5) for f in forecasts) / len(forecasts)

        return {
            "total_forecasts": len(forecasts),
            "average_confidence": avg_conf,
            "agent_reputations": reputations
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/proof/track-record")
async def proof_track_record(request: Request, pipeline: ForecastPipeline = Depends(get_pipeline)):
    require_server_api_key(request)
    if _proof_publisher is not None and _proof_publisher.configured:
        try:
            durable = await _proof_publisher.track_record()
            if durable:
                return durable
        except Exception as exc:
            fallback_error = str(exc)[:240]
        else:
            fallback_error = None
    else:
        fallback_error = None
    fallback = pipeline.memory_store.get_track_record()
    fallback["source"] = "local_memory"
    if fallback_error:
        fallback["onchain_error"] = fallback_error
    return fallback

@router.get("/proof/publisher-status")
async def proof_publisher_status(request: Request):
    require_server_api_key(request)
    status = get_proof_publisher_status()
    status["configured"] = bool(_proof_publisher and _proof_publisher.configured)
    if _proof_publisher:
        status["chain_id"] = _proof_publisher.config.chain_id
        status["registry_address"] = _proof_publisher.config.registry_address or None
        status["explorer_url"] = _proof_publisher.config.explorer_url or None
    return status

@router.post("/proof/publish-pending")
async def publish_pending_proofs(request: Request):
    require_server_api_key(request)
    if _proof_publisher is None or not _proof_publisher.configured:
        raise HTTPException(status_code=503, detail="Proof publisher is not fully configured.")
    return await _proof_publisher.publish_pending()

@router.post("/proof/resolve")
async def resolve_forecasts(
    request: Request,
    req: ResolveForecastRequest,
    pipeline: ForecastPipeline = Depends(get_pipeline),
):
    require_server_api_key(request)
    if req.outcome not in (0, 1):
        raise HTTPException(status_code=400, detail="Outcome must be 0 or 1.")
    resolved = pipeline.memory_store.resolve_market_forecasts(
        market_id=req.market_id,
        outcome=req.outcome,
        resolution_source=req.resolution_source,
        resolved_at=req.resolved_at,
    )
    if resolved:
        await pipeline.queue_onchain_resolution(
            req.market_id, req.outcome, req.resolution_source, req.resolved_at
        )
    return {"market_id": req.market_id, "resolved_count": len(resolved), "forecasts": resolved}

@router.post("/proof/resolve-due")
async def resolve_due_forecasts(request: Request, pipeline: ForecastPipeline = Depends(get_pipeline)):
    require_server_api_key(request)
    return await pipeline.resolve_due_forecasts()

@router.post("/reputation/calibrate")
async def calibrate_reputation(request: Request, req: CalibrateRequest, pipeline: ForecastPipeline = Depends(get_pipeline)):
    require_server_api_key(request)
    try:
        pipeline.memory_store.update_agent_reputation(
            agent_name=req.agent_name,
            outcome_correct=req.outcome_correct,
            error_delta=req.error_delta
        )
        return {"status": "success", "new_reputation": pipeline.memory_store.get_agent_reputation(req.agent_name)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/config")
async def get_config(request: Request, pipeline: ForecastPipeline = Depends(get_pipeline)):
    require_server_api_key(request)
    # Redact sensitive keys
    cfg = pipeline.config
    providers_redacted = {}
    for name, p in cfg.providers.items():
        providers_redacted[name] = {
            "provider": p.provider,
            "model_id": p.model_id,
            "api_key": "********" if p.api_key else ""
        }
    return {
        "default_provider": cfg.default_provider,
        "providers": providers_redacted,
        "polymarket": {
            "gamma_api_url": cfg.polymarket.gamma_api_url,
            "clob_api_url": cfg.polymarket.clob_api_url,
            "wallet_address": getattr(cfg.polymarket, "wallet_address", ""),
            "builder_code": getattr(cfg.polymarket, "builder_code", "")
        },
        "agents": {
            name: {
                "enabled": a.enabled,
                "provider": a.provider,
                "weight": a.weight
            } for name, a in cfg.agents.items()
        }
    }
