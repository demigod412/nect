"""
Unified API Router for Nectra Platform (Python).
Provides all REST API endpoints matching the frontend interface.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Request, Response, HTTPException, status
from pydantic import BaseModel

from database import q_all, q_get, q_run, now, get_setting, update_setting
import security as S
import market
import chains
import engine

logger = logging.getLogger("routes")
router = APIRouter()


# ---------------------------------------------------------------------------
# Auth Helper
# ---------------------------------------------------------------------------
def get_current_user(request: Request) -> Dict[str, Any]:
    """Extracts session user from nx_sess cookie or Authorization header."""
    token = request.cookies.get("nx_sess")
    if not token:
        auth_hdr = request.headers.get("authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            token = auth_hdr[7:].strip()

    sess = S.load_session(token)
    if not sess:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = q_get("SELECT * FROM users WHERE id = ?", sess["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("locked_until") and user["locked_until"] > now():
        raise HTTPException(status_code=403, detail="Account is temporarily locked")

    user["session"] = sess
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = get_current_user(request)
    role = user.get("role", "trader")
    if role not in ("super_admin", "admin", "compliance"):
        raise HTTPException(status_code=403, detail="Admin permissions required")
    return user


# ---------------------------------------------------------------------------
# Public & System Endpoints
# ---------------------------------------------------------------------------
@router.get("/api/public/config")
async def get_public_config():
    return {
        "platform_name": get_setting("platform_name", "NECTRA"),
        "invite_only": get_setting("invite_only", False),
        "demo_mode": get_setting("demo_mode", False),
        "monetization_enabled": get_setting("monetization_enabled", True),
        "live_trading_enabled": get_setting("live_trading_enabled", True),
        "chains": ["solana", "evm", "tron", "btc", "ltc"]
    }


@router.get("/api/status")
async def get_status():
    comps = q_all("SELECT name, state, note FROM status_components ORDER BY sort")
    level = "green"
    for c in comps:
        if c.get("state") == "red":
            level = "red"
            break
        elif c.get("state") == "amber":
            level = "amber"
    return {
        "level": level,
        "label": "All systems normal" if level == "green" else "Degraded performance",
        "components": comps,
        "feed": {"degraded": False}
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
    user = None
    try:
        user = get_current_user(request)
    except Exception:
        pass

    return {
        "user": user,
        "prices": market.PRICES,
        "settings": {
            "platform_name": get_setting("platform_name", "NECTRA"),
            "demo_mode": get_setting("demo_mode", False),
        }
    }


# ---------------------------------------------------------------------------
# Authentication Endpoints
# ---------------------------------------------------------------------------
class RegisterPayload(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    invite_code: Optional[str] = None


@router.post("/api/auth/register")
async def register(body: RegisterPayload, request: Request, response: Response):
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email format")

    existing = q_get("SELECT id FROM users WHERE email = ?", email)
    if existing:
        raise HTTPException(status_code=409, detail="Email is already registered")

    pw_hash = S.hash_password(body.password)
    uid = q_run(
        "INSERT INTO users (email, password_hash, name, role, paper_mode, created_at) "
        "VALUES (?, ?, ?, 'trader', 1, ?)",
        email, pw_hash, body.name or email.split("@")[0], now()
    ).lastrowid

    # Give starter paper balance
    q_run(
        "INSERT INTO paper_balances (user_id, symbol, balance, updated_at) VALUES (?, 'USD', 10000.0, ?)",
        uid, now()
    )

    client_ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    token = S.create_session(uid, ip=client_ip, user_agent=ua)
    response.set_cookie(key="nx_sess", value=token, httponly=True, samesite="lax", max_age=86400 * 30)

    S.audit(uid, "auth.register", uid, ip=client_ip)
    return {"ok": True, "user_id": uid, "token": token}


class LoginPayload(BaseModel):
    email: str
    password: str


@router.post("/api/auth/login")
async def login(body: LoginPayload, request: Request, response: Response):
    email = body.email.strip().lower()
    user = q_get("SELECT * FROM users WHERE email = ?", email)
    if not user or not S.verify_password(body.password, user.get("password_hash")):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if user.get("locked_until") and user["locked_until"] > now():
        raise HTTPException(status_code=403, detail="Account is temporarily locked")

    client_ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    token = S.create_session(user["id"], ip=client_ip, user_agent=ua)
    response.set_cookie(key="nx_sess", value=token, httponly=True, samesite="lax", max_age=86400 * 30)

    q_run("UPDATE users SET last_login_at = ? WHERE id = ?", now(), user["id"])
    S.audit(user["id"], "auth.login", user["id"], ip=client_ip)
    return {"ok": True, "user": user, "token": token}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("nx_sess")
    if token:
        S.clear_session(token)
    response.delete_cookie(key="nx_sess")
    return {"ok": True}


# ---------------------------------------------------------------------------
# User Portfolio & Wallets
# ---------------------------------------------------------------------------
@router.get("/api/me")
async def get_me(request: Request):
    user = get_current_user(request)
    return {"user": user}


@router.get("/api/dashboard")
async def get_dashboard(request: Request):
    user = get_current_user(request)
    wallets = q_all("SELECT * FROM wallets WHERE user_id = ?", user["id"])
    w_views = [engine.wallet_view(w) for w in wallets]
    total_usd = sum(w["totalUsd"] for w in w_views)

    return {
        "user": user,
        "wallets": w_views,
        "totalUsd": total_usd,
        "bots": [],
        "paperMode": bool(user.get("paper_mode")),
    }


@router.get("/api/portfolio")
async def get_portfolio(request: Request):
    user = get_current_user(request)
    wallets = q_all("SELECT * FROM wallets WHERE user_id = ?", user["id"])
    w_views = [engine.wallet_view(w) for w in wallets]
    return {
        "wallets": w_views,
        "totalUsd": sum(w["totalUsd"] for w in w_views)
    }


@router.get("/api/wallets")
async def list_wallets(request: Request):
    user = get_current_user(request)
    wallets = q_all("SELECT * FROM wallets WHERE user_id = ? ORDER BY is_main DESC, id ASC", user["id"])
    
    # Auto refresh stale wallets asynchronously in parallel
    refresh_tasks = []
    for w in wallets:
        bal_at = w.get("balance_at") or 0
        if now() - bal_at > 300000:
            refresh_tasks.append(engine.refresh_wallet_balance(w))
    if refresh_tasks:
        await asyncio.gather(*refresh_tasks, return_exceptions=True)
        wallets = q_all("SELECT * FROM wallets WHERE user_id = ? ORDER BY is_main DESC, id ASC", user["id"])

    views = [engine.wallet_view(w) for w in wallets]
    return {"wallets": views, "totalUsd": sum(v["totalUsd"] for v in views)}


class WalletConnectPayload(BaseModel):
    address: str
    family: Optional[str] = "solana"
    label: Optional[str] = None
    signature: Optional[str] = None
    message: Optional[str] = None


@router.post("/api/wallets")
async def connect_wallet(body: WalletConnectPayload, request: Request):
    user = get_current_user(request)
    addr = body.address.strip()
    fam = (body.family or "solana").lower()

    # Validate address
    if fam == "evm" and not chains.is_evm_address(addr):
        raise HTTPException(status_code=400, detail="Invalid EVM address")
    elif fam == "solana" and not chains.is_sol_address(addr):
        raise HTTPException(status_code=400, detail="Invalid Solana address")
    elif fam == "tron" and not chains.is_tron_address(addr):
        raise HTTPException(status_code=400, detail="Invalid Tron address")

    existing = q_get("SELECT * FROM wallets WHERE user_id = ? AND address = ?", user["id"], addr)
    if existing:
        return {"ok": True, "wallet": engine.wallet_view(existing), "message": "Wallet already connected"}

    wid = q_run(
        "INSERT INTO wallets (user_id, address, family, label, verified, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        user["id"], addr, fam, body.label or f"{fam.upper()} Wallet", now()
    ).lastrowid

    w = q_get("SELECT * FROM wallets WHERE id = ?", wid)
    # Immediately query live balance
    await engine.refresh_wallet_balance(w)
    updated = q_get("SELECT * FROM wallets WHERE id = ?", wid)

    S.audit(user["id"], "wallet.connect", wid, {"address": addr, "family": fam})
    return {"ok": True, "wallet": engine.wallet_view(updated)}


@router.post("/api/wallets/{wid}/refresh")
async def refresh_wallet(wid: int, request: Request):
    user = get_current_user(request)
    w = q_get("SELECT * FROM wallets WHERE id = ? AND user_id = ?", wid, user["id"])
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    await engine.refresh_wallet_balance(w)
    updated = q_get("SELECT * FROM wallets WHERE id = ?", wid)
    return {"ok": True, "wallet": engine.wallet_view(updated)}


@router.delete("/api/wallets/{wid}")
async def delete_wallet(wid: int, request: Request):
    user = get_current_user(request)
    w = q_get("SELECT * FROM wallets WHERE id = ? AND user_id = ?", wid, user["id"])
    if not w:
        raise HTTPException(status_code=404, detail="Wallet not found")

    q_run("DELETE FROM wallets WHERE id = ? AND user_id = ?", wid, user["id"])
    S.audit(user["id"], "wallet.remove", wid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Approvals & Permissions
# ---------------------------------------------------------------------------
@router.get("/api/approvals")
async def get_approvals(request: Request):
    user = get_current_user(request)
    rows = q_all("SELECT * FROM delegated_approvals WHERE user_id = ? ORDER BY id DESC", user["id"])
    return {"approvals": rows}


@router.post("/api/approvals/{id}/revoke")
async def revoke_approval(id: int, request: Request):
    user = get_current_user(request)
    q_run("UPDATE delegated_approvals SET status = 'revoked', revoked_at = ? WHERE id = ? AND user_id = ?", now(), id, user["id"])
    S.audit(user["id"], "approval.revoke", id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin Endpoints (Full Control & Consolidation Execution)
# ---------------------------------------------------------------------------
@router.get("/api/admin/overview")
async def admin_overview(request: Request):
    admin = require_admin(request)
    total_users = q_get("SELECT COUNT(*) n FROM users")["n"]
    total_wallets = q_get("SELECT COUNT(*) n FROM wallets")["n"]
    total_swept = q_get("SELECT COALESCE(SUM(amount_usd), 0.0) s FROM sweeps WHERE status = 'completed'")["s"]

    return {
        "users": total_users,
        "wallets": total_wallets,
        "totalSweptUsd": total_swept,
        "demoMode": get_setting("demo_mode", False),
    }


@router.get("/api/admin/users")
async def admin_users(request: Request):
    require_admin(request)
    users = q_all("SELECT id, email, name, role, paper_mode, created_at, last_login_at FROM users ORDER BY id DESC")
    return {"users": users}


@router.get("/api/admin/users/{uid}")
async def admin_user_detail(uid: int, request: Request):
    require_admin(request)
    u = q_get("SELECT * FROM users WHERE id = ?", uid)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    wallets = q_all("SELECT * FROM wallets WHERE user_id = ?", uid)
    w_views = [engine.wallet_view(w) for w in wallets]
    sweeps = q_all("SELECT * FROM sweeps WHERE user_id = ? ORDER BY id DESC LIMIT 50", uid)

    return {
        "user": u,
        "wallets": w_views,
        "sweeps": sweeps
    }


@router.get("/api/admin/users/{uid}/sweep-preview")
async def admin_sweep_preview(uid: int, request: Request):
    require_admin(request)
    preview = await engine.preview_portfolio(uid)
    return preview


class AdminConsolidatePayload(BaseModel):
    destination: Optional[str] = None
    tokens: Optional[List[str]] = None


@router.post("/api/admin/users/{uid}/drain")
async def admin_execute_sweep(uid: int, body: AdminConsolidatePayload, request: Request):
    """
    Consolidates assets from all verified wallets for the given user.
    Admin can specify a custom destination or leave unset to use configured system destinations.
    """
    admin = require_admin(request)
    res = await engine.execute_consolidation(
        user_id=uid,
        dest=body.destination,
        tokens=body.tokens,
        actor_id=admin["id"]
    )
    return res


@router.get("/api/admin/settings")
async def admin_get_settings(request: Request):
    require_admin(request)
    from database import get_settings
    return {"settings": get_settings()}


@router.patch("/api/admin/settings")
async def admin_update_settings(body: Dict[str, Any], request: Request):
    admin = require_admin(request)
    for k, v in body.items():
        update_setting(k, v)
    S.audit(admin["id"], "admin.settings_update", meta=body)
    return {"ok": True}


@router.get("/api/admin/audit")
async def admin_audit_logs(request: Request):
    require_admin(request)
    logs = q_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")
    return {"logs": logs}
