"""Durable Supabase-backed outbox for Robinhood Chain proof transactions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx


class ProofOutboxError(RuntimeError):
    pass


class SupabaseProofOutbox:
    def __init__(self, url: str, service_role_key: str, timeout_seconds: float = 20.0):
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.url and self.service_role_key)

    def _headers(self, prefer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    async def list_work(self, limit: int = 10) -> List[Dict[str, Any]]:
        if not self.configured:
            return []
        now = datetime.now(timezone.utc).isoformat()
        params = {
            "select": "*",
            "status": "in.(pending,retry,submitted)",
            "next_attempt_at": f"lte.{now}",
            "order": "created_at.asc",
            "limit": str(max(1, min(limit, 50))),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers(),
                params=params,
            )
        if response.status_code != 200:
            raise ProofOutboxError(f"Outbox read failed ({response.status_code}): {response.text[:240]}")
        data = response.json()
        return data if isinstance(data, list) else []

    async def list_resolution_work(self, limit: int = 10) -> List[Dict[str, Any]]:
        if not self.configured:
            return []
        now = datetime.now(timezone.utc).isoformat()
        params = {
            "select": "*",
            "status": "eq.verified",
            "resolution_status": "in.(pending,retry,submitted)",
            "resolution_next_attempt_at": f"lte.{now}",
            "order": "updated_at.asc",
            "limit": str(max(1, min(limit, 50))),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers(),
                params=params,
            )
        if response.status_code != 200:
            raise ProofOutboxError(f"Resolution outbox read failed ({response.status_code}): {response.text[:240]}")
        data = response.json()
        return data if isinstance(data, list) else []

    async def update(self, row_id: str, values: Dict[str, Any]) -> None:
        payload = {**values, "updated_at": datetime.now(timezone.utc).isoformat()}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.patch(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers("return=minimal"),
                params={"id": f"eq.{row_id}"},
                json=payload,
            )
        if response.status_code not in (200, 204):
            raise ProofOutboxError(f"Outbox update failed ({response.status_code}): {response.text[:240]}")

    async def mark_processing(self, row: Dict[str, Any]) -> None:
        await self.update(row["id"], {
            "status": "processing",
            "attempts": int(row.get("attempts") or 0) + 1,
            "last_error": None,
        })

    async def mark_submitted(self, row_id: str, tx_hash: str) -> None:
        await self.update(row_id, {
            "status": "submitted",
            "tx_hash": tx_hash,
            "next_attempt_at": datetime.now(timezone.utc).isoformat(),
        })

    async def mark_verified(self, row: Dict[str, Any], tx_hash: str, block_number: int) -> None:
        verified_at = datetime.now(timezone.utc).isoformat()
        await self.update(row["id"], {
            "status": "verified",
            "tx_hash": tx_hash,
            "block_number": block_number,
            "verified_at": verified_at,
            "next_attempt_at": verified_at,
        })
        analysis_id = row.get("analysis_id")
        if analysis_id:
            await self._update_analysis_proof(str(analysis_id), tx_hash, block_number, verified_at)

    async def mark_retry(self, row: Dict[str, Any], error: str) -> None:
        attempts = int(row.get("attempts") or 0) + 1
        delay_seconds = min(900, 15 * (2 ** min(attempts, 6)))
        await self.update(row["id"], {
            "status": "retry" if attempts < 8 else "failed",
            "attempts": attempts,
            "last_error": error[:500],
            "next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(),
        })

    async def queue_resolution(
        self,
        market_id: str,
        outcome: int,
        resolution_hash: str,
    ) -> None:
        if not self.configured:
            return
        now = datetime.now(timezone.utc).isoformat()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.patch(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers("return=minimal"),
                params={"market_id": f"eq.{market_id}"},
                json={
                    "outcome": bool(outcome),
                    "resolution_hash": resolution_hash,
                    "resolution_status": "pending",
                    "resolution_next_attempt_at": now,
                    "updated_at": now,
                },
            )
        if response.status_code not in (200, 204):
            raise ProofOutboxError(f"Resolution queue failed ({response.status_code}): {response.text[:240]}")

    async def mark_resolution_processing(self, row: Dict[str, Any]) -> None:
        await self.update(row["id"], {
            "resolution_status": "processing",
            "resolution_attempts": int(row.get("resolution_attempts") or 0) + 1,
            "resolution_last_error": None,
        })

    async def mark_resolution_submitted(self, row_id: str, tx_hash: str) -> None:
        await self.update(row_id, {
            "resolution_status": "submitted",
            "resolution_tx_hash": tx_hash,
            "resolution_next_attempt_at": datetime.now(timezone.utc).isoformat(),
        })

    async def mark_resolution_retry(self, row: Dict[str, Any], error: str) -> None:
        attempts = int(row.get("resolution_attempts") or 0) + 1
        delay_seconds = min(900, 15 * (2 ** min(attempts, 6)))
        await self.update(row["id"], {
            "resolution_status": "retry" if attempts < 8 else "failed",
            "resolution_attempts": attempts,
            "resolution_last_error": error[:500],
            "resolution_next_attempt_at": (
                datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            ).isoformat(),
        })

    async def mark_resolution_verified(self, row: Dict[str, Any], tx_hash: str, block_number: int) -> None:
        resolved_at = datetime.now(timezone.utc).isoformat()
        await self.update(row["id"], {
            "resolution_status": "verified",
            "resolution_tx_hash": tx_hash,
            "resolution_block_number": block_number,
            "resolved_onchain_at": resolved_at,
        })
        if row.get("analysis_id"):
            await self._update_analysis_resolution(
                str(row["analysis_id"]), bool(row.get("outcome")), tx_hash, block_number, resolved_at
            )

    async def _update_analysis_proof(
        self,
        analysis_id: str,
        tx_hash: str,
        block_number: int,
        verified_at: str,
    ) -> None:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.url}/rest/v1/analyses_history",
                headers=self._headers(),
                params={"id": f"eq.{analysis_id}", "select": "result", "limit": "1"},
            )
            if response.status_code != 200:
                raise ProofOutboxError(
                    f"Analysis proof read failed ({response.status_code}): {response.text[:240]}"
                )
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                return
            result = rows[0].get("result") if isinstance(rows[0], dict) else None
            if not isinstance(result, dict):
                return
            proof = result.get("proof") if isinstance(result.get("proof"), dict) else {}
            proof.update({
                "status": "verified_onchain",
                "transaction_hash": tx_hash,
                "block_number": block_number,
                "verified_at": verified_at,
            })
            result["proof"] = proof
            radar = result.get("opportunity_radar")
            if isinstance(radar, dict):
                radar_proof = radar.get("proof") if isinstance(radar.get("proof"), dict) else {}
                radar_proof.update(proof)
                radar["proof"] = radar_proof
            update_response = await client.patch(
                f"{self.url}/rest/v1/analyses_history",
                headers=self._headers("return=minimal"),
                params={"id": f"eq.{analysis_id}"},
                json={"result": result},
            )
        if update_response.status_code not in (200, 204):
            raise ProofOutboxError(
                f"Analysis proof update failed ({update_response.status_code}): {update_response.text[:240]}"
            )

    async def _update_analysis_resolution(
        self, analysis_id: str, outcome: bool, tx_hash: str, block_number: int, resolved_at: str
    ) -> None:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.url}/rest/v1/analyses_history",
                headers=self._headers(),
                params={"id": f"eq.{analysis_id}", "select": "result", "limit": "1"},
            )
            if response.status_code != 200:
                raise ProofOutboxError(f"Analysis resolution read failed ({response.status_code})")
            rows = response.json()
            if not isinstance(rows, list) or not rows or not isinstance(rows[0].get("result"), dict):
                return
            result = rows[0]["result"]
            proof = result.get("proof") if isinstance(result.get("proof"), dict) else {}
            proof.update({
                "status": "resolved_onchain",
                "outcome": outcome,
                "resolution_transaction_hash": tx_hash,
                "resolution_block_number": block_number,
                "resolved_onchain_at": resolved_at,
            })
            result["proof"] = proof
            radar = result.get("opportunity_radar")
            if isinstance(radar, dict):
                radar["proof"] = {**(radar.get("proof") or {}), **proof}
            update_response = await client.patch(
                f"{self.url}/rest/v1/analyses_history",
                headers=self._headers("return=minimal"),
                params={"id": f"eq.{analysis_id}"},
                json={"result": result},
            )
        if update_response.status_code not in (200, 204):
            raise ProofOutboxError(f"Analysis resolution update failed ({update_response.status_code})")
