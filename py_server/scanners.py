"""
Full-asset scanners. Discovery + valuation per chain family.

EVM      : Alchemy token API and/or Transfer-log discovery -> Multicall3 batch read
Solana   : getTokenAccountsByOwner on Token and Token-2022
Tron     : TRC-20 transfer history -> constant-call balanceOf
UTXO     : Esplora address stats (watch-only; approvals are impossible on UTXO)

Every scanner returns a list of
  {symbol, token, decimals, balance_raw, balance, usd, network, native}
`token` is the ERC-20 contract or SPL mint; None for native assets.
"""
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from eth_abi import decode as abi_decode, encode as abi_encode

import abi
import market
from chains import (EVM_NETWORKS, SOLANA_RPCS, is_evm_address, is_sol_address,
                    is_tron_address)

logger = logging.getLogger("scanners")

ALCHEMY_KEY = os.environ.get("ALCHEMY_KEY", "").strip()
TRON_API_KEY = os.environ.get("TRON_API_KEY", "").strip()

LOG_CHUNK = 5_000
LOG_MAX_CHUNKS = 120
MAX_LOG_TOKENS = 500
PRICE_TTL = 120
CACHE_CAP = 5_000
WALLET_CONCURRENCY = 4

_market_cache: Dict[str, tuple] = {}
_wallet_sem = asyncio.Semaphore(WALLET_CONCURRENCY)

STABLES = {"USDC", "USDT", "DAI", "BUSD", "FDUSD", "TUSD", "USDP"}


def rpc_for(net: str) -> str:
    return os.environ.get(f"RPC_{net.upper()}", "").strip() or EVM_NETWORKS[net]["rpc"]


def _pad(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def _trim_cache() -> None:
    if len(_market_cache) <= CACHE_CAP:
        return
    for k in sorted(_market_cache, key=lambda x: _market_cache[x][1])[: len(_market_cache) - CACHE_CAP]:
        _market_cache.pop(k, None)


# ------------------------------------------------------------------ market data
async def _market(tokens: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Price + symbol for a batch of contracts or mints, keyed lowercase.
    DexScreener is case-insensitive on lookup, so lowercasing the key lets the
    same map serve EVM (already lowercase) and base58 Solana mints.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not tokens:
        return out

    now_ts = time.time()
    want: List[str] = []
    for t in tokens:
        key = t.lower()
        hit = _market_cache.get(key)
        if hit and now_ts - hit[1] < PRICE_TTL:
            out[key] = hit[0]
        elif key not in out:
            want.append(t)

    for i in range(0, len(want), 30):
        batch = want[i:i + 30]
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(batch))
            if r.status_code != 200:
                continue
            best: Dict[str, tuple] = {}
            for pair in (r.json().get("pairs") or []):
                base = (pair.get("baseToken") or {})
                addr = (base.get("address") or "").lower()
                if not addr:
                    continue
                try:
                    px = float(pair.get("priceUsd") or 0)
                except (TypeError, ValueError):
                    continue
                if px <= 0:
                    continue
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if addr not in best or liq > best[addr][0]:
                    best[addr] = (liq, px, (base.get("symbol") or "?")[:12])
            for addr, (_liq, px, sym) in best.items():
                node = {"usd": px, "symbol": sym}
                out[addr] = node
                _market_cache[addr] = (node, now_ts)
        except Exception as e:
            logger.debug("dexscreener batch failed: %s", e)

    _trim_cache()
    return out


def _usd_of(node: Optional[Dict[str, Any]], symbol: str) -> float:
    if node and node.get("usd"):
        return float(node["usd"])
    return 1.0 if (symbol or "").upper() in STABLES else 0.0


# ------------------------------------------------------------------ EVM
async def _evm_log_tokens(client: httpx.AsyncClient, rpc: str, address: str) -> List[str]:
    """Walk backwards through Transfer logs addressed to `address`."""
    try:
        r = await client.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                         "method": "eth_blockNumber", "params": []})
        latest = int(r.json()["result"], 16)
    except Exception:
        return []

    found: List[str] = []
    seen: set = set()
    to_block = latest

    for _ in range(LOG_MAX_CHUNKS):
        if to_block < 0:
            break
        from_block = max(0, to_block - LOG_CHUNK + 1)
        try:
            r = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(from_block), "toBlock": hex(to_block),
                    "topics": [abi.TRANSFER_TOPIC, None, "0x" + _pad(address)],
                }],
            })
            logs = r.json().get("result")
            if isinstance(logs, list):
                for log in logs:
                    t = (log.get("address") or "").lower()
                    if t and t not in seen:
                        seen.add(t)
                        found.append(t)
        except Exception as e:
            logger.debug("getLogs %s-%s failed: %s", from_block, to_block, e)

        if from_block == 0 or len(found) >= MAX_LOG_TOKENS:
            break
        to_block = from_block - 1

    return found


