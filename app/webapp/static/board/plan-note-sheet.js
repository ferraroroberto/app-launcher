/* A plan row's note sheet, opened from a row that carries a chief note
 * (chief-plan.js::planRow). A phone has no hover, so the note, free text with
 * figures in it (PR numbers, short hashes), is read here in full, wrapped.
 * The sheet shows the row's title, `repo#N`, model and status, the note, and
 * Open on GitHub when the row links to an issue. The note is text, never HTML.
 */

import { el } from '../dom-utils.js';
import { chip } from '../glance.js';

let wired = false;
let url = '';

function byId(id) { return document.getElementById(id); }

function wire() {
  const dialog = byId('planNoteDialog');
  byId('planNoteClose').addEventListener('click', function () { dialog.close(); });
  // A tap on the backdrop lands on the <dialog> itself, outside the card.
  dialog.addEventListener('click', function (ev) { if (ev.target === dialog) dialog.close(); });
  byId('planNoteOpen').addEventListener('click', function () {
    if (url) window.open(url, '_blank', 'noopener');
    dialog.close();
  });
  wired = true;
}

// `o`: { title, ref, model, status, tone, note, url }.
export function openPlanNoteSheet(o) {
  const dialog = byId('planNoteDialog');
  if (!dialog) return;
  if (!wired) wire();
  url = o.url || '';
  const nodes = [el('h3', 'plan-note-title', o.title)];
  const meta = el('div', 'plan-note-meta');
  if (o.ref && o.ref !== o.title) meta.appendChild(el('span', 'plan-note-ref', o.ref));
  if (o.model) meta.appendChild(el('span', 'plan-note-model', o.model.charAt(0).toUpperCase() + o.model.slice(1)));
  if (o.status) meta.appendChild(chip(o.status, o.tone || 'neutral', 'board-plan-chip'));
  nodes.push(meta, el('p', 'plan-note-text', o.note));
  const body = byId('planNoteBody');
  body.replaceChildren.apply(body, nodes);
  byId('planNoteActions').hidden = !url;
  if (!dialog.open) dialog.showModal();
}
