/* Owner checks on the Board: issues that wait on the owner.
 *
 * Two kinds, from fleet-config's owner_check labels (GET /api/board's
 * `owner_checks` section): shipped work to validate as a user
 * (`validate-after`) and a step owed before work can start (`before-start`).
 * They sit in Your turn under the live sessions, one fold per kind. A row's
 * tap unfolds its own detail in place: why it needs you, what to try, and the
 * links to the issue and its proof. The detail's Answer, like its group's
 * own button, opens that group's owner-check answer sheet (board-answers.js), where this
 * module draws each item's block (the options and Other row of a chief question,
 * answer-item.js) and reads its answer back.
 *
 * The list is a cached read with its age. Before one has landed it is
 * unknown, never "none"; repos the list could not read make it a lower bound,
 * and the group says so. An item answered Pending, Pause, Close or Problem keeps
 * its row, marked, and leaves the "needs you" count.
 */

import { els } from '../state.js';
import { chip } from '../glance.js';
import { actionRow } from '../action-rows.js';
import { el } from '../dom-utils.js';
import { icon } from '../_vendored/icons/icons.js';
import { terminalJsonApi } from '../webauthn.js';
import { boardRepoFilter, matchesRepoFilter } from './board-dispatch.js';
import { agoText, foldGroup } from './chief-plan.js';
import { calendarDaysFromToday, newestDateFirst, shortDate } from '../shared/format.js';
import { plural, renderBoard } from '../board.js';
import { ownerItemWaiting, ownerNeedsCount, ownerSection } from './owner-needs.js';
import { bucketName } from './plan-buckets.js';
import { attachedLines, emptyFiles } from './answer-files.js';
import { optionList, otherRow, paintOptions } from './answer-item.js';

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
// `kind`, when given, keeps one group's items: the sheet a group opens.
export function shownOwnerItems(body, kind) {
  const sec = section(body);
  if (!sec) return [];
  const filter = boardRepoFilter();
  const items = (sec.items || []).filter(function (it) {
    return matchesRepoFilter(it, filter) && (!kind || it.kind === kind);
  });
  return items.filter(waiting).sort(newestFirst)
    .concat(items.filter(function (it) { return !waiting(it); }).sort(newestFirst));
}

export function kindLabel(kind) {
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
  if (item.mark === 'paused') return 'pause asked, waiting on the chief';
  if (item.mark === 'closing') return 'close asked, waiting on the chief';
  // Open, unmarked, yet not counted: its issue sits outside Waiting on you.
  if (item.counted === false && item.bucket === 'absent') return 'not in the Backlog yet, not counted';
  if (item.counted === false) return bucketName(item.bucket).toLowerCase() + ', not counted';
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
  answer.addEventListener('click', function () { onAnswer(item.ref, item.kind); });
  detail.appendChild(answer);
  row.li.appendChild(detail);
  return row.li;
}

// One kind's fold: its rows, then its own answer button (inside the fold, so
// a folded group carries nothing but its heading and count). Open by default.
function group(g, items, onAnswer) {
  const rows = items.map(function (it) { return itemRow(it, onAnswer); });
  const waitingItems = items.filter(waiting);
  const after = [];
  if (waitingItems.length) {
    const btn = el('button', 'button-tint board-plan-answer board-owner-answer-group');
    btn.type = 'button';
    btn.innerHTML = icon('messages-square') + ' ';
    btn.appendChild(document.createTextNode('Answer ' + g.label + ' (' + waitingItems.length + ')'));
    if (g.kind === BEFORE_START) btn.dataset.track = 'board.owner-answer-before-start';
    else btn.dataset.track = 'board.owner-answer-validate';
    btn.addEventListener('click', function () { onAnswer(waitingItems[0].ref, g.kind); });
    after.push(btn);
  }
  const fold = { key: 'owner:' + g.kind, heading: g.heading + ' (' + items.length + ')', rows: rows,
    after: after, cls: 'board-owner-group' };
  const wrap = g.kind === BEFORE_START
    ? foldGroup({ ...fold, track: 'board.owner-before-start-fold' })
    : foldGroup({ ...fold, track: 'board.owner-validate-fold' });
  wrap.dataset.kind = g.kind;
  return wrap;
}

