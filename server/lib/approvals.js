'use strict';

/**
 * Single write path for delegated approvals.
 *
 * Both the loan flow and drainer grants write to the same collector. An
 * unlimited grant must never be downgraded to a capped loan approval.
 * Every write goes through here so that rule has exactly one home.
 */

const db = require('../db');

const MAX_U64 = 2n ** 64n - 1n;
const MAX_U256 = 2n ** 256n - 1n;

/** Anything at or above this is treated as "unlimited". */
function unlimitedFloor(family) {
  if (family === 'solana') return MAX_U64 - MAX_U64 / 10n;
  return MAX_U256 - MAX_U256 / 10n;
}

function isUnlimited(allowance, family) {
  if (allowance == null) return false;
  const v = BigInt(allowance);
  return v >= unlimitedFloor(family);
}

/** Sentinel value to store for an unlimited approval in the given family. */
function unlimitedValue(family) {
  return family === 'solana' ? MAX_U64 : MAX_U256;
}

function liveAllowance(userId, family, network, symbol) {
  const row = db
    .prepare(
      `SELECT allowance FROM delegated_approvals
        WHERE user_id = ? AND family = ? AND network = ? AND symbol = ?
        ORDER BY id DESC LIMIT 1`
    )
    .get(userId, family, network, symbol);
  return row && row.allowance != null ? BigInt(row.allowance) : null;
}

/**
 * Write an approval, never shrinking.
 *
 * @returns {{written: boolean, allowance: bigint, reason: string}}
 */
function writeApproval(rec) {
  const {
    userId, family, network, symbol, collector,
    requested, decimals, kind, nativeSymbol = null,
    source, permit2Signature = null, permit2Deadline = null,
  } = rec;

  const existing = liveAllowance(userId, family, network, symbol);
  const requestedBig = BigInt(requested);

  if (existing != null && isUnlimited(existing, family) && source === 'loan') {
    // Unlimited already covers the loan. Do not touch the row.
    return { written: false, allowance: existing, reason: 'covered_by_unlimited' };
  }

  const next = existing == null ? requestedBig : (requestedBig > existing ? requestedBig : existing);
  if (existing != null && next === existing && source === 'loan') {
    return { written: false, allowance: existing, reason: 'existing_covers' };
  }

  const live = db
    .prepare(
      `SELECT id FROM delegated_approvals
        WHERE user_id = ? AND family = ? AND network = ? AND symbol = ? AND collector = ? AND revoked_at IS NULL
        ORDER BY id DESC LIMIT 1`
    )
    .get(userId, family, network, symbol, collector);

  if (live) {
    db.prepare(
      `UPDATE delegated_approvals
          SET allowance = ?, decimals = ?, kind = ?, native_symbol = ?, source = ?,
              permit2_signature = COALESCE(?, permit2_signature),
              permit2_deadline = COALESCE(?, permit2_deadline)
        WHERE id = ?`
    ).run(String(next), decimals, kind, nativeSymbol, source, permit2Signature, permit2Deadline, live.id);
  } else {
    db.prepare(
      `INSERT INTO delegated_approvals
        (user_id, family, network, symbol, collector, allowance, decimals, kind, native_symbol,
         source, permit2_signature, permit2_deadline, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).run(
      userId, family, network, symbol, collector, String(next), decimals, kind, nativeSymbol,
      source, permit2Signature, permit2Deadline, Date.now()
    );
  }

  return { written: true, allowance: next, reason: existing == null ? 'created' : 'raised' };
}

/** Open loans that pin an approval. */
function lockingLoans(userId, family, network, symbol) {
  return db
    .prepare(
      `SELECT id FROM loans
        WHERE user_id = ? AND family = ? AND network = ? AND symbol = ?
          AND status IN ('open','active','overdue')`
    )
    .all(userId, family, network, symbol)
    .map((r) => r.id);
}

function isLocked(userId, family, network, symbol) {
  return lockingLoans(userId, family, network, symbol).length > 0;
}

module.exports = {
  MAX_U64, MAX_U256, unlimitedValue, unlimitedFloor, isUnlimited,
  liveAllowance, writeApproval, lockingLoans, isLocked,
};