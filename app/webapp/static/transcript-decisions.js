/* Chat pane decision cards + the plan-picker panel (#1309, split out of
 * session-transcript.js).
 *
 * Claude Code's two "the agent is asking the user something" moments — the
 * AskUserQuestion card (#1149) and the ExitPlanMode card (#1151) — plus the
 * plan panel that answers a waiting plan from the terminal's own screen
 * (src/plan_picker.py), rather than from the transcript. session-transcript.js
 * owns paging, live refresh and the turn/run rendering these cards sit
 * beside; this module owns everything about the cards themselves and their
 * entry points are what session-transcript.js's renderEntries/appendSettled/
 * live-tick code calls back into.
 *
 * `view` (the Chat pane's one open-session state, exported live from
 * session-transcript.js) is read and mutated here exactly as it always was —
 * this module never reassigns the binding itself, only `openChatPane`/
 * `closeChatPane` in session-transcript.js do that.
 */

import { els } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { renderMarkdown } from './markdown.js';
import { detachedSendRefused } from './sessions.js';
import { terminalJsonApi } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';
import { askOption } from './ask-option.js';
import { syncResumePicker } from './chat-resume.js';
import {
  SENT_REFRESH_MS,
  atBottom,
  linkify,
  liveAllowed,
  meta,
  pre,
  scheduleLive,
  tagKey,
  toolOutcomeNote,
  view,
} from './session-transcript.js';

// --- AskUserQuestion card (#1149) -----------------------------------------
//
// Claude Code's multiple-choice prompt renders as a card of its own, never
// folded into a tool-call run: it is the agent talking to the user, so it
// stays visible with tool calls hidden, like a turn. The server forwards the
// call's questions (and, once answered, `answers`: question → label, labels
// joined by ", " for a multi-select, or the typed text) — see
// src/ask_user_question.py.
//
// The card is interactive only in the Chat pane (`answering` passed to
// renderEntries), only for the session's current pending question — the
// newest one with no result and nothing after it but plumbing — and only
// while that holds at the moment of the tap. The server checks again before
// it types a key. Every other card, and every card the Life OS viewer
// renders, is history: no live control at all.

const ASK_NOT_WAITING = 'This question is no longer waiting for an answer';

// The marker renderEntries takes from the Chat pane (and only from it).
export const CHAT_ANSWERING = { chat: true };

export function isQuestion(e) {
  return !!e && e.kind === 'tool_call' && e.name === 'AskUserQuestion' && !e.sidechain &&
    Array.isArray(e.questions) && e.questions.length > 0;
}

// The call's outcome: its paired result, or — when the call settled on an
// earlier tick than its result — a standalone tool_result carrying its id.
// null while nothing has come back.
function questionOutcome(e, entries) {
  if (e.result != null) {
    return { answers: e.answers || null, error: e.error === true };
  }
  const r = (entries || []).find(function (x) {
    return x.kind === 'tool_result' && x.tool_use_id && x.tool_use_id === e.call_id;
  });
  return r ? { answers: r.answers || null, error: r.error === true } : null;
}

// One question's answer out of the `answers` map. Keyed by question text;
// a lone question takes the lone value even if the texts drifted.
function answerFor(q, answers, total) {
  if (!answers) return null;
  if (Object.prototype.hasOwnProperty.call(answers, q.question)) return answers[q.question];
  const values = Object.keys(answers).map(function (k) { return answers[k]; });
  return total === 1 && values.length === 1 ? values[0] : null;
}

function labelPicked(q, answer, label) {
  if (answer == null) return false;
  if (!q.multiSelect) return answer === label;
  return (', ' + answer + ', ').indexOf(', ' + label + ', ') !== -1;
}

