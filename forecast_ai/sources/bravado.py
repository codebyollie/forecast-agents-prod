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

    VALID_WINDOWS = ("1h", "4h", "24h", "7d", "30d", "90d", "365d", "all")
    WINDOW_WEIGHTS = {"24h": 3.0, "7d": 2.0, "30d": 1.0}

    def __init__(
        self,
        api_token: str,
        api_url: str = "https://partner-api.bravadotrade.com/trader-analytics",
        timeout_seconds: float = 20.0,
        leaderboard_window: str = "24h,7d,30d",
        scan_limit: int = 30,
        min_trades: int = 5,
    ):
        self.api_token = api_token
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        requested_windows = [
            value.strip().lower()
            for value in str(leaderboard_window).split(",")
            if value.strip()
        ]
        self.leaderboard_windows = tuple(dict.fromkeys(
            value for value in requested_windows if value in self.VALID_WINDOWS
        )) or ("24h", "7d", "30d")
        self.leaderboard_window = ",".join(self.leaderboard_windows)
        self.scan_limit = max(1, min(int(scan_limit), 30))
        self.min_trades = max(1, int(min_trades))
        self._leaderboard_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
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

    async def _leaderboard(self, window: str) -> Dict[str, Any]:
        cached = self._leaderboard_cache.get(window)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
        payload = await self._get("leaderboard", {
            "window": window,
            # Fetch a wider cohort, then spend position requests only on traders
            # that the leaderboard reports as having active positions.
            "limit": max(50, self.scan_limit),
            "offset": 0,
            "min_trades": self.min_trades,
            "exclude_bots": "true",
        })
        self._leaderboard_cache[window] = (time.time(), payload)
        return payload

    async def _leaderboard_candidates(self) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
        payloads = await asyncio.gather(*(
            self._leaderboard(window) for window in self.leaderboard_windows
        ))
        candidates: Dict[str, Dict[str, Any]] = {}
        window_counts: Dict[str, int] = {}
        page_size = max(50, self.scan_limit)

        for window, payload in zip(self.leaderboard_windows, payloads):
            rows = self._records(payload, "results")
            window_counts[window] = len(rows)
            weight = self.WINDOW_WEIGHTS.get(window, 0.75)
            for index, row in enumerate(rows):
                wallet = self._wallet(row)
                if not wallet or bool(row.get("is_mm_bot")):
                    continue
                if row.get("active_positions") is not None and (
                    self._number(row.get("active_positions")) or 0
                ) <= 0:
                    continue

                rank = int(self._number(row.get("rank")) or index + 1)
                rank_factor = max(0.05, 1.0 - ((rank - 1) / max(1, page_size - 1)))
                candidate = candidates.get(wallet)
                if candidate is None:
                    candidate = dict(row)
                    candidate["trader"] = wallet
                    candidate["_leaderboard_windows"] = []
                    candidate["_window_ranks"] = {}
                    candidate["_recent_score"] = 0.0
                    candidates[wallet] = candidate
                candidate["_leaderboard_windows"].append(window)
                candidate["_window_ranks"][window] = rank
                candidate["_recent_score"] += weight * rank_factor

        ranked = sorted(
            candidates.values(),
            key=lambda row: (
                -float(row.get("_recent_score") or 0.0),
                min(row.get("_window_ranks", {}).values() or [10_000]),
            ),
        )
        return ranked, window_counts

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
    def _display_content(
        question: str,
        matches: List[Dict[str, Any]],
        direction: str,
        windows: tuple[str, ...],
        scanned: int,
    ) -> str:
        window_label = " / ".join(window.upper() for window in windows)
        if not matches:
            return (
                f"Bravado checked ranked, non-bot Polymarket traders for: {question}.\n"
                f"Leaderboard windows: {window_label} | Unique traders scanned: {scanned}.\n"
                "No matching active position was found in the scanned leaderboard cohort."
            )
        lines = [
            f"Bravado Smart Money for: {question}",
            f"Leaderboard windows: {window_label} | Unique non-bot traders scanned: {scanned}",
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
            match_windows = match.get("leaderboard_windows") or []
            if match_windows:
                details.append("ranked " + "/".join(window.upper() for window in match_windows))
            lines.append("- " + " | ".join(details))
        return "\n".join(lines)

    async def fetch_market_trader_intelligence(
        self,
        market_id: str,
        question: str,
        condition_id: Optional[str] = None,
    ) -> List[Evidence]:
        leaderboard_rows, window_counts = await self._leaderboard_candidates()
        leaders = leaderboard_rows[:self.scan_limit]
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
                    "leaderboard_windows": list(row.get("_leaderboard_windows") or []),
                    "window_ranks": dict(row.get("_window_ranks") or {}),
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
            "leaderboard_windows": list(self.leaderboard_windows),
            "leaderboard_window_counts": window_counts,
            "leaderboard_wallets_considered": sum(window_counts.values()),
            "leaderboard_unique_wallets_considered": len(leaderboard_rows),
            "leaderboard_wallets_scanned": len(leaders),
        }
        return [Evidence(
            source_name="bravado_trader_intelligence",
            content=self._display_content(
                question,
                matches,
                direction,
                self.leaderboard_windows,
                len(leaders),
            ),
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