async def _evm_alchemy_tokens(client: httpx.AsyncClient, net: str, address: str) -> List[str]:
    if not ALCHEMY_KEY:
        return []
    sub = {"Ethereum": "eth-mainnet", "Base": "base-mainnet",
           "Arbitrum": "arb-mainnet", "Polygon": "polygon-mainnet"}.get(net)
    if not sub:
        return []
    try:
        r = await client.post(f"https://{sub}.g.alchemy.com/v2/{ALCHEMY_KEY}", json={
            "jsonrpc": "2.0", "id": 1, "method": "alchemy_getTokenBalances",
            "params": [address, "erc20"],
        })
        rows = (r.json().get("result") or {}).get("tokenBalances") or []
        return [t["contractAddress"].lower() for t in rows
                if int(t.get("tokenBalance") or "0x0", 16) > 0]
    except Exception as e:
        logger.debug("alchemy token scan failed: %s", e)
        return []


def _call3(target: str, data: str) -> tuple:
    return (target, True, bytes.fromhex(data[2:]))


async def _multicall(client: httpx.AsyncClient, rpc: str,
                     calls: List[tuple]) -> List[Optional[bytes]]:
    payload = abi.SEL_AGGREGATE3 + abi_encode(["(address,bool,bytes)[]"], [calls]).hex()
    try:
        r = await client.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": abi.MULTICALL3, "data": payload}, "latest"],
        })
        raw = r.json().get("result")
        if not raw or raw == "0x":
            return [None] * len(calls)
        decoded = abi_decode(["bytes[]"], bytes.fromhex(raw[2:]))[0]
        if len(decoded) != len(calls):
            return [None] * len(calls)
        return list(decoded)
    except Exception as e:
        logger.debug("multicall failed: %s", e)
        return [None] * len(calls)


def _decode_string(raw: Optional[bytes]) -> str:
    if not raw or len(raw) < 32:
        return "?"
    try:
        if len(raw) >= 64:
            ln = int.from_bytes(raw[32:64], "big")
            if 0 < ln <= 64:
                return raw[64:64 + ln].decode(errors="ignore").strip() or "?"
        return raw[:32].rstrip(b"\x00").decode(errors="ignore").strip() or "?"
    except Exception:
        return "?"


async def scan_evm(net: str, address: str) -> List[Dict[str, Any]]:
    if not is_evm_address(address) or net not in EVM_NETWORKS:
        return []

    cfg = EVM_NETWORKS[net]
    rpc = rpc_for(net)
    out: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        tokens: set = set()
        tokens |= set(await _evm_alchemy_tokens(client, net, address))
        tokens |= set(await _evm_log_tokens(client, rpc, address))
        tokens |= {c.lower() for c in cfg["tokens"].values()}
        tokens = {t for t in tokens if is_evm_address(t)}

        try:
            r = await client.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                             "method": "eth_getBalance",
                                             "params": [address, "latest"]})
            wei = int(r.json().get("result", "0x0"), 16)
            if wei > 0:
                px = market.price(cfg["native"])
                out.append({"symbol": cfg["native"], "token": None, "decimals": 18,
                            "balance_raw": wei, "balance": wei / 1e18,
                            "usd": wei / 1e18 * px, "network": net, "native": True})
        except Exception as e:
            logger.debug("native balance %s/%s failed: %s", net, address, e)

        tok = sorted(tokens)
        if not tok:
            return out

        # decimals + balance + symbol in a single round trip
        n = len(tok)
        calls = ([_call3(t, abi.SEL_DECIMALS) for t in tok]
                 + [_call3(t, abi.encode_balance_of(address)) for t in tok]
                 + [_call3(t, abi.SEL_SYMBOL) for t in tok])
        res = await _multicall(client, rpc, calls)
        if len(res) != len(calls):
            return out

        priced = await _market(tok)

        for i, contract in enumerate(tok):
            raw_dec, raw_bal, raw_sym = res[i], res[n + i], res[2 * n + i]
            if not raw_bal or len(raw_bal) < 32:
                continue
            bal = int.from_bytes(raw_bal[:32], "big")
            if bal <= 0:
                continue

            dec = 18
            if raw_dec and len(raw_dec) >= 32:
                d = int.from_bytes(raw_dec[:32], "big")
                if 0 < d <= 36:
                    dec = d

            node = priced.get(contract)
            sym = (node or {}).get("symbol") or _decode_string(raw_sym)
            human = bal / (10 ** dec)
            out.append({"symbol": sym, "token": contract, "decimals": dec,
                        "balance_raw": bal, "balance": human,
                        "usd": human * _usd_of(node, sym),
                        "network": net, "native": False})
    return out