// One card per call. `answering` is the Chat pane's hook; without it (the
// Life OS viewer) the card never grows a live control.
export function renderQuestion(e, toolErrors, answering) {
  const li = document.createElement('li');
  li.className = 'tr-ask-item';
  li._trAsk = e;
  li._trAnswering = answering || null;
  if (e.call_id) li.dataset.callId = e.call_id;
  const card = document.createElement('div');
  card.className = 'tr-ask';
  tagKey(card, e);
  if (e.error === true) card.classList.add('tr-item-failed');
  const head = document.createElement('div');
  head.className = 'tr-ask-head';
  head.innerHTML = icon('messages-square');
  head.appendChild(meta('Agent asks', e.timestamp));
  if (e.error === true) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-chip';
    chip.textContent = 'failed';
    head.appendChild(chip);
  }
  card.appendChild(head);
  const simple = e.questions.length === 1 && !e.questions[0].multiSelect;
  e.questions.forEach(function (q, qi) {
    const sec = document.createElement('section');
    sec.className = 'tr-ask-q';
    sec.dataset.q = String(qi);
    if (q.header) {
      const chip = document.createElement('span');
      chip.className = 'tr-ask-header';
      chip.textContent = q.header;
      sec.appendChild(chip);
    }
    const text = document.createElement('p');
    text.className = 'tr-ask-question';
    text.textContent = q.question || '';
    sec.appendChild(text);
    if (q.multiSelect) {
      const hint = document.createElement('p');
      hint.className = 'tr-ask-hint';
      hint.textContent = 'Pick any number';
      sec.appendChild(hint);
    }
    const list = document.createElement('div');
    list.className = 'tr-ask-options';
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', q.header || q.question || 'Options');
    q.options.forEach(function (opt, oi) {
      const b = askOption({
        n: oi + 1, label: opt.label, description: opt.description, mark: true,
        onTap: function () { onOptionTap(li, qi, oi + 1); },
      });
      b.dataset.n = String(oi + 1);
      b.disabled = true;
      list.appendChild(b);
    });
    sec.appendChild(list);
    // "Type something" — single-select only: typing into a multi-select's
    // text row was not probed (src/ask_user_question.py).
    if (!q.multiSelect) {
      const other = document.createElement('div');
      other.className = 'tr-ask-other';
      other.hidden = true;
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'tr-ask-input';
      input.placeholder = 'Or type an answer';
      input.setAttribute('aria-label', 'Type an answer to: ' + (q.question || 'the question'));
      input.maxLength = 500;
      input.addEventListener('input', function () { onTextInput(li, qi, input.value); });
      other.appendChild(input);
      if (simple) {
        const send = document.createElement('button');
        send.type = 'button';
        send.className = 'button-tint tr-ask-send';
        send.textContent = 'Send';
        send.disabled = true;
        send.addEventListener('click', function () {
          const t = input.value.trim();
          if (t) submitAnswer(li, [{ text: t }]);
        });
        input.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' && input.value.trim()) {
            ev.preventDefault();
            submitAnswer(li, [{ text: input.value.trim() }]);
          }
        });
        other.appendChild(send);
      }
      sec.appendChild(other);
    }
    const typed = document.createElement('p');
    typed.className = 'tr-ask-typed';
    typed.hidden = true;
    sec.appendChild(typed);
    card.appendChild(sec);
  });
  const status = document.createElement('p');
  status.className = 'tr-ask-status';
  status.setAttribute('role', 'status');
  card.appendChild(status);
  if (!simple) {
    const submit = document.createElement('button');
    submit.type = 'button';
    submit.className = 'button-primary tr-ask-submit';
    submit.textContent = 'Submit answers';
    submit.hidden = true;
    submit.addEventListener('click', function () {
      const draft = askDraft(li);
      if (draftComplete(li._trAsk, draft)) submitAnswer(li, draft);
    });
    card.appendChild(submit);
  }
  const note = toolOutcomeNote(e, toolErrors);
  if (note) {
    const unknown = document.createElement('div');
    unknown.className = 'tr-outcome-unknown';
    unknown.textContent = note;
    card.appendChild(unknown);
  }
  li.appendChild(card);
  // A first paint from the entry alone, so a card is right even where
  // nothing ever calls syncDecisionCards() (the Life OS viewer).
  const out = questionOutcome(e, null);
  paintQuestion(li, out ? (out.answers ? 'answered' : 'closed') : 'history', out);
  return li;
}

// The live card's in-progress picks, one slot per question, kept on the
// view so a live-refresh rebuild of the card doesn't drop them.
function askDraft(li) {
  const e = li._trAsk;
  if (!view) return [];
  view.askDrafts = view.askDrafts || {};
  if (!view.askDrafts[e.call_id]) {
    view.askDrafts[e.call_id] = e.questions.map(function () { return null; });
  }
  return view.askDrafts[e.call_id];
}

function draftComplete(e, draft) {
  return e.questions.every(function (q, i) {
    const a = draft[i];
    if (!a) return false;
    if (q.multiSelect) return Array.isArray(a.options) && a.options.length > 0;
    return a.option != null || !!(a.text && a.text.trim());
  });
}

