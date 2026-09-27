/* The Chat pane's resume card (#1300).
 *
 * Claude Code's /resume opens a long scrolling session picker in the
 * terminal, hard to read and drive on a phone. Chat offers the project's
 * conversations as a searchable card instead, in the AskUserQuestion card's
 * style (#1149, the same `.tr-ask-*` parts). It opens two ways:
 *
 *   - by itself, while the terminal shows the /resume picker (the plan
 *     picker's screen poll reports it, `resume_picker`), and closes when the
 *     picker goes;
 *   - when `/resume` is typed in the Chat composer, which opens the card
 *     instead of sending the bare command. That card stays until a pick or ✕.
 *
 * A pick asks the server to resume that conversation (POST .../resume). The
 * server re-checks the list and the screen, leaves the picker, types
 * `/resume <id>`, and reads the result back off the terminal (the probe on
 * #1300 showed the id form resumes exactly that conversation). The list is
 * titles and times only: no transcript text reaches the phone.
 *
 * session-transcript.js owns the pane and calls in through the exports; the
 * `hooks` it passes say which session is open and refresh the transcript
 * after a resume.
 */

import { els } from './state.js';
import { apiFailToast, authHeaders, jsonApi, toast } from './api.js';
import { fmtAgo } from './sessions.js';
import { ensureTerminalToken } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';

let hooks = null;
// The open card: { sid, via: 'picker' | 'composer', sessions, busy,
// dismissed }. A picker card ✕'d while the picker is still up stays here,
// hidden, so the next poll does not reopen it; it clears once the picker
// goes.
let card = null;

const OUTCOME_TOASTS = {
  resumed: ['Resumed: ', 'good'],
  unconfirmed: ['Sent: check the terminal for ', ''],
  not_found: ['Claude Code could not find that session: ', 'error'],
  cancelled: ['The resume was cancelled on the terminal: ', 'error'],
};

function sessionPath(sid, tail) {
  return '/api/claude-code/sessions/' + encodeURIComponent(sid) + '/' + tail;
}

function openSid() {
  const s = hooks && hooks.session();
  return s ? s.session_id : null;
}

export function wireResumeCard(h) {
  hooks = h;
}

export function closeResumeCard() {
  card = null;
  const box = els.transcriptResumeLive;
  if (box) {
    box.innerHTML = '';
    box.hidden = true;
  }
}

// The screen poll's word on the terminal's /resume picker.
export function syncResumePicker(showing) {
  const sid = openSid();
  if (showing) {
    if (!card || card.sid !== sid) openResumeCard('picker');
  } else if (card && card.via === 'picker' && !card.busy) {
    closeResumeCard();
  }
}

