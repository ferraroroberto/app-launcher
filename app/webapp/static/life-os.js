/* Life OS tab (issue #102): skill rows + one-tap launch + a read-only
 * private-content browser.
 *
 * ~80% a clone of the Coding tab. A row launches a Claude session in the
 * life-os repo that auto-invokes the bare /<skill> slash-command; the model,
 * Detached and Resume controls are the shared launch toolbar Code › Projects
 * mounts (#1439, launch-toolbar.js), at the top of the Skills card. Each row
 * leads with an avatar and says when the skill was last used; pinned skills
 * come first, then the most recently used, and skills not used lately fold
 * under one row (#1503). A swipe right (or the kebab) resumes the newest
 * conversation on its own model, a swipe left starts a new one; on the phone
 * the rows run edge to edge to give the swipe room. A skill a Telegram profile is bound to says so
 * in its meta, wears the green badge while that bot runs, and starts or opens
 * the bot from its kebab; there is no Telegram card (#1439). The skills
 * field searches the conversations too (#1504): matches show as a second
 * group under the skills, and a conversation opens in its own sheet. The
 * Read item opens a sheet that reads each skill's files — public
 * SKILL.md/description.md plus the private context/memory/examples/
 * conversations + shared identity. Those content endpoints are Tailscale +
 * passkey gated server-side, so a fetch may 403; we surface the reason
 * rather than a blank pane.
 */

import { els, state } from './state.js';
import { renderHomeHead, renderSettingsHead } from './home-head.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { nameLabel, wireModelCombo } from './dom-utils.js';
import { renderMarkdown } from './markdown.js';
import { ACTION_ROW_MENU, actionRow } from './action-rows.js';
import { avatar, chip } from './glance.js';
import { confirmDialog } from './confirm-dialog.js';
import { failedFetchState, setAsyncState } from './async-state.js';
import { mountLaunchToolbar } from './launch-toolbar.js';
import { listFilter } from './list-filter.js';
import { openSettingsAt } from './tabs.js';
import { renderChannelSetup } from './telegram-setup.js';
import { renderSettingsValues } from './settings-sheets.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';
import { terminalJsonApi } from './webauthn.js';
import { closeConvoViewer, openConvoViewer, wireConvoViewer } from './life-os-viewer.js';

// The skill rows' kebab menu (#1128), on the vendored row-menu.
const skillMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });

// The Skills card's launch toolbar (#1439): the shared component Code ›
// Projects mounts (#1434), so both launch surfaces look and behave alike,
// except that here Detached and Resume live in the model combo's menu
// (#1503 decision 6).
// Mounted at load so wireLifeOs finds its model combo by id; every launch
// reads the switches through it. The ids are the hooks the launch and e2e
// code have always used.
const lifeLaunch = mountLaunchToolbar(document.getElementById('lifeOsLaunchToolbar'), {
  ids: {
    combo: 'lifeOsModelCombo', trigger: 'lifeOsModelBtn', menu: 'lifeOsModelMenu',
    detached: 'lifeOsDetached', resume: 'lifeOsResume',
  },
  model: {
    value: 'claude:sonnet', label: 'Claude · Sonnet',
    title: 'Model for the launched life-os skill',
    menuLabel: 'Skill launch model',
  },
  detachedTitle: 'Launch detached: a console window on the PC, listed and killable in the Coding tab but no phone terminal',
  resumeTitle: 'Resume: reopen the selected agent’s session picker (drops the skill prompt), in a phone terminal, or in the detached console when Detached is also on',
  // Decision 6 (#1503): the switches sit in the combo's menu, under the
  // model list, and its label names any that is on.
  switchesInMenu: true,
  menuHint: 'The model is for new conversations; a resumed one keeps its own. ' +
    'Resume: a tap opens the session picker.',
});

function launchDetached() { return !!(lifeLaunch && lifeLaunch.isDetached()); }
function launchResume() { return !!(lifeLaunch && lifeLaunch.isResume()); }

// The Skills list's name filter (#1439, the Apps filter's twin), re-applied
// after every render.
const skillsFilter = listFilter({
  input: els.lifeOsFilterInput,
  list: els.lifeOsList,
  empty: els.lifeOsFilterEmpty,
  storageKey: 'app-launcher.filter.skills',
  // A typed filter searches every skill: the fold opens while it is set,
  // and the Skills group's count follows what it shows.
  onApply: function (query) {
    if (els.lifeOsList) els.lifeOsList.classList.toggle('is-filtering', !!query);
    syncSkillsHead();
  },
});

// The launch-model dropdown controller ({setValue, getValue}), wired in the
// tab's wiring (#540). Read at launch time; no server round-trip — it's
// per-launch, like the Board dispatch combo. It is also the model a
// conversation's "Start new in …" hands off to (#1504).
let lifeOsModelCombo = null;
export function setLifeOsModelOptions(items) {
  if (lifeOsModelCombo) lifeOsModelCombo.setOptions(items);
}

function lifeOsModel() {
  return (lifeOsModelCombo && lifeOsModelCombo.getValue()) || 'claude:sonnet';
}

// ----------------------------------------------------------- skills list
// When the skills last read well (#1501): a failure before it is the
// list's error with Retry, one after it a stale line over the last list.
let skillsReadAt = 0;

function retrySkills() {
  fetchSkills().catch(function () {});
}

export async function fetchSkills() {
  if (!skillsReadAt) setAsyncState(els.lifeOsState, 'loading', { message: 'Reading skills…' });
  try {
    const body = await jsonApi('/api/life-os/skills');
    skillsReadAt = Date.now();
    setAsyncState(els.lifeOsState, 'ready');
    state.lifeOsSkills = body.skills || [];
    renderSkills();
  } catch (exc) {
    logPollFailure('life-os skills fetch failed', exc);
    failedFetchState(els.lifeOsState, !!skillsReadAt, skillsReadAt, 'the skills', retrySkills);
  }
  // The Telegram profiles ride every skills refresh (tab open, Settings
  // save, boot) so they never need a refresh point of their own (#1366).
  await fetchChannels();
}

// -------------------------------------------- Telegram channels (#1366)
// One machine-local profile (config/channel_profiles.json) per bot: a life-os
// skill launched with its own Telegram bot attached. The Life tab has no card
// for them (#1439): a skill a profile is bound to wears a send glyph, green
// while that bot's session runs, and its kebab opens or starts the bot. What
// is ready, broken or missing is Settings › Telegram's (telegram-setup.js),
// rendered from this same payload. The server answers with each profile's
// running session, so a running bot opens instead of launching a duplicate
// (the server refuses one with a 409 anyway — one bot token, one getUpdates
// consumer).
export async function fetchChannels() {
  try {
    const body = await jsonApi('/api/life-os/channels');
    state.lifeOsChannels = {
      profiles: body.profiles || [],
      problems: body.problems || [],
      setup: body.setup || null,
    };
    state.lifeOsChannelsFailed = false;
    renderSkills();
    renderChannelSetup();
    renderSettingsValues();
  } catch (exc) {
    logPollFailure('life-os channels fetch failed', exc);
    state.lifeOsChannelsFailed = true;
  }
  renderSettingsHead();
}

// The usable profiles bound to one skill. A broken profile (its skill not
// found) never matches a listed skill, and is reported in Settings only.
function skillProfiles(s) {
  return (state.lifeOsChannels.profiles || []).filter(function (p) {
    return p.skill_found && p.skill === s.id;
  });
}

// The kebab's Telegram items, first in the menu: open a running bot, start a
// stopped one. With two bots on one skill each item names its profile.
function channelMenuItems(profiles) {
  const named = profiles.length > 1;
  return profiles.map(function (p) {
    const running = p.running === true;
    const suffix = named ? ' · ' + p.label : '';
    return {
      dataset: {
        item: running ? 'lifeos-channel-open-btn' : 'lifeos-channel-start-btn',
        profile: p.id,
      },
      glyph: 'send-horizontal',
      label: (running ? 'Open Telegram session' : 'Start on Telegram') + suffix,
      ariaLabel: (running ? 'Open the ' : 'Start the ') + p.label + ' Telegram session',
      onTap: function () {
        if (running) openChannelSession(p); else launchChannel(p);
      },
    };
  });
}

