"""Optional website campaign scoring callback; never executes trades."""

import os
from urllib.parse import urlsplit

import httpx


async def resolve_website_activities() -> bool:
    url = os.getenv("FORECAST_ACTIVITY_RESOLVER_URL", "").strip()
    secret = os.getenv("FORECAST_ACTIVITY_CRON_SECRET", "").strip()
    if not url or not secret:
        return False
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Activity resolver requires an HTTPS URL without embedded credentials")
    if parsed.path != "/api/activities/resolve" or parsed.query or parsed.fragment:
        raise ValueError("Activity resolver URL must end in /api/activities/resolve")
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        response = await client.post(url, headers={"x-forecast-cron-secret": secret})
        response.raise_for_status()
    return True