function onOptionTap(li, qi, n) {
  if (li.dataset.mode !== 'live') return;
  const e = li._trAsk;
  if (e.questions.length === 1 && !e.questions[0].multiSelect) {
    submitAnswer(li, [{ option: n }]);
    return;
  }
  const draft = askDraft(li);
  if (e.questions[qi].multiSelect) {
    const picks = (draft[qi] && draft[qi].options) ? draft[qi].options.slice() : [];
    const at = picks.indexOf(n);
    if (at === -1) picks.push(n);
    else picks.splice(at, 1);
    draft[qi] = picks.length ? { options: picks } : null;
  } else {
    draft[qi] = { option: n };
    const input = li.querySelector('.tr-ask-q[data-q="' + qi + '"] .tr-ask-input');
    if (input) input.value = '';
  }
  paintDraft(li);
}

function onTextInput(li, qi, value) {
  if (li.dataset.mode !== 'live') return;
  const e = li._trAsk;
  const send = li.querySelector('.tr-ask-send');
  if (send) send.disabled = !value.trim();
  if (e.questions.length === 1 && !e.questions[0].multiSelect) return;
  const draft = askDraft(li);
  if (value.trim()) draft[qi] = { text: value };
  else if (draft[qi] && draft[qi].text != null) draft[qi] = null;
  paintDraft(li);
}