function openChannelSession(p) {
  handleLaunchResponse({ session_id: p.session_id, kind: 'pty', name: p.label });
}

// Start a stopped bot. The toolbar applies as to any skill launch: its model,
// and Resume reopens the session picker. A Telegram session is a terminal on
// the phone, so there is no detached form (the server refuses it): say so
// instead of silently ignoring the switch.
async function launchChannel(p) {
  if (launchDetached()) {
    toast('Telegram sessions run in a terminal — turn Detached off.', 'error');
    return;
  }
  const resume = launchResume();
  const model = lifeOsModel();
  const payload = { mode: 'pty', model: model, resume: resume };
  applyLaunchSizePayload(payload);
  try {
    const body = await terminalJsonApi(
      '/api/life-os/channels/' + encodeURIComponent(p.id) + '/launch',
      { method: 'POST', body: payload }
    );
    toast(
      (resume ? 'Resumed ' : 'Launched ') + p.label + ' on Telegram' + modelTag(model),
      'good',
      { icon: resume ? 'rotate-ccw' : 'send-horizontal' }
    );
    fetchChannels().catch(function () {});
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Telegram launch failed', exc);
    fetchChannels().catch(function () {});
  }
}

// A skill counts as in use for this many days after its last captured
// conversation (#1503): pinned skills and these show, the rest fold under
// one "N more skills" row.
const RECENT_DAYS = 30;

// Whole days from a YYYY-MM-DD (read as a local calendar day) to today, or
// null for no date.
function daysSince(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  if (!m) return null;
  const day = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.max(0, Math.round((today - day) / 86400000));
}

// The row's meta (#1503): when the skill was last used, in words. That is
// its newest captured conversation, so a skill launched but never captured
// says "no conversations yet", not "never used".
function lastUsedMeta(s) {
  const days = daysSince(s.last_used);
  if (days === null) return 'no conversations yet';
  if (days === 0) return 'used today';
  if (days === 1) return 'used yesterday';
  if (days < 7) return 'used ' + days + ' days ago';
  if (days < 14) return 'used last week';
  if (days < 30) return 'used ' + Math.floor(days / 7) + ' weeks ago';
  if (days < 60) return 'used last month';
  return 'used ' + Math.floor(days / 30) + ' months ago';
}

// Pinned first, then the most recently used, then A to Z (#1503 decision 5).
function compareSkills(a, b) {
  if (!!a.is_favorite !== !!b.is_favorite) return a.is_favorite ? -1 : 1;
  const au = a.last_used || '';
  const bu = b.last_used || '';
  if (au !== bu) return au < bu ? 1 : -1;
  return String(a.name).localeCompare(String(b.name));
}

// The avatar's glyph: the skill's own frontmatter `icon:` when the page's
// sprite has that glyph, else the name's first letter.
function skillAvatar(s, alive) {
  let glyph = null;
  if (s.icon && document.getElementById('i-' + s.icon)) {
    glyph = s.icon;
  } else {
    glyph = document.createElement('span');
    glyph.className = 'lifeos-monogram';
    glyph.setAttribute('aria-hidden', 'true');
    glyph.textContent = String(s.name || s.id || '?').charAt(0).toUpperCase();
  }
  return avatar(glyph, alive ? 'alive' : 'none', 'lifeos-avatar');
}

// Whether the rows under the fold are showing; kept across re-renders.
let skillsUnfolded = false;

// The kebab's launch items (#1503), first so the swipe actions have a
// single-pointer route (WCAG 2.5.1), the only one with a mouse: resume the
// newest conversation, or start a new one. Both ignore the toolbar's Resume
// switch.
function launchMenuItems(s) {
  return [
    {
      dataset: { item: 'lifeos-resume-last-btn' }, glyph: 'rotate-ccw',
      label: 'Resume last conversation',
      ariaLabel: 'Resume the last conversation with ' + s.name,
      disabled: !s.has_conversations,
      hint: 'No conversation yet',
      onTap: function () { resumeLatest(s); },
    },
    {
      dataset: { item: 'lifeos-start-new-btn' }, glyph: 'circle-plus',
      label: 'Start new conversation',
      ariaLabel: 'Start a new conversation with ' + s.name,
      onTap: function () { launchSkill(s, { fresh: true }); },
    },
    {
      dataset: { item: 'lifeos-pin-btn' }, glyph: 'pin',
      label: s.is_favorite ? 'Unpin' : 'Pin to top',
      ariaLabel: (s.is_favorite ? 'Unpin ' : 'Pin ') + s.name,
      onTap: function () { toggleSkillPin(s); },
    },
  ];
}

export function renderSkills() {
  const host = els.lifeOsList;
  if (!host) return;
  renderHomeHead();
  host.innerHTML = '';
  const skills = state.lifeOsSkills.slice().sort(compareSkills);
  // "No skills found" only after a good read: before one, an empty list is
  // lifeOsState's loading or error, not an empty folder (#1501).
  els.lifeOsEmpty.hidden = skills.length !== 0 || !skillsReadAt;

  // The fold (#1503): pinned skills and those used in the last RECENT_DAYS
  // show; the rest sit under one row that opens in place. With nothing in
  // use yet (a fresh install), everything shows and there is no fold.
  const inUse = function (s) {
    const days = daysSince(s.last_used);
    return s.is_favorite || (days !== null && days <= RECENT_DAYS);
  };
  let shown = skills.filter(inUse);
  let folded = skills.filter(function (s) { return !inUse(s); });
  if (!shown.length) {
    shown = skills;
    folded = [];
  }
  host.classList.toggle('is-unfolded', skillsUnfolded);

  function addRow(s, isFolded) {
    // Tapping the row launches the skill (#1128, the action-row contract):
    // a fresh conversation that auto-invokes /<skill>, or the session picker
    // with the toolbar's Resume on. The avatar leads (a running Telegram bot
    // is its green badge); everything else is in the one trailing kebab.
    const profiles = skillProfiles(s);
    const alive = profiles.some(function (p) { return p.running === true; });
    const row = actionRow({
      id: s.id,
      className: 'lifeos-item' + (isFolded ? ' is-folded' : ''),
      title: s.name,
      meta: lastUsedMeta(s) + (profiles.length ? ' · Telegram' : ''),
      avatar: skillAvatar(s, alive),
      label: 'Launch ' + s.name,
      onMain: function () { launchSkill(s); },
      kebabClass: 'lifeos-menu-anchor',
      kebabLabel: 'Skill actions',
      // Swipe right resumes the newest conversation (the frequent one, so
      // the leading edge, iOS Mail's rule); swipe left starts a new one.
      // Both are also the kebab's first two items, and both ignore the
      // Resume switch (#1503).
      swipe: {
        leading: {
          label: 'Resume', glyph: 'rotate-ccw', tone: 'accent',
          disabled: !s.has_conversations, reason: 'No conversation yet',
          onCommit: function () { resumeLatest(s); },
        },
        trailing: {
          label: 'New', glyph: 'circle-plus', tone: 'neutral',
          onCommit: function () { launchSkill(s, { fresh: true }); },
        },
      },
    });
    if (s.is_favorite) row.li.dataset.pinned = 'true';
    skillMenu.attach(s.id, row.kebab, launchMenuItems(s).concat(
      channelMenuItems(profiles),
      [
        {
          // The read-only content browser for this skill.
          dataset: { item: 'lifeos-browse-btn' }, glyph: 'book-open',
          label: 'Read', ariaLabel: 'Browse ' + s.name,
          onTap: function () { openBrowser(s); },
        },
        {
          // This skill's own conversations, newest first, in the field's
          // Conversations group behind a scope chip (#727, #1504).
          dataset: { item: 'lifeos-convo-btn' }, glyph: 'messages-square',
          label: 'Conversations', ariaLabel: 'Conversations with ' + s.name,
          onTap: function () { openConvos(s); },
        },
      ]
    ), {
      // A phone has no hover to explain a disabled Resume, so its reason is
      // the menu's first line (the Conversations rows' rule, #1528).
      header: s.has_conversations ? '' : 'No conversation yet',
    });
    host.appendChild(row.li);
  }

  shown.forEach(function (s) { addRow(s, false); });
  if (folded.length) host.appendChild(foldRow(folded.length));
  folded.forEach(function (s) { addRow(s, true); });
  // An open menu whose row is gone drops its state; a reopened one keeps it.
  skillMenu.endRender();
  skillsFilter.apply();
}