# ------------------------------------------------------------------ Solana
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


async def _solana_rpc(method: str, params: list) -> Any:
    last: Optional[Exception] = None
    for rpc in SOLANA_RPCS:
        try:
            async with httpx.AsyncClient(timeout=12.0) as c:
                r = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                            "method": method, "params": params})
            body = r.json()
            if "error" in body:
                last = RuntimeError(str(body["error"]))
                continue
            return body.get("result")
        except Exception as e:
            last = e
    if last:
        logger.debug("solana %s exhausted RPCs: %s", method, last)
    return None


async def scan_solana(address: str) -> List[Dict[str, Any]]:
    if not is_sol_address(address):
        return []

    out: List[Dict[str, Any]] = []

    bal = await _solana_rpc("getBalance", [address, {"commitment": "confirmed"}])
    lamports = int(((bal or {}).get("value")) or 0)
    if lamports > 0:
        px = market.price("SOL")
        out.append({"symbol": "SOL", "token": None, "decimals": 9,
                    "balance_raw": lamports, "balance": lamports / 1e9,
                    "usd": lamports / 1e9 * px, "network": "Solana",
                    "native": True, "token_account": None, "program": SYSTEM_PROGRAM})

    accounts: List[Dict[str, Any]] = []
    for prog in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        res = await _solana_rpc("getTokenAccountsByOwner",
                                [address, {"programId": prog}, {"encoding": "jsonParsed"}])
        for acc in ((res or {}).get("value") or []):
            info = (acc.get("account", {}).get("data", {})
                       .get("parsed", {}).get("info", {}))
            amt = info.get("tokenAmount") or {}
            raw = int(amt.get("amount") or 0)
            if raw <= 0 or not info.get("mint"):
                continue
            accounts.append({"mint": info["mint"],
                             "token_account": acc.get("pubkey"),
                             "decimals": int(amt.get("decimals") or 0),
                             "raw": raw, "program": prog})

    if not accounts:
        return out

    priced = await _market([a["mint"] for a in accounts])

    for a in accounts:
        node = priced.get(a["mint"].lower())
        sym = (node or {}).get("symbol") or "?"
        human = a["raw"] / (10 ** a["decimals"])
        out.append({"symbol": sym, "token": a["mint"], "decimals": a["decimals"],
                    "balance_raw": a["raw"], "balance": human,
                    "usd": human * _usd_of(node, sym),
                    "network": "Solana", "native": False,
                    "token_account": a["token_account"], "program": a["program"]})
    return out


# ------------------------------------------------------------------ Tron
TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _tron_base() -> str:
    return os.environ.get("TRON_RPC", "https://api.trongrid.io")


def _tron_headers() -> Dict[str, str]:
    return {"TRON-PRO-API-KEY": TRON_API_KEY} if TRON_API_KEY else {}


async def _tron_call(client: httpx.AsyncClient, owner: str, contract: str,
                     selector: str, parameter: str = "") -> Optional[int]:
    try:
        r = await client.post(f"{_tron_base()}/wallet/triggerconstantcontract", json={
            "owner_address": owner, "contract_address": contract,
            "function_selector": selector, "parameter": parameter, "visible": True,
        })
        cres = (r.json() or {}).get("constant_result") or []
        if not cres or not cres[0]:
            return None
        return int(cres[0], 16)
    except Exception:
        return None