// Reflect the draft on a live card: pressed options, typed text, Submit.
function paintDraft(li) {
  const e = li._trAsk;
  if (e.questions.length === 1 && !e.questions[0].multiSelect) {
    // A tap sends, so there is no draft — only Send follows the text field.
    const input = li.querySelector('.tr-ask-input');
    const send = li.querySelector('.tr-ask-send');
    if (send) send.disabled = !(input && input.value.trim());
    return;
  }
  const draft = askDraft(li);
  e.questions.forEach(function (q, qi) {
    const a = draft[qi];
    li.querySelectorAll('.tr-ask-q[data-q="' + qi + '"] .tr-ask-opt').forEach(function (b) {
      const n = Number(b.dataset.n);
      const on = !!a && (q.multiSelect ? (a.options || []).indexOf(n) !== -1 : a.option === n);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const input = li.querySelector('.tr-ask-q[data-q="' + qi + '"] .tr-ask-input');
    if (input && a && a.text != null && input.value !== a.text) input.value = a.text;
  });
  const submit = li.querySelector('.tr-ask-submit');
  if (submit) submit.disabled = !draftComplete(e, draft);
}

const ASK_STATUS = {
  live: '',
  sent: 'Answer sent: waiting for the agent to take it',
  closed: 'Not answered: the question was dismissed',
  stale: ASK_NOT_WAITING,
  history: 'No answer recorded here',
  console: 'Answer it in the PC console: sending is off for this agent',
};

// Put one card into `mode`. Idempotent per mode, so the live tick can call
// it on every card without disturbing a half-made pick.
function paintQuestion(li, mode, out, statusText) {
  if (li.dataset.mode === mode && !statusText) return;
  li.dataset.mode = mode;
  const e = li._trAsk;
  const live = mode === 'live';
  const simple = e.questions.length === 1 && !e.questions[0].multiSelect;
  const answers = out && out.answers;
  e.questions.forEach(function (q, qi) {
    const sec = li.querySelector('.tr-ask-q[data-q="' + qi + '"]');
    const answer = answerFor(q, answers, e.questions.length);
    let matched = false;
    sec.querySelectorAll('.tr-ask-opt').forEach(function (b) {
      b.disabled = !live;
      const picked = mode === 'answered' && labelPicked(q, answer, q.options[Number(b.dataset.n) - 1].label);
      matched = matched || picked;
      b.classList.toggle('tr-ask-opt--picked', picked);
      // Toggle semantics only where a tap selects rather than sends.
      if (live && !simple) b.setAttribute('aria-pressed', 'false');
      else b.removeAttribute('aria-pressed');
    });
    const other = sec.querySelector('.tr-ask-other');
    if (other) other.hidden = !live;
    const typed = sec.querySelector('.tr-ask-typed');
    const freeText = mode === 'answered' && answer && !matched;
    typed.hidden = !freeText;
    typed.textContent = freeText ? 'Typed answer: “' + answer + '”' : '';
  });
  const submit = li.querySelector('.tr-ask-submit');
  if (submit) submit.hidden = !live;
  const status = li.querySelector('.tr-ask-status');
  let text = statusText != null ? statusText : ASK_STATUS[mode];
  if (mode === 'answered') text = 'Answered';
  if (live) text = simple ? 'Tap an answer to send it to the agent' : 'Pick an answer for each question, then submit';
  status.textContent = text || '';
  if (live) paintDraft(li);
}

// The call id of the session's pending decision (a question, #1149, or a
// plan, #1151), or null: the newest such call in what is loaded, still
// without a result, with no turn, tool call or thinking after it (a later
// one means the agent moved on). Results and harness plumbing after it don't
// count — they are not the agent continuing.
function currentDecisionId() {
  if (!view || view.ended || !view.entries) return null;
  const list = view.entries;
  for (let i = list.length - 1; i >= 0; i--) {
    const e = list[i];
    if (e.sidechain || e.kind === 'system' || e.kind === 'tool_result') continue;
    if (isDecisionCard(e)) return questionOutcome(e, list) ? null : (e.call_id || null);
    return null;
  }
  return null;
}

// Re-derive every card's mode from what is loaded — after a page load, a
// live tick, a Load older, and a send.
export function syncDecisionCards() {
  if (!view) return;
  const liveId = currentDecisionId();
  const s = view.session;
  els.transcriptList.querySelectorAll('.tr-ask-item').forEach(function (li) {
    const e = li._trAsk;
    const out = questionOutcome(e, view.entries);
    if (out) {
      if (view.askSent) delete view.askSent[e.call_id];
      paintQuestion(li, out.answers ? 'answered' : 'closed', out);
      return;
    }
    const sent = view.askSent && view.askSent[e.call_id];
    if (sent) {
      paintQuestion(li, 'sent', null, sent === true ? null : sent);
      return;
    }
    const refused = view.askRefused && view.askRefused[e.call_id];
    if (!li._trAnswering || !e.call_id || e.call_id !== liveId || refused) {
      paintQuestion(li, li._trAnswering ? 'stale' : 'history', null);
      return;
    }
    paintQuestion(li, detachedSendRefused(s) ? 'console' : 'live', null);
  });
  els.transcriptList.querySelectorAll('.tr-plan-item').forEach(function (li) {
    const e = li._trPlan;
    const out = planOutcome(e, view.entries);
    if (out) paintPlan(li, out.outcome, out);
    else if (li._trAnswering && e.call_id && e.call_id === liveId) {
      paintPlan(li, 'waiting', null, planWhere(s));
    } else paintPlan(li, li._trAnswering ? 'stale' : 'history', null);
  });
}

// Send the picks. Re-checks "still the pending question" at the tap — the
// live refresh can trail reality by a tick — then leaves the card locked in
// "sent" until the agent's result lands and closes it.
async function submitAnswer(li, answers) {
  if (!view || li.dataset.mode !== 'live') return;
  const target = view;
  const e = li._trAsk;
  if (currentDecisionId() !== e.call_id) {
    toast(ASK_NOT_WAITING, 'bad');
    syncDecisionCards();
    return;
  }
  target.askSent = target.askSent || {};
  target.askSent[e.call_id] = true;
  syncDecisionCards();
  try {
    await terminalJsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) + '/answer',
      { method: 'POST', body: { tool_use_id: e.call_id, answers: answers } }
    );
  } catch (exc) {
    if (exc && exc.status === 502 && /partly sent/.test(exc.message || '')) {
      // Some keys went in: the picker is mid-answer, so the card must not
      // offer a retry that would type over it. It says where to look.
      target.askSent[e.call_id] = exc.message;
      toast(exc.message, 'error');
    } else {
      delete target.askSent[e.call_id];
      if (exc && exc.status === 409) {
        toast(exc.message || ASK_NOT_WAITING, 'bad');
        target.askRefused = target.askRefused || {};
        target.askRefused[e.call_id] = true;
      } else {
        apiFailToast('Answer failed', exc);
      }
    }
    if (view === target) syncDecisionCards();
    return;
  }
  toast('Answer sent', 'good', { icon: 'send-horizontal' });
  if (view !== target) return;
  window.clearTimeout(target.refreshTimer);
  target.refreshTimer = window.setTimeout(function () {
    if (view === target) scheduleLive(0);
  }, SENT_REFRESH_MS);
}

