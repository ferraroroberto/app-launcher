/* Life OS archived conversations: what the nightly zombie cleanup moved
 * into its backup, found again from the Conversations group.
 *
 * Whenever the group shows a search or a skill's list, the archived entries
 * that search (or skill) covers load beside it into a collapsed "Archived (n)"
 * disclosure under the list, absent when there are none. They never mix into
 * the live list or its count. A row opens the conversation reader on the
 * backup's copy, with Restore as its one primary; a refusal (a same-name
 * capture or session id has appeared since) shows the server's reason and
 * changes nothing.
 */

import { els } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { actionRow } from './action-rows.js';
import { closeConvoViewer, openConvoViewer } from './life-os-viewer.js';

// Bumped by every read and by hiding, so an answer that lands after the
// field or scope moved on is dropped.
let seq = 0;
// The row look the live list uses, and what to refresh after a restore:
// handed in by life-os-convos.js, which owns both.
let look = null;

function entryUrl(r) {
  return '/api/life-os/archived/' + [r.day, r.skill, r.capture].map(encodeURIComponent).join('/');
}

// The owner's title when the conversation had one, else the file name's
// slug ("2026-09-13-0800-ferry-plans.md" reads "ferry plans").
function archivedName(r) {
  if (r.title) return r.title;
  const slug = String(r.capture || '').replace(/\.md$/, '').replace(/^\d{4}-\d{2}-\d{2}-\d{4}-?/, '');
  return slug.replace(/-/g, ' ') || r.capture || 'untitled';
}

function archivedWhen(r) {
  const when = look.when(r.day);
  if (!when) return '';
  return 'Archived ' + (/^(Today|Yesterday)$/.test(when) ? when.toLowerCase() : when);
}

function archivedRow(r, scoped) {
  const name = archivedName(r);
  const open = function () { openArchived(r); };
  return actionRow({
    id: 'archived/' + r.day + '/' + r.skill + '/' + r.capture,
    className: 'lifeos-convo-row lifeos-archived-row',
    title: name,
    meta: [archivedWhen(r), scoped ? '' : r.skill].filter(Boolean).join(' · '),
    avatar: look.avatar(r),
    label: 'Read archived ' + name,
    onMain: open,
    trailing: look.chevron(open),
  }).li;
}

function openArchived(r) {
  openConvoViewer({ title: archivedName(r), agent: r.agent }, null, {
    url: entryUrl(r) + '/transcript',
    primary: { text: 'Restore', onTap: function (btn) { restore(r, btn); } },
  });
}

async function restore(r, btn) {
  btn.disabled = true;
  btn.textContent = 'Restoring…';
  try {
    await jsonApi(entryUrl(r) + '/restore', { method: 'POST' });
  } catch (exc) {
    btn.disabled = false;
    btn.textContent = 'Restore';
    apiFailToast('Not restored', exc);
    return;
  }
  closeConvoViewer();
  toast('Restored ' + archivedName(r), 'good', { icon: 'rotate-ccw' });
  await look.onRestored();
}

export function hideArchived() {
  seq += 1;
  if (!els.lifeOsArchived) return;
  els.lifeOsArchived.hidden = true;
  els.lifeOsArchived.open = false;
  els.lifeOsArchivedList.replaceChildren();
}

// The archived entries a search (`query`) or a skill's list (`skillId`)
// covers. Quiet on failure: the group is a side door, and the live list
// beside it says what is wrong with the content endpoints.
export async function loadArchived(query, skillId) {
  if (!els.lifeOsArchived || (!query && !skillId)) { hideArchived(); return; }
  const mine = ++seq;
  let url = '/api/life-os/archived?q=' + encodeURIComponent(query || '');
  if (skillId) url += '&skill=' + encodeURIComponent(skillId);
  let rows;
  try {
    rows = (await jsonApi(url)).archived || [];
  } catch (exc) {
    if (mine === seq) hideArchived();
    return;
  }
  if (mine !== seq) return;
  if (!rows.length) { hideArchived(); return; }
  els.lifeOsArchivedCount.textContent = '(' + rows.length + ')';
  els.lifeOsArchivedList.replaceChildren.apply(els.lifeOsArchivedList,
    rows.map(function (r) { return archivedRow(r, !!skillId); }));
  els.lifeOsArchived.hidden = false;
}

// `deps`: { avatar(row), when(isoDay), chevron(onTap), onRestored() }.
export function wireArchived(deps) {
  look = deps;
}
