"""Background publisher for ForecastRegistry commitments on Robinhood Chain."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

try:
    from eth_account import Account
    from web3 import Web3
except ImportError:  # Keeps non-onchain/self-hosted installs operational.
    Account = None  # type: ignore[assignment]
    Web3 = None  # type: ignore[assignment]

from ..config import RobinhoodChainConfig
from .outbox import SupabaseProofOutbox

logger = logging.getLogger(__name__)


FORECAST_REGISTRY_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "bytes32", "name": "forecastId", "type": "bytes32"},
                    {"internalType": "bytes32", "name": "payloadHash", "type": "bytes32"},
                    {"internalType": "bytes32", "name": "marketIdHash", "type": "bytes32"},
                    {"internalType": "bytes32", "name": "agentId", "type": "bytes32"},
                    {"internalType": "bytes32", "name": "categoryId", "type": "bytes32"},
                    {"internalType": "uint16", "name": "probabilityBps", "type": "uint16"},
                    {"internalType": "uint64", "name": "closesAt", "type": "uint64"},
                ],
                "internalType": "struct ForecastRegistry.ForecastInput[]",
                "name": "inputs",
                "type": "tuple[]",
            }
        ],
        "name": "commitForecastBatch",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32[]", "name": "forecastIds", "type": "bytes32[]"},
            {"internalType": "bool", "name": "outcome", "type": "bool"},
            {"internalType": "bytes32", "name": "resolutionHash", "type": "bytes32"},
        ],
        "name": "resolveForecastBatch",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


_RUNTIME_STATUS: Dict[str, Any] = {
    "status": "disabled",
    "last_checked_at": None,
    "last_transaction_hash": None,
    "message": None,
}


def get_proof_publisher_status() -> Dict[str, Any]:
    return dict(_RUNTIME_STATUS)


def _set_status(status: str, message: Optional[str] = None, tx_hash: Optional[str] = None) -> None:
    _RUNTIME_STATUS.update({
        "status": status,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
    })
    if tx_hash:
        _RUNTIME_STATUS["last_transaction_hash"] = tx_hash


def commitment_tuples(commitments: List[Dict[str, Any]]) -> List[tuple[Any, ...]]:
    values: List[tuple[Any, ...]] = []
    for item in commitments:
        closes_at = item.get("closes_at")
        if not closes_at:
            raise ValueError("A market close timestamp is required for onchain commitment.")
        values.append((
            item["forecast_id"],
            item["payload_hash"],
            item["market_id_hash"],
            item["agent_id"],
            item["category_id"],
            int(item["probability_bps"]),
            int(closes_at),
        ))
    if not values:
        raise ValueError("At least one forecast commitment is required.")
    return values


def transaction_fee_fields(web3: Any) -> Dict[str, int]:
    """Build fee fields with enough headroom for a changing EIP-1559 base fee."""
    gas_price = max(1, int(web3.eth.gas_price))
    try:
        pending_block = web3.eth.get_block("pending")
        base_fee_value = pending_block.get("baseFeePerGas")
    except Exception:
        base_fee_value = None

    if base_fee_value is None:
        return {"gasPrice": max(gas_price, int(gas_price * 1.25))}

    base_fee = int(base_fee_value)
    try:
        priority_fee = int(web3.eth.max_priority_fee)
    except Exception:
        priority_fee = max(gas_price - base_fee, 1_000_000)
    priority_fee = max(priority_fee, 1_000_000)

    return {
        "maxPriorityFeePerGas": priority_fee,
        "maxFeePerGas": max(gas_price * 2, base_fee * 2 + priority_fee),
    }


class ProofPublisher:
    def __init__(self, config: RobinhoodChainConfig):
        self.config = config
        self.outbox = SupabaseProofOutbox(
            config.supabase_url,
            config.supabase_service_role_key,
            chain_id=config.chain_id,
            registry_address=config.registry_address,
        )
        self._task: Optional[asyncio.Task] = None

    @property
    def configured(self) -> bool:
        base_configured = bool(
            self.config.proof_enabled
            and self.config.rpc_url
            and self.config.registry_address
            and self.config.publisher_private_key
            and self.outbox.configured
            and Account is not None
            and Web3 is not None
        )
        if not base_configured:
            return False
        try:
            Account.from_key(self.config.publisher_private_key)
            Web3.to_checksum_address(self.config.registry_address)
        except (TypeError, ValueError):
            return False
        return True

    async def start(self) -> None:
        if not self.configured:
            _set_status("disabled", "Proof publisher is not fully configured.")
            return
        if self._task and not self._task.done():
            return
        account = Account.from_key(self.config.publisher_private_key)
        _set_status("ready", f"Publisher {account.address} ready on chain {self.config.chain_id}.")
        self._task = asyncio.create_task(self._run_loop(), name="forecast-proof-publisher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.publish_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[ProofPublisher] Poll failed: %s", exc)
                _set_status("unavailable", str(exc)[:240])
            await asyncio.sleep(self.config.publish_interval_seconds)

    async def publish_pending(self, limit: int = 5) -> Dict[str, int]:
        if not self.configured:
            return {"checked": 0, "verified": 0, "failed": 0}
        rows = await self.outbox.list_work(limit=limit)
        verified = 0
        failed = 0
        for row in rows:
            try:
                if row.get("tx_hash"):
                    receipt = await asyncio.to_thread(self._wait_for_receipt, str(row["tx_hash"]))
                    await self._verify_receipt(row, receipt)
                    verified += 1
                    continue

                await self.outbox.mark_processing(row)
                commitments = commitment_tuples(row.get("commitments") or [])
                tx_hash = await asyncio.to_thread(self._send_commitments, commitments)
                await self.outbox.mark_submitted(row["id"], tx_hash)
                receipt = await asyncio.to_thread(self._wait_for_receipt, tx_hash)
                await self._verify_receipt(row, receipt)
                verified += 1
            except Exception as exc:
                failed += 1
                logger.warning("[ProofPublisher] Forecast %s failed: %s", row.get("forecast_id"), exc)
                await self.outbox.mark_retry(row, str(exc))
                _set_status("retry", str(exc)[:240])
        resolution_result = await self._publish_resolutions(limit)
        if not rows and resolution_result["checked"] == 0:
            _set_status("ready")
        return {
            "checked": len(rows),
            "verified": verified,
            "failed": failed,
            "resolutions_checked": resolution_result["checked"],
            "resolutions_verified": resolution_result["verified"],
            "resolutions_failed": resolution_result["failed"],
        }

    async def track_record(self) -> Dict[str, Any]:
        return await self.outbox.get_track_record()

    async def _publish_resolutions(self, limit: int) -> Dict[str, int]:
        rows = await self.outbox.list_resolution_work(limit=limit)
        verified = 0
        failed = 0
        for row in rows:
            try:
                existing_hash = row.get("resolution_tx_hash")
                if existing_hash:
                    receipt = await asyncio.to_thread(self._wait_for_receipt, str(existing_hash))
                else:
                    await self.outbox.mark_resolution_processing(row)
                    forecast_ids = [item["forecast_id"] for item in row.get("commitments") or []]
                    if not forecast_ids:
                        raise ValueError("No committed forecasts are available for resolution.")
                    tx_hash = await asyncio.to_thread(
                        self._send_resolution,
                        forecast_ids,
                        bool(row.get("outcome")),
                        str(row.get("resolution_hash") or ""),
                    )
                    await self.outbox.mark_resolution_submitted(row["id"], tx_hash)
                    receipt = await asyncio.to_thread(self._wait_for_receipt, tx_hash)
                if int(receipt.get("status", 0)) != 1:
                    raise RuntimeError("ForecastRegistry resolution transaction reverted.")
                tx_value = receipt.get("transactionHash") or row.get("resolution_tx_hash")
                tx_hash = Web3.to_hex(tx_value) if not isinstance(tx_value, str) else tx_value
                await self.outbox.mark_resolution_verified(
                    row, tx_hash, int(receipt.get("blockNumber") or 0)
                )
                _set_status("active", tx_hash=tx_hash)
                verified += 1
            except Exception as exc:
                failed += 1
                logger.warning("[ProofPublisher] Resolution %s failed: %s", row.get("forecast_id"), exc)
                await self.outbox.mark_resolution_retry(row, str(exc))
                _set_status("retry", str(exc)[:240])
        return {"checked": len(rows), "verified": verified, "failed": failed}

    def _web3(self) -> Web3:
        web3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": 30}))
        if not web3.is_connected():
            raise RuntimeError("Robinhood Chain RPC is unavailable.")
        chain_id = int(web3.eth.chain_id)
        if chain_id != int(self.config.chain_id):
            raise RuntimeError(f"RPC chain ID {chain_id} does not match configured chain ID {self.config.chain_id}.")
        return web3

    def _send_commitments(self, commitments: List[tuple[Any, ...]]) -> str:
        web3 = self._web3()
        account = Account.from_key(self.config.publisher_private_key)
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(self.config.registry_address),
            abi=FORECAST_REGISTRY_ABI,
        )
        nonce = web3.eth.get_transaction_count(account.address, "pending")
        transaction = contract.functions.commitForecastBatch(commitments).build_transaction({
            "from": account.address,
            "chainId": int(self.config.chain_id),
            "nonce": nonce,
            **transaction_fee_fields(web3),
        })
        estimated_gas = web3.eth.estimate_gas(transaction)
        transaction["gas"] = max(100_000, int(estimated_gas * 1.2))
        signed = account.sign_transaction(transaction)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        value = web3.to_hex(tx_hash)
        _set_status("submitted", tx_hash=value)
        return value

    def _send_resolution(
        self, forecast_ids: List[str], outcome: bool, resolution_hash: str
    ) -> str:
        if not resolution_hash:
            raise ValueError("A resolution hash is required.")
        web3 = self._web3()
        account = Account.from_key(self.config.publisher_private_key)
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(self.config.registry_address),
            abi=FORECAST_REGISTRY_ABI,
        )
        nonce = web3.eth.get_transaction_count(account.address, "pending")
        transaction = contract.functions.resolveForecastBatch(
            forecast_ids, outcome, resolution_hash
        ).build_transaction({
            "from": account.address,
            "chainId": int(self.config.chain_id),
            "nonce": nonce,
            **transaction_fee_fields(web3),
        })
        transaction["gas"] = max(100_000, int(web3.eth.estimate_gas(transaction) * 1.2))
        signed = account.sign_transaction(transaction)
        value = web3.to_hex(web3.eth.send_raw_transaction(signed.raw_transaction))
        _set_status("submitted", tx_hash=value)
        return value

    def _wait_for_receipt(self, tx_hash: str) -> Dict[str, Any]:
        receipt = self._web3().eth.wait_for_transaction_receipt(tx_hash, timeout=180, poll_latency=2)
        return dict(receipt)

    async def _verify_receipt(self, row: Dict[str, Any], receipt: Dict[str, Any]) -> None:
        if int(receipt.get("status", 0)) != 1:
            raise RuntimeError("ForecastRegistry transaction reverted.")
        tx_hash_value = receipt.get("transactionHash") or row.get("tx_hash")
        tx_hash = Web3.to_hex(tx_hash_value) if not isinstance(tx_hash_value, str) else tx_hash_value
        block_number = int(receipt.get("blockNumber") or 0)
        await self.outbox.mark_verified(row, tx_hash, block_number)
        _set_status("active", tx_hash=tx_hash)