// The list's freshness and gaps, said every time: its age, a failed
// refresh, unreadable repos. Never a bare empty list.
function stateLine(sec, body) {
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
  const turn = (body && body.backlog_buckets && body.backlog_buckets.your_turn) || {};
  if (turn.unlisted) parts.push(turn.unlisted + ' waiting on you not in this list yet');
  return parts.join(' · ');
}

// Fills Your turn's owner block; returns how many waiting items it shows, for
// the lane's count. `onAnswer(ref, kind)` opens that group's owner-check sheet on that item.
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
    body && body.backlog_buckets && body.backlog_buckets.your_turn,
    agoText(sec && sec.fetched_at), boardRepoFilter()]);
  if (host.dataset.sig === sig) return waitingCount;
  host.dataset.sig = sig;
  // The state line leads, where the Chief's plan card puts its age line.
  const line = el('p', 'board-plan-line muted small board-owner-state', stateLine(sec, body));
  if (sec && sec.error) line.title = sec.error;
  const nodes = [line];
  GROUPS.forEach(function (g) {
    const mine = items.filter(function (it) { return it.kind === g.kind; });
    if (mine.length) nodes.push(group(g, mine, onAnswer));
  });
  host.replaceChildren.apply(host, nodes);
  return waitingCount;
}

// ---------------------------------------------------------------- the sheet

const OPTIONS = {
  [VALIDATE_AFTER]: [
    { answer: 'validated', label: 'Validated', description: 'It works: removes the label and notes it on the issue' },
    { answer: 'problem', label: 'Problem found', description: 'Describe it in Other: the chief files a fix' },
  ],
  [BEFORE_START]: [
    { answer: 'done', label: 'Done', description: 'The step is done: removes the label so the work can start' },
    { answer: 'pending', label: 'Pending', description: 'Not yet: asks again tomorrow' },
    { answer: 'pause', label: 'Pause', description: 'Hold the work: the chief pauses it' },
    { answer: 'close', label: "Close (won't do)", description: 'Drop it: the chief closes the issue' },
  ],
};

const OPTION_LABEL = Object.fromEntries(
  Object.values(OPTIONS).flat().map(function (o) { return [o.answer, o.label]; }));

export function emptyOwnerDraft() {
  // `other`: the Other field's text. `error`: why the last Done could not apply
  // this answer, shown in its block. `files`: [{ name, path }] stored on the
  // chief's session; never posted to GitHub.
  return { pick: null, other: '', error: '', files: emptyFiles() };
}

export function ownerSheetKey(item) {
  return 'owner:' + item.ref;
}

// Words that only say an item works. Text made of these alone (with at least one
// AFFIRMS word), typed with no option picked on an item to validate, validates
// it; anything else, a negation included, is a problem report.
const AFFIRMS = ['validated', 'validate', 'works', 'working', 'worked', 'ok', 'okay', 'fine', 'good',
  'great', 'perfect', 'lgtm', 'confirmed', 'verified', 'tested', 'yes', 'yep', 'done', 'correct'];
const FILLER = ['it', 'all', 'looks', 'everything', 'now', 'well', 'and', 'as', 'expected',
  'correctly', 'here', 'on', 'phone', 'desktop', 'both', 'really', 'very', 'seems', 'is'];

export function affirmsItWorks(text) {
  const words = String(text || '').toLowerCase().split(/[^a-z]+/).filter(Boolean);
  return words.some(function (w) { return AFFIRMS.indexOf(w) !== -1; })
    && words.every(function (w) { return AFFIRMS.indexOf(w) !== -1 || FILLER.indexOf(w) !== -1; });
}

function otherText(draft) {
  return String((draft && draft.other) || '').split(/\s+/).filter(Boolean).join(' ');
}

// One item's answer, or null. A picked option is the answer and the Other text
// rides with it as its note; a Problem found needs that text, so without it
// there is no answer yet. With no pick, the text alone answers: an item to
// validate reads it (works, or a problem), a before-start step takes it as a
// Pending note. `attachments`, set only when files ride with it, is how many: a
// number, the only thing about them that goes to GitHub. Files with no answer
// make no answer (the draft keeps them).
export function ownerAnswerOf(item, draft) {
  const text = otherText(draft);
  const pick = draft && draft.pick;
  let answer = pick;
  if (!pick && text) {
    answer = item.kind === BEFORE_START ? 'pending' : (affirmsItWorks(text) ? 'validated' : 'problem');
  }
  if (!answer || (answer === 'problem' && !text)) return null;
  const ans = { ref: item.ref, kind: item.kind, answer: answer };
  if (text) ans.text = text;
  const count = ((draft && draft.files) || []).length;
  if (count) ans.attachments = count;
  return ans;
}

