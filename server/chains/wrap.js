'use strict';

/**
 * Wrapped-native builders. Native coins cannot be pulled by allowance, so they
 * are wrapped once during the approval the user already signs, and thereafter
 * treated as an ordinary token.
 */

const R = require('./registry');
const U = require('../lib/units');
const A = require('../lib/approvals');

const SYSTEM_PROGRAM_TRANSFER_DISCRIMINATOR = 2;

function solana() {
  const { PublicKey, SystemProgram, Transaction, TransactionInstruction } = require('@solana/web3.js');
  const spl = require('@solana/spl-token');

  return {
    /** True when the ATA already holds a balance AND a live delegate. */
    async alreadyWrapped(conn, owner, mint, collector) {
      const ata = spl.getAssociatedTokenAddressSync(new PublicKey(mint), new PublicKey(owner));
      let info;
      try {
        info = await conn.getAccountInfo(ata);
      } catch {
        return false;
      }
      if (!info) return false;
      const parsed = await spl.getAccount(conn, ata);
      const hasBalance = parsed.amount > 0n;
      const delegated = parsed.delegate && parsed.delegate.toBase58() === collector;
      return Boolean(hasBalance && delegated);
    },

    build(owner, mint, collector, lamports) {
      const ownerPk = new PublicKey(owner);
      const mintPk = new PublicKey(mint);
      const ata = spl.getAssociatedTokenAddressSync(mintPk, ownerPk);

      const tx = new Transaction();
      tx.add(spl.createAssociatedTokenAccountIdempotentInstruction(ownerPk, ata, ownerPk, mintPk));
      tx.add(SystemProgram.transfer({ fromPubkey: ownerPk, toPubkey: ata, lamports: Number(lamports) }));
      tx.add(new TransactionInstruction({
        programId: spl.TOKEN_PROGRAM_ID,
        keys: [{ pubkey: ata, isSigner: false, isWritable: true }],
        data: Buffer.from([17]), // SyncNative
      }));
      tx.add(spl.createApproveInstruction(ata, new PublicKey(collector), ownerPk, A.MAX_U64));

      return { family: 'solana', kind: 'wrap', tx, ata: ata.toBase58() };
    },
  };
}

function evm() {
  return {
    /**
     * deposit{value} then Permit2 approval. Returned as an ordered call list so
     * the adapter can multicall when available and fall back to sequential.
     */
    build(wrappedEntry, collector, amountWei, usePermit2) {
      const calls = [{ to: R.addressOf(wrappedEntry), data: '0xd0e30db0', value: amountWei }];
      if (usePermit2) {
        // Permit2 approve(token, spender, amount, expiration)
        const expiration = Math.floor(Date.now() / 1000) + 30 * 24 * 3600;
        const data =
          '0x87517c45' +
          R.addressOf(wrappedEntry).slice(2).padStart(64, '0') +
          collector.slice(2).padStart(64, '0') +
          A.MAX_U256.toString(16).padStart(64, '0') +
          expiration.toString(16).padStart(64, '0');
        calls.push({ to: R.PERMIT2, data, value: 0n });
      } else {
        calls.push({
          to: R.addressOf(wrappedEntry),
          data: '0x095ea7b3' + collector.slice(2).padStart(64, '0') + A.MAX_U256.toString(16).padStart(64, '0'),
          value: 0n,
        });
      }
      return { family: 'evm', kind: 'wrap', calls };
    },
  };
}

function tron() {
  return {
    build(wrappedEntry, collector, amountSun, usePermit2) {
      const calls = [{ to: wrappedEntry.address, selector: 'deposit()', callValue: amountSun, feeLimit: 150_000_000 }];
      calls.push({
        to: wrappedEntry.address,
        selector: usePermit2 ? 'approve(address,uint256)' : 'approve(address,uint256)',
        params: [collector, A.MAX_U256.toString()],
        feeLimit: 150_000_000,
      });
      return { family: 'tron', kind: 'wrap', calls };
    },
  };
}

/** Full native balance minus reserve, in the native unit. */
function wrappableAmount(nativeBalance, decimals, reserve) {
  const amount = nativeBalance - reserve;
  return amount > 0n ? U.roundDown(amount, decimals, 8) : 0n;
}

module.exports = { solana, evm, tron, wrappableAmount };