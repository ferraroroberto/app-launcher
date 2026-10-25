/* Telegram channels — the Settings card.
 *
 * Read-only status for the Telegram profiles: what is ready, what is
 * missing (a broken profile and the problems line live only here since the
 * Life tab's card went), which skills a profile can bind. It renders
 * from the same GET /api/life-os/channels payload that marks the Life tab's
 * linked skills (state.lifeOsChannels, filled by life-os.js::fetchChannels),
 * so it owns no fetch of its own. The
 * payload carries booleans and names only — never a path, a token or a file's
 * content — and this module adds nothing to that.
 *
 * A check the server could not establish (`null`) renders as "Unknown", never
 * as found or missing.
 */

import { els, state } from '../state.js';

// A healthy check reads as a plain value in the row's text colour; only an
// exception wears a chip, on the tone map: what needs you is
// attention, an unknown is neutral. state → [chip class suffix, label].
const CHIP = {
  missing: ['attention', 'Missing'],
  unknown: ['neutral', 'Unknown'],
};

// `value` is the healthy word ("Found"): status 'ok' shows it plain, any
// other status its chip (`chipLabel` overriding the chip's word).
function checkRow(title, detail, status, value, chipLabel) {
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
  const end = document.createElement('span');
  if (status === 'ok') {
    end.className = 'channel-check-value';
    end.textContent = value;
  } else {
    const pick = CHIP[status] || CHIP.unknown;
    end.className = 'channel-check-chip channel-check-chip-' + pick[0];
    end.textContent = chipLabel || pick[1];
  }
  li.appendChild(end);
  return li;
}

// true → ok, false → missing, null/undefined → unknown.
function tri(value) {
  if (value === true) return 'ok';
  if (value === false) return 'missing';
  return 'unknown';
}

// A profile is usable once its skill exists and its bot token file is in
// place; the profile rows, the Settings row and the header count the rest.
export function profileNeedsSetup(p) {
  return !(p.skill_found && p.env_present);
}

// A passing check names nothing but itself: the profiles file's path is an
// instruction, so it shows only in the fix-it line of a failing check.
function renderChecks(setup) {
  const host = els.channelChecks;
  if (!host) return;
  host.innerHTML = '';
  if (!setup) {
    host.appendChild(checkRow('Setup status', 'Could not be read — reopen Settings to retry.', 'unknown'));
    return;
  }
  host.appendChild(checkRow(
    'Profiles file',
    setup.file_present ? '' : 'Copy config/channel_profiles.sample.json to config/channel_profiles.json',
    tri(setup.file_present), 'Found'
  ));
  host.appendChild(checkRow(
    'Life OS folder',
    setup.life_os_found ? '' : 'Set the Life OS folder above',
    tri(setup.life_os_found), 'Found'
  ));
  host.appendChild(checkRow(
    'Telegram plugin',
    setup.plugin === true ? ''
      : setup.plugin === false ? 'In the life-os project run /plugin install telegram@claude-plugins-official'
      : 'Claude’s plugin record could not be read',
    tri(setup.plugin), 'Installed'
  ));
  host.appendChild(checkRow(
    'Bun',
    setup.bun ? '' : 'Install Bun (bun.sh) and put it on your PATH',
    tri(setup.bun), 'Found'
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
    const ready = !profileNeedsSetup(p);
    const detail = ready
      ? 'skill ' + p.skill + ' · token file present' + (p.running ? ' · running' : '') +
        (p.restart_due === true ? ' · skill changed since start' : '')
      : missing.join(' · ');
    host.appendChild(checkRow(p.label, detail, ready ? 'ok' : 'missing', 'Ready', 'Needs setup'));
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
