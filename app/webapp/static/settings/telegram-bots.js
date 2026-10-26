/* Telegram bots — the Bots group and each bot's own sheet.
 *
 * The Bots group at the top of Settings › Telegram lists every configured
 * bot; a row opens that bot's sheet. Every field there saves through
 * PATCH /api/life-os/channels/{id} as it changes, with a toast, and a refusal
 * shows the server's message under the field. The payload (state.lifeOsChannels,
 * filled by life-os.js::fetchChannels) carries names and booleans only, so
 * neither a path nor a token reaches this module.
 *
 * `restart_needed` is true / false / null. Only true shows the chip, the
 * banner and the Restart button; null (the bot is not running, or the launch
 * record can't be read) shows nothing, never an "up to date" claim. The
 * restart runs on the owner's tap and is never started from here.
 *
 * The sheet navigation and the channel refresh belong to other modules, so
 * main.js hands them in (wireTelegramBots) rather than this module importing
 * them back.
 */

import { els, state } from '../state.js';
import { apiFailToast, toast } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { confirmDialog } from '../confirm-dialog.js';
import { setSwitch } from '../_vendored/switch/switch.js';
import { patchConfig } from './claude-options.js';
import { checkRow } from './telegram-setup.js';

const IDLE_LABEL = { 30: '30 min', 60: '1 h', 120: '2 h' };
const MODEL_LABEL = { sonnet: 'Sonnet', opus: 'Opus', fable: 'Fable' };
const FIELD_LABEL = { skill: 'Skill', relay: 'Relay', model: 'Model' };
const SVG_NS = 'http://www.w3.org/2000/svg';

let hooks = { refresh: async function () {}, showSheets: function () {} };
let openId = '';

function profileById(id) {
  return (state.lifeOsChannels.profiles || []).find(function (p) { return p.id === id; });
}

function icon(name, cls) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'icon ' + cls);
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}

function span(cls, text) {
  const el = document.createElement('span');
  el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function sharedModelLabel() {
  const model = state.config && state.config.channel_model;
  return model ? (MODEL_LABEL[model] || model) : 'shared model';
}

// "Relay · closes after 1 h idle", "Not running · relay off": the facts the
// row exists to show, running state first. A profile that can't start says so
// instead, since nothing else about it matters until it is set up.
function summary(p) {
  const parts = [];
  if (!p.skill_found || !p.env_present) parts.push('Needs setup');
  else if (p.running === false) parts.push('Not running');
  else if (p.running == null) parts.push('Status unknown');
  if (p.relay) {
    parts.push(parts.length ? 'relay on' : 'Relay');
    parts.push(p.idle_close_minutes ? 'closes after ' + IDLE_LABEL[p.idle_close_minutes] + ' idle' : 'no idle close');
  } else {
    parts.push(parts.length ? 'relay off' : 'Relay off');
  }
  return parts.join(' · ');
}

function botRow(p) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'settings-row bot-row';
  row.dataset.botId = p.id;
  row.setAttribute('aria-haspopup', 'dialog');
  const avatar = span('bot-avatar');
  avatar.appendChild(icon('send-horizontal', 'bot-avatar-icon'));
  if (p.running === true) {
    const badge = span('bot-avatar-badge');
    badge.setAttribute('aria-hidden', 'true');
    avatar.appendChild(badge);
  }
  const text = span('bot-row-text');
  const name = span('bot-row-name', p.label);
  const meta = span('muted small bot-row-meta', summary(p));
  text.append(name, meta);
  row.append(avatar, text);
  if (p.restart_needed === true) {
    row.appendChild(span('channel-check-chip channel-check-chip-attention', 'Restart to apply'));
  }
  row.appendChild(icon('chevron-right', 'settings-row-chevron'));
  row.setAttribute('aria-label', [
    p.label, summary(p), p.restart_needed === true ? 'restart to apply' : '',
  ].filter(Boolean).join(', '));
  row.addEventListener('click', function () { openBot(p.id); });
  return row;
}

