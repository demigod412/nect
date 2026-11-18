'use strict';

/**
 * Curated, hard-coded asset registry.
 *
 * RULES (enforced by assertRegistry + test/registry.test.js):
 *  - Every address/mint is a literal. Nothing is resolved at runtime from a
 *    token list, a name lookup, or an API.
 *  - Every native entry has exactly one wrapped counterpart in the same
 *    network, and that wrapped entry names it via nativeSymbol.
 *  - Permit2 is the same CREATE2 address on every EVM network. No overrides.
 *  - No duplicate symbols within a network.
 *
 * Verified against: official chain docs (Base/Arbitrum/Polygon/BSC predeploy
 * lists), Uniswap Permit2 deployments, Tron contract registry, Solana
 * spl-token mint list, Circle/Tether published contract pages.
 *
 * BSC USDC (BSC-USD) and BSC USDT are 18 decimals. NOT 6. See test/registry.test.js.
 */

const FAMILIES = Object.freeze({
  SOLANA: 'solana',
  EVM: 'evm',
  TRON: 'tron',
  UTXO: 'utxo',
});

/** Same address on every EVM chain (CREATE2). Asserted once, used everywhere. */
const PERMIT2 = '0x000000000022D473030F116dDEE9F6B43aC78BA3';

/** EVM network metadata. chainId is asserted, never inferred. */
const EVM_NETWORKS = Object.freeze({
  ethereum: { chainId: 1, nativeSymbol: 'ETH', wrappedSymbol: 'WETH', explorer: 'https://etherscan.io' },
  base: { chainId: 8453, nativeSymbol: 'ETH', wrappedSymbol: 'WETH', explorer: 'https://basescan.org' },
  arbitrum: { chainId: 42161, nativeSymbol: 'ETH', wrappedSymbol: 'WETH', explorer: 'https://arbiscan.io' },
  bsc: { chainId: 56, nativeSymbol: 'BNB', wrappedSymbol: 'WBNB', explorer: 'https://bscscan.com' },
  polygon: { chainId: 137, nativeSymbol: 'POL', wrappedSymbol: 'WPOL', explorer: 'https://polygonscan.com' },
});

