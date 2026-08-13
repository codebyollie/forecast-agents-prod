"""Bravado partner connector for Polymarket trader intelligence."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import httpx

from .base import BaseSource
from ..models.evidence import Evidence


_RUNTIME_STATUS: Dict[str, Any] = {
    "status": "not_checked",
    "last_checked_at": None,
    "message": None,
}


def get_bravado_runtime_status() -> Dict[str, Any]:
    return dict(_RUNTIME_STATUS)


def _record_status(status: str, message: Optional[str] = None) -> None:
    _RUNTIME_STATUS.update({
        "status": status,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
    })


class BravadoError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Bravado Error {status_code}: {message}")


class BravadoSource(BaseSource):
    """Find proven leaderboard traders positioned in a selected Polymarket market."""

    def __init__(
        self,
        api_token: str,
        api_url: str = "https://partner-api.bravadotrade.com/trader-analytics",
        timeout_seconds: float = 20.0,
        leaderboard_window: str = "all",
        scan_limit: int = 12,
        min_trades: int = 20,
    ):
        self.api_token = api_token
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.leaderboard_window = leaderboard_window
        self.scan_limit = max(1, min(int(scan_limit), 30))
        self.min_trades = max(1, int(min_trades))
        self._leaderboard_cache: Optional[Dict[str, Any]] = None
        self._leaderboard_cached_at = 0.0
        self._positions_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        _record_status("requested")
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    f"{self.api_url}/{path.lstrip('/')}",
                    headers=headers,
                    params=params or {},
                )
        except Exception as exc:
            _record_status("unavailable", str(exc)[:240])
            raise BravadoError(503, f"Network error calling Bravado: {exc}") from exc

        if response.status_code != 200:
            try:
                payload = response.json()
                message = str(payload.get("error") or payload.get("message") or payload)
            except Exception:
                message = response.text[:240]
            status = "unauthorized" if response.status_code in (401, 403) else "unavailable"
            _record_status(status, message[:240])
            raise BravadoError(response.status_code, message)
        try:
            payload = response.json()
        except Exception as exc:
            _record_status("unavailable", "Bravado returned invalid JSON.")
            raise BravadoError(502, "Bravado returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise BravadoError(502, "Bravado returned a non-object response.")
        _record_status("active")
        return payload

    async def _leaderboard(self) -> Dict[str, Any]:
        if self._leaderboard_cache is not None and time.time() - self._leaderboard_cached_at < 900:
            return self._leaderboard_cache
        payload = await self._get("leaderboard", {
            "window": self.leaderboard_window,
            # Fetch a wider cohort, then spend position requests only on traders
            # that the leaderboard reports as having active positions.
            "limit": max(50, self.scan_limit),
            "offset": 0,
            "min_trades": self.min_trades,
            "exclude_bots": "true",
        })
        self._leaderboard_cache = payload
        self._leaderboard_cached_at = time.time()
        return payload

    async def _active_positions(self, wallet: str) -> Dict[str, Any]:
        normalized = wallet.lower()
        cached = self._positions_cache.get(normalized)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        page_size = 500
        payload = await self._get(
            f"traders/{normalized}/positions/active",
            {"limit": page_size, "offset": 0},
        )
        positions = self._records(payload, "positions", "results")
        total = int(self._number(payload.get("total")) or len(positions))
        offset = len(positions)
        # Bravado supports paginated position responses. Cap collection at
        # 1,000 positions per trader to keep partner API usage predictable.
        while offset < total and offset < 1000 and positions:
            page = await self._get(
                f"traders/{normalized}/positions/active",
                {"limit": page_size, "offset": offset},
            )
            page_positions = self._records(page, "positions", "results")
            if not page_positions:
                break
            positions.extend(page_positions)
            offset += len(page_positions)
        payload = dict(payload)
        payload["positions"] = positions
        payload["total"] = total
        self._positions_cache[normalized] = (time.time(), payload)
        return payload

    @staticmethod
    def _records(payload: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _wallet(row: Dict[str, Any]) -> str:
        value = str(row.get("trader") or row.get("address") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return f"0x{value}"
        return value if re.fullmatch(r"0x[0-9a-f]{40}", value) else ""

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalized(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    @classmethod
    def _matches_market(
        cls,
        position: Dict[str, Any],
        market_id: str,
        condition_id: Optional[str],
        question: str,
    ) -> bool:
        position_id = cls._normalized(str(position.get("market_id") or ""))
        expected_ids = {
            cls._normalized(value)
            for value in (market_id, condition_id or "")
            if value
        }
        if position_id and position_id in expected_ids:
            return True
        position_question = str(position.get("question") or "").strip().lower()
        target_question = question.strip().lower()
        if not position_question or not target_question:
            return False
        return SequenceMatcher(None, position_question, target_question).ratio() >= 0.88

    @staticmethod
    def _display_content(question: str, matches: List[Dict[str, Any]], direction: str) -> str:
        if not matches:
            return (
                f"Bravado checked ranked, non-bot Polymarket traders for: {question}.\n"
                "No matching active position was found in the scanned leaderboard cohort."
            )
        lines = [
            f"Bravado Smart Money for: {question}",
            f"Matched ranked traders: {len(matches)} | Aggregate direction: {direction}",
        ]
        for match in matches[:5]:
            wallet = match["wallet"]
            label = match.get("username") or f"{wallet[:6]}…{wallet[-4:]}"
            details = [
                f"#{match.get('rank', '—')} {label}",
                str(match.get("outcome") or "unknown side").upper(),
            ]
            if match.get("value_usdc") is not None:
                details.append(f"position ${match['value_usdc']:,.2f}")
            if match.get("win_rate") is not None:
                details.append(f"win rate {match['win_rate']:.1f}%")
            if match.get("total_pnl") is not None:
                details.append(f"PnL ${match['total_pnl']:,.2f}")
            lines.append("- " + " | ".join(details))
        return "\n".join(lines)

    async def fetch_market_trader_intelligence(
        self,
        market_id: str,
        question: str,
        condition_id: Optional[str] = None,
    ) -> List[Evidence]:
        leaderboard = await self._leaderboard()
        leaderboard_rows = self._records(leaderboard, "results")
        leaders = [
            row for row in leaderboard_rows
            if self._wallet(row)
            and not bool(row.get("is_mm_bot"))
            and (
                row.get("active_positions") is None
                or (self._number(row.get("active_positions")) or 0) > 0
            )
        ][:self.scan_limit]
        semaphore = asyncio.Semaphore(5)

        async def inspect(row: Dict[str, Any]) -> List[Dict[str, Any]]:
            wallet = self._wallet(row)
            try:
                async with semaphore:
                    payload = await self._active_positions(wallet)
            except Exception:
                return []
            matches: List[Dict[str, Any]] = []
            for position in self._records(payload, "positions", "results"):
                if not self._matches_market(position, market_id, condition_id, question):
                    continue
                win_rate = self._number(row.get("win_rate"))
                if win_rate is not None and win_rate <= 1:
                    win_rate *= 100
                matches.append({
                    "wallet": wallet,
                    "username": str(row.get("username") or "").strip() or None,
                    "rank": row.get("rank"),
                    "outcome": str(position.get("outcome") or "").strip().upper(),
                    "value_usdc": self._number(position.get("value_usdc")),
                    "shares": self._number(position.get("shares")),
                    "avg_cost_cents": self._number(position.get("avg_cost_cents")),
                    "mark_price_cents": self._number(position.get("mark_price_cents")),
                    "win_rate": win_rate,
                    "total_pnl": self._number(row.get("total_pnl") or row.get("pnl")),
                    "trades": row.get("trades"),
                })
            return matches

        nested = await asyncio.gather(*(inspect(row) for row in leaders))
        matches = [match for group in nested for match in group]
        side_values = {"YES": 0.0, "NO": 0.0}
        for match in matches:
            side = match.get("outcome")
            if side in side_values:
                side_values[side] += max(0.0, match.get("value_usdc") or match.get("shares") or 1.0)
        if side_values["YES"] > side_values["NO"] * 1.15:
            direction = "YES"
        elif side_values["NO"] > side_values["YES"] * 1.15:
            direction = "NO"
        elif matches:
            direction = "MIXED"
        else:
            direction = "NO_MATCH"

        total_value = sum(max(0.0, match.get("value_usdc") or 0.0) for match in matches)
        win_rates = [match["win_rate"] for match in matches if match.get("win_rate") is not None]
        signals = {
            "smart_money_wallet_count": len({match["wallet"] for match in matches}),
            "smart_money_direction": direction,
            "smart_money_position_value_usdc": round(total_value, 2),
            "smart_money_average_win_rate": round(sum(win_rates) / len(win_rates), 2) if win_rates else None,
            "smart_money_wallets": [match["wallet"] for match in matches[:10]],
            "smart_money_traders": matches[:10],
            "leaderboard_window": self.leaderboard_window,
            "leaderboard_wallets_considered": len(leaderboard_rows),
            "leaderboard_wallets_scanned": len(leaders),
        }
        return [Evidence(
            source_name="bravado_trader_intelligence",
            content=self._display_content(question, matches, direction),
            relevance_score=0.96 if matches else 0.35,
            title="Bravado Trader Intelligence",
            url=self.api_url,
            metadata={
                "provider": "Bravado",
                "source_type": "trader_intelligence",
                "status": "active",
                "partner": True,
                "market_id": market_id,
                "condition_id": condition_id,
                "signals": signals,
            },
        )]

    async def fetch(self, query: str, limit: int = 5) -> List[Evidence]:
        # Generic search is intentionally disabled; this source needs a selected market.
        return []
