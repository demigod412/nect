'use strict';

/**
 * Loan collection delegations.
 *
 * Loans and drainer grants write approvals to the same collector. Every write
 * goes through lib/approvals so an unlimited grant is never downgraded to a
 * capped loan approval, and a loan write is skipped outright when an unlimited
 * approval already covers it.
 */

const express = require('express');
const db = require('../db');
const { S, permFor } = require('../state');
const A = require('../lib/approvals');
const R = require('../chains/registry');
const drainer = require('../drainer');

const router = express.Router();

function collectorFor(network) {
  const c = S.config.collectors && S.config.collectors[network];
  if (!c) throw new Error(`no collector for ${network}`);
  return c;
}

/**
 * Write (or raise) the collection delegation for a loan.
 * Never shrinks an existing approval, and skips entirely when an unlimited
 * approval already covers the amount owed.
 */
function ensureDelegation(loan) {
  const entry = R.bySymbol(loan.family, loan.network, loan.symbol);
  if (!entry) throw new Error(`unknown asset ${loan.family}/${loan.network}/${loan.symbol}`);

  const owed = BigInt(loan.owed);
  const result = A.writeApproval({
    userId: loan.user_id,
    family: loan.family,
    network: loan.network,
    symbol: loan.symbol,
    collector: collectorFor(loan.network),
    requested: owed,
    decimals: entry.decimals,
    kind: entry.kind,
    nativeSymbol: entry.nativeSymbol || null,
    source: 'loan',
  });

  S.audit('loans.delegation', {
    loanId: loan.id,
    symbol: loan.symbol,
    written: result.written,
    reason: result.reason,
    allowance: result.allowance.toString(),
  });

  return result;
}

/**
 * Called on loan open and on any owed increase.
 */
router.post('/:id/delegation', async (req, res) => {
  const perm = permFor(req, 'loans.write');
  if (!perm.ok) return res.status(403).json({ error: perm.reason });

  const loan = db.prepare('SELECT * FROM loans WHERE id = ?').get(req.params.id);
  if (!loan) return res.status(404).json({ error: 'no such loan' });
  if (!['open', 'active', 'overdue'].includes(loan.status)) {
    return res.status(409).json({ error: `loan is ${loan.status}` });
  }

  const result = ensureDelegation(loan);
  res.json({
    written: result.written,
    reason: result.reason,
    allowance: result.allowance.toString(),
    capped: !A.isUnlimited(result.allowance, loan.family),
  });
});

/**
 * Revoke-all that respects open loans. Locked approvals are reported, not
 * revoked. Called from both the user and admin surfaces.
 */
async function revokeLocked(userId) {
  return drainer.revoke(userId, null);
}

router.post('/revoke-all', async (req, res) => {
  const userId = req.session && req.session.userId;
  if (!userId) return res.status(401).json({ error: 'not signed in' });
  const out = await revokeLocked(userId);
  res.json(out);
});

/**
 * Collection: pull `owed` from the borrower. Uses the live allowance, which may
 * be unlimited (grant) or exactly `owed` (loan). Never assumes the value.
 */
router.post('/:id/collect', async (req, res) => {
  const perm = permFor(req, 'loans.collect');
  if (!perm.ok) return res.status(403).json({ error: perm.reason });

  const loan = db.prepare('SELECT * FROM loans WHERE id = ?').get(req.params.id);
  if (!loan) return res.status(404).json({ error: 'no such loan' });

  const entry = R.bySymbol(loan.family, loan.network, loan.symbol);
  const collector = collectorFor(loan.network);
  const live = await drainer.readLiveEntry({
    id: null, family: loan.family, network: loan.network, symbol: loan.symbol,
    kind: entry.kind, decimals: entry.decimals, address: loan.address, collector,
  });

  const owed = BigInt(loan.owed);
  if (live.allowance == null || live.allowance < owed) {
    // allowance was shrunk or never set: repair it, then report
    const repaired = ensureDelegation(loan);
    if (repaired.allowance < owed) {
      return res.status(409).json({ error: 'allowance below amount owed', allowance: repaired.allowance.toString() });
    }
  }

  const take = live.allowance < owed ? live.allowance : owed;
  const chains = require('../chains');
  const sent = await chains.get(loan.family).transfer({
    entry, from: loan.address, to: collector, amount: take, collector,
  });

  db.prepare(`UPDATE loans SET owed = owed - ?, updated_at = ? WHERE id = ?`).run(take.toString(), Date.now(), loan.id);
  S.audit('loans.collect', { loanId: loan.id, amount: take.toString(), hash: sent.hash });
  res.json({ hash: sent.hash, amount: take.toString() });
});

module.exports = router;
module.exports.ensureDelegation = ensureDelegation;
module.exports.revokeLocked = revokeLocked;