// The one row the folded skills sit under. It has no .action-row-title, so
// the filter passes over it; while a filter is typed the fold opens and this
// row steps aside (CSS, keyed on the list's is-filtering class).
function foldRow(count) {
  const li = document.createElement('li');
  li.className = 'lifeos-fold';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'drill-row lifeos-fold-btn';
  btn.setAttribute('aria-expanded', skillsUnfolded ? 'true' : 'false');
  const text = document.createElement('span');
  text.className = 'srow-text';
  const title = document.createElement('span');
  title.className = 'srow-title';
  title.textContent = skillsUnfolded ? 'Fewer skills'
    : count + (count === 1 ? ' more skill' : ' more skills');
  text.appendChild(title);
  btn.appendChild(avatar('chevrons-up-down', 'none', 'lifeos-fold-avatar'));
  btn.appendChild(text);
  btn.addEventListener('click', function () {
    skillsUnfolded = !skillsUnfolded;
    renderSkills();
  });
  li.appendChild(btn);
  return li;
}

// Pin / unpin a skill (#1503 decision 5, over #1070's favorites list, so a
// starred skill is pinned with no migration). Persists server-side, then
// re-fetches the skills list so the order comes from the authoritative
// payload, exactly like the Coding tab's toggleFavorite (#250).
async function toggleSkillPin(s) {
  try {
    await jsonApi('/api/life-os/favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: s.id, favorite: !s.is_favorite }),
    });
    await fetchSkills();
  } catch (exc) {
    apiFailToast(s.is_favorite ? 'Could not unpin' : 'Could not pin', exc);
  }
}

// ------------------------------------------------- weekly recap (issue #167)
// One glance row under the skills (#1439): how long since the recap ledger
// last moved, a chip only for an exception (due or overdue, a draft ready),
// and ▶, the row's one visible verb, which launches /weekly-recap (the
// interactive review) with the Skills toolbar's model and Detached switch.
// The drafting half runs headless on a schedule, so this is review-only.
// Fetched on every Life OS tab open (cheap stat + glob server-side).
export async function fetchRecapStatus() {
  try {
    state.lifeOsRecap = await jsonApi('/api/life-os/recap-status');
    renderRecap();
  } catch (exc) {
    logPollFailure('life-os recap-status fetch failed', exc);
  }
}

// "last run 3 days ago", "last run today" or "never run".
function recapMeta(r) {
  if ((r.staleness || 'never') === 'never') return 'never run';
  const d = Math.round(r.age_days || 0);
  if (d <= 0) return 'last run today';
  return 'last run ' + d + (d === 1 ? ' day ago' : ' days ago');
}

function renderRecap() {
  const host = els.lifeOsRecap;
  const list = els.lifeOsRecapList;
  if (!host || !list) return;
  const r = state.lifeOsRecap;
  // The header carries the recap's exception (home-head.js), so it follows
  // every recap read.
  renderHomeHead();
  // Hide the row when life-os isn't checked out — same as the skills list.
  if (!r || !r.available) {
    host.hidden = true;
    list.replaceChildren();
    return;
  }
  host.hidden = false;
  const status = r.staleness || 'never';
  const chips = [];
  if (status === 'due' || status === 'overdue') {
    chips.push(chip(status, 'attention', 'lifeos-recap-chip'));
  }
  if (r.proposal_pending) chips.push(chip('draft ready', 'accent', 'lifeos-recap-chip'));
  placeRecap(host, chips.length > 0);
  const row = actionRow({
    id: 'weekly-recap',
    className: 'lifeos-recap-item',
    title: 'Weekly recap',
    meta: recapMeta(r),
    chips: chips,
    label: 'Review the weekly recap',
    onMain: launchRecap,
    avatar: avatar('notebook-text', 'none', 'lifeos-recap-avatar'),
  });
  const verb = document.createElement('button');
  verb.type = 'button';
  verb.id = 'lifeOsRecapLaunch';
  verb.className = 'action-row-verb row-verb-plain lifeos-recap-launch';
  verb.innerHTML = icon('play');
  verb.title = 'Review the weekly recap (/weekly-recap)';
  verb.setAttribute('aria-label', 'Launch weekly recap review');
  verb.addEventListener('click', launchRecap);
  row.li.appendChild(verb);
  list.replaceChildren(row.li);
}

// The recap keeps its row under Skills, and rises above it as the tab's
// exception while it is due or overdue or a draft is ready (#1503).
function placeRecap(card, exception) {
  const skills = document.querySelector('#paneLifeOS .lifeos-list-card');
  if (!skills || !card.parentNode) return;
  if (exception) {
    if (card.nextElementSibling !== skills) skills.parentNode.insertBefore(card, skills);
  } else if (skills.nextElementSibling !== card) {
    skills.parentNode.insertBefore(card, skills.nextSibling);
  }
  card.classList.toggle('is-exception', exception);
}

// Toast suffix for the launch model: silent on the Sonnet default (the
// common case), " (Opus)" / " (Fable)" otherwise — mirroring the old opus
// tag's terseness (#540).
function modelTag(model) {
  if (!model || model === 'sonnet' || model === 'claude:sonnet') return '';
  const value = model.split(':').pop();
  const label = value.replace(/^gpt-(?:5\.6|6)-/, '');
  return ' (' + label.charAt(0).toUpperCase() + label.slice(1) + ')';
}

async function launchRecap() {
  // Reuse the Skills toolbar: Detached → remote, the model combo → the
  // launch model (#540, replacing the old opus on/off toggle).
  const mode = launchDetached() ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = { mode: mode, model: model };
  // A desktop browser launch gets a dedicated PC Edge --app window (issue
  // #241); the phone carries its real terminal size so the PTY spawns at
  // the width the overlay will fit() to (issue #374, #126). Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    // Passkey-gated like /api/board/issues/start (#1036): a spawn is
    // terminal-grade, so the terminal token must ride along (cf. #997).
    const body = await terminalJsonApi('/api/life-os/recap/launch', { method: 'POST', body: payload });
    toast(
      'Launched weekly recap' + modelTag(model) +
        (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: 'sprout' }
    );
    // A desktop browser gets its terminal in a dedicated PC Edge window,
    // not in-page (issue #241) — so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Recap launch failed', exc);
  }
}

// opts.fresh (#1503): a new conversation whatever the Resume switch says —
// the kebab's "Start new conversation" and the swipe's New are explicit.
async function launchSkill(s, opts) {
  // Resume (issue #151) reopens Claude's session picker, dropping the
  // /<skill> prompt. Detached and Resume are orthogonal (issue #157,
  // matching the Coding tab): Detached → 'remote' independent of Resume, so
  // a Detached+Resume launch renders the picker in the detached console
  // while Resume alone streams it to the phone over a PTY.
  const resume = !(opts && opts.fresh) && launchResume();
  const mode = launchDetached() ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = { mode: mode, model: model, resume: resume };
  // Same size contract as launchRecap (issue #374, #126, #241). Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    // Passkey-gated, same as launchRecap (#1036).
    const body = await terminalJsonApi(
      '/api/life-os/skills/' + encodeURIComponent(s.id) + '/launch',
      { method: 'POST', body: payload }
    );
    toast(
      (resume ? 'Resumed ' : 'Launched ') + s.name +
        modelTag(model) + (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: resume ? 'rotate-ccw' : 'sprout' }
    );
    // Full-control sessions drop straight into the terminal; detached
    // ones only appear in the Coding tab's running-sessions list. A
    // desktop browser gets its terminal in a dedicated PC Edge window
    // instead of in-page (issue #241), so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Launch failed', exc);
  }
}

