"""Source-level dependency analysis for multi-agent consensus.

The agents are role-separated, but a source can still reach more than one role.
This module identifies that shared provenance and reduces its aggregate influence
without discarding a relevant agent view.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Set
from urllib.parse import urlsplit, urlunsplit

from ..models.prediction import Prediction


def _canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        host = parsed.netloc.lower().removeprefix("www.")
        path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
        if not host:
            return ""
        return urlunsplit((parsed.scheme.lower() or "https", host, path, "", ""))
    except Exception:
        return ""


def _content_fingerprint(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    normalized = re.sub(r"[^a-z0-9 .:%$+-]", "", normalized)
    if len(normalized) < 32:
        return ""
    digest = hashlib.sha256(normalized[:1200].encode("utf-8")).hexdigest()[:16]
    return f"content:{digest}"


def _title_fingerprint(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    normalized = re.sub(r"[^a-z0-9 ]", "", normalized)
    if len(normalized) < 24 or normalized in {"source", "research source", "news source"}:
        return ""
    return f"title:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def _prediction_sources(prediction: Prediction) -> Set[str]:
    sources: Set[str] = set()
    for evidence in prediction.evidence_used:
        metadata = evidence.metadata or {}
        if float(evidence.relevance_score or 0) <= 0 or str(metadata.get("source_type") or "").lower() == "status":
            continue
        source_id = _title_fingerprint(evidence.title or "") or _canonical_url(evidence.url or "") or _content_fingerprint(evidence.content or "")
        if source_id:
            sources.add(source_id)
    for citation in prediction.citations:
        source_id = _title_fingerprint(str(citation.get("title") or "")) or _canonical_url(str(citation.get("url") or ""))
        if source_id:
            sources.add(source_id)
    return sources


def analyze_evidence_dependencies(predictions: Iterable[Prediction]) -> Dict[str, Any]:
    prediction_list = list(predictions)
    by_agent = {prediction.agent_name: _prediction_sources(prediction) for prediction in prediction_list}
    source_agents: Dict[str, Set[str]] = defaultdict(set)
    for agent_name, sources in by_agent.items():
        for source in sources:
            source_agents[source].add(agent_name)

    shared = {source: agents for source, agents in source_agents.items() if len(agents) > 1}
    multipliers: Dict[str, float] = {}
    burdens: Dict[str, float] = {}
    total_occurrences = sum(len(sources) for sources in by_agent.values())
    duplicate_occurrences = sum(len(agents) - 1 for agents in shared.values())

    for agent_name, sources in by_agent.items():
        if not sources:
            burdens[agent_name] = 0.0
            multipliers[agent_name] = 1.0
            continue
        burden = sum((len(source_agents[source]) - 1) / len(source_agents[source]) for source in sources if len(source_agents[source]) > 1)
        normalized_burden = min(1.0, burden / len(sources))
        burdens[agent_name] = round(normalized_burden, 4)
        multipliers[agent_name] = round(max(0.55, 1.0 - 0.45 * normalized_burden), 4)

    overlap_ratio = duplicate_occurrences / total_occurrences if total_occurrences else 0.0
    clusters: List[Dict[str, Any]] = [
        {"source_id": source, "agents": sorted(agents), "agent_count": len(agents)}
        for source, agents in sorted(shared.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    return {
        "version": "1.0",
        "unique_source_count": len(source_agents),
        "shared_source_count": len(shared),
        "duplicate_occurrences": duplicate_occurrences,
        "overlap_ratio": round(overlap_ratio, 4),
        "confidence_penalty": round(min(0.12, overlap_ratio * 0.12), 4),
        "agent_source_counts": {agent: len(sources) for agent, sources in by_agent.items()},
        "agent_dependency_burdens": burdens,
        "agent_weight_multipliers": multipliers,
        "influence_cap_multiple": 1.75,
        "clusters": clusters[:25],
    }
