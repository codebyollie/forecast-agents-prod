"""Normalize provider telemetry into stable product-facing states."""

from typing import Dict


def normalize_provider_statuses(statuses: Dict[str, str]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for provider, raw_status in statuses.items():
        status = str(raw_status or "").strip().lower()
        if status in {"active", "fallback", "used", "success"}:
            normalized[provider] = "used"
        elif status in {"empty", "irrelevant", "no_match", "not_relevant"}:
            normalized[provider] = "irrelevant"
        elif status in {"unavailable", "unauthorized", "disabled", "not_configured"}:
            normalized[provider] = "unavailable"
        else:
            normalized[provider] = "failed"
    return normalized