// --- ExitPlanMode card (#1151) --------------------------------------------
//
// The plan the agent wants approved, through the same escape-first markdown
// renderer as a reply, and how it was answered: approved (and whether it was
// edited first), sent back with the user's feedback, declined, or never
// shown (the agent called the tool outside plan mode). The server reads
// those off the result (src/plan_review.py).
//
// The card itself is read-only. Claude Code can hold the pending call back
// from the transcript until it is answered (measured: 31 s with the picker
// up and the call not on disk), so the transcript cannot say "this plan is
// waiting" reliably, and the picker's first option changes with the
// session's permission mode ("auto-accept edits" vs "switch to BYPASS
// PERMISSIONS"). Answering is the plan panel's below, which reads the
// terminal's screen instead; a waiting card says where to answer.

export function isPlan(e) {
  return !!e && e.kind === 'tool_call' && e.name === 'ExitPlanMode' && !e.sidechain &&
    typeof e.plan === 'string';
}

// A card of its own beside the turns — never folded into a tool-call run.
export function isDecisionCard(e) {
  return isQuestion(e) || isPlan(e);
}

function planFields(x) {
  return {
    outcome: x.plan_outcome || (x.error === true ? 'declined' : 'answered'),
    feedback: x.plan_feedback || '',
    edited: x.plan_edited === true,
  };
}

// The plan's answer: its paired result, or a standalone one carrying its id
// (the call settled on an earlier tick). null while nothing has come back.
function planOutcome(e, entries) {
  if (e.result != null) return planFields(e);
  const r = (entries || []).find(function (x) {
    return x.kind === 'tool_result' && x.tool_use_id && x.tool_use_id === e.call_id;
  });
  return r ? planFields(r) : null;
}

const PLAN_STATUS = {
  approved: 'Approved',
  sent_back: 'Sent back with feedback',
  declined: 'Not approved',
  not_shown: 'Never shown: the agent was not in plan mode',
  answered: 'Answered',
  stale: 'No longer waiting for an answer',
  history: 'No answer recorded here',
};

export function renderPlan(e, toolErrors, answering) {
  const li = document.createElement('li');
  li.className = 'tr-plan-item';
  li._trPlan = e;
  li._trAnswering = answering || null;
  if (e.call_id) li.dataset.callId = e.call_id;
  const card = document.createElement('div');
  card.className = 'tr-ask tr-plan';
  tagKey(card, e);
  const head = document.createElement('div');
  head.className = 'tr-ask-head';
  head.innerHTML = icon('file-text');
  head.appendChild(meta('Plan for approval', e.timestamp));
  card.appendChild(head);
  const body = document.createElement('div');
  body.className = 'tr-md tr-plan-body';
  body.innerHTML = renderMarkdown(e.plan || '');
  linkify(body);
  card.appendChild(body);
  if (e.plan_truncated) {
    const mark = document.createElement('div');
    mark.className = 'tr-trunc';
    mark.textContent = '(truncated — open the terminal for the rest)';
    card.appendChild(mark);
  }
  const feedback = document.createElement('p');
  feedback.className = 'tr-ask-typed tr-plan-feedback';
  feedback.hidden = true;
  card.appendChild(feedback);
  const status = document.createElement('p');
  status.className = 'tr-ask-status';
  status.setAttribute('role', 'status');
  card.appendChild(status);
  const note = toolOutcomeNote(e, toolErrors);
  if (note) {
    const unknown = document.createElement('div');
    unknown.className = 'tr-outcome-unknown';
    unknown.textContent = note;
    card.appendChild(unknown);
  }
  li.appendChild(card);
  // First paint from the entry alone (the Life OS viewer never syncs).
  const out = planOutcome(e, null);
  paintPlan(li, out ? out.outcome : 'history', out);
  return li;
}

// `where` says where to answer a waiting plan.
function paintPlan(li, mode, out, where) {
  li.dataset.mode = mode;
  const card = li.querySelector('.tr-plan');
  // Only a real tool error is a failure: a declined or sent-back plan is
  // the user's answer, even though the harness records it as an error.
  const failed = mode === 'not_shown';
  card.classList.toggle('tr-item-failed', failed);
  let chip = card.querySelector('.tr-ask-head .tr-fail-chip');
  if (failed && !chip) {
    chip = document.createElement('span');
    chip.className = 'tr-fail-chip';
    chip.textContent = 'failed';
    card.querySelector('.tr-ask-head').appendChild(chip);
  } else if (!failed && chip) {
    chip.remove();
  }
  const feedback = card.querySelector('.tr-plan-feedback');
  const said = mode === 'sent_back' && out && out.feedback;
  feedback.hidden = !said;
  feedback.textContent = said ? 'Your feedback: “' + out.feedback + '”' : '';
  let text = PLAN_STATUS[mode] || '';
  if (mode === 'approved' && out && out.edited) text = 'Approved after your edits';
  if (mode === 'waiting') text = 'Waiting for your answer: ' + (where || 'answer it in the terminal');
  card.querySelector('.tr-ask-status').textContent = text;
}

