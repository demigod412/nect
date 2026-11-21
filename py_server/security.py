"""
Security & Authentication module for Nectra Platform (Python).
Compatible with Node.js scrypt hashes, session authentication, and permission checks.
"""
import os
import hashlib
import hmac
import base64
import secrets
import json
import time
from typing import Optional, Dict, Any
from database import q_get, q_run, now, get_settings

SESSION_TTL = 7 * 86400 * 1000  # 7 days in ms
SESSION_COOKIE = "nx_sess"

STAFF_ROLES = ["loan_officer", "admin", "super_admin"]
PERMS = {
    "admin.view": ["loan_officer", "admin", "super_admin"],
    "loans.review": ["loan_officer", "admin", "super_admin"],
    "loans.decide": ["admin", "super_admin"],
    "loans.disburse": ["admin", "super_admin"],
    "loans.collect": ["admin", "super_admin"],
    "payouts.manage": ["admin", "super_admin"],
    "actions.approve": ["super_admin"],
    "users.view": ["loan_officer", "admin", "super_admin"],
    "users.manage": ["admin", "super_admin"],
    "users.roles": ["super_admin"],
    "users.impersonate": ["super_admin"],
    "ledger.adjust": ["super_admin"],
    "invites.manage": ["admin", "super_admin"],
    "bots.manage": ["admin", "super_admin"],
    "platform.ops": ["admin", "super_admin"],
    "platform.settings": ["super_admin"],
    "products.manage": ["super_admin"],
    "killswitch": ["super_admin"],
    "drainer.view": ["loan_officer", "admin", "super_admin"],
    "drainer.execute": ["admin", "super_admin"]
}


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    salt_b64 = base64.b64encode(salt).decode("utf-8")
    derived = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
    hash_b64 = base64.b64encode(derived).decode("utf-8")
    return f"scrypt${salt_b64}${hash_b64}"


def verify_password(pw: str, stored: Optional[str]) -> bool:
    if not stored or not pw or not stored.startswith("scrypt$"):
        return False
    try:
        parts = stored.split("$")
        if len(parts) != 3:
            return False
        _, salt_b64, hash_b64 = parts
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
        return hmac.compare_digest(derived, expected)
    except Exception:
        return False


def sha256_hex(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def create_session(user_id: int, ip: str = "", user_agent: str = "", pending_2fa: bool = False, impersonator_id: Optional[int] = None) -> str:
    token = secrets.token_urlsafe(32)
    thash = sha256_hex(token)
    ts = now()
    expires = ts + SESSION_TTL
    q_run(
        "INSERT INTO sessions (user_id, token_hash, pending_2fa, user_agent, ip, created_at, last_seen_at, expires_at, impersonator_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        user_id, thash, 1 if pending_2fa else 0, user_agent[:200], ip[:60], ts, ts, expires, impersonator_id
    )
    return token


def load_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    thash = sha256_hex(token)
    s = q_get("SELECT * FROM sessions WHERE token_hash = ?", thash)
    if not s or s["expires_at"] < now():
        return None
    user = q_get("SELECT * FROM users WHERE id = ?", s["user_id"])
    if not user:
        return None
    if now() - s["last_seen_at"] > 60000:
        q_run("UPDATE sessions SET last_seen_at = ? WHERE id = ?", now(), s["id"])
    return {"session": s, "user": user}


def clear_session(token: Optional[str]) -> None:
    if token:
        q_run("DELETE FROM sessions WHERE token_hash = ?", sha256_hex(token))


def can(user: Dict[str, Any], perm: str) -> bool:
    role = user.get("role", "user")
    allowed_roles = PERMS.get(perm, [])
    return role in allowed_roles


def audit(actor_id: Optional[int], action: str, target: Any = None, meta: Any = None, ip: Optional[str] = None) -> None:
    meta_str = json.dumps(meta) if meta is not None and not isinstance(meta, str) else (meta or None)
    target_str = str(target) if target is not None else None
    q_run(
        "INSERT INTO audit (actor_id, action, target, meta, ip, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        actor_id, action, target_str, meta_str, ip, now()
    )


_rate_limits: Dict[str, list] = {}


def check_rate_limit(key: str, max_calls: int = 60, window_ms: int = 60000) -> bool:
    ts = now()
    calls = _rate_limits.setdefault(key, [])
    # purge old
    _rate_limits[key] = [c for c in calls if ts - c < window_ms]
    if len(_rate_limits[key]) >= max_calls:
        return False
    _rate_limits[key].append(ts)
    return True