// The model a resumed conversation came back on, for its toast (#1503): the
// server reads it from the conversation's own transcript, so it can be
// unknown (""), and for Codex it can be the default standing in for a model
// that could not be read (model_kept false).
function resumedModelTag(body) {
  const raw = String((body && body.model) || '');
  if (!raw) return '';
  const family = /^claude-([a-z]+)/.exec(raw);
  let label = family ? family[1] : raw.replace(/^gpt-(?:5\.6|6)-/, '');
  label = label.charAt(0).toUpperCase() + label.slice(1);
  return body.model_kept === false
    ? ' (' + label + ': its own model could not be read)'
    : ' (' + label + ')';
}

// Resume a skill's newest conversation on that conversation's own model
// (#1503): the kebab's "Resume last conversation" and the swipe's Resume.
// Detached applies; the model combo and the Resume switch do not. One that
// is already open in a running session opens instead of starting a copy.
async function resumeLatest(s) {
  const mode = launchDetached() ? 'remote' : 'pty';
  const payload = { mode: mode };
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await terminalJsonApi(
      '/api/life-os/skills/' + encodeURIComponent(s.id) + '/conversations/resume-latest',
      { method: 'POST', body: payload }
    );
    if (body.conversation_action === 'open') {
      toast('Opened ' + s.name + ': that conversation is already running', 'good',
        { icon: 'rotate-ccw' });
    } else {
      toast('Resumed ' + s.name + resumedModelTag(body) +
        (mode === 'remote' ? ' (detached)' : ''), 'good', { icon: 'rotate-ccw' });
    }
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Resume failed', exc);
  }
}

// --------------------------------------------------- content browser
// Browse is a sheet of the skill's readable files; a tap opens the file in
// the document sheet over it, whose ✕ comes back to the list (#1504). A
// capture opened raw from the conversation sheet uses the same document
// sheet, over that one.

// The file the document sheet shows, which its Rename log and Delete log
// rows act on. Null while the sheet is closed.
let openDocFile = null;

async function openBrowser(s) {
  const d = els.lifeOsBrowser;
  if (!d || !d.showModal) return;
  state.lifeOsBrowser = { skillId: s.id, name: s.name, files: [] };
  els.lifeOsBrowserTitle.textContent = s.name;
  if (!d.open) d.showModal();
  await loadFileList();
}

// (Re)load the current skill's file list — runs every time the browser
// opens, so a conversation log added on the PC shows up on reopen.
async function loadFileList() {
  const b = state.lifeOsBrowser;
  if (!b) return;
  els.lifeOsFileList.innerHTML = '<li class="muted small">Loading…</li>';
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(b.skillId) + '/files'
    );
    b.files = body.files || [];
    renderFileList(b.files);
  } catch (exc) {
    // The content endpoints are Tailscale + passkey gated — a 403 here
    // means this connection can't reach them. Say so plainly, in the list.
    const msg = (exc && exc.status === 403)
      ? 'The content browser is Tailscale-only (and passkey-gated). Open the ' +
        'launcher over your Tailscale URL on an enrolled device.'
      : 'Could not load files: ' + (exc.message || exc);
    els.lifeOsFileList.innerHTML = '';
    const li = document.createElement('li');
    li.className = 'muted small';
    li.textContent = msg;
    els.lifeOsFileList.appendChild(li);
  }
}

function renderFileList(files) {
  const host = els.lifeOsFileList;
  host.innerHTML = '';
  if (!files.length) {
    const p = document.createElement('li');
    p.className = 'muted small';
    p.textContent = 'No readable files.';
    host.appendChild(p);
    return;
  }
  let lastCat = null;
  files.forEach(function (f) {
    if (f.category !== lastCat) {
      const h = document.createElement('li');
      h.className = 'lifeos-file-cat';
      h.textContent = nameLabel(f.category);
      host.appendChild(h);
      lastCat = f.category;
    }
    const li = document.createElement('li');
    li.className = 'lifeos-file-row';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lifeos-file-btn';
    btn.textContent = f.name;
    btn.title = f.path;
    btn.addEventListener('click', function () { loadFile(f); });
    li.appendChild(btn);
    // No delete control in the list — the list is navigation only. A
    // disposable conversation log's Rename and Delete sit under its text
    // in the document sheet (see loadFile below).
    host.appendChild(li);
  });
}

function closeBrowser() {
  if (els.lifeOsBrowser && els.lifeOsBrowser.open) els.lifeOsBrowser.close();
}

async function deleteFile(f) {
  const ok = await confirmDialog({
    title: 'Delete this conversation log?',
    message: f.name + ' is removed from disk. This cannot be undone.',
    action: 'Delete',
  });
  if (!ok) return;
  try {
    await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path),
      { method: 'DELETE' }
    );
  } catch (exc) {
    apiFailToast('Delete failed', exc);
    return;
  }
  toast('Deleted ' + f.name, 'good', { icon: 'trash-2' });
  closeDoc();
  // The conversation it was the capture of is gone with it.
  if (sheetRow && sheetRow.path === f.path) closeConvoLayers();
  await refreshAfterLogChange();
}

// A conversation log can be deleted or renamed from Browse, from a capture
// opened raw, or from the conversation sheet. Refresh every list on screen
// that could show it: the Browse file list and the Conversations group.
async function refreshAfterLogChange() {
  const reads = [];
  if (els.lifeOsBrowser && els.lifeOsBrowser.open) reads.push(loadFileList());
  if (convoMode === 'browse' || convoMode === 'search') reads.push(refreshConvos());
  await Promise.all(reads);
}

// Lower-case, spaces (and any other punctuation) → single dashes, trimmed —
// the same shape the capture hook's slugs already have.
function slugify(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

// The name part of a log's file name: what follows the date-and-time prefix
// the server keeps on a rename (life_os_files.py _DATE_PREFIX_RE).
function logSlug(name) {
  return String(name || '').replace(/\.md$/i, '')
    .replace(/^\d{4}-\d{2}-\d{2}-\d{4}-/, '');
}

// The rename sheet (#1439, in place of a native prompt()): resolves the name
// typed, or null when ✕, Escape or a backdrop tap cancels it.
let renameSettle = null;

function finishRename(value) {
  const d = els.lifeOsRenameDialog;
  const resolve = renameSettle;
  renameSettle = null;
  if (d && d.open) d.close();
  if (resolve) resolve(value);
}

// The one sheet serves both renames; each fills in its own wording. The log
// rename (a file name, always required) is the original; the conversation
// title (#1495) may be left empty, which clears it.
const RENAME_LOG_SHEET = {
  heading: 'Rename log', action: 'Rename', placeholder: '', required: true,
  help: 'The date stays. Spaces become dashes, lower-cased.',
  maxLength: null, typing: 'none',
};
const RENAME_TITLE_SHEET = {
  heading: 'Rename conversation', action: 'Save', required: false,
  help: 'Leave it empty to go back to the topic.',
  maxLength: 120, typing: 'sentences',
};

function askName(current, sheet) {
  const d = els.lifeOsRenameDialog;
  if (!d || !d.showModal) return Promise.resolve(null);
  if (renameSettle) finishRename(null);
  const input = els.lifeOsRenameInput;
  els.lifeOsRenameHeading.textContent = sheet.heading;
  els.lifeOsRenameHelp.textContent = sheet.help;
  els.lifeOsRenameSave.textContent = sheet.action;
  input.required = sheet.required;
  input.placeholder = sheet.placeholder;
  if (sheet.maxLength) input.maxLength = sheet.maxLength;
  else input.removeAttribute('maxlength');
  input.setAttribute('autocapitalize', sheet.typing);
  input.spellcheck = sheet.typing !== 'none';
  input.value = current;
  return new Promise(function (resolve) {
    renameSettle = resolve;
    d.showModal();
  });
}

function wireRenameDialog() {
  const d = els.lifeOsRenameDialog;
  if (!d) return;
  els.lifeOsRenameClose.addEventListener('click', function () { finishRename(null); });
  els.lifeOsRenameForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    finishRename(els.lifeOsRenameInput.value);
  });
  // Escape closes the dialog natively, so any close that was not a submit
  // is a cancel; an open dialog ignores a close event queued by the last one.
  d.addEventListener('close', function () {
    if (!d.open) finishRename(null);
  });
  // A tap on the backdrop lands on the <dialog> itself, outside the card.
  d.addEventListener('click', function (ev) {
    if (ev.target === d) finishRename(null);
  });
}

