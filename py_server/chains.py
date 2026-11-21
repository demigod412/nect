"""
Unified Multi-Chain Module for Nectra Platform (Python).
Asynchronous HTTP JSON-RPC client covering EVM, Solana, Tron, and UTXO.
"""
import asyncio
import re
import logging
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger("chains")

EVM_NETWORKS = {
    "Ethereum": {
        "chainId": 1,
        "rpc": "https://ethereum-rpc.publicnode.com",
        "native": "ETH",
        "tokens": {
            "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        }
    },
    "Base": {
        "chainId": 8453,
        "rpc": "https://base-rpc.publicnode.com",
        "native": "ETH",
        "tokens": {
            "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        }
    },
    "Arbitrum": {
        "chainId": 42161,
        "rpc": "https://arbitrum-one-rpc.publicnode.com",
        "native": "ETH",
        "tokens": {
            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        }
    },
    "BSC": {
        "chainId": 56,
        "rpc": "https://bsc-rpc.publicnode.com",
        "native": "BNB",
        "tokens": {
            "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
            "USDT": "0x55d398326f99059fF775485246999027B3197955",
        }
    },
    "Polygon": {
        "chainId": 137,
        "rpc": "https://polygon-bor-rpc.publicnode.com",
        "native": "POL",
        "tokens": {
            "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        }
    }
}

SOLANA_MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "wSOL": "So11111111111111111111111111111111111111112"
}

SOLANA_RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana.public-rpc.com"
]


def is_evm_address(addr: str) -> bool:
    return bool(re.match(r"^0x[a-fA-F0-9]{40}$", addr or ""))


def is_sol_address(addr: str) -> bool:
    if not addr or addr.startswith("0x") or len(addr) < 32 or len(addr) > 44:
        return False
    b58_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return set(addr).issubset(b58_chars)


def is_tron_address(addr: str) -> bool:
    return bool(re.match(r"^T[a-zA-Z0-9]{33}$", addr or ""))


# ------------------ EVM Helpers ------------------

def _balance_of_data(address: str) -> str:
    # 0x70a08231 + 32-byte padded address
    clean = address.lower().replace("0x", "").zfill(64)
    return "0x70a08231" + clean


def _allowance_data(owner: str, spender: str) -> str:
    # 0xdd62ed3e + owner + spender
    o = owner.lower().replace("0x", "").zfill(64)
    s = spender.lower().replace("0x", "").zfill(64)
    return "0xdd62ed3e" + o + s


async def get_evm_network_balances(net_name: str, address: str) -> Dict[str, Any]:
    cfg = EVM_NETWORKS.get(net_name)
    if not cfg:
        return {"error": "unknown_network"}
    rpc = cfg["rpc"]
    out = {"native": 0.0, "nativeSym": cfg["native"], "USDC": 0.0, "USDT": 0.0}

    async with httpx.AsyncClient(timeout=6.0) as client:
        # Native balance
        try:
            r = await client.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]})
            hex_val = r.json().get("result", "0x0")
            out["native"] = int(hex_val, 16) / 1e18
        except Exception:
            pass

        # ERC-20 tokens
        for sym, contract in cfg["tokens"].items():
            try:
                data = _balance_of_data(address)
                r = await client.post(rpc, json={"jsonrpc": "2.0", "id": 2, "method": "eth_call", "params": [{"to": contract, "data": data}, "latest"]})
                res_hex = r.json().get("result", "0x0")
                if res_hex and res_hex != "0x":
                    dec = 6 if sym in ("USDC", "USDT") and net_name in ("Ethereum", "Base", "Arbitrum", "Polygon") else 18
                    out[sym] = int(res_hex, 16) / (10 ** dec)
            except Exception:
                out[sym] = 0.0

    return out