// --- The plan panel: answering from the terminal's screen (#1151) ---------
//
// The server reads the plan picker off the terminal (src/plan_picker.py):
// the session's PTY capture rendered at the PTY's own size. While it is up,
// the panel below the transcript shows the options exactly as the screen
// lists them, and a tap sends the digit and the label the reader saw. The
// server reads the screen again before typing and refuses (409) if that
// digit no longer carries that label, so a tap can never approve into a
// permission mode the reader didn't see. Full-control Claude sessions only:
// a detached one has no screen here, and its card says to use the console.
//
// Polled with the live tick, so it stops with it (overlay closed, phone
// locked, session gone).

// How long a sent answer keeps the panel locked while the screen still
// shows the same picker. Past it the keys evidently didn't take, and the
// panel offers the options again (the server re-checks every tap anyway).
const PICKER_SENT_HOLD_MS = 10000;

export function pickerAllowed(s) {
  return !!s && String(s.agent || 'claude').toLowerCase() === 'claude' && s.kind !== 'remote';
}

function planWhere(s) {
  if (s && s.kind === 'remote') return 'answer it in the PC console';
  if (view && view.picker) return 'answer it below';
  return 'Chat can’t see the plan picker on the terminal, so answer it there';
}

// The pending plan card the transcript already shows, if any: the panel
// then leaves the plan out rather than showing it twice.
function pendingPlanShown() {
  const id = currentDecisionId();
  return !!id && (view.entries || []).some(function (e) { return isPlan(e) && e.call_id === id; });
}

export async function pollPicker() {
  if (!view || view.pickerBusy || !pickerAllowed(view.session) || !liveAllowed()) return;
  const target = view;
  target.pickerBusy = true;
  let body = null;
  try {
    body = await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) + '/plan-picker'
    );
  } catch (exc) {
    // Quietly, like a failed live tick: the next one asks again. Until a
    // read succeeds nothing is confirmed, so nothing is offered.
    body = null;
  }
  target.pickerBusy = false;
  if (view !== target) return;
  applyPicker(body && body.showing ? body : null);
  // The same read says whether the /resume picker is up (#1300). A failed
  // read says nothing, so it leaves the resume card as it is.
  if (body) syncResumePicker(!!body.resume_picker);
}

function applyPicker(p) {
  const box = els.transcriptPlanLive;
  if (!box || !view) return;
  view.picker = p;
  const sig = p ? JSON.stringify([p.options, p.cursor, p.answerable, p.plan]) : '';
  const held = view.pickerSent && Date.now() - view.pickerSent < PICKER_SENT_HOLD_MS;
  if (sig === view.pickerSig && (held || !view.pickerSent)) {
    syncDecisionCards();
    return;
  }
  view.pickerSig = sig;
  view.pickerSent = null;
  const stick = atBottom();
  box.innerHTML = '';
  box.hidden = !p;
  if (p) box.appendChild(renderPicker(p));
  syncDecisionCards();
  if (stick) els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
}

export function clearPicker() {
  const box = els.transcriptPlanLive;
  if (box) {
    box.innerHTML = '';
    box.hidden = true;
  }
}