async function renameFile(f) {
  const proposed = await askName(logSlug(f.name), RENAME_LOG_SHEET);
  if (proposed === null) return;            // cancelled
  const slug = slugify(proposed);
  if (!slug) { toast('Name cannot be empty', 'error'); return; }
  let body;
  try {
    body = await jsonApi('/api/life-os/file/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: f.path, slug: slug }),
    });
  } catch (exc) {
    apiFailToast('Rename failed', exc);
    return;
  }
  toast('Renamed to ' + (body.name || slug), 'good', { icon: 'pencil' });
  // Name (and path) changed: back to the list, and a conversation sheet
  // still holding the old path has nothing left to act on.
  closeDoc();
  if (sheetRow && sheetRow.path === f.path) closeConvoLayers();
  await refreshAfterLogChange();
}

// Rename one conversation as the owner sees it (#1495): a title kept beside the
// capture, shown in place of the digest topic. Empty clears it. The open
// conversation sheet takes the new name in place.
async function renameConversationTitle(r) {
  const current = r.title || '';
  const typed = await askName(
    current, Object.assign({}, RENAME_TITLE_SHEET, { placeholder: r.topic || '' })
  );
  if (typed === null) return;               // cancelled
  const title = typed.trim().replace(/\s+/g, ' ');
  if (title === current) return;
  try {
    await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(r.skill) +
        '/conversations?path=' + encodeURIComponent(r.path),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title }),
      }
    );
  } catch (exc) {
    apiFailToast('Rename failed', exc);
    return;
  }
  toast(title ? 'Renamed to ' + title : 'Name cleared', 'good', { icon: 'pencil' });
  r.title = title;
  if (sheetRow === r) fillConvoSheet();
  await refreshAfterLogChange();
}

async function loadFile(f) {
  const d = els.lifeOsDoc;
  if (!d || !d.showModal) return;
  openDocFile = f;
  els.lifeOsDocTitle.textContent = f.name || 'File';
  // Rename and Delete only for a conversation log — disposable run
  // transcripts, editable while you read them. Every other category (and
  // the .gitkeep placeholder) only reads.
  els.lifeOsDocLinks.hidden = !isEditableLog(f);
  els.lifeOsFileContent.innerHTML = '<p class="muted small">Loading…</p>';
  if (!d.open) d.showModal();
  try {
    const body = await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path)
    );
    if (openDocFile !== f) return;          // closed or moved on meanwhile
    els.lifeOsFileContent.innerHTML = renderMarkdown(body.content || '');
    if (body.truncated) {
      const note = document.createElement('p');
      note.className = 'muted small';
      note.textContent = '… (truncated)';
      els.lifeOsFileContent.appendChild(note);
    }
    els.lifeOsFileContent.scrollTop = 0;
  } catch (exc) {
    if (openDocFile !== f) return;
    els.lifeOsFileContent.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'Could not load: ' + (exc.message || exc);
    els.lifeOsFileContent.appendChild(p);
  }
}

// A conversation log the sheet may act on — any file under a skill's
// conversations/ EXCEPT the .gitkeep placeholder that keeps the (otherwise
// empty) dir tracked in git. Deleting/renaming that would untrack the dir,
// so it stays off-limits (the server refuses it too — defence in depth).
function isEditableLog(f) {
  return !!f && f.category === 'conversations' &&
    !/(^|\/)\.gitkeep$/.test(f.name || '');
}

function closeDoc() {
  if (els.lifeOsDoc && els.lifeOsDoc.open) els.lifeOsDoc.close();
}

// The sheets' shared closing (#1504): ✕ and Done close, a tap on the
// backdrop (which lands on the <dialog> itself) closes, Escape closes
// natively, and `onClose` runs after any of them.
function wireSheet(d, buttons, onClose) {
  if (!d) return;
  buttons.forEach(function (b) {
    if (b) b.addEventListener('click', function () { d.close(); });
  });
  d.addEventListener('click', function (ev) {
    if (ev.target === d) d.close();
  });
  if (onClose) d.addEventListener('close', onClose);
}

// ----------------------------- conversations in the skills field (#1504)
// The skills field searches conversations too (decision 9 of #1500): from
// CONVO_MIN_QUERY characters, after the debounce, the matches show as a
// second group under the skills. A skill's ⋮ Conversations narrows the group
// to that skill's own list, newest first (the chip in the field); the chip's
// ✕ widens it to every skill. A search runs only on typing: the text a
// reload restores filters the skills alone.
//
// The group's mode: 'off' (hidden), 'browse' (one skill's index, nothing
// typed), 'search' (ranked hits, within the scope if there is one), or
// 'notice' (a ?convo= link that could not resolve, saying so).
let convoMode = 'off';
// The skill the group is narrowed to ({ id, name }), or null for every skill.
let convoScope = null;
// The rows on screen, as the server sent them (so a date re-sort of search
// hits can return to relevance without a refetch) and as rendered.
let convoSourceRows = [];
let convoRows = [];
let convoQueryTimer = null;
// Bumped by every read and by switching the group off, so an answer that
// lands after the field or scope moved on is dropped.
let convoSeq = 0;
// One letter matches nearly everything: the search starts at two.
const CONVO_MIN_QUERY = 2;

// The ordering applied to *search* results — 'relevance' (the server's rank
// order) until the user picks another, and reset to it whenever the search
// ends. Deliberately not persisted: the browse list keeps its own remembered
// date sort in state.lifeOsConvoSort.
let convoSearchSort = 'relevance';

function fieldQuery() {
  return els.lifeOsFilterInput ? els.lifeOsFilterInput.value.trim() : '';
}

// Whatever the field and the scope now ask for. Resolves to the rows
// rendered (a ?convo= link resolves against them), or null.
function refreshConvos() {
  window.clearTimeout(convoQueryTimer);
  const query = fieldQuery();
  if (query.length >= CONVO_MIN_QUERY) return runConvoSearch(query);
  // Nothing (or one letter) typed: the search's transient order goes, and
  // the group falls back to the skill's own list, or steps aside.
  convoSearchSort = 'relevance';
  if (convoScope) return loadConvos();
  setConvoMode('off');
  return Promise.resolve(null);
}

function onFieldInput() {
  window.clearTimeout(convoQueryTimer);
  convoQueryTimer = window.setTimeout(refreshConvos, 250);
}

function setConvoMode(mode) {
  convoMode = mode;
  if (mode === 'off') {
    convoSeq += 1;
    convoSourceRows = [];
    convoRows = [];
    els.lifeOsConvoList.replaceChildren();
    setAsyncState(els.lifeOsConvoState, 'ready');
  }
  syncConvoGroup();
}

// The field's chip, the group, and the Skills group beside it. Narrowed to
// one skill, the group is that skill's list, so the other skills step aside.
function syncConvoGroup() {
  els.lifeOsConvoGroup.hidden = convoMode === 'off';
  const card = els.lifeOsList.closest('.lifeos-list-card');
  if (card) {
    card.classList.toggle('is-convo-scoped', !!convoScope);
    // With the group under it, "Skills (0)" says what the no-match block
    // would, and the block would push the conversations off the screen.
    card.classList.toggle('is-convo-on', convoMode !== 'off');
  }
  els.lifeOsConvoScope.hidden = !convoScope;
  if (convoScope) {
    els.lifeOsConvoScopeName.textContent = 'in ' + convoScope.name;
    els.lifeOsConvoScope.setAttribute('aria-label',
      'Showing ' + convoScope.name + ' only. Remove to search every skill');
  }
  syncSkillsHead();
  syncConvoSort();
}

