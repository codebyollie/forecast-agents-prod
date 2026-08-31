"""Durable Supabase-backed outbox for Robinhood Chain proof transactions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from .ledger import calculate_commitment_brier_scores


class ProofOutboxError(RuntimeError):
    pass


class SupabaseProofOutbox:
    def __init__(
        self,
        url: str,
        service_role_key: str,
        timeout_seconds: float = 20.0,
        *,
        chain_id: int | None = None,
        registry_address: str = "",
    ):
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.timeout_seconds = timeout_seconds
        self.chain_id = chain_id
        self.registry_address = registry_address.lower()

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
            "status": "in.(pending,processing,retry,submitted)",
            "next_attempt_at": f"lte.{now}",
            "order": "created_at.asc",
            "limit": str(max(1, min(limit, 50))),
        }
        if self.chain_id is not None:
            params["chain_id"] = f"eq.{self.chain_id}"
        if self.registry_address:
            params["registry_address"] = f"eq.{self.registry_address}"
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
            "resolution_status": "in.(pending,processing,retry,submitted)",
            "resolution_next_attempt_at": f"lte.{now}",
            "order": "updated_at.asc",
            "limit": str(max(1, min(limit, 50))),
        }
        if self.chain_id is not None:
            params["chain_id"] = f"eq.{self.chain_id}"
        if self.registry_address:
            params["registry_address"] = f"eq.{self.registry_address}"
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

    async def list_due_markets(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return committed forecasts that are due but not queued for resolution."""
        if not self.configured:
            return []
        now = datetime.now(timezone.utc).isoformat()
        params: Dict[str, str] = {
            "select": "id,analysis_id,forecast_id,market_id,venue,closes_at,commitments",
            "status": "eq.verified",
            "resolution_status": "is.null",
            "closes_at": f"lte.{now}",
            "order": "closes_at.asc",
            "limit": str(max(1, min(limit, 500))),
        }
        if self.chain_id is not None:
            params["chain_id"] = f"eq.{self.chain_id}"
        if self.registry_address:
            params["registry_address"] = f"eq.{self.registry_address}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers(),
                params=params,
            )
        if response.status_code != 200:
            raise ProofOutboxError(
                f"Due market read failed ({response.status_code}): {response.text[:240]}"
            )
        data = response.json()
        return data if isinstance(data, list) else []

    async def get_analysis_results(self, analysis_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Load linked result payloads in one request for resolution routing."""
        ids = list(dict.fromkeys(str(item) for item in analysis_ids if item))
        if not self.configured or not ids:
            return {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.url}/rest/v1/analyses_history",
                headers=self._headers(),
                params={
                    "id": f"in.({','.join(ids)})",
                    "select": "id,result",
                    "limit": str(min(len(ids), 500)),
                },
            )
        if response.status_code != 200:
            raise ProofOutboxError(
                f"Due analysis read failed ({response.status_code}): {response.text[:240]}"
            )
        rows = response.json()
        if not isinstance(rows, list):
            return {}
        return {
            str(row["id"]): row["result"]
            for row in rows
            if isinstance(row, dict) and row.get("id") and isinstance(row.get("result"), dict)
        }

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
            "next_attempt_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
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
        venue: str | None = None,
    ) -> None:
        if not self.configured:
            return
        now = datetime.now(timezone.utc).isoformat()
        params: Dict[str, str] = {
            "market_id": f"eq.{market_id}",
            "status": "eq.verified",
        }
        if venue:
            params["venue"] = f"eq.{venue}"
        if self.chain_id is not None:
            params["chain_id"] = f"eq.{self.chain_id}"
        if self.registry_address:
            params["registry_address"] = f"eq.{self.registry_address}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.patch(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers("return=minimal"),
                params=params,
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

    async def queue_forecast_resolution(
        self,
        forecast_id: str,
        outcome: int,
        resolution_hash: str,
    ) -> None:
        """Queue one RWA forecast, never every run sharing its synthetic market ID."""
        if not self.configured:
            return
        now = datetime.now(timezone.utc).isoformat()
        params: Dict[str, str] = {
            "forecast_id": f"eq.{forecast_id}",
            "status": "eq.verified",
            "resolution_status": "is.null",
        }
        if self.chain_id is not None:
            params["chain_id"] = f"eq.{self.chain_id}"
        if self.registry_address:
            params["registry_address"] = f"eq.{self.registry_address}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.patch(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers("return=minimal"),
                params=params,
                json={
                    "outcome": bool(outcome),
                    "resolution_hash": resolution_hash,
                    "resolution_status": "pending",
                    "resolution_next_attempt_at": now,
                    "updated_at": now,
                },
            )
        if response.status_code not in (200, 204):
            raise ProofOutboxError(
                f"Forecast resolution queue failed ({response.status_code}): {response.text[:240]}"
            )

    async def update_analysis_resolution(
        self,
        analysis_id: str,
        result: Dict[str, Any],
        resolution: Dict[str, Any],
        *,
        proof_status: str,
    ) -> None:
        """Persist auditable RWA prices and Brier Scores in the public result."""
        updated_result = dict(result)
        updated_resolution = {**resolution, "status": proof_status}
        updated_result["resolution"] = updated_resolution
        proof = updated_result.get("proof") if isinstance(updated_result.get("proof"), dict) else {}
        proof.update({
            "resolution_status": proof_status,
            "outcome": bool(resolution.get("outcome")),
            "resolution_source": resolution.get("source"),
            "resolved_at": resolution.get("resolved_at"),
        })
        updated_result["proof"] = proof
        radar = updated_result.get("opportunity_radar")
        if isinstance(radar, dict):
            radar["proof"] = {**(radar.get("proof") or {}), **proof}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.patch(
                f"{self.url}/rest/v1/analyses_history",
                headers=self._headers("return=minimal"),
                params={"id": f"eq.{analysis_id}"},
                json={"result": updated_result},
            )
        if response.status_code not in (200, 204):
            raise ProofOutboxError(
                f"Analysis resolution details update failed ({response.status_code}): {response.text[:240]}"
            )

    async def mark_resolution_processing(self, row: Dict[str, Any]) -> None:
        await self.update(row["id"], {
            "resolution_status": "processing",
            "resolution_attempts": int(row.get("resolution_attempts") or 0) + 1,
            "resolution_last_error": None,
            "resolution_next_attempt_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        })

    async def get_track_record(self) -> Dict[str, Any]:
        """Build durable calibration stats from verified onchain proof rows."""
        if not self.configured:
            return {}
        params: Dict[str, str] = {
            "select": "forecast_id,category,status,resolution_status,outcome,commitments",
            "order": "created_at.asc",
            "limit": "5000",
        }
        if self.chain_id is not None:
            params["chain_id"] = f"eq.{self.chain_id}"
        if self.registry_address:
            params["registry_address"] = f"eq.{self.registry_address}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.url}/rest/v1/forecast_proof_outbox",
                headers=self._headers(),
                params=params,
            )
        if response.status_code != 200:
            raise ProofOutboxError(
                f"Track record read failed ({response.status_code}): {response.text[:240]}"
            )
        rows = response.json()
        if not isinstance(rows, list):
            rows = []

        agents: Dict[str, Dict[str, Any]] = {}
        categories: Dict[str, Dict[str, Any]] = {}
        resolved_rows = [
            row for row in rows
            if row.get("status") == "verified" and row.get("resolution_status") == "verified"
        ]
        consensus_scores: List[float] = []
        for row in resolved_rows:
            outcome = 1 if bool(row.get("outcome")) else 0
            category_name = str(row.get("category") or "Other")
            for commitment in row.get("commitments") or []:
                if not isinstance(commitment, dict):
                    continue
                probability = max(
                    0.0,
                    min(1.0, float(commitment.get("probability_bps") or 0) / 10_000),
                )
                score = (probability - outcome) ** 2
                agent_name = str(commitment.get("agent_name") or "unknown")
                stat = agents.setdefault(agent_name, {"resolved": 0, "brier_total": 0.0})
                stat["resolved"] += 1
                stat["brier_total"] += score
                if agent_name == "consensus" or agent_name.endswith(":consensus"):
                    consensus_scores.append(score)
                    category = categories.setdefault(
                        category_name, {"resolved": 0, "brier_total": 0.0}
                    )
                    category["resolved"] += 1
                    category["brier_total"] += score

        for collection in (agents, categories):
            for value in collection.values():
                value["average_brier_score"] = round(
                    value.pop("brier_total") / value["resolved"], 6
                )
        return {
            "source": "onchain_outbox",
            "chain_id": self.chain_id,
            "registry_address": self.registry_address or None,
            "total_forecasts": len(rows),
            "committed_forecasts": sum(1 for row in rows if row.get("status") == "verified"),
            "resolved_forecasts": len(resolved_rows),
            "pending_forecasts": sum(1 for row in rows if row.get("status") == "verified") - len(resolved_rows),
            "failed_forecasts": sum(1 for row in rows if row.get("status") == "failed"),
            "average_brier_score": (
                round(sum(consensus_scores) / len(consensus_scores), 6)
                if consensus_scores else None
            ),
            "agents": agents,
            "categories": categories,
        }

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
                str(row["analysis_id"]),
                bool(row.get("outcome")),
                tx_hash,
                block_number,
                resolved_at,
                commitments=row.get("commitments") or [],
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
        self,
        analysis_id: str,
        outcome: bool,
        tx_hash: str,
        block_number: int,
        resolved_at: str,
        *,
        commitments: List[Dict[str, Any]],
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
            resolution = result.get("resolution") if isinstance(result.get("resolution"), dict) else {}
            scores = calculate_commitment_brier_scores(commitments, 1 if outcome else 0)
            resolution.update({
                **scores,
                "status": "resolved_onchain",
                "outcome": 1 if outcome else 0,
                "resolved_onchain_at": resolved_at,
                "resolution_transaction_hash": tx_hash,
                "resolution_block_number": block_number,
            })
            result["resolution"] = resolution
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
