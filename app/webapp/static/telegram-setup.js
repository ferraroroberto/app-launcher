/* Telegram channels — the Settings card (#1369).
 *
 * Read-only status for the Life OS tab's Telegram profiles (#1366): what is
 * ready, what is missing, which skills a profile can bind. It renders from the
 * same GET /api/life-os/channels payload the Life OS card uses (state.lifeOsChannels,
 * filled by life-os.js::fetchChannels), so it owns no fetch of its own. The
 * payload carries booleans and names only — never a path, a token or a file's
 * content — and this module adds nothing to that.
 *
 * A check the server could not establish (`null`) renders as "Unknown", never
 * as ready or missing.
 */

import { els, state } from './state.js';

// One status pill per check: state → [chip class suffix, label].
const CHIP = {
  ok: ['ok', 'Ready'],
  missing: ['missing', 'Missing'],
  unknown: ['unknown', 'Unknown'],
};

function checkRow(title, detail, status, chipLabel) {
  const li = document.createElement('li');
  li.className = 'row channel-check';
  const meta = document.createElement('span');
  meta.className = 'channel-check-meta';
  const strong = document.createElement('strong');
  strong.textContent = title;
  meta.appendChild(strong);
  if (detail) {
    const note = document.createElement('span');
    note.className = 'muted small';
    note.textContent = detail;
    meta.appendChild(note);
  }
  li.appendChild(meta);
  const chip = document.createElement('span');
  const pick = CHIP[status] || CHIP.unknown;
  chip.className = 'channel-check-chip channel-check-chip-' + pick[0];
  chip.textContent = chipLabel || pick[1];
  li.appendChild(chip);
  return li;
}

// true → ready, false → missing, null/undefined → unknown.
function tri(value) {
  if (value === true) return 'ok';
  if (value === false) return 'missing';
  return 'unknown';
}

function renderChecks(setup) {
  const host = els.channelChecks;
  if (!host) return;
  host.innerHTML = '';
  if (!setup) {
    host.appendChild(checkRow('Setup status', 'Could not be read — reopen Settings to retry.', 'unknown'));
    return;
  }
  host.appendChild(checkRow(
    'Profile file',
    setup.file_present ? 'config/channel_profiles.json' : 'Copy config/channel_profiles.sample.json to config/channel_profiles.json',
    tri(setup.file_present)
  ));
  host.appendChild(checkRow(
    'Life OS folder',
    setup.life_os_found ? '' : 'Set the Life OS folder above',
    tri(setup.life_os_found)
  ));
  host.appendChild(checkRow(
    'Telegram plugin',
    setup.plugin === true ? 'Installed for the life-os project'
      : setup.plugin === false ? 'In the life-os project run /plugin install telegram@claude-plugins-official'
      : 'Claude’s plugin record could not be read',
    tri(setup.plugin)
  ));
  host.appendChild(checkRow(
    'Bun',
    setup.bun ? '' : 'Install Bun (bun.sh) and put it on your PATH',
    tri(setup.bun)
  ));
}

function renderProfiles(profiles, problems) {
  const host = els.channelProfileChecks;
  if (!host) return;
  host.innerHTML = '';
  profiles.forEach(function (p) {
    const missing = [];
    if (!p.skill_found) missing.push('skill “' + p.skill + '” not found');
    if (!p.env_present) missing.push('no .env with the bot token in its folder');
    const ready = missing.length === 0;
    const detail = ready
      ? 'skill ' + p.skill + ' · token file present' + (p.running ? ' · running' : '')
      : missing.join(' · ');
    host.appendChild(checkRow(p.label, detail, ready ? 'ok' : 'missing', ready ? 'Ready' : 'Needs setup'));
  });
  if (els.channelProfilesEmpty) els.channelProfilesEmpty.hidden = profiles.length !== 0;
  const note = els.channelProfileProblems;
  if (note) {
    note.hidden = problems.length === 0;
    note.textContent = problems.join(' · ');
  }
}

function renderSkills(setup) {
  const host = els.channelSkills;
  if (!host) return;
  host.innerHTML = '';
  const skills = (setup && setup.skills) || [];
  if (skills.length === 0) {
    host.textContent = 'No life-os skills found.';
    return;
  }
  skills.forEach(function (s) {
    const chip = document.createElement('code');
    chip.className = 'channel-skill-chip';
    chip.textContent = s.id;
    if (s.name && s.name !== s.id) chip.title = s.name;
    host.appendChild(chip);
  });
}

export function renderChannelSetup() {
  const { profiles, problems, setup } = state.lifeOsChannels;
  renderChecks(setup);
  renderProfiles(profiles || [], problems || []);
  renderSkills(setup);
}