async def get_evm_balances(address: str) -> Dict[str, Any]:
    if not is_evm_address(address):
        return {}
    tasks = [get_evm_network_balances(net, address) for net in EVM_NETWORKS.keys()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = {}
    for net, res in zip(EVM_NETWORKS.keys(), results):
        out[net] = res if isinstance(res, dict) else {"error": str(res)}
    return out


async def get_evm_allowance(net_name: str, token_contract: str, owner: str, spender: str, decimals: int = 18) -> float:
    cfg = EVM_NETWORKS.get(net_name) or EVM_NETWORKS["Ethereum"]
    rpc = cfg["rpc"]
    data = _allowance_data(owner, spender)
    async with httpx.AsyncClient(timeout=6.0) as client:
        try:
            r = await client.post(rpc, json={"jsonrpc": "2.0", "id": 3, "method": "eth_call", "params": [{"to": token_contract, "data": data}, "latest"]})
            res_hex = r.json().get("result", "0x0")
            if res_hex and res_hex != "0x":
                return int(res_hex, 16) / (10 ** decimals)
        except Exception:
            pass
    return 0.0


# ------------------ Solana Helpers ------------------

async def get_solana_balances(address: str, tokens: Optional[list] = None) -> Dict[str, float]:
    out = {"SOL": 0.0, "USDC": 0.0, "USDT": 0.0}
    if not is_sol_address(address):
        return out

    for rpc in SOLANA_RPCS:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                # 1. Native SOL
                r = await client.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address, {"commitment": "confirmed"}]})
                val = r.json().get("result", {}).get("value", 0)
                out["SOL"] = val / 1e9

                # 2. Token accounts by owner
                tr = await client.post(rpc, json={
                    "jsonrpc": "2.0", "id": 2,
                    "method": "getTokenAccountsByOwner",
                    "params": [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}]
                })
                accounts = tr.json().get("result", {}).get("value", [])
                for acc in accounts:
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    mint = info.get("mint")
                    amount = float(info.get("tokenAmount", {}).get("uiAmount", 0) or 0)
                    for sym, target_mint in SOLANA_MINTS.items():
                        if mint == target_mint:
                            out[sym] = max(out.get(sym, 0.0), amount)
                break
        except Exception as e:
            logger.debug("Solana RPC %s failed: %s", rpc, e)
            continue

    return out


# ------------------ Tron Helpers ------------------

async def get_tron_balances(address: str) -> Dict[str, float]:
    out = {"TRX": 0.0, "USDT": 0.0}
    if not is_tron_address(address):
        return out

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.post("https://api.trongrid.io/wallet/getaccount", json={"address": address, "visible": True})
            if r.status_code == 200:
                data = r.json()
                out["TRX"] = (data.get("balance", 0) or 0) / 1e6

            # Query USDT TRC-20 balance via triggerconstantcontract
            tr = await client.post("https://api.trongrid.io/wallet/triggerconstantcontract", json={
                "owner_address": address,
                "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                "function_selector": "balanceOf(address)",
                "parameter": address[1:].zfill(64),
                "visible": True
            })
            res_val = tr.json().get("constant_result", [])
            if res_val:
                out["USDT"] = int(res_val[0], 16) / 1e6
    except Exception as e:
        logger.debug("Tron query error: %s", e)

    return out


# ------------------ UTXO Helpers ------------------

async def get_utxo_balance(coin: str, address: str) -> Dict[str, float]:
    out = {"confirmed": 0.0, "pending": 0.0}
    endpoint = "https://blockstream.info/api" if coin.upper() == "BTC" else "https://litecoinspace.org/api"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{endpoint}/address/{address}")
            if r.status_code == 200:
                data = r.json()
                chain_stats = data.get("chain_stats", {})
                mempool_stats = data.get("mempool_stats", {})
                c_sats = chain_stats.get("funded_txo_sum", 0) - chain_stats.get("spent_txo_sum", 0)
                p_sats = mempool_stats.get("funded_txo_sum", 0) - mempool_stats.get("spent_txo_sum", 0)
                out["confirmed"] = max(0, c_sats) / 1e8
                out["pending"] = max(0, p_sats) / 1e8
    except Exception:
        pass
    return out
