"""
Market & Price Feed module for Nectra Platform (Python).
Provides real-time USD exchange rates with fallback caching.
"""
import asyncio
import logging
from typing import Dict
import httpx

logger = logging.getLogger("market")

PRICES: Dict[str, float] = {
    "SOL": 180.0,
    "ETH": 3100.0,
    "WETH": 3100.0,
    "BTC": 90000.0,
    "TRX": 0.18,
    "LTC": 85.0,
    "USDC": 1.0,
    "USDT": 1.0,
    "BNB": 600.0,
    "WBNB": 600.0,
    "POL": 0.45,
    "WPOL": 0.45,
    "JUP": 1.1,
    "BONK": 0.00002,
    "WIF": 2.5,
    "PYTH": 0.4,
    "RAY": 2.1,
}

SYMBOL_MAP = {
    "SOLUSDT": "SOL",
    "ETHUSDT": "ETH",
    "BTCUSDT": "BTC",
    "TRXUSDT": "TRX",
    "LTCUSDT": "LTC",
    "BNBUSDT": "BNB",
    "POLUSDT": "POL",
    "JUPUSDT": "JUP",
    "BONKUSDT": "BONK",
    "WIFUSDT": "WIF",
    "RAYUSDT": "RAY",
}


def price(symbol: str) -> float:
    s = (symbol or "").upper()
    if s in ("USDC", "USDT", "DAI", "BUSD"):
        return 1.0
    return PRICES.get(s, 0.0)


def all_prices() -> Dict[str, float]:
    return dict(PRICES)


async def update_prices_loop() -> None:
    """Background task to fetch live crypto prices every 30 seconds."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get("https://api.binance.com/api/v3/ticker/price")
                if res.status_code == 200:
                    data = res.json()
                    for item in data:
                        sym = item.get("symbol")
                        if sym in SYMBOL_MAP:
                            val = float(item.get("price", 0))
                            target = SYMBOL_MAP[sym]
                            PRICES[target] = val
                            if target == "ETH":
                                PRICES["WETH"] = val
                            elif target == "BNB":
                                PRICES["WBNB"] = val
                            elif target == "POL":
                                PRICES["WPOL"] = val
        except Exception as e:
            logger.debug("Market price refresh error (using cached prices): %s", e)
        await asyncio.sleep(30)
