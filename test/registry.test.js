'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const R = require('../server/chains/registry');

const EXPECTED = {
  'evm/ethereum/WETH': ['0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 18],
  'evm/base/WETH': ['0x4200000000000000000000000000000000000006', 18],
  'evm/arbitrum/WETH': ['0x82aF49447D8a07e3bd95BD0d56f35241523fBab1', 18],
  'evm/bsc/WBNB': ['0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c', 18],
  'evm/polygon/WPOL': ['0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270', 18],
  'tron/mainnet/WTRX': ['TNUC9Qb1rRmC8sm3XbTv2sXjmXvzS7dWcU', 6],

  'evm/ethereum/USDC': ['0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', 6],
  'evm/ethereum/USDT': ['0xdAC17F958D2ee523a2206206994597C13D831ec7', 6],
  'evm/base/USDC': ['0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', 6],
  'evm/arbitrum/USDC': ['0xaf88d065e77c8cC2239327C5EDb3A432268e5831', 6],
  'evm/arbitrum/USDT': ['0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9', 6],
  'evm/polygon/USDC': ['0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359', 6],
  'evm/polygon/USDT': ['0xc2132D05D31c914a87C6611C10748AEb04B58e8F', 6],

  // 18 decimals. The whole point.
  'evm/bsc/USDC': ['0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d', 18],
  'evm/bsc/USDT': ['0x55d398326f99059fF775485246999027B3197955', 18],

  'tron/mainnet/USDT': ['TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', 6],

  'solana/mainnet/USDC': ['EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 6],
  'solana/mainnet/USDT': ['Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', 6],
  'solana/mainnet/wSOL': ['So11111111111111111111111111111111111111112', 9],
};

test('registry passes structural invariants', () => {
  assert.equal(R.assertRegistry(), true);
});

test('every expected address and decimals matches exactly', () => {
  for (const [key, [address, decimals]] of Object.entries(EXPECTED)) {
    const [family, network, symbol] = key.split('/');
    const entry = R.bySymbol(family, network, symbol);
    assert.ok(entry, `missing ${key}`);
    assert.equal(R.addressOf(entry), address, `${key} address`);
    assert.equal(entry.decimals, decimals, `${key} decimals`);
  }
});

test('BSC stablecoins are 18 decimals and differ from every other network', () => {
  assert.equal(R.bySymbol('evm', 'bsc', 'USDC').decimals, 18);
  assert.equal(R.bySymbol('evm', 'bsc', 'USDT').decimals, 18);
  assert.notEqual(R.bySymbol('evm', 'bsc', 'USDC').decimals, R.bySymbol('evm', 'ethereum', 'USDC').decimals);
});

test('Permit2 is one address on every EVM chain and identical', () => {
  assert.equal(R.PERMIT2, '0x000000000022D473030F116dDEE9F6B43aC78BA3');
  const addrs = new Set();
  for (const network of R.networksOf('evm')) {
    assert.ok(R.PERMIT2);
    assert.ok(R.EVM_NETWORKS[network].chainId > 0);
    addrs.add(R.PERMIT2);
  }
  assert.equal(addrs.size, 1);
});

test('native entries carry no address and every one has a wrapped counterpart', () => {
  for (const { family, network } of R.allNetworks()) {
    for (const e of R.tokensFor(family, network)) {
      if (e.kind === 'native') {
        assert.equal(R.addressOf(e), null);
        const wrapped = R.wrappedFor(family, network);
        assert.ok(wrapped, `${family}/${network} native without wrapped`);
        assert.equal(wrapped.nativeSymbol, e.symbol);
      }
    }
  }
});

test('bySymbol is case-insensitive and returns undefined for unknown', () => {
  assert.equal(R.bySymbol('solana', 'mainnet', 'wsol').symbol, 'wSOL');
  assert.equal(R.bySymbol('evm', 'bsc', 'USDC').decimals, 18);
  assert.equal(R.bySymbol('evm', 'bsc', 'DOGE'), undefined);
});

// ---- RPC-gated decimals cross-check -------------------------------------
// Skipped unless the relevant RPC env var is set.

const RPC_ENV = {
  ethereum: 'RPC_URL_ETHEREUM',
  base: 'RPC_URL_BASE',
  arbitrum: 'RPC_URL_ARBITRUM',
  bsc: 'RPC_URL_BSC',
  polygon: 'RPC_URL_POLYGON',
  tron: 'RPC_URL_TRON',
};

async function rpcCall(url, method, params) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
  });
  const body = await res.json();
  if (body.error) throw new Error(body.error.message);
  return body.result;
}

test('on-chain decimals() matches the registry', async (t) => {
  for (const network of R.networksOf('evm')) {
    const env = RPC_ENV[network];
    const url = env && process.env[env];
    if (!url) {
      t.diagnostic(`skip evm/${network}: ${env} unset`);
      continue;
    }
    for (const e of R.tokensFor('evm', network)) {
      if (e.kind !== 'token' && e.kind !== 'wrapped') continue;
      const hex = await rpcCall(url, 'eth_call', [{ to: e.address, data: '0x313ce567' }, 'latest']);
      const onchain = Number(BigInt(hex));
      assert.equal(onchain, e.decimals, `${network}/${e.symbol} decimals mismatch on chain`);
    }
  }
});

test('on-chain TRC20 decimals() matches the registry', async (t) => {
  const url = process.env[RPC_ENV.tron];
  if (!url) {
    t.diagnostic('skip tron: RPC_URL_TRON unset');
    return;
  }
  for (const e of R.tokensFor('tron', 'mainnet')) {
    if (e.kind !== 'token' && e.kind !== 'wrapped') continue;
    const hex = await rpcCall(url, 'eth_call', [{ to: e.address, data: '0x313ce567' }, 'latest']);
    assert.equal(Number(BigInt(hex)), e.decimals, `tron/${e.symbol} decimals mismatch`);
  }
});