export async function openResumeCard(via) {
  const sid = openSid();
  const box = els.transcriptResumeLive;
  if (!sid || !box) return;
  const mine = { sid: sid, via: via, sessions: null, busy: false, dismissed: false };
  card = mine;
  render();
  let body = null;
  try {
    body = await jsonApi(sessionPath(sid, 'resume-sessions'));
  } catch (exc) {
    if (card !== mine) return;
    apiFailToast('Sessions not loaded', exc);
    closeResumeCard();
    return;
  }
  if (card !== mine) return;
  if (!body || !body.available) {
    toast('Chat can’t list this project’s sessions here: use the terminal', 'error', { icon: 'rotate-ccw' });
    closeResumeCard();
    return;
  }
  mine.sessions = body.sessions || [];
  render();
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function ago(iso) {
  const t = Date.parse(iso || '');
  if (!Number.isFinite(t)) return '';
  const text = fmtAgo(Math.floor(t / 1000));
  return text ? text + ' ago' : '';
}

// Every search word must appear in the title, in any order and case.
function matches(title, query) {
  const hay = String(title || '').toLowerCase();
  return query.toLowerCase().split(/\s+/).filter(Boolean).every(function (w) {
    return hay.indexOf(w) !== -1;
  });
}

function render() {
  const box = els.transcriptResumeLive;
  if (!box || !card) return;
  box.hidden = card.dismissed;
  box.innerHTML = '';
  if (card.dismissed) return;
  const root = el('section', 'tr-ask tr-resume-card');
  root.setAttribute('aria-label', 'Resume a session');

  const head = el('div', 'tr-ask-head tr-resume-head');
  head.innerHTML = icon('rotate-ccw');
  head.appendChild(el('span', 'tr-ask-header', 'Resume a session'));
  const close = el('button', 'icon-btn tr-resume-close');
  close.type = 'button';
  close.title = 'Close';
  close.setAttribute('aria-label', 'Close');
  close.innerHTML = icon('x');
  close.addEventListener('click', function () {
    if (card.via === 'picker') {
      card.dismissed = true;
      render();
    } else {
      closeResumeCard();
    }
  });
  head.appendChild(close);
  root.appendChild(head);

  if (card.sessions === null) {
    root.appendChild(el('p', 'tr-ask-status', 'Loading this project’s sessions…'));
    box.appendChild(root);
    return;
  }
  if (!card.sessions.length) {
    root.appendChild(el('p', 'tr-ask-status', 'No sessions to resume in this project yet.'));
    box.appendChild(root);
    return;
  }

  const search = el('input', 'tr-ask-input tr-resume-search');
  search.type = 'search';
  search.placeholder = 'Search sessions';
  search.setAttribute('aria-label', 'Search sessions');
  search.autocomplete = 'off';
  root.appendChild(search);

  const list = el('div', 'tr-ask-options tr-resume-list');
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', 'Sessions, newest first');
  const buttons = card.sessions.map(function (s) {
    const b = el('button', 'tr-ask-opt tr-resume-opt');
    b.type = 'button';
    b.dataset.id = s.id;
    b.disabled = card.busy;
    const body = el('span', 'tr-ask-opt-body');
    body.appendChild(el('span', 'tr-ask-label', s.title));
    const when = ago(s.updated_at);
    if (when) body.appendChild(el('span', 'tr-ask-desc', when));
    b.appendChild(body);
    b.addEventListener('click', function () { pick(s); });
    list.appendChild(b);
    return { b: b, s: s };
  });
  root.appendChild(list);
  const none = el('p', 'tr-ask-status tr-resume-none', 'No session matches the search.');
  none.hidden = true;
  root.appendChild(none);
  const status = el('p', 'tr-ask-status tr-resume-status');
  status.setAttribute('role', 'status');
  status.textContent = card.busy
    ? 'Resuming…'
    : 'Tap a session to resume it in this terminal.';
  root.appendChild(status);

  search.addEventListener('input', function () {
    let shown = 0;
    buttons.forEach(function (x) {
      x.b.hidden = !matches(x.s.title, search.value);
      if (!x.b.hidden) shown++;
    });
    none.hidden = shown > 0;
  });
  box.appendChild(root);
}

async function pick(s) {
  if (!card || card.busy) return;
  const mine = card;
  mine.busy = true;
  render();
  let res;
  try {
    const tt = await ensureTerminalToken();
    res = await jsonApi(sessionPath(mine.sid, 'resume'), {
      method: 'POST',
      headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
      body: JSON.stringify({ session_id: s.id, via: mine.via }),
    });
  } catch (exc) {
    if (card !== mine) return;
    mine.busy = false;
    if (exc && exc.status === 409) {
      toast(exc.message || 'The terminal changed: nothing was sent', 'error', { icon: 'rotate-ccw' });
      // A fresh list, since the one shown is what the server just refused.
      openResumeCard(mine.via);
      return;
    }
    apiFailToast('Resume failed', exc);
    closeResumeCard();
    return;
  }
  const said = OUTCOME_TOASTS[res && res.outcome] || OUTCOME_TOASTS.unconfirmed;
  toast(said[0] + ((res && res.title) || s.title), said[1], { icon: 'rotate-ccw' });
  if (card === mine) closeResumeCard();
  if (hooks && hooks.onResumed) hooks.onResumed();
}
