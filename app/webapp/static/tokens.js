/* Settings-tab "API tokens" panel.
 *
 * Mint / list / revoke (after a confirm) job-scoped bearer tokens
 * against /api/tokens. The
 * raw token appears exactly once, in the mint response — the panel shows
 * it in a readonly field with a copy button and it is gone on the next
 * render. Lazy: nothing is fetched until the Settings tab is first
 * opened (the job <select> needs /api/jobs, which is not a boot cost
 * worth paying for a panel most sessions never open).
 *
 * The same sheet holds the delegate token's one control against
 * /api/delegate-token: turn on / regenerate (show-once, like a mint) and
 * turn off, both asked first because the old value stops working.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { confirmDialog } from './confirm-dialog.js';
import { revealInCard } from './dom-utils.js';
import { renderSettingsValues } from './settings-sheets.js';

let loaded = false;
let delegateOn = false;

function fmtStamp(iso) {
  return iso ? iso.replace('T', ' ').slice(0, 16) : '';
}

function scopeText(scope) {
  if (scope === '*') return 'all endpoints';
  const jobs = (scope && scope.jobs) || [];
  return 'job: ' + jobs.join(', ');
}

function renderTokens(tokens) {
  state.apiTokenCount = tokens.length;
  renderSettingsValues();
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
    // Asked first, in the themed dialog, like removing a passkey:
    // a revoked token cannot be brought back.
    revoke.addEventListener('click', async function () {
      if (!(await confirmDialog({
        title: 'Revoke this token?',
        message: 'Anything still calling the launcher with "' + (t.label || t.id) +
          '" stops working. A new token has to be created and pasted in its place.',
        action: 'Revoke',
      }))) return;
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

function renderDelegate(info) {
  const on = !!(info && info.enabled);
  delegateOn = on;
  els.delegateTokenStatus.textContent = on
    ? 'On · made ' + fmtStamp(info.created_at)
    : 'Off';
  els.delegateTokenRegenLabel.textContent = on ? 'Regenerate' : 'Turn on';
  els.delegateTokenOffBtn.hidden = !on;
}

async function fetchDelegate() {
  renderDelegate(await jsonApi('/api/delegate-token'));
}

async function regenerateDelegate() {
  if (delegateOn && !(await confirmDialog({
    title: 'Regenerate the delegate token?',
    message: 'The current value stops working at once. The agent using it ' +
      'needs the new one pasted in.',
    action: 'Regenerate',
  }))) return;
  try {
    const body = await jsonApi('/api/delegate-token', { method: 'POST' });
    els.delegateTokenValue.value = body.token || '';
    els.delegateTokenResult.hidden = false;
    renderDelegate(body);
    toast('Delegate token ready — copy it now, it is shown only once.', 'good');
  } catch (exc) {
    apiFailToast('Regenerate failed', exc);
  }
}

async function disableDelegate() {
  if (!(await confirmDialog({
    title: 'Turn off the delegate token?',
    message: 'Anything calling the launcher with it stops working.',
    action: 'Turn off',
  }))) return;
  try {
    renderDelegate(await jsonApi('/api/delegate-token', { method: 'DELETE' }));
    els.delegateTokenValue.value = '';
    els.delegateTokenResult.hidden = true;
    toast('Delegate token turned off.', 'good');
  } catch (exc) {
    apiFailToast('Turn off failed', exc);
  }
}

function ensureLoaded() {
  if (loaded) return;
  loaded = true;
  fetchTokens().catch(function (exc) {
    console.warn('tokens: list fetch failed', exc);
  });
  fetchDelegate().catch(function (exc) {
    console.warn('tokens: delegate token fetch failed', exc);
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
    toast('Pick the job this token can run.', 'error');
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
    toast('Token created — copy it now, it is shown only once.', 'good');
    await fetchTokens();
  } catch (exc) {
    apiFailToast('Mint failed', exc);
  }
}

async function copyShown(input) {
  const value = input.value;
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast('Token copied.', 'good');
  } catch (_) {
    // Clipboard API can be denied (http, permissions) — fall back to a
    // select-all so a manual copy is one keystroke away.
    input.focus();
    input.select();
    toast('Copy blocked — token selected, copy manually.', 'error');
  }
}

export function wireTokens() {
  if (!els.tokensList) return;
  // No tokens yet: the create form sits right below.
  const tokensEmptyAction = document.getElementById('tokensEmptyAction');
  if (tokensEmptyAction) {
    tokensEmptyAction.addEventListener('click', function () {
      revealInCard(els.tokenLabelInput);
    });
  }
  // Load the token list when Settings opens (from any header's gear,
  // or an empty state landing on a field), so its row shows the count.
  document.addEventListener('launcher:tab', function (e) {
    if (e.detail && e.detail.tab === 'settings') ensureLoaded();
  });
  els.tokenMintBtn.addEventListener('click', mintToken);
  els.tokenCopyBtn.addEventListener('click', function () {
    copyShown(els.tokenMintValue);
  });
  els.delegateTokenRegenBtn.addEventListener('click', regenerateDelegate);
  els.delegateTokenOffBtn.addEventListener('click', disableDelegate);
  els.delegateTokenCopyBtn.addEventListener('click', function () {
    copyShown(els.delegateTokenValue);
  });
}