function renderBotList(profiles) {
  const host = els.channelBots;
  if (!host) return;
  host.textContent = '';
  profiles.forEach(function (p) { host.appendChild(botRow(p)); });
  host.hidden = profiles.length === 0;
  if (els.channelBotsEmpty) els.channelBotsEmpty.hidden = profiles.length !== 0;
}

// ------------------------------------------------------------ bot sheet
function setError(el, message) {
  if (!el) return;
  el.textContent = message || '';
  el.hidden = !message;
}

function option(value, label) {
  const o = document.createElement('option');
  o.value = value;
  o.textContent = label;
  return o;
}

function fillSelect(select, options, current) {
  select.textContent = '';
  const known = options.some(function (o) { return o[0] === current; });
  options.forEach(function (o) { select.appendChild(option(o[0], o[1])); });
  // A stored value the list no longer offers still shows, never a silent swap.
  if (!known) select.appendChild(option(current, current));
  select.value = current;
}

function changedList(fields) {
  const names = (fields || []).map(function (f) { return FIELD_LABEL[f] || f; });
  if (names.length < 2) return names.join('');
  return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1].toLowerCase();
}

function renderRestart(p) {
  const needed = p.restart_needed === true;
  els.botRestartBanner.hidden = !needed;
  els.botRestartRow.hidden = !needed;
  els.botRestartHelp.hidden = !needed;
  if (needed) {
    els.botRestartText.textContent = ' ' + (changedList(p.restart_fields) || 'A setting') +
      ' changed after this bot started. Until it restarts, it runs with the old settings.';
  }
}

function renderStatus(p) {
  const list = els.botStatus;
  list.textContent = '';
  list.appendChild(p.running == null
    ? checkRow('Running', 'The session host could not be asked', 'unknown')
    : checkRow('Running', '', 'ok', p.running ? 'Yes' : 'No — start it from the Life tab'));
  list.appendChild(p.env_present == null
    ? checkRow('Token file', '', 'unknown')
    : checkRow('Token file', '', p.env_present ? 'ok' : 'missing', 'Present'));
}

function renderBotSheet() {
  const p = profileById(openId);
  if (!p || !els.channelBotSheet) return;
  els.channelBotSheetTitle.textContent = p.label;
  renderRestart(p);
  if (els.botNameInput !== document.activeElement) els.botNameInput.value = p.label;
  const skills = ((state.lifeOsChannels.setup || {}).skills || []).map(function (s) {
    return [s.id, s.id];
  });
  fillSelect(els.botSkillSelect, skills, p.skill);
  fillSelect(els.botModelSelect, [
    ['', 'Shared (' + sharedModelLabel() + ')'],
    ['sonnet', 'Sonnet'], ['opus', 'Opus'], ['fable', 'Fable'],
  ], p.model || '');
  setSwitch(els.botRelayToggle, p.relay === true);
  setSwitch(els.botResumeToggle, p.resume_offer === true);
  els.botResumeToggle.disabled = !p.relay;
  els.botResumeToggle.title = p.relay ? 'Offer a resume when a chat starts' : 'Needs Relay on';
  els.botResumeHelp.textContent = p.relay ? 'Takes effect now.' : 'Needs Relay on.';
  els.botIdleSeg.querySelectorAll('.segmented-item').forEach(function (b) {
    b.setAttribute('aria-pressed', String(Number(b.dataset.value) === p.idle_close_minutes));
    b.disabled = !p.relay;
  });
  els.botIdleHelp.textContent = p.relay ? 'Takes effect now, no restart.' : 'Needs Relay on.';
  renderStatus(p);
}

export function renderBots() {
  renderBotList(state.lifeOsChannels.profiles || []);
  renderBotSheet();
}

function openBot(id) {
  openId = id;
  ['botName', 'botSkill', 'botModel', 'botRelay', 'botResume', 'botIdle'].forEach(function (k) {
    setError(els[k + 'Error'], '');
  });
  renderBotSheet();
  hooks.showSheets(['channelsSheet', 'channelBotSheet']);
  // A fresh read, so the chip and the banner reflect the bot as it is now.
  hooks.refresh().catch(function () {});
}

