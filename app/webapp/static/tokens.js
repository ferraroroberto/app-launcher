/* Settings-tab "API tokens" panel (issue #72).
 *
 * Mint / list / revoke job-scoped bearer tokens against /api/tokens. The
 * raw token appears exactly once, in the mint response — the panel shows
 * it in a readonly field with a copy button and it is gone on the next
 * render. Lazy: nothing is fetched until the Settings tab is first
 * opened (the job <select> needs /api/jobs, which is not a boot cost
 * worth paying for a panel most sessions never open).
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';

let loaded = false;

function fmtStamp(iso) {
  return iso ? iso.replace('T', ' ').slice(0, 16) : '';
}

function scopeText(scope) {
  if (scope === '*') return 'all endpoints';
  const jobs = (scope && scope.jobs) || [];
  return 'job: ' + jobs.join(', ');
}

function renderTokens(tokens) {
  els.tokensList.innerHTML = '';
  els.tokensEmpty.hidden = tokens.length > 0;
  tokens.forEach(function (t) {
    const li = document.createElement('li');
    li.className = 'row token-row';
    const meta = document.createElement('span');
    meta.className = 'token-meta';
    const label = document.createElement('strong');
    label.textContent = t.label || t.id;
    meta.appendChild(label);
    const detail = document.createElement('span');
    detail.className = 'muted small';
    const used = t.last_used_at
      ? 'last used ' + fmtStamp(t.last_used_at)
      : 'never used';
    detail.textContent =
      ' · ' + scopeText(t.scope) + ' · minted ' + fmtStamp(t.created_at) +
      ' · ' + used;
    meta.appendChild(detail);
    li.appendChild(meta);
    const revoke = document.createElement('button');
    revoke.type = 'button';
    revoke.className = 'button-ghost';
    revoke.textContent = 'Revoke';
    revoke.addEventListener('click', async function () {
      try {
        await jsonApi('/api/tokens/' + encodeURIComponent(t.id), {
          method: 'DELETE',
        });
        toast('Token revoked.', 'good');
        await fetchTokens();
      } catch (exc) {
        apiFailToast('Revoke failed', exc);
      }
    });
    li.appendChild(revoke);
    els.tokensList.appendChild(li);
  });
}

export async function fetchTokens() {
  const body = await jsonApi('/api/tokens');
  renderTokens(body.tokens || []);
}

async function fetchJobChoices() {
  const body = await jsonApi('/api/jobs');
  els.tokenJobSelect.innerHTML = '';
  (body.jobs || []).forEach(function (j) {
    const opt = document.createElement('option');
    opt.value = j.id;
    opt.textContent = j.name;
    els.tokenJobSelect.appendChild(opt);
  });
}

function ensureLoaded() {
  if (loaded) return;
  loaded = true;
  fetchTokens().catch(function (exc) {
    console.warn('tokens: list fetch failed', exc);
  });
  fetchJobChoices().catch(function (exc) {
    console.warn('tokens: job choices fetch failed', exc);
  });
}

async function mintToken() {
  const label = els.tokenLabelInput.value.trim();
  const jobId = els.tokenJobSelect.value;
  if (!label) {
    toast('Give the token a label first.', 'error');
    return;
  }
  if (!jobId) {
    toast('Pick the job this token may fire.', 'error');
    return;
  }
  try {
    const body = await jsonApi('/api/tokens', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: label, jobs: [jobId] }),
    });
    els.tokenMintValue.value = body.token || '';
    els.tokenMintResult.hidden = false;
    // Ready-to-paste Stream Deck URL when the tunnel hostname is known
    // (state.status is filled at boot). Informational only — the token
    // field above is the canonical show-once value.
    const tunnel = (state.status && state.status.tunnel_url) || '';
    els.tokenMintUrl.textContent = tunnel
      ? 'Stream Deck URL: ' + tunnel + '/api/jobs/' + jobId +
        '/run?token=' + (body.token || '')
      : '';
    els.tokenLabelInput.value = '';
    toast('Token minted — copy it now, it is shown only once.', 'good');
    await fetchTokens();
  } catch (exc) {
    apiFailToast('Mint failed', exc);
  }
}

async function copyMinted() {
  const value = els.tokenMintValue.value;
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast('Token copied.', 'good');
  } catch (_) {
    // Clipboard API can be denied (http, permissions) — fall back to a
    // select-all so a manual copy is one keystroke away.
    els.tokenMintValue.focus();
    els.tokenMintValue.select();
    toast('Copy blocked — token selected, copy manually.', 'error');
  }
}

export function wireTokens() {
  if (!els.tokensList) return;
  if (els.tabSettings) els.tabSettings.addEventListener('click', ensureLoaded);
  els.tokenMintBtn.addEventListener('click', mintToken);
  els.tokenCopyBtn.addEventListener('click', copyMinted);
}
