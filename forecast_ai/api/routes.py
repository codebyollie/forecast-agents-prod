"""
API Routes for Forecast AI API Server.
"""

import os
import time
import secrets
from fastapi import APIRouter, HTTPException, Depends, Request, Query
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from ..pipelines.forecast import ForecastPipeline
from ..polymarket.gamma import GammaClient
from ..services.market_search import MarketSearchService
from ..proof.publisher import ProofPublisher, get_proof_publisher_status

router = APIRouter()

# Global reference to pipeline, will be set during server init
_pipeline: Optional[ForecastPipeline] = None
_proof_publisher: Optional[ProofPublisher] = None

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