async def scan_tron(address: str) -> List[Dict[str, Any]]:
    if not is_tron_address(address):
        return []

    out: List[Dict[str, Any]] = []
    param = address[1:].rjust(64, "0")

    async with httpx.AsyncClient(timeout=15.0, headers=_tron_headers()) as c:
        try:
            r = await c.post(f"{_tron_base()}/wallet/getaccount",
                             json={"address": address, "visible": True})
            sun = int((r.json() or {}).get("balance", 0) or 0)
            if sun > 0:
                px = market.price("TRX")
                out.append({"symbol": "TRX", "token": None, "decimals": 6,
                            "balance_raw": sun, "balance": sun / 1e6,
                            "usd": sun / 1e6 * px, "network": "Tron", "native": True})
        except Exception as e:
            logger.debug("tron native balance failed: %s", e)

        contracts = {TRON_USDT}
        try:
            r = await c.get(f"{_tron_base()}/v1/accounts/{address}/transactions/trc20",
                            params={"limit": 200, "only_confirmed": "true"})
            for tx in (r.json().get("data") or []):
                t = (tx.get("token_info") or {}).get("address")
                if t:
                    contracts.add(t)
        except Exception as e:
            logger.debug("tron trc20 history failed: %s", e)

        held: List[Dict[str, Any]] = []
        for contract in contracts:
            bal = await _tron_call(c, address, contract, "balanceOf(address)", param)
            if not bal or bal <= 0:
                continue
            dec = 6 if contract == TRON_USDT else 18
            d = await _tron_call(c, address, contract, "decimals()")
            if d is not None and 0 < d <= 18:
                dec = d
            held.append({"contract": contract, "raw": bal, "decimals": dec})

    if not held:
        return out

    priced = await _market([h["contract"] for h in held])

    for h in held:
        node = priced.get(h["contract"].lower())
        sym = "USDT" if h["contract"] == TRON_USDT else ((node or {}).get("symbol") or "?")
        human = h["raw"] / (10 ** h["decimals"])
        out.append({"symbol": sym, "token": h["contract"], "decimals": h["decimals"],
                    "balance_raw": h["raw"], "balance": human,
                    "usd": human * _usd_of(node, sym),
                    "network": "Tron", "native": False})
    return out


# ------------------------------------------------------------------ UTXO
async def scan_utxo(coin: str, address: str) -> List[Dict[str, Any]]:
    base = ("https://blockstream.info/api" if coin.upper() == "BTC"
            else "https://litecoinspace.org/api")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{base}/address/{address}")
        if r.status_code != 200:
            return []
        d = r.json()
        sats = (int(d["chain_stats"]["funded_txo_sum"])
                - int(d["chain_stats"]["spent_txo_sum"]))
        if sats <= 0:
            return []
        px = market.price(coin.upper())
        return [{"symbol": coin.upper(), "token": None, "decimals": 8,
                 "balance_raw": sats, "balance": sats / 1e8,
                 "usd": sats / 1e8 * px, "network": coin.upper(), "native": True}]
    except Exception as e:
        logger.debug("utxo scan %s/%s failed: %s", coin, address, e)
        return []


# ------------------------------------------------------------------ dispatch
def _networks(wallet: Dict[str, Any]) -> List[str]:
    raw = wallet.get("networks")
    nets: List[str] = []
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                nets = [str(n) for n in parsed]
        except (ValueError, TypeError):
            nets = [n.strip() for n in raw.split(",") if n.strip()]
    elif isinstance(raw, list):
        nets = [str(n) for n in raw]
    nets = [n for n in nets if n in EVM_NETWORKS]
    return nets or list(EVM_NETWORKS.keys())


async def scan_wallet(wallet: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not wallet:
        return []

    fam = (wallet.get("family") or "").lower()
    addr = wallet.get("address") or ""

    if fam == "evm":
        batches = await asyncio.gather(
            *[scan_evm(n, addr) for n in _networks(wallet)], return_exceptions=True)
        flat: List[Dict[str, Any]] = []
        for b in batches:
            if isinstance(b, list):
                flat.extend(b)
        return flat
    if fam == "solana":
        return await scan_solana(addr)
    if fam == "tron":
        return await scan_tron(addr)
    if fam in ("btc", "ltc"):
        return await scan_utxo(fam, addr)
    return []


async def _scan_one(wallet: Dict[str, Any]) -> Dict[str, Any]:
    async with _wallet_sem:
        try:
            assets = await scan_wallet(wallet)
        except Exception as e:
            logger.debug("scan_wallet %s failed: %s", wallet.get("id"), e)
            assets = []
    return {"wallet_id": wallet.get("id"), "address": wallet.get("address"),
            "family": wallet.get("family"), "assets": assets,
            "usd": sum(a["usd"] for a in assets)}


async def scan_user(user_id: int) -> Dict[str, Any]:
    from database import q_all
    wallets = q_all("SELECT * FROM wallets WHERE user_id = ? AND verified = 1", user_id)
    rows = await asyncio.gather(*[_scan_one(w) for w in wallets])
    rows.sort(key=lambda r: r["usd"], reverse=True)
    return {"wallets": list(rows), "total_usd": sum(r["usd"] for r in rows)}