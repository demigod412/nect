'use strict';

/** Integer-safe decimal helpers. No floats anywhere. */

function assertDecimals(decimals) {
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 36) {
    throw new TypeError(`bad decimals: ${decimals}`);
  }
}

function parseUnits(value, decimals) {
  assertDecimals(decimals);
  let s = String(value == null ? '0' : value).trim();
  if (!/^-?\d*(\.\d*)?$/.test(s) || s === '' || s === '-' || s === '.') {
    throw new TypeError(`bad decimal string: ${s}`);
  }
  const neg = s.startsWith('-');
  if (neg) s = s.slice(1);
  const [whole = '0', frac = ''] = s.split('.');
  const padded = (frac + '0'.repeat(decimals)).slice(0, decimals);
  const out = BigInt((whole || '0') + padded);
  return neg ? -out : out;
}

function formatUnits(amount, decimals) {
  assertDecimals(decimals);
  const neg = amount < 0n;
  const abs = neg ? -amount : amount;
  const base = 10n ** BigInt(decimals);
  const whole = abs / base;
  const frac = (abs % base).toString().padStart(decimals, '0').replace(/0+$/, '');
  return `${neg ? '-' : ''}${whole}${frac ? '.' + frac : ''}`;
}

/** Truncate to `keep` fractional digits, in the token's own units. */
function roundDown(amount, decimals, keep = 6) {
  assertDecimals(decimals);
  if (keep >= decimals) return amount;
  const step = 10n ** BigInt(decimals - keep);
  return (amount / step) * step;
}

function minBig(...vals) {
  return vals.reduce((a, b) => (b < a ? b : a));
}

function maxBig(...vals) {
  return vals.reduce((a, b) => (b > a ? b : a));
}

/** basis points: 125n -> 1.25x */
function mulBps(amount, bps) {
  return (amount * BigInt(bps)) / 100n;
}

module.exports = { parseUnits, formatUnits, roundDown, minBig, maxBig, mulBps, assertDecimals };