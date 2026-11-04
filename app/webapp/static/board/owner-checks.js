/* Owner checks on the Board: issues that wait on the owner.
 *
 * Two kinds, from fleet-config's owner_check labels (GET /api/board's
 * `owner_checks` section): shipped work to validate as a user
 * (`validate-after`) and a step owed before work can start (`before-start`).
 * They sit in Your turn under the live sessions, one group per kind. A row's
 * tap unfolds its own detail in place: why it needs you, what to try, and the
 * links to the issue and its proof. The detail's Answer, like the group's
 * button, opens the owner-check answer sheet (board-answers.js), where this
 * module draws each item's block and reads its answer back.
 *
 * The list is a cached read with its age. Before one has landed it is
 * unknown, never "none"; repos the list could not read make it a lower bound,
 * and the group says so. An item answered Later or Problem keeps its row,
 * marked, and leaves the "needs you" count.
 */

import { els } from '../state.js';
import { chip } from '../glance.js';
import { actionRow } from '../action-rows.js';
import { el } from '../dom-utils.js';
import { askOption } from '../chat/ask-option.js';
import { icon } from '../_vendored/icons/icons.js';
import { terminalJsonApi } from '../webauthn.js';
import { boardRepoFilter, matchesRepoFilter } from './board-dispatch.js';
import { agoText } from './chief-plan.js';
import { calendarDaysFromToday, newestDateFirst, shortDate } from '../shared/format.js';
import { plural, renderBoard } from '../board.js';
import { ownerItemWaiting, ownerNeedsCount, ownerSection } from './owner-needs.js';
import { attachedLines, emptyFiles } from './answer-files.js';

export const VALIDATE_AFTER = 'validate-after';
export const BEFORE_START = 'before-start';

// Before start blocks work from starting, so it leads.
const GROUPS = [
  { kind: BEFORE_START, heading: 'Before you start it', label: 'Before start' },
  { kind: VALIDATE_AFTER, heading: 'To validate', label: 'Validate' },
];

// The ref whose detail is open, across the 5 s poll's rebuilds.
let expandedRef = null;

const section = ownerSection;
const waiting = ownerItemWaiting;

// Re-exported: the Board's head line and the nav badge share one rule.
export { ownerNeedsCount };

// Newest first: `since` descending, the higher (later filed) issue number
// breaking a tie, and an item with no readable `since` after the dated ones.
function newestFirst(a, b) {
  const byDate = newestDateFirst(a.since, b.since);
  if (byDate) return byDate;
  return (Number(b.number) || 0) - (Number(a.number) || 0);
}

// The items the repo filter shows, the waiting ones first, each half newest
// first (each group of the card filters this list, so it inherits the order).
export function shownOwnerItems(body) {
  const sec = section(body);
  if (!sec) return [];
  const filter = boardRepoFilter();
  const items = (sec.items || []).filter(function (it) { return matchesRepoFilter(it, filter); });
  return items.filter(waiting).sort(newestFirst)
    .concat(items.filter(function (it) { return !waiting(it); }).sort(newestFirst));
}

function kindLabel(kind) {
  const g = GROUPS.find(function (x) { return x.kind === kind; });
  return g ? g.label : kind;
}

// "3 Nov · waiting 3 d" from the block's `since` date; '' when there is none.
function sinceText(since) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(since || ''));
  if (!m) return '';
  const days = Math.max(0, calendarDaysFromToday(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))));
  return shortDate(since) + (days ? ' · waiting ' + days + ' d' : ' · since today');
}

function markText(item) {
  if (item.mark === 'later') return 'snoozed';
  if (item.mark === 'problem') return 'problem reported, waiting on a fix';
  return '';
}

// A proof link's label: a PR by its number, an artifact by name, else the host.
function linkLabel(url) {
  const pr = /\/pull\/(\d+)/.exec(url);
  if (pr) return 'PR #' + pr[1];
  if (/claude\.ai\/(code\/)?artifact/.test(url)) return 'Artifact';
  const issue = /\/issues\/(\d+)/.exec(url);
  if (issue) return 'Issue #' + issue[1];
  try {
    return new URL(url).hostname;
  } catch (_) {
    return 'Link';
  }
}

