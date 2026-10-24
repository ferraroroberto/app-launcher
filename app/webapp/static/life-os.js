/* Life OS tab: skill rows + one-tap launch, the Telegram
 * channel binding and the weekly recap glance row. This is the tab's entry
 * point (`wireLifeOs`) and the skills list itself; the read-only
 * private-content browser and the conversation search + sheet are their own
 * modules (life-os-content.js, life-os-convos.js), wired
 * from here and sharing this module's launch helpers and `wireSheet`.
 *
 * ~80% a clone of the Coding tab. A row launches a Claude session in the
 * life-os repo that auto-invokes the bare /<skill> slash-command; the model,
 * Detached and Resume controls are the shared launch toolbar Code › Projects
 * mounts (launch-toolbar.js), at the top of the Skills card. Each row
 * leads with an avatar and says when the skill was last used; pinned skills
 * come first, then the most recently used, and skills not used lately fold
 * under one row. A swipe right (or the kebab) resumes the newest
 * conversation on its own model, a swipe left starts a new one; on the phone
 * the rows run edge to edge to give the swipe room. A skill a Telegram profile is bound to says so
 * in its meta, wears the green badge while that bot runs, and starts or opens
 * the bot from its kebab; there is no Telegram card. The skills
 * field searches the conversations too (life-os-convos.js): matches
 * show as a second group under the skills, and a conversation opens in its
 * own sheet. The Read item (life-os-content.js) opens a sheet that reads
 * each skill's files — public SKILL.md/description.md plus the private
 * context/memory/examples/conversations + shared identity.
 */

import { els, state } from './state.js';
import { renderHomeHead, renderSettingsHead } from './home-head.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { calendarDaysFromToday, wireModelCombo } from './dom-utils.js';
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
import { openBrowser, refreshFileListIfOpen, wireContentBrowser } from './life-os-content.js';
import {
  openConvoByLink, openConvos, refreshConvosIfShowing, syncSkillsHead, wireConvoSearch,
} from './life-os-convos.js';

// main.js's boot (?convo= links) imports this from here; life-os-convos.js
// owns the implementation.
export { openConvoByLink };

// The skill rows' kebab menu, on the vendored row-menu.
const skillMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });

// The Skills card's launch toolbar: the shared component Code ›
// Projects mounts, so both launch surfaces look and behave alike,
// except that here Detached and Resume live in the model combo's menu.
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
  // Decision 6: the switches sit in the combo's menu, under the
  // model list, and its label names any that is on.
  switchesInMenu: true,
  menuHint: 'The model is for new conversations; a resumed one keeps its own. ' +
    'Resume: a tap opens the session picker.',
});

// Exported: life-os-convos.js's resumeConversation reads it too.
export function launchDetached() { return !!(lifeLaunch && lifeLaunch.isDetached()); }
function launchResume() { return !!(lifeLaunch && lifeLaunch.isResume()); }

