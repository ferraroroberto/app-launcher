/* Settings > This PC > Local hub: the address of the local LLM hub, which reads
 * the PC health dot and speaks Read aloud. Empty is off. The server checks the
 * shape and asks the hub's /healthz before it saves, so a refused address comes
 * back with its reason and nothing changes; this file shows that reason under
 * the field and repaints the stored address. A saved address applies at once.
 */
import { state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { fetchConfig } from './claude-options.js';

function field() {
  return document.getElementById('llmHubUrl');
}

function showError(message) {
  const err = document.getElementById('llmHubUrlError');
  const input = field();
  if (err) {
    err.textContent = message || '';
    err.hidden = !message;
  }
  if (input) {
    if (message) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
  }
}

// The row's value: the hub's host and port, or Off.
function summary(url) {
  const text = String(url || '').trim();
  if (!text) return 'Off';
  try { return new URL(text).host; } catch (_e) { return text; }
}

export function renderHubUrl(body, overFocus) {
  const url = (body && body.llm_hub_url) || '';
  document.querySelectorAll('[data-settings-value="hub"]').forEach(function (el) {
    el.textContent = summary(url);
  });
  const input = field();
  // Leave a field being typed in alone: a re-read must not overwrite it. A
  // save passes overFocus, as its field is locked and nothing can be typed.
  if (input && (overFocus || input !== document.activeElement)) input.value = url;
  const local = document.getElementById('llmHubUseLocal');
  if (local && body && body.llm_hub_local_url) local.dataset.localUrl = body.llm_hub_local_url;
}

async function save(value, message) {
  const input = field();
  const button = document.getElementById('llmHubUseLocal');
  showError('');
  if (input) input.disabled = true;
  if (button) button.disabled = true;
  let saved = false;
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ llm_hub_url: value }),
    });
    saved = true;
  } catch (exc) {
    showError((exc && exc.message) || 'Not saved. Check the address and try again.');
    apiFailToast('Hub not saved', exc);
  }
  if (saved) await fetchConfig().catch(function () {});
  // Put the stored address back before unlocking: a disabled field keeps focus
  // until the next frame, which a fast answer beats.
  renderHubUrl(state.config, true);
  if (input) input.disabled = false;
  if (button) button.disabled = false;
  if (saved) toast(message, 'good');
}

export function wireHubUrl() {
  const input = field();
  if (!input) return;
  // `change` fires once the field loses focus or on Enter, never per keystroke,
  // so a half-typed address is never checked. A refused one is put back.
  input.addEventListener('change', async function () {
    const value = input.value.trim();
    await save(value, value ? 'Local hub saved.' : 'Local hub off.');
  });
  const local = document.getElementById('llmHubUseLocal');
  if (local) {
    local.addEventListener('click', async function () {
      const url = local.dataset.localUrl || 'http://127.0.0.1:8000';
      input.value = url;
      await save(url, 'Local hub saved.');
    });
  }
}