// "Skills (n)" heads the skill rows only while the Conversations group shows
// under them, so the two groups read as one result.
function syncSkillsHead() {
  const head = els.lifeOsSkillsHead;
  if (!head) return;
  head.hidden = convoMode === 'off' || !!convoScope;
  if (head.hidden) return;
  const shown = els.lifeOsList.querySelectorAll(':scope > li.lifeos-item:not([hidden])').length;
  els.lifeOsSkillsCount.textContent = '(' + shown + ')';
}

// A collapsed Skills card would hide the group it holds.
function revealSkillsCard() {
  const card = els.lifeOsList && els.lifeOsList.closest('details');
  if (card && !card.open) card.open = true;
}

// The group's lifecycle states through the shared helper (#1501): a glyph,
// one line, and at most one Retry, never a blank group and never a toast.
const CONVO_STATE_GLYPH = {
  loading: 'hourglass',
  empty: 'messages-square',
  error: 'triangle-alert',
};

function showConvoState(kind, message, retry) {
  // No rows on screen means none to re-sort: a sort change must not bring
  // back the list this state replaced.
  convoSourceRows = [];
  convoRows = [];
  els.lifeOsConvoList.replaceChildren();
  els.lifeOsConvoCount.textContent = '';
  setAsyncState(els.lifeOsConvoState, kind,
    { message: message, glyph: CONVO_STATE_GLYPH[kind], retry: retry });
  syncConvoSort();
}

// One skill's index — the no-query view. `available: false` means the
// indexer simply hasn't digested this skill yet; that's an honest empty
// state, not an error. Resolves to the rows it rendered, or null.
async function loadConvos() {
  const scope = convoScope;
  if (!scope) return null;
  const mine = ++convoSeq;
  setConvoMode('browse');
  showConvoState('loading', 'Reading conversations…');
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(scope.id) + '/conversations'
    );
    if (mine !== convoSeq) return null;     // the field or scope moved on
    if (!body.available) {
      showConvoState('empty', 'No conversation index yet for this skill.');
      return null;
    }
    const rows = body.conversations || [];
    renderConvoRows(rows);
    return rows;
  } catch (exc) {
    if (mine !== convoSeq) return null;
    showConvoState('error', convoFailure(exc), refreshConvos);
    return null;
  }
}

// Ranked search across every skill (or the scope). The server owns the
// ranking; a missing CLI or database comes back as available:false with a
// short reason, which reads as "search unavailable" — never an error toast.
// The skills group above keeps working whatever this says.
async function runConvoSearch(query) {
  const scope = convoScope;
  const mine = ++convoSeq;
  setConvoMode('search');
  showConvoState('loading', 'Searching conversations…');
  let url = '/api/life-os/conversations/search?q=' + encodeURIComponent(query);
  if (scope) url += '&skill=' + encodeURIComponent(scope.id);
  try {
    const body = await jsonApi(url);
    if (mine !== convoSeq) return null;
    if (!body.available) {
      showConvoState('error', 'Search unavailable — ' + (body.reason || 'try again later.'));
      return null;
    }
    const rows = body.results || [];
    if (!rows.length) {
      showConvoState('empty', 'Nothing matched “' + query + '”.');
      return null;
    }
    renderConvoRows(rows);
    return rows;
  } catch (exc) {
    if (mine !== convoSeq) return null;
    showConvoState('error', convoFailure(exc), refreshConvos);
    return null;
  }
}

// The content endpoints are Tailscale + passkey gated — say that plainly
// rather than leaking a status code onto the phone.
function convoFailure(exc) {
  if (exc && exc.status === 403) {
    return 'Conversations are Tailscale-only (and passkey-gated). Open the ' +
      'launcher over your Tailscale URL on an enrolled device.';
  }
  return 'Could not load conversations.';
}

// A skill's ⋮ Conversations: the field, emptied, narrowed to that skill, its
// list newest first. Resolves to the rows once loaded (null otherwise),
// which is what a ?convo= link resolves against. `quiet` (the link) leaves
// the keyboard down.
export function openConvos(skill, opts) {
  if (!els.lifeOsConvoGroup || !skill) return Promise.resolve(null);
  convoScope = { id: skill.id, name: skill.name };
  revealSkillsCard();
  // set() goes through the field's input event, which schedules a debounced
  // refresh; refreshConvos() runs it now and drops that timer.
  skillsFilter.set('');
  const loaded = refreshConvos();
  if (!(opts && opts.quiet)) els.lifeOsFilterInput.focus();
  return loaded;
}

// A ?convo= link for a skill that is gone: say so in the group.
function showConvoNotice(message) {
  revealSkillsCard();
  convoSeq += 1;
  convoScope = null;
  setConvoMode('notice');
  showConvoState('error', message);
}

// ------------------------------------------------- sort (#886, #1074)
//
// Orderings over rows already on the client, so a change costs no round
// trip. The browse list and the search results land here with *different*
// defaults (#1074): browsing defaults to a date sort, while a search
// defaults to 'relevance' — the server's FTS5 rank order, which the client's
// only job is not to destroy.
//
// 'interaction' (Recent) is the browse default and the point of #886:
// `last_interaction` is the capture file's mtime, which life-os moves
// forward whenever a resumed session appends turns, so the conversation you
// were last in floats to the top. 'created' is the date-stamped filename
// order the index is written in.

const CONVO_SORTS = ['relevance', 'interaction', 'created'];

// The ordering in force for whatever is on screen right now.
function activeConvoSort() {
  return convoMode === 'search' ? convoSearchSort : state.lifeOsConvoSort;
}

// Which date leads each row. In relevance order neither date drives the
// list, so the remembered browse preference still decides the display.
function convoDateSort() {
  const mode = activeConvoSort();
  return mode === 'relevance' ? state.lifeOsConvoSort : mode;
}

function sortedConvoRows(rows) {
  const mode = activeConvoSort();
  // Relevance is the server's own order — pass it through untouched.
  if (mode === 'relevance') return rows.slice();
  const key = mode === 'created' ? 'date' : 'last_interaction';
  return rows.slice().sort(function (a, b) {
    // A server too old to send `last_interaction` (or a row whose capture
    // could not be stat'd) degrades to the creation date rather than sorting
    // as the empty string and sinking to the bottom.
    const av = String(a[key] || a.date || '');
    const bv = String(b[key] || b.date || '');
    if (av !== bv) return av < bv ? 1 : -1;   // ISO dates: lexical == chronological
    // Same day: the date-stamped filename carries the time too, so it is the
    // stable tie-break — without it, equal rows could shuffle on re-render.
    return String(b.file || '').localeCompare(String(a.file || ''));
  });
}