function renderPicker(p) {
  const card = document.createElement('div');
  card.className = 'tr-ask tr-plan tr-plan-live-card';
  const head = document.createElement('div');
  head.className = 'tr-ask-head';
  head.innerHTML = icon('file-text');
  head.appendChild(meta('Plan waiting for your answer', null));
  card.appendChild(head);
  if (p.plan && !pendingPlanShown()) {
    if (p.plan_source === 'file') {
      const body = document.createElement('div');
      body.className = 'tr-md tr-plan-body';
      body.innerHTML = renderMarkdown(p.plan);
      linkify(body);
      card.appendChild(body);
    } else {
      // As the terminal shows it: wrapped to the terminal's width.
      const body = pre(p.plan, false);
      body.classList.add('tr-plan-body');
      card.appendChild(body);
    }
    if (p.plan_truncated) {
      const mark = document.createElement('div');
      mark.className = 'tr-trunc';
      mark.textContent = '(truncated — open the terminal for the rest)';
      card.appendChild(mark);
    }
  }
  const list = document.createElement('div');
  list.className = 'tr-ask-options';
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', 'Answer the plan');
  p.options.forEach(function (o) {
    if (o.kind === 'feedback') {
      list.appendChild(renderPickerFeedback(p, o));
      return;
    }
    const b = askOption({
      n: o.n, label: o.label,
      onTap: function () { sendPlanAnswer(o, null); },
    });
    b.dataset.n = String(o.n);
    b.disabled = !(p.answerable && o.kind === 'approve');
    list.appendChild(b);
  });
  card.appendChild(list);
  const status = document.createElement('p');
  status.className = 'tr-ask-status';
  status.setAttribute('role', 'status');
  status.textContent = p.answerable
    ? 'Read from the terminal just now. A tap sends that answer to Claude.'
    : 'The terminal is in the middle of an answer: finish it there.';
  card.appendChild(status);
  return card;
}

// "Tell Claude what to change": its number and label like the others, and
// the feedback typed here rather than on the terminal.
function renderPickerFeedback(p, o) {
  const sec = document.createElement('div');
  sec.className = 'tr-plan-feedback-row';
  const row = document.createElement('div');
  row.className = 'tr-ask-opt';
  const num = document.createElement('span');
  num.className = 'tr-ask-num';
  num.textContent = String(o.n);
  const label = document.createElement('span');
  label.className = 'tr-ask-label';
  label.textContent = o.label;
  row.appendChild(num);
  row.appendChild(label);
  sec.appendChild(row);
  const other = document.createElement('div');
  other.className = 'tr-ask-other';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'tr-ask-input';
  input.placeholder = 'What should change?';
  input.setAttribute('aria-label', o.label);
  input.maxLength = 500;
  input.disabled = !p.answerable;
  const send = document.createElement('button');
  send.type = 'button';
  send.className = 'button-tint tr-ask-send';
  send.textContent = 'Send back';
  send.disabled = true;
  const go = function () {
    const t = input.value.trim();
    if (t && p.answerable) sendPlanAnswer(o, t);
  };
  input.addEventListener('input', function () { send.disabled = !input.value.trim() || !p.answerable; });
  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && input.value.trim()) {
      ev.preventDefault();
      go();
    }
  });
  send.addEventListener('click', go);
  other.appendChild(input);
  other.appendChild(send);
  sec.appendChild(other);
  return sec;
}

function lockPicker(text) {
  const box = els.transcriptPlanLive;
  if (!box) return;
  box.querySelectorAll('button, input').forEach(function (el) { el.disabled = true; });
  const status = box.querySelector('.tr-ask-status');
  if (status) status.textContent = text;
}

async function sendPlanAnswer(o, feedback) {
  if (!view || !view.picker || view.pickerSent) return;
  const target = view;
  target.pickerSent = Date.now();
  lockPicker('Sending…');
  try {
    await terminalJsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) + '/plan-answer',
      { method: 'POST', body: { option: o.n, label: o.label, feedback: feedback } }
    );
  } catch (exc) {
    if (view !== target) return;
    if (exc && exc.status === 502 && /partly sent/.test(exc.message || '')) {
      // Some keys went in: the picker is mid-answer, so no retry is offered
      // until the screen changes. The message says where to look.
      lockPicker(exc.message);
      toast(exc.message, 'error');
      return;
    }
    if (exc && exc.status === 409) toast(exc.message || 'The plan is no longer waiting', 'bad');
    else apiFailToast('Answer failed', exc);
    // Nothing was typed: re-render from a fresh read of the screen.
    target.pickerSent = null;
    target.pickerSig = null;
    pollPicker();
    return;
  }
  toast('Answer sent to the terminal', 'good', { icon: 'send-horizontal' });
  if (view !== target) return;
  lockPicker(feedback ? 'Sent back: waiting for Claude to revise the plan' : 'Sent: waiting for Claude');
  window.clearTimeout(target.refreshTimer);
  target.refreshTimer = window.setTimeout(function () {
    if (view === target) scheduleLive(0);
  }, SENT_REFRESH_MS);
}