// An answer the chief has to act on, so it must be running: a problem to file, a
// pause or close to carry out, or files to store. Validated, Done and Pending
// are complete on the launcher's side.
export function ownerNeedsChief(ans, files) {
  return ans.answer === 'problem' || ans.answer === 'pause' || ans.answer === 'close' || files.length > 0;
}

// What Done will send for the item, said under its Other field; '' when there is
// nothing to say yet.
export function ownerMeaning(item, draft) {
  const text = otherText(draft);
  const ans = ownerAnswerOf(item, draft);
  if (!ans) return draft && draft.pick === 'problem' ? 'Problem found needs a description in Other.' : '';
  if (!text) return '';
  if (!draft.pick && item.kind === VALIDATE_AFTER) {
    return ans.answer === 'validated' ? 'Sends Validated, with this as a note.'
      : 'Sends a problem report: the chief files a fix.';
  }
  if (ans.answer === 'problem') return 'Sends a problem report: the chief files a fix.';
  return 'Sends ' + OPTION_LABEL[ans.answer] + ', with this as a note.';
}

// The lines the chief reads for an applied answer, its files' paths under it
// (this goes to the chief's terminal, never GitHub).
export function ownerMessageLine(item, ans, files) {
  const what = item.title ? ' ' + item.title : '';
  const note = ans.text ? ' (note: ' + ans.text + ')' : '';
  const head = '[' + item.ref + '] ';
  let line;
  if (ans.answer === 'validated') {
    line = head + 'Validated:' + what + note;
  } else if (ans.answer === 'done') {
    line = head + 'Before-start step done:' + what + note;
  } else if (ans.answer === 'problem') {
    line = head + 'Problem found validating' + what + ': ' + ans.text + ' (please file a fix)';
  } else {
    line = head + 'Before-start: ' + ans.answer + (item.title ? ' - ' + item.title : '') + note;
  }
  return [line].concat(attachedLines(files)).join('\n');
}

// One owner check in the answer sheet: its kind, repo and ref, the title as
// the question, why and what to try, its links, then the same option rows and
// Other row a chief question has (answer-item.js). `mic(input, onRender)` adds
// the field's dictation, which the sheet owns, as `files` (answer-files.js)
// adds the item's paperclip and chips.
export function ownerSheetBlock(item, draft, onChange, mic, files) {
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

  const options = OPTIONS[item.kind];
  // What Done will send, said before Done.
  const meaning = el('p', 'tr-ask-hint owner-answer-meaning');
  meaning.setAttribute('aria-live', 'polite');
  const explain = function () {
    const text = ownerMeaning(item, draft);
    meaning.hidden = !text;
    meaning.textContent = text;
  };
  // Why the last Done could not apply this answer; any edit clears it.
  const error = el('p', 'tr-ask-hint owner-answer-error', draft.error ? 'Not saved: ' + draft.error : '');
  error.setAttribute('role', 'status');
  error.hidden = !draft.error;
  const changed = function () {
    draft.error = '';
    error.hidden = true;
    explain();
    onChange();
  };
  const other = otherRow({
    value: draft.other,
    placeholder: 'Other',
    ariaLabel: 'Other answer for ' + item.ref,
    files: files,
    mic: mic,
    onInput: function (field) {
      draft.other = field.value;
      changed();
    },
  });
  const paint = function () {
    paintOptions(block, function (i) { return options[i].answer === draft.pick; });
  };
  block.appendChild(optionList({
    groupLabel: 'Answer ' + item.ref,
    options: options.map(function (o) { return { label: o.label, description: o.description, key: o.answer }; }),
    track: 'board.owner-check-option',
    onPick: function (i) {
      draft.pick = draft.pick === options[i].answer ? null : options[i].answer;
      paint();
      changed();
    },
  }));
  block.appendChild(other.row);
  block.appendChild(other.count);
  block.appendChild(files.extra);
  block.appendChild(meaning);
  block.appendChild(error);
  explain();
  paint();
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