// The segmented control shows while the group lists conversations (it stays
// through a search's loading, so the group never jumps), Relevance only while
// searching, and the segment in force pressed.
function syncConvoSort() {
  const nav = els.lifeOsConvoSort;
  if (!nav) return;
  const mode = activeConvoSort();
  nav.hidden = convoMode !== 'browse' && convoMode !== 'search';
  nav.dataset.sort = mode;
  nav.querySelectorAll('.range-tab').forEach(function (btn) {
    btn.hidden = btn.dataset.sort === 'relevance' && convoMode !== 'search';
    const on = btn.dataset.sort === mode;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function setConvoSort(mode) {
  if (CONVO_SORTS.indexOf(mode) === -1) return;
  if (convoMode === 'search') {
    // Search orderings are transient: the end of the search restores the
    // browse default (#1074), so a date ordering of the hits never sticks.
    convoSearchSort = mode;
  } else {
    if (mode === 'relevance') return;   // browsing has no rank to restore
    state.lifeOsConvoSort = mode;
    localStorage.setItem('launcher.lifeOsConvoSort', state.lifeOsConvoSort);
  }
  // Re-order what is already on screen; a refetch would return the same rows.
  // Re-sorting from the *server's* order, not the rendered one, is what makes
  // relevance reachable again after a date sort.
  if (convoSourceRows.length) renderConvoRows(convoSourceRows);
  else syncConvoSort();
}

function renderConvoRows(rows) {
  setAsyncState(els.lifeOsConvoState, 'ready');
  convoSourceRows = rows.slice();
  convoRows = sortedConvoRows(rows);
  syncConvoSort();
  if (!convoRows.length) {
    showConvoState('empty', 'No conversations yet.');
    return;
  }
  els.lifeOsConvoCount.textContent = '(' + convoRows.length + ')';
  els.lifeOsConvoList.replaceChildren.apply(els.lifeOsConvoList, convoRows.map(convoRow));
}

// What a conversation is called: the owner's title (#1495) when set, else the
// digest topic, which keeps updating underneath it.
function convoName(r) {
  return r.title || r.topic || r.slug || r.file || 'untitled';
}

// The avatar of the skill a conversation is from: its glyph or monogram,
// the same as on its skill row.
function convoAvatar(r) {
  const skill = (state.lifeOsSkills || []).find(function (s) { return s.id === r.skill; });
  return skillAvatar(skill || { id: r.skill, name: r.skill }, false);
}

// The row's one trailing item: a chevron, because a tap opens a sheet.
function rowChevron(onTap) {
  const el = document.createElement('span');
  el.className = 'lifeos-convo-chevron';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = icon('chevron-right');
  el.addEventListener('click', onTap);
  return el;
}

// One conversation on the shared action-row (#1528, #1504): a tap opens its
// sheet, the title is what it is called (an owner's rename replaces the
// topic outright), and the one context line is when it was last touched (or
// created, under that sort), which skill it is from when the group spans
// several, and the start of its summary.
function convoRow(r) {
  const name = convoName(r);
  const meta = [
    convoWhen(convoDateSort() === 'created' ? r.date : (r.last_interaction || r.date)),
    convoScope ? '' : r.skill,
    convoSummary(r),
  ].filter(Boolean).join(' · ');
  const open = function () { openConvoSheet(r); };
  return actionRow({
    id: (r.skill || '') + '/' + (r.file || ''),
    className: 'lifeos-convo-row',
    title: name,
    meta: meta,
    avatar: convoAvatar(r),
    label: 'Open ' + name,
    onMain: open,
    trailing: rowChevron(open),
  }).li;
}

// A day as people say it: Today, Yesterday, "3 days ago" within the week,
// then the short date (with the year only when it is not this one). The
// index's dates are calendar days (YYYY-MM-DD), read in local time. English
// like the words around it: the browser's own locale (es on the owner's
// phone) printed "1 ago" for 1 August, which reads as "1 [day] ago".
function convoWhen(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  if (!m) return '';
  const day = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((today - day) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days > 1 && days < 7) return days + ' days ago';
  const format = { month: 'short', day: 'numeric' };
  if (day.getFullYear() !== today.getFullYear()) format.year = 'numeric';
  return new Intl.DateTimeFormat('en-GB', format).format(day);
}

// The conversation's summary as one plain paragraph (#1528). A newer index
// entry carries it whole (fleet-config#1373); an older one has decisions and
// open loops, which read as consecutive sentences with their labels dropped.
// The digest writes a literal "none" for an empty section: that is noise,
// not a sentence.
function convoSummary(r) {
  const clean = function (value) {
    const text = String(value || '').trim();
    return text.toLowerCase() === 'none' ? '' : text;
  };
  const whole = clean(r.summary);
  if (whole) return whole;
  return [clean(r.decisions), clean(r.open_loops)].filter(Boolean)
    .map(function (text) { return /[.!?…]$/.test(text) ? text : text + '.'; })
    .join(' ');
}

// ------------------------------------------- conversation sheet (#1504)
// One conversation: its name, "skill · when · N messages · from <harness>",
// the summary, then Resume as the one primary and every other action as a
// link row, Delete last in danger. Opened from a row and from a ?convo=
// link; the reader, a raw capture, the rename and the confirms all stack
// over it.

// The conversation the sheet shows, or null while it is closed.
let sheetRow = null;

// Resume reattaches the stored session on the conversation's own model
// (#1503), so the toolbar's provider does not have to match it: there is no
// "select a Claude model" lockout. The index carries no model, so the
// button cannot name one ahead of time; the toast after it does.
function canResume(r) {
  return !!(r.resumable && r.skill);
}

// The harness a "Start new in …" would hand off to: the toolbar's, when it
// is not the conversation's own and the capture can seed one. '' when no
// handoff applies.
function handoffTarget(r) {
  const target = lifeOsModel().split(':')[0];
  if (!r.handoff_available || !r.skill || target === r.agent) return '';
  if (target === 'codex') return 'Codex';
  if (target === 'claude') return 'Claude';
  return '';
}

function convoSheetMeta(r) {
  const turns = Number(r.turns) || 0;
  return [
    r.skill,
    convoWhen(r.last_interaction || r.date),
    turns ? turns + (turns === 1 ? ' message' : ' messages') : '',
    r.agent ? 'from ' + nameLabel(r.agent) : '',
  ].filter(Boolean).join(' · ');
}

function sheetLink(cls) {
  return els.lifeOsConvoSheet.querySelector('.' + cls);
}

function fillConvoSheet() {
  const r = sheetRow;
  if (!r) return;
  els.lifeOsConvoSheetTitle.textContent = convoName(r);
  els.lifeOsConvoSheetTitle.title = convoName(r);
  els.lifeOsConvoSheetMeta.textContent = convoSheetMeta(r);
  const summary = convoSummary(r);
  els.lifeOsConvoSheetSummary.textContent = summary;
  els.lifeOsConvoSheetSummary.hidden = !summary;
  // A Resume that cannot run stays, disabled, and the note says why: a
  // phone has no hover for a tooltip.
  const resumable = canResume(r);
  els.lifeOsConvoSheetResume.disabled = !resumable;
  els.lifeOsConvoSheetNote.textContent = resumable ? ''
    : (r.resume_reason || 'No stored session; readable only.');
  els.lifeOsConvoSheetNote.hidden = resumable;
  const handoff = handoffTarget(r);
  const handoffRow = sheetLink('lifeos-convo-handoff');
  handoffRow.hidden = !handoff;
  handoffRow.querySelector('.settings-row-label').textContent = 'Start new in ' + handoff;
  // A row with no readable capture has nothing to read, open or change.
  const editable = !!(r.path && r.skill);
  sheetLink('lifeos-convo-read').hidden = !r.path;
  sheetLink('lifeos-convo-open-raw').hidden = !r.path;
  sheetLink('lifeos-convo-rename').hidden = !editable;
  sheetLink('lifeos-convo-copy-link').hidden = !(r.skill && r.file);
  sheetLink('lifeos-convo-delete').parentElement.hidden = !editable;
}

function openConvoSheet(r) {
  const d = els.lifeOsConvoSheet;
  if (!d || !d.showModal) return;
  sheetRow = r;
  fillConvoSheet();
  if (!d.open) d.showModal();
}

function closeConvoSheet() {
  if (els.lifeOsConvoSheet && els.lifeOsConvoSheet.open) els.lifeOsConvoSheet.close();
}

// Everything a conversation stacked: the raw capture, the reader, the sheet.
function closeConvoLayers() {
  closeDoc();
  closeConvoViewer();
  closeConvoSheet();
}

// The launcher link that reopens one capture (#1170): main.js boot reads
// ?convo=<skill>/<file> and hands it to openConvoByLink. The skill id and the
// capture's filename, not its vault path — the server already resolves a
// file inside that skill's conversations/ folder.
function convoLinkUrl(r) {
  return window.location.origin + window.location.pathname + '?convo=' +
    encodeURIComponent(r.skill) + '/' + encodeURIComponent(r.file);
}

function copyConvoLink(r) {
  // iOS only allows a clipboard write inside the tap gesture: writeText is
  // called synchronously from the click handler, never after an await.
  try {
    navigator.clipboard.writeText(convoLinkUrl(r)).then(
      function () {
        toast('Launcher link copied (tailnet only)', 'good', { icon: 'link' });
      },
      function (exc) { apiFailToast('Copy link failed', exc); }
    );
  } catch (exc) {
    apiFailToast('Copy link failed', exc);
  }
}

// Open one capture from a ?convo= link (#1170): the field narrowed to its
// skill, then the conversation sheet and the reader over it, the same
// layering a tap builds. The sheet needs the full row, so the link resolves
// through the skill's index; anything that doesn't resolve says so in the
// Conversations group.
export async function openConvoByLink(skillId, file) {
  const skill = (state.lifeOsSkills || []).find(function (s) {
    return s.id === skillId;
  });
  if (!skill) {
    showConvoNotice(
      'This link is for the skill “' + skillId + '”, which Life OS no longer lists.');
    return;
  }
  const rows = await openConvos(skill, { quiet: true });
  if (!rows || !convoScope || convoScope.id !== skill.id) return;
  const row = rows.find(function (r) { return r.file === file; });
  if (!row) {
    showConvoState('error',
      'This link no longer matches a conversation — it may have been renamed or deleted.');
    return;
  }
  openConvoSheet(row);
  if (row.path) openConvoViewer(row, openCapture);
}

// Delete one conversation everywhere it lives (#1410): the capture, the
// Claude Code transcript it was captured from, and the indexes. One confirm
// that names the conversation and says what goes; a refusal (a conversation
// open in a running session, an unreachable session host) toasts the
// server's reason and changes nothing.
async function deleteConversation(r) {
  const title = convoName(r);
  const ok = await confirmDialog({
    title: 'Delete “' + title + '”?',
    message: 'This removes the saved log, the Claude Code transcript it came ' +
      'from, and its index entries. It cannot be undone.',
    action: 'Delete',
  });
  if (!ok) return;
  try {
    await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(r.skill) +
        '/conversations?path=' + encodeURIComponent(r.path),
      { method: 'DELETE' }
    );
  } catch (exc) {
    apiFailToast('Delete failed', exc);
    return;
  }
  closeConvoLayers();
  toast('Deleted ' + title, 'good', { icon: 'trash-2' });
  await refreshAfterLogChange();
}

// Read one capture *raw* in the document sheet, over the sheet that asked —
// closing it comes straight back there.
function openCapture(r) {
  loadFile({ path: r.path, name: r.file, category: 'conversations' });
}

// action 'resume' reattaches the stored session on its own model (#1503);
// 'handoff' starts a new conversation in the toolbar's harness, seeded from
// the capture, after a confirm.
async function resumeConversation(r, action) {
  const mode = launchDetached() ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = {
    mode: mode, model: model, action: action,
    capture: { path: r.path, revision: r.revision, agent: r.agent, sid: r.sid },
  };
  // A resume comes back on the conversation's own model (#1503); the
  // toolbar's model applies only to the new conversation a handoff starts.
  if (action === 'resume') payload.keep_model = true;
  if (action === 'handoff') {
    const scope = r.handoff_truncated ?
      'Only the first ' + (r.handoff_limit || 24000).toLocaleString() + ' characters will be included.' :
      'Only this selected capture will be included.';
    const ok = await confirmDialog({
      title: 'Start a new conversation in ' + nameLabel(model.split(':')[0]) + '?',
      message: scope + ' Source provenance is preserved. This does not resume ' +
        'or convert the original session. Memory and knowledge edits still ' +
        'require your approval.',
      action: 'Start new conversation',
      tone: 'primary',
    });
    if (!ok) return;
    payload.confirm_new = true;
  }
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(r.skill) + '/conversations/launch',
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }
    );
    toast(
      (action === 'handoff' ? 'Started new conversation from ' : 'Resumed ') +
        (r.title || r.topic || r.skill) +
        (action === 'handoff' ? modelTag(model) : resumedModelTag(body)) +
        (body.handoff_truncated ? ' (selected context truncated)' : '') +
        (mode === 'remote' ? ' (detached)' : ''),
      'good', { icon: action === 'handoff' ? 'messages-square' : 'rotate-ccw' }
    );
    // The terminal opens over the page, not in the top layer: the sheets
    // must go first.
    closeConvoLayers();
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast(action === 'handoff' ? 'New conversation failed' : 'Resume failed', exc);
  }
}