// The Skills list's name filter (the Apps filter's twin), re-applied
// after every render. Exported: life-os-convos.js's openConvos clears it
// when narrowing the field to one skill's conversations.
export const skillsFilter = listFilter({
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
// tab's wiring. Read at launch time; no server round-trip — it's
// per-launch, like the Board dispatch combo. It is also the model a
// conversation's "Start new in …" hands off to.
let lifeOsModelCombo = null;
export function setLifeOsModelOptions(items) {
  if (lifeOsModelCombo) lifeOsModelCombo.setOptions(items);
}

// Exported: life-os-convos.js reads it for handoffTarget/resumeConversation
// 
export function lifeOsModel() {
  return (lifeOsModelCombo && lifeOsModelCombo.getValue()) || 'claude:sonnet';
}

// ----------------------------------------------------------- skills list
// When the skills last read well: a failure before it is the
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
  // save, boot) so they never need a refresh point of their own.
  await fetchChannels();
}

// -------------------------------------------- Telegram channels
// One machine-local profile (config/channel_profiles.json) per bot: a life-os
// skill launched with its own Telegram bot attached. The Life tab has no card
// for them: a skill a profile is bound to wears a send glyph, green
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

// The kebab's Telegram items, after the launch items: open a running bot and
// restart it, start a stopped one. With two bots on one skill each
// item names its profile.
function channelMenuItems(profiles) {
  const named = profiles.length > 1;
  const items = [];
  profiles.forEach(function (p) {
    const running = p.running === true;
    const suffix = named ? ' · ' + p.label : '';
    items.push({
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
    });
    if (!running) return;
    // The skill or project settings changed since the bot started: a
    // restart would pick them up (restart_due, true only when known).
    const due = p.restart_due === true ? ' (skill changed)' : '';
    items.push({
      dataset: { item: 'lifeos-channel-restart-btn', profile: p.id },
      glyph: 'refresh-cw',
      label: 'Restart Telegram session' + due + suffix,
      ariaLabel: 'Restart the ' + p.label + ' Telegram session',
      onTap: function () { restartChannel(p); },
    });
  });
  return items;
}

function openChannelSession(p) {
  handleLaunchResponse({ session_id: p.session_id, kind: 'pty', name: p.label });
}

// Start a stopped bot. It sends no model, so the server's channel_model
// applies: a bot runs for days, and a toolbar left on another model
// from an interactive run would quietly become the bot's. The toolbar's
// Resume still reopens the session picker. A Telegram session is a terminal
// on the phone, so there is no detached form (the server refuses it): say so
// instead of silently ignoring the switch.
async function launchChannel(p) {
  if (launchDetached()) {
    toast('Telegram sessions run in a terminal — turn Detached off.', 'error');
    return;
  }
  const resume = launchResume();
  const payload = { mode: 'pty', resume: resume };
  applyLaunchSizePayload(payload);
  try {
    const body = await terminalJsonApi(
      '/api/life-os/channels/' + encodeURIComponent(p.id) + '/launch',
      { method: 'POST', body: payload }
    );
    toast(
      (resume ? 'Resumed ' : 'Launched ') + p.label + ' on Telegram',
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

// Restart a running bot: the server quits it, confirms it is gone and
// starts it fresh, so it picks up the current skill, settings and model. A
// stray tap would cost the bot's turn in progress, hence the confirm. On an
// error the server has started nothing.
async function restartChannel(p) {
  const ok = await confirmDialog({
    title: 'Restart the ' + p.label + ' Telegram session?',
    message: 'It quits the running session and starts a fresh one with the ' +
      'current skill and settings. The chat context is not kept.',
    action: 'Restart',
  });
  if (!ok) return;
  const payload = {};
  applyLaunchSizePayload(payload);
  try {
    const body = await terminalJsonApi(
      '/api/life-os/channels/' + encodeURIComponent(p.id) + '/restart',
      { method: 'POST', body: payload }
    );
    toast(
      (body.restarted ? 'Restarted ' : 'Started ') + p.label + ' on Telegram',
      'good',
      { icon: 'refresh-cw' }
    );
    fetchChannels().catch(function () {});
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Telegram restart failed', exc);
    fetchChannels().catch(function () {});
  }
}

// A skill counts as in use for this many days after its last captured
// conversation: pinned skills and these show, the rest fold under
// one "N more skills" row.
const RECENT_DAYS = 30;

// Whole days from a YYYY-MM-DD (read as a local calendar day) to today, or
// null for no date.
function daysSince(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  if (!m) return null;
  const day = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Math.max(0, calendarDaysFromToday(day));
}

// The row's meta: when the skill was last used, in words. That is
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

// Pinned first, then the most recently used, then A to Z.
// Exported: life-os-convos.js's openStartPicker sorts the same way.
export function compareSkills(a, b) {
  if (!!a.is_favorite !== !!b.is_favorite) return a.is_favorite ? -1 : 1;
  const au = a.last_used || '';
  const bu = b.last_used || '';
  if (au !== bu) return au < bu ? 1 : -1;
  return String(a.name).localeCompare(String(b.name));
}

// The avatar's glyph: the skill's own frontmatter `icon:` when the page's
// sprite has that glyph, else the name's first letter. Exported:
// life-os-convos.js's convoAvatar/openStartPicker reuse it.
export function skillAvatar(s, alive) {
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

// The kebab's launch items, first so the swipe actions have a
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
      onTap: function () { launchSkill(s); },
    },
    {
      // Claude's own session picker, the one route to it now that
      // the row's tap opens Conversations: any older session, not only
      // this skill's captured ones.
      dataset: { item: 'lifeos-pick-session-btn' }, glyph: 'clipboard-list',
      label: 'Pick a session…',
      ariaLabel: 'Open Claude’s session picker for ' + s.name,
      onTap: function () { launchSkill(s, { picker: true }); },
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
  // lifeOsState's loading or error, not an empty folder.
  els.lifeOsEmpty.hidden = skills.length !== 0 || !skillsReadAt;

  // The fold: pinned skills and those used in the last RECENT_DAYS
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
    // Tapping the row opens the skill's Conversations list: most taps
    // want an older conversation, and a tap that started a session left an
    // empty one to close. Start new sits in that list; the swipe's New and the
    // kebab's first items still start one in a gesture. The avatar leads (a
    // running Telegram bot is its green badge); the rest is in the kebab.
    const profiles = skillProfiles(s);
    const alive = profiles.some(function (p) { return p.running === true; });
    const due = profiles.some(function (p) { return p.restart_due === true; });
    const row = actionRow({
      id: s.id,
      className: 'lifeos-item' + (isFolded ? ' is-folded' : ''),
      title: s.name,
      meta: lastUsedMeta(s) + (profiles.length ? ' · Telegram' : '') +
        (due ? ' · restart due' : ''),
      avatar: skillAvatar(s, alive),
      label: 'Conversations with ' + s.name,
      onMain: function () { openConvos(s); },
      kebabClass: 'lifeos-menu-anchor',
      kebabLabel: 'Skill actions',
      // Swipe right resumes the newest conversation (the frequent one, so
      // the leading edge, iOS Mail's rule); swipe left starts a new one.
      // Both are also the kebab's first two items, and both ignore the
      // Resume switch.
      swipe: {
        leading: {
          label: 'Resume', glyph: 'rotate-ccw', tone: 'accent',
          disabled: !s.has_conversations, reason: 'No conversation yet',
          onCommit: function () { resumeLatest(s); },
        },
        trailing: {
          label: 'New', glyph: 'circle-plus', tone: 'neutral',
          onCommit: function () { launchSkill(s); },
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
          // Conversations group behind a scope chip.
          dataset: { item: 'lifeos-convo-btn' }, glyph: 'messages-square',
          label: 'Conversations', ariaLabel: 'Conversations with ' + s.name,
          onTap: function () { openConvos(s); },
        },
      ]
    ), {
      // A phone has no hover to explain a disabled Resume, so its reason is
      // the menu's first line (the Conversations rows' rule).
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

// Pin / unpin a skill (over the favorites list, so a
// starred skill is pinned with no migration). Persists server-side, then
// re-fetches the skills list so the order comes from the authoritative
// payload, exactly like the Coding tab's toggleFavorite.
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

// ------------------------------------------------- weekly recap
// One glance row under the skills: how long since the recap ledger
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
  verb.className = 'icon-button action-row-verb lifeos-recap-launch';
  verb.innerHTML = icon('play');
  verb.title = 'Review the weekly recap (/weekly-recap)';
  verb.setAttribute('aria-label', 'Run weekly recap review');
  verb.addEventListener('click', launchRecap);
  row.li.appendChild(verb);
  list.replaceChildren(row.li);
}

// The recap keeps its row under Skills, and rises above it as the tab's
// exception while it is due or overdue or a draft is ready.
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
// tag's terseness. Exported: life-os-convos.js's resumeConversation
// uses the same suffix for a handoff launch.
export function modelTag(model) {
  if (!model || model === 'sonnet' || model === 'claude:sonnet') return '';
  const value = model.split(':').pop();
  const label = value.replace(/^gpt-(?:5\.6|6)-/, '');
  return ' (' + label.charAt(0).toUpperCase() + label.slice(1) + ')';
}

async function launchRecap() {
  // Reuse the Skills toolbar: Detached → remote, the model combo → the
  // launch model (replacing the old opus on/off toggle).
  const mode = launchDetached() ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = { mode: mode, model: model };
  // A desktop browser launch gets a dedicated PC Edge --app window; the phone carries its real
  // terminal size so the PTY spawns at
  // the width the overlay will fit() to. Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    // Passkey-gated like /api/board/issues/start: a spawn is
    // terminal-grade, so the terminal token must ride along.
    const body = await terminalJsonApi('/api/life-os/recap/launch', { method: 'POST', body: payload });
    toast(
      'Launched weekly recap' + modelTag(model) +
        (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: 'sprout' }
    );
    // A desktop browser gets its terminal in a dedicated PC Edge window,
    // not in-page — so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Recap launch failed', exc);
  }
}

// A new conversation with the skill: the kebab's "Start new
// conversation", the swipe's New and the Conversations list's Start new. It
// ignores the Resume switch. opts.picker is the kebab's "Pick a
// session…": it posts resume: true so Claude opens its own session picker
// (dropping the /<skill> prompt), whatever the switch says. Detached applies
// to both, as in the Coding tab: the picker renders in the detached console,
// or streams to the phone over a PTY. Nothing else launches a skill
// (a Telegram bot still honours the Resume switch). Exported:
// life-os-convos.js's startNewFromSearch/openStartPicker launch the same
// way.
export async function launchSkill(s, opts) {
  const picker = !!(opts && opts.picker);
  const mode = launchDetached() ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = { mode: mode, model: model, resume: picker };
  // Same size contract as launchRecap. Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    // Passkey-gated, same as launchRecap.
    const body = await terminalJsonApi(
      '/api/life-os/skills/' + encodeURIComponent(s.id) + '/launch',
      { method: 'POST', body: payload }
    );
    toast(
      (picker ? 'Opening Claude’s session picker for ' + s.name
        : 'Launched ' + s.name + modelTag(model)) +
        (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: picker ? 'clipboard-list' : 'sprout' }
    );
    // Full-control sessions drop straight into the terminal; detached
    // ones only appear in the Coding tab's running-sessions list. A
    // desktop browser gets its terminal in a dedicated PC Edge window
    // instead of in-page, so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Launch failed', exc);
  }
}

// The model a resumed conversation came back on, for its toast: the
// server reads it from the conversation's own transcript, so it can be
// unknown (""), and for Codex it can be the default standing in for a model
// that could not be read (model_kept false). Exported: life-os-convos.js's
// resumeConversation uses the same wording for a resume's toast.
export function resumedModelTag(body) {
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
// the kebab's "Resume last conversation" and the swipe's Resume.
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

// A sheet (the browser, document, start picker, conversation sheets) that
// simply closes on its own buttons, a backdrop tap, or Escape — shared by
// life-os-content.js and life-os-convos.js, which import it from here.
export function wireSheet(d, buttons, onClose) {
  if (!d) return;
  buttons.forEach(function (b) {
    if (b) b.addEventListener('click', function () { d.close(); });
  });
  d.addEventListener('click', function (ev) {
    if (ev.target === d) d.close();
  });
  if (onClose) d.addEventListener('close', onClose);
}

// A conversation log deleted or renamed, or a conversation's title renamed,
// refreshes every list on screen that could show it: the Browse file list
// (life-os-content.js) and the Conversations group (life-os-convos.js), each
// only while its own sheet or group is actually showing.
export async function refreshAfterLogChange() {
  await Promise.all([refreshFileListIfOpen(), refreshConvosIfShowing()]);
}

// --------------------------------------------------------------- wire
export function wireLifeOs() {
  // No skills found: its fix is the Life OS folder.
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
  // Browse, its document sheet and the shared rename dialog.
  wireContentBrowser();
  // The skills field's conversation half: the debounced
  // search, the scope chip that widens to every skill, the sort, the start
  // picker and the sheet a row opens.
  wireConvoSearch();
  // Detached/Resume are the launch toolbar's own switches (no
  // server config, read at launch time above). The provider-qualified model
  // dropdown is its first row; wireModelCombo owns its
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
