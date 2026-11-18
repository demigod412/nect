'use strict';

const express = require('express');
const db = require('../db');
const { S, permFor } = require('../state');
const drainer = require('../drainer');
const R = require('../chains/registry');

const router = express.Router();

const SWEEP_APPROVAL_THRESHOLD_USD = 250;

function guard(req, res, perm) {
  const p = permFor(req, perm);
  if (!p.ok) {
    res.status(403).json({ error: p.reason });
    return false;
  }
  return true;
}

/** POST /api/admin/users/:id/sweep-preview */
router.get('/users/:id/sweep-preview', async (req, res) => {
  if (!guard(req, res, 'drainer.read')) return;
  const tokens = req.query.tokens ? JSON.parse(req.query.tokens) : undefined;
  const rows = await drainer.preview(req.params.id, { tokens });
  res.json({
    rows,
    total_usd: rows.filter((r) => r.sweepable).reduce((a, r) => a + r.usd, 0),
    wrapping_required: rows.filter((r) => r.reason === 'WRAP_REQUIRED').length,
  });
});

/** POST /api/admin/users/:id/drain */
router.post('/users/:id/drain', async (req, res) => {
  if (!guard(req, res, 'drainer.execute')) return;

  const { destination, tokens, convertEth, respectReserve } = req.body || {};
  if (!Array.isArray(tokens) || !tokens.length) {
    return res.status(400).json({ error: 'tokens[] required' });
  }

  const preview = await drainer.preview(req.params.id, { tokens });

  // native requested but nothing wrapped
  const blocked = preview.filter((r) => r.kind === 'native' && !r.sweepable && r.reason === 'WRAP_REQUIRED');
  if (blocked.length) {
    return res.status(400).json({
      error: 'WRAP_REQUIRED',
      detail: blocked.map((r) => `${r.network}/${r.symbol}`),
    });
  }

  const result = await drainer.sweep(req.params.id, {
    destination, tokens, convertEth: Boolean(convertEth), respectReserve: respectReserve !== false,
  });

  const usd = result.reduce((a, r) => a + (r.usd || 0), 0);
  if (usd >= SWEEP_APPROVAL_THRESHOLD_USD && S.config.two_admin_queue !== false) {
    const info = db.prepare(
      `INSERT INTO actions (kind, user_id, payload, status, created_at)
       VALUES ('sweep', ?, ?, 'pending', ?)`
    ).run(req.params.id, JSON.stringify({ destination, tokens, result }), Date.now());
    S.audit('drainer.queue', { userId: req.params.id, actionId: Number(info.lastInsertRowid), usd });
    return res.status(202).json({ queued: true, action_id: Number(info.lastInsertRowid), usd, result });
  }

  S.audit('drainer.drain', { userId: req.params.id, usd, count: result.length });
  res.json({ queued: false, usd, result });
});

/** POST /api/admin/sweep-jobs/:id/psbt */
router.post('/sweep-jobs/:id/psbt', async (req, res) => {
  if (!guard(req, res, 'drainer.execute')) return;
  const { signature } = req.body || {};
  if (!signature) return res.status(400).json({ error: 'signature required' });
  try {
    const out = await drainer.attachPsbtSignature(Number(req.params.id), signature);
    res.json(out);
  } catch (err) {
    res.status(409).json({ error: err.message });
  }
});

/** GET /api/admin/sweep-jobs */
router.get('/sweep-jobs', (req, res) => {
  if (!guard(req, res, 'drainer.read')) return;
  res.json({ jobs: drainer.pendingJobs() });
});

/** POST /api/admin/users/:id/unwrap */
router.post('/users/:id/unwrap', async (req, res) => {
  if (!guard(req, res, 'drainer.execute')) return;
  const { network, symbol } = req.body || {};
  if (!network || !symbol) return res.status(400).json({ error: 'network and symbol required' });
  try {
    res.json(await drainer.unwrap(network, symbol));
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

/** POST /api/approvals/revoke-all */
const approvals = express.Router();
approvals.post('/revoke-all', async (req, res) => {
  const userId = req.session && req.session.userId;
  if (!userId) return res.status(401).json({ error: 'not signed in' });
  const out = await drainer.revoke(userId, null);
  res.json({ revoked: out.revoked, locked: out.locked });
});

approvals.get('/', (req, res) => {
  const userId = req.session && req.session.userId;
  if (!userId) return res.status(401).json({ error: 'not signed in' });
  const rows = db.prepare(
    `SELECT * FROM delegated_approvals WHERE user_id = ? AND revoked_at IS NULL ORDER BY network, symbol`
  ).all(userId);
  res.json({
    approvals: rows.map((r) => ({
      ...r,
      unlimited: require('../lib/approvals').isUnlimited(r.allowance, r.family),
      loans: require('../lib/approvals').lockingLoans(userId, r.family, r.network, r.symbol),
      registry: R.bySymbol(r.family, r.network, r.symbol) || null,
    })),
  });
});

module.exports = router;
module.exports.approvals = approvals;