// Save one change; false after showing why it was refused. Either way the
// controls are redrawn from the server's answer (refresh), so a refused
// value never stays on screen.
async function save(patch, errorEl, message) {
  setError(errorEl, '');
  let ok = false;
  try {
    await terminalJsonApi('/api/life-os/channels/' + encodeURIComponent(openId), {
      method: 'PATCH', body: patch,
    });
    ok = true;
    toast(message, 'good');
  } catch (exc) {
    setError(errorEl, (exc && exc.message) || 'Not saved. Try again.');
    apiFailToast('Save failed', exc);
  }
  await hooks.refresh().catch(function () {});
  renderBotSheet();
  return ok;
}

async function restartBot() {
  const p = profileById(openId);
  if (!p) return;
  const ok = await confirmDialog({
    title: 'Restart the ' + p.label + ' bot?',
    message: 'It quits the running session and starts a fresh one with its current ' +
      'settings. The chat context is not kept.',
    action: 'Restart',
  });
  if (!ok) return;
  els.botRestartBtn.disabled = true;
  try {
    const body = await terminalJsonApi(
      '/api/life-os/channels/' + encodeURIComponent(p.id) + '/restart',
      { method: 'POST', body: {} }
    );
    toast((body.restarted ? 'Restarted ' : 'Started ') + p.label + ' on Telegram', 'good',
      { icon: 'refresh-cw' });
  } catch (exc) {
    apiFailToast('Telegram restart failed', exc);
  } finally {
    els.botRestartBtn.disabled = false;
  }
  await hooks.refresh().catch(function () {});
  renderBotSheet();
}

function wireSwitch(toggle, key, errorEl, message) {
  toggle.addEventListener('click', function () {
    if (toggle.disabled) return;
    const next = toggle.getAttribute('aria-checked') !== 'true';
    setSwitch(toggle, next);
    save({ [key]: next }, errorEl, message(next));
  });
}

function wireBotSheet() {
  els.botNameInput.addEventListener('change', function () {
    const p = profileById(openId);
    const name = els.botNameInput.value.trim();
    if (!p || name === p.label) return;
    if (!name) {
      setError(els.botNameError, 'A bot needs a name.');
      return;
    }
    save({ label: name }, els.botNameError, 'Name saved.');
  });
  els.botSkillSelect.addEventListener('change', function () {
    const skill = els.botSkillSelect.value;
    save({ skill: skill }, els.botSkillError, 'Skill: ' + skill + '.');
  });
  els.botModelSelect.addEventListener('change', function () {
    const model = els.botModelSelect.value;
    save({ model: model }, els.botModelError,
      model ? 'Model: ' + MODEL_LABEL[model] + '.' : 'Model: shared.');
  });
  wireSwitch(els.botRelayToggle, 'relay', els.botRelayError,
    function (on) { return 'Relay ' + (on ? 'on.' : 'off.'); });
  wireSwitch(els.botResumeToggle, 'resume_offer', els.botResumeError,
    function (on) { return 'Resume offer ' + (on ? 'on.' : 'off.'); });
  els.botIdleSeg.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.segmented-item');
    if (!btn || btn.disabled) return;
    const minutes = Number(btn.dataset.value);
    save({ idle_close_minutes: minutes }, els.botIdleError,
      minutes ? 'Closes after ' + IDLE_LABEL[minutes] + ' idle.' : 'Idle close off.');
  });
  els.botRestartBtn.addEventListener('click', restartBot);
}

// The shared model every bot without its own starts on (POST /api/config).
function wireChannelModel() {
  const select = els.channelModelSelect;
  if (!select) return;
  select.addEventListener('change', async function () {
    const model = select.value;
    if (await patchConfig({ channel_model: model })) {
      toast('Telegram model: ' + (MODEL_LABEL[model] || model) + '.', 'good');
    } else if (state.config) {
      select.value = state.config.channel_model;   // patchConfig already toasted
    }
    renderBotSheet();
  });
}

// `refresh` re-reads the channels payload; `showSheets` opens a sheet path.
export function wireTelegramBots(handed) {
  hooks = handed;
  if (!els.channelBotSheet) return;
  wireBotSheet();
  wireChannelModel();
}