// A reference pill inside a 44px tap target.
function link(url, label) {
  const a = el('a', 'board-owner-link');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  const pill = el('span', 'board-owner-pill');
  pill.innerHTML = icon('link');
  pill.appendChild(document.createTextNode(label));
  a.appendChild(pill);
  return a;
}

// The issue first, then every proof link the block carries (http(s) only).
function links(item) {
  const wrap = el('div', 'board-owner-links');
  if (item.url) wrap.appendChild(link(item.url, 'Issue #' + item.number));
  (item.proof || []).forEach(function (url) {
    if (/^https?:\/\//.test(url)) wrap.appendChild(link(url, linkLabel(url)));
  });
  return wrap;
}

// Why it needs you and what to try; a label with no readable block says so.
function details(item, cls) {
  const nodes = [];
  if (!item.block) {
    nodes.push(el('p', cls, 'Details unknown: the issue carries the label but no readable owner-check note. Open the issue to see what it needs.'));
    return nodes;
  }
  if (item.reason) nodes.push(labelled(cls, 'Why', item.reason));
  nodes.push.apply(nodes, tryNodes(item, cls));
  return nodes;
}

// What to try: the recorded Try; else lines the issue's own checklist offers,
// quoted and labelled as such; else a plain "none", never boilerplate.
function tryNodes(item, cls) {
  if (item.try) return [labelled(cls, 'Try', item.try)];
  const lines = item.try_lines || [];
  if (!lines.length) {
    return [el('p', cls + ' board-owner-try-none', 'No specific Try on record. Open the issue to see what it shipped.')];
  }
  const head = el('p', cls);
  head.appendChild(el('strong', null, 'Try'));
  head.appendChild(document.createTextNode(" (from the issue's " + (item.try_from || 'checklist').toLowerCase() + '):'));
  const list = el('ul', 'board-owner-try-list ' + cls);
  lines.forEach(function (line) { list.appendChild(el('li', null, line)); });
  return [head, list];
}

function labelled(cls, label, text) {
  const p = el('p', cls);
  p.appendChild(el('strong', null, label + ': '));
  p.appendChild(document.createTextNode(text));
  return p;
}

// ---------------------------------------------------------------- the lane

function itemRow(item, onAnswer) {
  const expanded = expandedRef === item.ref;
  const title = item.title || item.ref;
  const meta = [item.ref, markText(item) || sinceText(item.since)].filter(Boolean).join(' · ');
  const chips = item.block ? [] : [chip('details unknown', 'attention', 'board-owner-unknown')];
  const row = actionRow({
    className: 'board-item board-owner-row' + (waiting(item) ? '' : ' is-answered'),
    title: title,
    meta: meta,
    chips: chips,
    label: (expanded ? 'Hide' : 'Show') + ' what ' + item.ref + ' needs',
    onMain: function () {
      expandedRef = expanded ? null : item.ref;
      renderBoard();
    },
  });
  row.li.dataset.ref = item.ref;
  row.main.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  if (!expanded) return row.li;
  row.li.classList.add('expanded');
  const detail = el('div', 'board-owner-detail');
  detail.id = 'owner-detail-' + item.ref.replace(/[^A-Za-z0-9_-]/g, '-');
  row.main.setAttribute('aria-controls', detail.id);
  details(item, 'board-owner-line').forEach(function (n) { detail.appendChild(n); });
  detail.appendChild(links(item));
  const answer = el('button', 'button-tint board-owner-answer', 'Answer');
  answer.type = 'button';
  answer.addEventListener('click', function () { onAnswer(item.ref); });
  detail.appendChild(answer);
  row.li.appendChild(detail);
  return row.li;
}

function group(g, items, onAnswer) {
  const wrap = el('div', 'board-plan-group board-owner-group');
  wrap.dataset.kind = g.kind;
  wrap.appendChild(el('h4', 'board-plan-heading', g.heading + ' (' + items.length + ')'));
  const list = el('ul', 'card-list board-list');
  items.forEach(function (it) { list.appendChild(itemRow(it, onAnswer)); });
  wrap.appendChild(list);
  return wrap;
}

// The list's freshness and gaps, said every time: its age, a failed
// refresh, unreadable repos. Never a bare empty list.
function stateLine(sec) {
  if (!sec.available && !sec.error && !sec.refreshing) {
    return 'Owner checks not loaded yet. Refresh reads them.';
  }
  if (!sec.available) {
    return sec.refreshing ? 'Reading owner checks…' : 'Owner checks unknown: the list could not be read.';
  }
  const parts = ['Owner checks updated ' + (agoText(sec.fetched_at) || 'at an unknown time')];
  if (sec.refreshing) parts.push('refreshing');
  if (sec.error) parts.push('the last refresh failed');
  const unknown = (sec.unknown || []).length;
  if (unknown) parts.push(plural(unknown, 'repo', 'repos') + ' unreadable, so this list may be short');
  return parts.join(' · ');
}

// Fills Your turn's owner block; returns how many waiting items it shows, for
// the lane's count. `onAnswer(ref)` opens the owner-check sheet on that item.
export function renderOwnerChecks(body, onAnswer) {
  const host = els.boardColumns && els.boardColumns.querySelector('.board-owner');
  if (!host) return 0;
  const sec = section(body);
  if (!sec) {
    host.dataset.sig = '';
    host.replaceChildren();
    return 0;
  }
  const items = shownOwnerItems(body);
  const waitingCount = items.filter(waiting).length;
  const sig = JSON.stringify([sec && sec.available, sec && sec.fetched_at, sec && sec.error,
    sec && sec.refreshing, sec && sec.unknown, items, expandedRef,
    agoText(sec && sec.fetched_at), boardRepoFilter()]);
  if (host.dataset.sig === sig) return waitingCount;
  host.dataset.sig = sig;
  // The state line leads, where the Chief's plan card puts its age line.
  const line = el('p', 'board-plan-line muted small board-owner-state', stateLine(sec));
  if (sec && sec.error) line.title = sec.error;
  const nodes = [line];
  GROUPS.forEach(function (g) {
    const mine = items.filter(function (it) { return it.kind === g.kind; });
    if (mine.length) nodes.push(group(g, mine, onAnswer));
  });
  if (waitingCount) {
    const btn = el('button', 'button-tint board-plan-answer board-owner-answer-all');
    btn.type = 'button';
    btn.innerHTML = icon('messages-square') + ' ';
    btn.appendChild(document.createTextNode('Answer ' + plural(waitingCount, 'owner check', 'owner checks')));
    const first = items.find(waiting);
    btn.addEventListener('click', function () { onAnswer(first.ref); });
    nodes.push(btn);
  }
  host.replaceChildren.apply(host, nodes);
  return waitingCount;
}

// ---------------------------------------------------------------- the sheet

const OPTIONS = {
  [VALIDATE_AFTER]: [
    { answer: 'validated', label: 'Validated', description: 'It works: removes the label and notes it on the issue' },
    { answer: 'later', label: 'Later', description: 'Snooze it until tomorrow' },
  ],
  [BEFORE_START]: [
    { answer: 'done', label: 'Done', description: 'The step is done: removes the label so the work can start' },
    { answer: 'later', label: 'Later', description: 'Snooze it until tomorrow' },
  ],
};

export function emptyOwnerDraft() {
  // `error`: why the last Done could not apply this answer, shown in its block.
  // `files`: [{ name, path }] stored on the chief's session; never posted to GitHub.
  return { pick: null, problem: '', error: '', files: emptyFiles() };
}

export function ownerSheetKey(item) {
  return 'owner:' + item.ref;
}

// Words that only say an item works. Typed text made of these alone (with at
// least one AFFIRMS word) validates the item; anything else, a negation
// included, is a problem report.
const AFFIRMS = ['validated', 'validate', 'works', 'working', 'worked', 'ok', 'okay', 'fine', 'good',
  'great', 'perfect', 'lgtm', 'confirmed', 'verified', 'tested', 'yes', 'yep', 'done', 'correct'];
const FILLER = ['it', 'all', 'looks', 'everything', 'now', 'well', 'and', 'as', 'expected',
  'correctly', 'here', 'on', 'phone', 'desktop', 'both', 'really', 'very', 'seems', 'is'];

export function affirmsItWorks(text) {
  const words = String(text || '').toLowerCase().split(/[^a-z]+/).filter(Boolean);
  return words.some(function (w) { return AFFIRMS.indexOf(w) !== -1; })
    && words.every(function (w) { return AFFIRMS.indexOf(w) !== -1 || FILLER.indexOf(w) !== -1; });
}

// One item's answer, or null. Typed text wins over a pick: text that only
// says it works validates with the text as a note, any other is a problem.
// `attachments`, set only when files ride with it, is how many: a number, the
// only thing about them that goes to GitHub. A snooze carries none, and files
// with no answer make no answer (the draft keeps them).
export function ownerAnswerOf(item, draft) {
  const text = String((draft && draft.problem) || '').split(/\s+/).filter(Boolean).join(' ');
  const count = ((draft && draft.files) || []).length;
  let ans = null;
  if (item.kind === VALIDATE_AFTER && text) {
    ans = { ref: item.ref, kind: item.kind, answer: affirmsItWorks(text) ? 'validated' : 'problem', text: text };
  } else if (draft && draft.pick) {
    ans = { ref: item.ref, kind: item.kind, answer: draft.pick };
  }
  if (ans && count && ans.answer !== 'later') ans.attachments = count;
  return ans;
}

// The lines the chief reads for an applied answer, its files' paths under it
// (this goes to the chief's terminal, never GitHub); null for a snooze, which
// the chief has nothing to do about.
export function ownerMessageLine(item, ans, files) {
  const what = item.title ? ' ' + item.title : '';
  let line = null;
  if (ans.answer === 'validated') {
    line = '[' + item.ref + '] Validated:' + what + (ans.text ? ' (note: ' + ans.text + ')' : '');
  } else if (ans.answer === 'done') {
    line = '[' + item.ref + '] Before-start step done:' + what;
  } else if (ans.answer === 'problem') {
    line = '[' + item.ref + '] Problem found validating' + what + ': ' + ans.text + ' (please file a fix)';
  }
  return line === null ? null : [line].concat(attachedLines(files)).join('\n');
}

function paint(block, draft) {
  block.querySelectorAll('.tr-ask-opt').forEach(function (b) {
    b.setAttribute('aria-pressed', draft.pick === b.dataset.answer ? 'true' : 'false');
  });
}

// The to-validate item's text field: a problem, or a note ("works" validates),
// with a line saying which way it will go, the item's dictation (`decorate`)
// and its paperclip. `hooks.edited` clears the last error.
function problemField(block, item, draft, hooks) {
  const input = el('input', 'tr-ask-input');
  input.type = 'text';
  input.maxLength = 2000;
  input.placeholder = 'A problem, or a note ("works" validates)';
  input.setAttribute('aria-label', 'Problem or note for ' + item.ref);
  input.value = draft.problem;
  // What the typed text will send, said before Done.
  const meaning = el('p', 'tr-ask-hint owner-answer-meaning');
  meaning.setAttribute('aria-live', 'polite');
  const explain = function () {
    const ans = ownerAnswerOf(item, { problem: draft.problem });
    meaning.hidden = !ans;
    meaning.textContent = !ans ? ''
      : ans.answer === 'validated' ? 'Sends Validated, with this as a note.'
        : 'Sends a problem report: the chief files a fix.';
  };
  const typed = function () {
    draft.problem = input.value;
    if (input.value.trim() && draft.pick) {
      draft.pick = null;
      paint(block, draft);
    }
    hooks.edited();
    explain();
    hooks.onChange();
  };
  input.addEventListener('input', typed);
  const other = el('div', 'tr-ask-other');
  other.appendChild(input);
  if (hooks.decorate) hooks.decorate(other, input, typed);
  hooks.files.wirePaste(input);
  other.appendChild(hooks.files.button);
  return { input: input, other: other, meaning: meaning, explain: explain };
}

// One owner check in the answer sheet: its kind, repo and ref, the title as
// the question, why and what to try, its links, Validated / Done and Later,
// and (to validate) a Problem field. `decorate(input, onInput)` adds the
// field's dictation, which the sheet owns, as `files` (answer-files.js) adds
// the item's paperclip and chips.
export function ownerSheetBlock(item, draft, onChange, decorate, files) {
  const block = el('section', 'tr-ask chief-answer owner-answer');
  block.dataset.key = ownerSheetKey(item);
  const head = el('div', 'tr-ask-head chief-answer-head');
  head.appendChild(el('span', 'tr-ask-header', kindLabel(item.kind)));
  head.appendChild(el('span', 'tr-ask-header chief-answer-repo', item.repo));
  const ref = el('a', 'chief-answer-ref', item.ref);
  ref.href = item.url;
  ref.target = '_blank';
  ref.rel = 'noopener';
  head.appendChild(ref);
  block.appendChild(head);
  block.appendChild(el('p', 'tr-ask-question', item.title || item.ref));
  details(item, 'tr-ask-hint owner-answer-line').forEach(function (n) { block.appendChild(n); });
  block.appendChild(links(item));

  let input = null;
  const list = el('div', 'tr-ask-options');
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', 'Answer ' + item.ref);
  OPTIONS[item.kind].forEach(function (opt, i) {
    const b = askOption({ n: i + 1, label: opt.label, description: opt.description, mark: true,
      track: 'board.owner-check-option' });
    b.dataset.answer = opt.answer;
    b.addEventListener('click', function () {
      draft.pick = draft.pick === opt.answer ? null : opt.answer;
      // One answer per item: a pick replaces a typed problem.
      if (draft.pick && input) {
        draft.problem = '';
        input.value = '';
      }
      paint(block, draft);
      onChange();
    });
    list.appendChild(b);
  });
  block.appendChild(list);

  // Why the last Done could not apply this answer; any edit clears it.
  const error = el('p', 'tr-ask-hint owner-answer-error', draft.error ? 'Not saved: ' + draft.error : '');
  error.setAttribute('role', 'status');
  error.hidden = !draft.error;
  const edited = function () {
    draft.error = '';
    error.hidden = true;
  };
  list.addEventListener('click', edited);

  if (item.kind === VALIDATE_AFTER) {
    const field = problemField(block, item, draft, { onChange: onChange, edited: edited, decorate: decorate, files: files });
    input = field.input;
    block.appendChild(field.other);
    block.appendChild(files.extra);
    block.appendChild(field.meaning);
    field.explain();
  } else {
    // A before-start step has no text field: its paperclip stands alone.
    const other = el('div', 'tr-ask-other');
    other.appendChild(files.button);
    block.appendChild(other);
    block.appendChild(files.extra);
  }
  block.appendChild(error);
  paint(block, draft);
  return block;
}

// Answers per request: well under the route's own cap, so one request's
// GitHub writes (a gh call each, seconds apiece) finish within a phone's
// patience.
export const OWNER_ANSWERS_PER_REQUEST = 10;

// POST the sheet's owner-check answers (passkey-gated: GitHub writes), one
// request per OWNER_ANSWERS_PER_REQUEST, one after another. Never rejects: it
// resolves to `{results: [{ref, ok, error?}], owner_checks}` with a result per
// answer, a request that failed outright failing each of its answers with its
// reason. `owner_checks` is the last list a request returned, else null.
export async function postOwnerAnswers(answers) {
  const results = [];
  let ownerChecks = null;
  for (let at = 0; at < answers.length; at += OWNER_ANSWERS_PER_REQUEST) {
    const chunk = answers.slice(at, at + OWNER_ANSWERS_PER_REQUEST);
    try {
      const res = await terminalJsonApi('/api/board/owner-checks/answer', { method: 'POST', body: { answers: chunk } });
      const got = (res && res.results) || [];
      chunk.forEach(function (a) {
        const r = got.find(function (y) { return y.ref === a.ref; });
        results.push(r || { ref: a.ref, ok: false, error: 'no result came back' });
      });
      if (res && res.owner_checks) ownerChecks = res.owner_checks;
    } catch (exc) {
      const why = (exc && exc.message) || String(exc);
      chunk.forEach(function (a) { results.push({ ref: a.ref, ok: false, error: why }); });
    }
  }
  return { results: results, owner_checks: ownerChecks };
}