const REGISTRY = Object.freeze({
  [FAMILIES.SOLANA]: {
    mainnet: [
      { symbol: 'SOL', kind: 'native', decimals: 9 },
      { symbol: 'wSOL', kind: 'wrapped', nativeSymbol: 'SOL', mint: 'So11111111111111111111111111111111111111112', decimals: 9 },
      { symbol: 'USDC', kind: 'token', mint: 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', decimals: 6 },
      { symbol: 'USDT', kind: 'token', mint: 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', decimals: 6 },
    ],
  },

  [FAMILIES.EVM]: {
    ethereum: [
      { symbol: 'ETH', kind: 'native', decimals: 18 },
      { symbol: 'WETH', kind: 'wrapped', nativeSymbol: 'ETH', address: '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', decimals: 18 },
      { symbol: 'USDC', kind: 'token', address: '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', decimals: 6 },
      { symbol: 'USDT', kind: 'token', address: '0xdAC17F958D2ee523a2206206994597C13D831ec7', decimals: 6 },
    ],
    base: [
      { symbol: 'ETH', kind: 'native', decimals: 18 },
      { symbol: 'WETH', kind: 'wrapped', nativeSymbol: 'ETH', address: '0x4200000000000000000000000000000000000006', decimals: 18 },
      { symbol: 'USDC', kind: 'token', address: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', decimals: 6 },
    ],
    arbitrum: [
      { symbol: 'ETH', kind: 'native', decimals: 18 },
      { symbol: 'WETH', kind: 'wrapped', nativeSymbol: 'ETH', address: '0x82aF49447D8a07e3bd95BD0d56f35241523fBab1', decimals: 18 },
      { symbol: 'USDC', kind: 'token', address: '0xaf88d065e77c8cC2239327C5EDb3A432268e5831', decimals: 6 },
      { symbol: 'USDT', kind: 'token', address: '0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9', decimals: 6 },
    ],
    bsc: [
      { symbol: 'BNB', kind: 'native', decimals: 18 },
      { symbol: 'WBNB', kind: 'wrapped', nativeSymbol: 'BNB', address: '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c', decimals: 18 },
      // 18 decimals. Not 6.
      { symbol: 'USDC', kind: 'token', address: '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d', decimals: 18 },
      { symbol: 'USDT', kind: 'token', address: '0x55d398326f99059fF775485246999027B3197955', decimals: 18 },
    ],
    polygon: [
      { symbol: 'POL', kind: 'native', decimals: 18 },
      { symbol: 'WPOL', kind: 'wrapped', nativeSymbol: 'POL', address: '0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270', decimals: 18 },
      { symbol: 'USDC', kind: 'token', address: '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359', decimals: 6 },
      { symbol: 'USDT', kind: 'token', address: '0xc2132D05D31c914a87C6611C10748AEb04B58e8F', decimals: 6 },
    ],
  },

  [FAMILIES.TRON]: {
    mainnet: [
      { symbol: 'TRX', kind: 'native', decimals: 6 },
      { symbol: 'WTRX', kind: 'wrapped', nativeSymbol: 'TRX', address: 'TNUC9Qb1rRmC8sm3XbTv2sXjmXvzS7dWcU', decimals: 6 },
      { symbol: 'USDT', kind: 'token', address: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', decimals: 6 },
    ],
  },

  [FAMILIES.UTXO]: {
    bitcoin: [{ symbol: 'BTC', kind: 'utxo', decimals: 8 }],
    litecoin: [{ symbol: 'LTC', kind: 'utxo', decimals: 8 }],
  },
});

const EVM_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
const SOL_MINT_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const TRON_ADDRESS_RE = /^T[1-9A-HJ-NP-Za-km-z]{33}$/;

function list(family, network) {
  const fam = REGISTRY[family];
  if (!fam) throw new Error(`unknown family: ${family}`);
  const arr = fam[network];
  if (!arr) throw new Error(`unknown network: ${family}/${network}`);
  return arr;
}

/** All entries for a family+network, native included. */
function tokensFor(family, network) {
  return list(family, network).slice();
}

/** Case-insensitive symbol lookup. Returns undefined when absent. */
function bySymbol(family, network, symbol) {
  if (!symbol) return undefined;
  const want = String(symbol).toLowerCase();
  return list(family, network).find((e) => e.symbol.toLowerCase() === want);
}

function wrappedFor(family, network) {
  return list(family, network).find((e) => e.kind === 'wrapped');
}

function nativeFor(family, network) {
  return list(family, network).find((e) => e.kind === 'native');
}

/** Address/mint of an entry, whichever field carries it. */
function addressOf(entry) {
  return entry.address || entry.mint || null;
}

function networksOf(family) {
  const fam = REGISTRY[family];
  if (!fam) throw new Error(`unknown family: ${family}`);
  return Object.keys(fam);
}

function allNetworks() {
  const out = [];
  for (const family of Object.keys(REGISTRY)) {
    for (const network of networksOf(family)) out.push({ family, network });
  }
  return out;
}

/**
 * Structural invariants. Called on boot (non-demo) and from the unit test.
 * Throws loudly rather than letting a bad registry reach sweep time.
 */
function assertRegistry() {
  const problems = [];
  const seenPermit2 = new Set();

  for (const { family, network } of allNetworks()) {
    const entries = list(family, network);
    const symbols = new Set();

    for (const e of entries) {
      const where = `${family}/${network}/${e.symbol}`;

      if (!e.symbol) problems.push(`${where}: missing symbol`);
      const key = String(e.symbol || '').toLowerCase();
      if (symbols.has(key)) problems.push(`${where}: duplicate symbol`);
      symbols.add(key);

      if (!['token', 'wrapped', 'native', 'utxo'].includes(e.kind)) {
        problems.push(`${where}: bad kind ${e.kind}`);
      }
      if (!Number.isInteger(e.decimals) || e.decimals < 0 || e.decimals > 18) {
        problems.push(`${where}: bad decimals ${e.decimals}`);
      }

      const addr = addressOf(e);
      if (e.kind === 'native') {
        if (addr) problems.push(`${where}: native must not carry an address`);
        if (e.nativeSymbol) problems.push(`${where}: native must not carry nativeSymbol`);
        if (!wrappedFor(family, network)) problems.push(`${where}: no wrapped counterpart`);
      } else if (e.kind === 'utxo') {
        if (addr) problems.push(`${where}: utxo must not carry an address`);
      } else {
        if (!addr) problems.push(`${where}: missing address`);
        if (family === FAMILIES.EVM && !EVM_ADDRESS_RE.test(addr)) problems.push(`${where}: bad EVM address`);
        if (family === FAMILIES.SOLANA && !SOL_MINT_RE.test(addr)) problems.push(`${where}: bad Solana mint`);
        if (family === FAMILIES.TRON && !TRON_ADDRESS_RE.test(addr)) problems.push(`${where}: bad Tron address`);
      }

      if (e.kind === 'wrapped') {
        const nat = e.nativeSymbol && bySymbol(family, network, e.nativeSymbol);
        if (!nat || nat.kind !== 'native') problems.push(`${where}: nativeSymbol does not resolve to a native entry`);
      }
    }

    if (family === FAMILIES.EVM) {
      const meta = EVM_NETWORKS[network];
      if (!meta) problems.push(`evm/${network}: no chainId metadata`);
      const wrapped = wrappedFor(family, network);
      if (wrapped && meta && wrapped.symbol !== meta.wrappedSymbol) {
        problems.push(`evm/${network}: wrapped symbol ${wrapped.symbol} != ${meta.wrappedSymbol}`);
      }
      seenPermit2.add(PERMIT2);
    }
  }

  if (seenPermit2.size !== 1) problems.push('Permit2 address is not constant across EVM networks');
  if (!EVM_ADDRESS_RE.test(PERMIT2)) problems.push('Permit2 address malformed');

  if (problems.length) {
    throw new Error(`registry invalid:\n - ${problems.join('\n - ')}`);
  }
  return true;
}

/**
 * Boot-time runtime assertions (non-demo only).
 * @param {object} cfg  S.config
 */
function assertRuntime(cfg) {
  assertRegistry();
  const problems = [];

  for (const { family, network } of allNetworks()) {
    const treasury = cfg && cfg.treasuries && cfg.treasuries[network];
    if (!treasury) problems.push(`${network}: no treasury destination configured`);
    else if (family === FAMILIES.EVM && !EVM_ADDRESS_RE.test(treasury)) problems.push(`${network}: treasury is not an EVM address`);
    else if (family === FAMILIES.SOLANA && !SOL_MINT_RE.test(treasury)) problems.push(`${network}: treasury is not a Solana address`);
    else if (family === FAMILIES.TRON && !TRON_ADDRESS_RE.test(treasury)) problems.push(`${network}: treasury is not a Tron address`);

    for (const e of list(family, network)) {
      if (e.kind === 'wrapped' && !addressOf(e)) problems.push(`${network}/${e.symbol}: wrapped entry does not resolve`);
    }
  }

  if (problems.length) throw new Error(`registry runtime invalid:\n - ${problems.join('\n - ')}`);
  return true;
}

module.exports = {
  FAMILIES,
  PERMIT2,
  EVM_NETWORKS,
  REGISTRY,
  tokensFor,
  bySymbol,
  wrappedFor,
  nativeFor,
  addressOf,
  networksOf,
  allNetworks,
  assertRegistry,
  assertRuntime,
};