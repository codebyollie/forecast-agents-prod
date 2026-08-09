"""Deterministic Opportunity Radar built from forecast and evidence metadata.

No LLM call is made here. The radar only exposes values that are present in the
selected market, partner evidence, or consensus output, so missing intelligence
is explicit instead of being invented for the UI.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ..models.evidence import Evidence
from ..models.forecast import ForecastResult


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _probability(value: Any) -> Optional[float]:
    number = _number(value)
    if number is None:
        return None
    if number > 1:
        number /= 100.0
    return round(max(0.0, min(1.0, number)), 4)


def _first_number(mapping: Dict[str, Any], names: Iterable[str]) -> Optional[float]:
    for name in names:
        number = _number(mapping.get(name))
        if number is not None:
            return number
    return None


def _native_market(evidence: List[Evidence]) -> Optional[Evidence]:
    return next((item for item in evidence if item.source_name in ("polymarket", "kalshi")), None)


def _market_probability(market: Optional[Evidence]) -> Optional[float]:
    if market is None:
        return None
    metadata = market.metadata or {}
    outcomes = metadata.get("outcomes")
    if isinstance(outcomes, list) and outcomes:
        first = outcomes[0] if isinstance(outcomes[0], dict) else {}
        probability = _probability(first.get("price"))
        if probability is not None:
            return probability
    return _probability(metadata.get("current_price"))


def _provider_states(evidence: List[Evidence]) -> List[Dict[str, Any]]:
    states: Dict[str, Dict[str, Any]] = {}
    for item in evidence:
        metadata = item.metadata or {}
        provider = str(metadata.get("provider") or "").strip()
        if not provider:
            continue
        status = str(metadata.get("status") or "active")
        existing = states.get(provider)
        if existing is None or (existing["status"] != "active" and status == "active"):
            states[provider] = {
                "provider": provider,
                "status": status,
                "partner": bool(metadata.get("partner")),
            }
    return sorted(states.values(), key=lambda item: item["provider"].lower())


def build_opportunity_radar(
    result: ForecastResult,
    evidence: List[Evidence],
    question: str,
    venue: Optional[str],
) -> Dict[str, Any]:
    market = _native_market(evidence)
    market_metadata = market.metadata if market else {}
    market_probability = _market_probability(market)
    ai_probability = round(float(result.probability), 4)
    edge = round(ai_probability - market_probability, 4) if market_probability is not None else None

    falcon_items = [item for item in evidence if (item.metadata or {}).get("provider") == "Falcon"]
    falcon_signals: Dict[str, Any] = {}
    for item in falcon_items:
        signals = (item.metadata or {}).get("signals")
        if isinstance(signals, dict):
            falcon_signals.update({key: value for key, value in signals.items() if value is not None})

    bravado_items = [item for item in evidence if (item.metadata or {}).get("provider") == "Bravado"]
    bravado_signals: Dict[str, Any] = {}
    for item in bravado_items:
        signals = (item.metadata or {}).get("signals")
        if isinstance(signals, dict):
            bravado_signals.update({key: value for key, value in signals.items() if value is not None})

    volume = _first_number(market_metadata, ("volume", "volume_total"))
    if volume is None:
        volume = _first_number(falcon_signals, ("volume", "volume_total"))
    liquidity = _first_number(market_metadata, ("liquidity",))
    if liquidity is None:
        liquidity = _first_number(falcon_signals, ("liquidity",))

    spread = None
    order_books = market_metadata.get("order_books")
    if isinstance(order_books, list):
        spreads = [
            _number(item.get("spread"))
            for item in order_books
            if isinstance(item, dict) and _number(item.get("spread")) is not None
        ]
        if spreads:
            spread = round(sum(spreads) / len(spreads), 4)
    if spread is None:
        spread = _number(market_metadata.get("spread"))

    concentration = _first_number(falcon_signals, ("holder_concentration", "concentration"))
    risk_flags: List[str] = []
    if spread is not None and spread > 0.08:
        risk_flags.append("wide_spread")
    if liquidity is not None and liquidity < 10_000:
        risk_flags.append("low_liquidity")
    if concentration is not None and concentration > 0.5:
        risk_flags.append("high_concentration")

    confidence = round(float(result.confidence.score), 4)
    edge_strength = min(1.0, abs(edge) / 0.20) if edge is not None else 0.0
    liquidity_factor = 0.5
    if liquidity is not None:
        liquidity_factor = min(1.0, max(0.0, liquidity / 100_000.0))
    elif volume is not None:
        liquidity_factor = min(1.0, max(0.0, volume / 250_000.0))
    bravado_wallet_count = int(_number(bravado_signals.get("smart_money_wallet_count")) or 0)
    bravado_direction = str(bravado_signals.get("smart_money_direction") or "").upper()
    bravado_confirmed = bravado_wallet_count > 0 and bravado_direction in {"YES", "NO", "MIXED"}
    falcon_confirmed = bool(falcon_signals.get("smart_money_wallet_count")) or any(
        key in falcon_signals for key in ("falcon_score", "win_rate", "roi", "total_pnl")
    )
    smart_money_confirmed = bravado_confirmed or falcon_confirmed
    forecast_direction = "YES" if edge is not None and edge >= 0 else "NO" if edge is not None else None
    smart_money_aligned = (
        bravado_direction == forecast_direction
        if bravado_direction in {"YES", "NO"} and forecast_direction
        else None
    )
    if smart_money_aligned is False:
        risk_flags.append("smart_money_divergence")
    smart_money_factor = 1.0 if smart_money_aligned is True else 0.25 if smart_money_aligned is False else 0.5
    risk_penalty = min(0.45, len(risk_flags) * 0.15)
    score = round(max(0.0, min(1.0,
        edge_strength * 0.40
        + confidence * 0.30
        + liquidity_factor * 0.20
        + smart_money_factor * 0.10
        - risk_penalty
    )) * 100, 1)

    social_present = any((item.metadata or {}).get("source_type") == "social_intelligence" for item in falcon_items)
    social_failed = any(
        (item.metadata or {}).get("source_type") == "social_status"
        and (item.metadata or {}).get("status") == "unavailable"
        for item in falcon_items
    )
    smart_money_failed = any(
        (item.metadata or {}).get("source_type") == "smart_money_status"
        and (item.metadata or {}).get("status") == "unavailable"
        for item in falcon_items
    )
    bravado_failed = any(
        (item.metadata or {}).get("source_type") == "trader_status"
        and (item.metadata or {}).get("status") == "unavailable"
        for item in bravado_items
    )
    social_acceleration = _number(falcon_signals.get("acceleration"))
    social_trend = falcon_signals.get("narrative_trend")
    if social_trend is None and social_acceleration is not None:
        social_trend = (
            "surging" if social_acceleration >= 1.5
            else "rising" if social_acceleration > 1.0
            else "fading" if social_acceleration < 0.8
            else "stable"
        )
    return {
        "version": "1.0",
        "question": question,
        "market": {
            "id": result.market_id,
            "venue": venue or (market_metadata.get("venue") if market else None),
            "title": market.title if market else question,
            "url": market.url if market else None,
            "probability": market_probability,
            "volume": volume,
            "liquidity": liquidity,
            "spread": spread,
            "concentration": concentration,
        },
        "forecast": {
            "probability": ai_probability,
            "confidence": confidence,
            "edge": edge,
            "direction": forecast_direction,
        },
        "signals": {
            "smart_money": {
                "status": "available" if smart_money_confirmed else "unavailable" if (smart_money_failed or bravado_failed) else "not_available",
                "metrics": ({
                    key: falcon_signals[key]
                    for key in (
                        "falcon_score", "win_rate", "roi", "total_pnl",
                        "smart_money_wallet_count", "smart_money_wallets",
                    )
                    if key in falcon_signals
                } | {
                    key: bravado_signals[key]
                    for key in (
                        "smart_money_wallet_count", "smart_money_direction",
                        "smart_money_position_value_usdc", "smart_money_average_win_rate",
                        "smart_money_wallets", "smart_money_traders",
                        "leaderboard_window", "leaderboard_wallets_scanned",
                    )
                    if key in bravado_signals
                } | {
                    "source": "Bravado" if bravado_confirmed else "Falcon" if falcon_confirmed else None,
                    "aligned_with_swarm": smart_money_aligned,
                }),
            },
            "social": {
                "status": "available" if social_present else "unavailable" if social_failed else "not_enabled",
                "trend": social_trend,
                "sentiment": falcon_signals.get("sentiment_score"),
                "mentions": falcon_signals.get("mention_volume", falcon_signals.get("tweet_count")),
                "acceleration": social_acceleration,
                "author_diversity_pct": _number(falcon_signals.get("author_diversity_pct")),
                "price_divergence": falcon_signals.get("price_sentiment_divergence"),
            },
        },
        "risk": {
            "level": "high" if len(risk_flags) >= 2 else "medium" if risk_flags else "low",
            "flags": risk_flags,
        },
        "score": score,
        "providers": _provider_states(evidence),
        "proof": {
            "status": "pending",
            "network": "Robinhood Chain",
            "message": "ForecastRegistry is not deployed yet.",
        },
    }