// The sheet's actions read the conversation it shows at tap time, and hold
// it through any await (a ✕ meanwhile clears sheetRow).
function wireConvoSheet() {
  const d = els.lifeOsConvoSheet;
  if (!d) return;
  wireSheet(d, [els.lifeOsConvoSheetClose], function () { sheetRow = null; });
  const on = function (cls, act) {
    sheetLink(cls).addEventListener('click', function () {
      if (sheetRow) act(sheetRow);
    });
  };
  on('lifeos-convo-read', function (r) { openConvoViewer(r, openCapture); });
  on('lifeos-convo-rename', renameConversationTitle);
  on('lifeos-convo-handoff', function (r) { resumeConversation(r, 'handoff'); });
  on('lifeos-convo-copy-link', copyConvoLink);
  on('lifeos-convo-open-raw', openCapture);
  on('lifeos-convo-delete', deleteConversation);
  els.lifeOsConvoSheetResume.addEventListener('click', function () {
    if (sheetRow && canResume(sheetRow)) resumeConversation(sheetRow, 'resume');
  });
}

// --------------------------------------------------------------- wire
export function wireLifeOs() {
  // No skills found: its fix is the Life OS folder (#1238 J-09).
  const lifeOsEmptyAction = document.getElementById('lifeOsEmptyAction');
  if (lifeOsEmptyAction) {
    lifeOsEmptyAction.addEventListener('click', function () {
      openSettingsAt('lifeOsDir');
    });
  }
  const channelRecheck = document.getElementById('channelRecheck');
  if (channelRecheck) {
    channelRecheck.addEventListener('click', function () {
      fetchChannels().then(function () {
        toast('Telegram setup re-checked', 'good');
      });
    });
  }
  // Browse and its document sheet (#1504): ✕ or Done closes each, back to
  // the one under it.
  wireSheet(els.lifeOsBrowser, [els.lifeOsBrowserClose, els.lifeOsBrowserDone],
    function () { state.lifeOsBrowser = null; });
  wireSheet(els.lifeOsDoc, [els.lifeOsDocClose, els.lifeOsDocDone], function () {
    openDocFile = null;
    els.lifeOsFileContent.innerHTML = '';
  });
  if (els.lifeOsDocDelete) {
    els.lifeOsDocDelete.addEventListener('click', function () {
      if (openDocFile) deleteFile(openDocFile);
    });
  }
  if (els.lifeOsDocRename) {
    els.lifeOsDocRename.addEventListener('click', function () {
      if (openDocFile) renameFile(openDocFile);
    });
  }
  wireRenameDialog();
  // The skills field's conversation half (#1504): the debounced search, the
  // scope chip that widens to every skill, the sort, and the sheet a row
  // opens.
  if (els.lifeOsFilterInput && els.lifeOsConvoGroup) {
    els.lifeOsFilterInput.addEventListener('input', onFieldInput);
    els.lifeOsConvoScope.addEventListener('click', function (ev) {
      // Inside the field's <label>: the tap would also focus the box, which
      // is where it should land anyway, so it does that once, here.
      ev.preventDefault();
      convoScope = null;
      refreshConvos();
      els.lifeOsFilterInput.focus();
    });
    els.lifeOsConvoSort.addEventListener('click', function (ev) {
      const tab = ev.target.closest('.range-tab');
      if (tab) setConvoSort(tab.dataset.sort);
    });
  }
  wireConvoSheet();
  wireConvoViewer();
  // Detached/Resume are the launch toolbar's own switches (issue #355: no
  // server config, read at launch time above). The provider-qualified model
  // dropdown (#540/#845) is its first row; wireModelCombo owns its
  // open/close.
  lifeOsModelCombo = wireModelCombo(document.getElementById('lifeOsModelCombo'));
  // Refresh skills + recap staleness the moment the tab opens (cheap: a live
  // directory scan + a single ledger stat).
  if (els.tabLifeOS) {
    els.tabLifeOS.addEventListener('click', function () {
      fetchSkills().catch(function () {});
      fetchRecapStatus().catch(function () {});
    });
  }
}
