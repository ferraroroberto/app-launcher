/* The transcript's step lines: a run of folded entries as one quiet line
 * in plain words, the items that hang off its rail, the step in progress,
 * a step's diff, and an opened step's card. transcript-render.js places
 * them between a turn's replies.
 */

import { sessionRoute } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { icon } from '../_vendored/icons/icons.js';
import { renderHunks } from '../changes/diff-view.js';
import { baseName, deltaEl, fileParts, firstLine, fmtTime, pre } from './transcript-text.js';
import { thumbs } from './transcript-media.js';

// How far the open session's harness can be trusted to mark a failed tool
// call — the server's per-flavour `tool_errors`. Only a `reported`
// harness lets an unmarked call be read as "it worked"; for the other two
// values an unmarked call is "nobody recorded the outcome", which the
// expanded card says in as many words rather than leaving silence to pass
// for success. `reported` is the fallback for a page that predates the
// field only because that is also what Claude — the default flavour —
// reports.
const TOOL_ERRORS_NOTE = {
  partial: 'This agent doesn’t record every tool failure — an unmarked call may still have failed.',
  none: 'This agent doesn’t record whether a tool call failed.',
};

export function toolOutcomeNote(e, toolErrors) {
  if (e.error === true) return '';
  if (e.kind !== 'tool_call' && e.kind !== 'tool_result') return '';
  return TOOL_ERRORS_NOTE[toolErrors] || '';
}

const KIND_ICON = {
  tool_call: 'zap',
  tool_result: 'terminal',
  thinking: 'sparkle',
  system: 'plug',
  user: 'messages-square',
  assistant: 'messages-square',
};

// A step item's verb glyph: what the call did — run, read, edit,
// search — rather than one terminal glyph for every tool.
const VERB_ICON = {
  ran: 'terminal',
  read: 'book-open',
  edited: 'pencil',
  wrote: 'pencil',
  deleted: 'trash-2',
};

function itemGlyph(e) {
  if (e.kind === 'tool_call') {
    const verb = e.action && e.action.verb;
    if (VERB_ICON[verb]) return VERB_ICON[verb];
    if (/grep|glob|search|find/i.test(e.name || '')) return 'search';
  }
  return KIND_ICON[e.kind] || 'plug';
}
// --- an opened step ----------------------------------------------
//
// A card on the code-listing frame: a small header (the tool, the file as
// "folder / name", a recorded exit status), then the call's input and its
// output as separate blocks. A command reads `$ command` with light shell
// highlighting. Escape-first: the command is cut into tokens and every token
// is a text node or a span's textContent, never markup, so a command holding
// `<script>` reads as exactly that.

// One token per match, in order, and every character lands in one: the last
// alternative takes whatever the others don't, so the tokens always rejoin
// to the command.
const SHELL_TOKEN_RE = /\s+|'[^']*'?|"(?:\\.|[^"\\])*"?|\d*>>?(?:&\d+)?|&&|\|\||\$\(|[|;&<()]|\$\{[^}]*\}?|\$(?:env:)?\w+|[^\s'"|;&<>()$]+|[\s\S]/g;
// After these the next word is a command again.
const SHELL_CMD_AFTER = new Set(['&&', '||', '|', ';', '&', '$(', '(']);

function shellTokens(command) {
  const out = [];
  let expectCmd = true;
  const re = new RegExp(SHELL_TOKEN_RE.source, 'g');
  let m;
  while ((m = re.exec(command))) {
    const t = m[0];
    let kind = '';
    if (/^\s/.test(t)) {
      if (t.indexOf('\n') !== -1) expectCmd = true;
    } else if (t.charAt(0) === '\'' || t.charAt(0) === '"') {
      kind = 'str';
      expectCmd = false;
    } else if (SHELL_CMD_AFTER.has(t)) {
      kind = 'op';
      expectCmd = true;
    } else if (/^\d*>|^[<)]$/.test(t)) {
      kind = 'op';
    } else if (t.charAt(0) === '$' && t.length > 1) {
      kind = 'var';
      expectCmd = false;
    } else if (expectCmd && !/^\w+=/.test(t)) {
      kind = 'cmd';
      expectCmd = false;
    } else if (t.charAt(0) === '-' && t.length > 1) {
      kind = 'flag';
    }
    out.push([kind, t]);
  }
  return out;
}

function cmdBlock(command) {
  const el = document.createElement('pre');
  el.className = 'tr-cmd';
  const prompt = document.createElement('span');
  prompt.className = 'tr-cmd-prompt';
  prompt.setAttribute('aria-hidden', 'true');
  prompt.textContent = '$ ';
  const text = document.createElement('span');
  text.className = 'tr-cmd-text';
  shellTokens(command).forEach(function (tok) {
    if (!tok[0]) {
      text.append(tok[1]);
      return;
    }
    const span = document.createElement('span');
    span.className = 'sh-' + tok[0];
    span.textContent = tok[1];
    text.appendChild(span);
  });
  el.append(prompt, text);
  return el;
}

// Claude opens a failed command's output with "Exit code N" (every failed
// Bash result on the dev box did); no other harness records one. The code
// moves to the header, where it reads quietly, instead of leading the output.
const EXIT_LINE_RE = /^Exit code (\d+)[ \t]*(?:\r?\n|$)/;

function stepCard(e, session) {
  const a = e.action;
  const ran = !!(a && a.verb === 'ran');
  const card = document.createElement('div');
  card.className = 'tr-cmd-card';
  const head = document.createElement('div');
  head.className = 'tr-cmd-head';
  const tool = document.createElement('span');
  tool.className = 'tr-cmd-tool';
  tool.textContent = e.name || 'tool';
  head.appendChild(tool);
  if (a && a.path) {
    // "folder / name", never the whole absolute path; that stays on hover.
    const f = fileParts(a.path, session);
    const where = document.createElement('span');
    where.className = 'tr-cmd-path';
    where.title = a.path;
    where.textContent = f.dir ? f.dir + ' / ' + f.base : f.base;
    head.appendChild(where);
  }
  let result = e.result;
  const exit = ran && e.error === true && result != null ? EXIT_LINE_RE.exec(result) : null;
  if (exit) {
    result = result.slice(exit[0].length);
    const code = document.createElement('span');
    code.className = 'tr-cmd-exit';
    code.textContent = 'exit ' + exit[1];
    head.appendChild(code);
  }
  card.appendChild(head);
  if (ran) card.appendChild(cmdBlock(a.command));
  else if (!(a && a.path) && e.summary) card.appendChild(pre(e.summary, false, 'tr-cmd tr-cmd-plain'));
  if (result != null) {
    card.appendChild(pre(result, e.result_truncated, 'tr-out'));
  } else {
    const none = document.createElement('div');
    none.className = 'tr-trunc tr-out-none';
    none.textContent = 'no result on this page';
    card.appendChild(none);
  }
  return card;
}
// --- the step in progress ------------------------------------------
//
// A step line whose newest call is still running shows a counter in its
// duration's place. Which call that is, and the tick that redraws it, are the
// Chat pane's (chat-activity.js liveStep / syncLiveStep).

// "1:05", "1:02:03": an elapsed counter, as the strip's activity line reads.
export function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = function (n) { return String(n).padStart(2, '0'); };
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return (h ? h + ':' + pad(m) : String(m)) + ':' + pad(s % 60);
}

function liveStepEl(e) {
  const el = document.createElement('span');
  el.className = 'tr-step-live';
  el._trEntry = e;
  el.innerHTML = icon('timer', 'tr-step-live-icon');
  const text = document.createElement('span');
  text.className = 'tr-step-live-text';
  text.textContent = fmtElapsed(Date.now() - Date.parse(e.timestamp));
  el.appendChild(text);
  return el;
}

// --- per-step diffs -------------------------------------------------
//
// An edit/write call's `action.diff` carries the page's share of its diff
// (capped per step, server-side); the rest is one request away through the
// step's own ref ({offset, n}). A failed call carries no diff and keeps
// today's body, as does any call whose tool the server doesn't recognise.

// `bare` leaves the "folder / name" header off: the Changed files panel's
// This turn scope draws a step under the file's own row. `session`
// shortens the path and serves the full diff (null: neither).
export function stepDiff(e, bare, session) {
  const a = e.action;
  if (!a || !a.diff || e.error === true || (a.verb !== 'edited' && a.verb !== 'wrote')) return null;
  const wrap = document.createElement('div');
  wrap.className = 'tr-diff-wrap';
  if (!bare) {
    // Headed "folder / name"; the whole path stays on hover.
    const path = document.createElement('div');
    path.className = 'tr-diff-path';
    path.title = a.path || '';
    const f = fileParts(a.path, session);
    if (f.dir) {
      const dir = document.createElement('span');
      dir.className = 'tr-diff-dir';
      dir.textContent = f.dir;
      path.append(dir, ' / ');
    }
    const base = document.createElement('span');
    base.className = 'tr-diff-base';
    base.textContent = f.base;
    path.appendChild(base);
    wrap.appendChild(path);
  }
  wrap.appendChild(renderHunks(a.diff, a.path));
  if (a.diff.truncated) wrap.appendChild(fullDiffControl(wrap, a.diff, a.path, session ? session.session_id : null));
  return wrap;
}

function truncNote(text) {
  const note = document.createElement('div');
  note.className = 'tr-trunc tr-diff-note';
  note.setAttribute('role', 'status');
  note.textContent = text;
  return note;
}

// "Show full diff" in Chat; outside it (the Life OS viewer has no session
// to ask) just the note that there is more.
function fullDiffControl(wrap, diff, path, sid) {
  if (!sid || diff.offset == null) return truncNote('Diff truncated');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'button-tint tr-diff-more';
  btn.textContent = 'Show full diff';
  btn.addEventListener('click', async function (ev) {
    ev.stopPropagation();
    btn.disabled = true;
    btn.textContent = 'Loading…';
    let body = null;
    try {
      body = await terminalJsonApi(
        sessionRoute(
          sid,
          '/transcript/diff?offset=' + encodeURIComponent(diff.offset) +
            '&n=' + encodeURIComponent(diff.n || 0)
        )
      );
    } catch (exc) {
      body = null;
    }
    if (!body || !body.available || !body.diff) {
      btn.replaceWith(truncNote('Couldn’t load the full diff — showing the first part'));
      return;
    }
    wrap.querySelector('.tr-diff').replaceWith(renderHunks(body.diff, path));
    if (body.diff.truncated) btn.replaceWith(truncNote('Diff truncated at 200 KB'));
    else btn.remove();
  });
  return btn;
}

// One folded item hanging off a step line's rail — its own <details>, so a
// single tool call can be opened without expanding its siblings. `opts` is
// appendEntries' (its toolErrors and session).
export function renderItem(e, opts) {
  const d = document.createElement('details');
  d.className = 'tr-item tr-item-' + e.kind;
  tagKey(d, e, 'item');
  // A failed call is marked on the row itself, so it reads as failed while
  // the group is still folded open at one item — never as a banner over the
  // whole turn.
  if (e.error === true) d.classList.add('tr-item-failed');
  const s = document.createElement('summary');
  s.innerHTML = icon(itemGlyph(e));
  const name = document.createElement('span');
  name.className = 'tr-item-name';
  const hint = document.createElement('span');
  hint.className = 'tr-item-hint';
  let delta = null;
  if (e.kind === 'tool_call') {
    // Plain words where the server knows the tool: the command's
    // first line, or the file's name with its folder as the hint
    // and, for an edit, its +N −M.
    // A call that carries its own description is named by it, its
    // command the hint.
    const a = e.action;
    if (a && a.verb === 'ran' && described(e)) {
      name.textContent = e.label;
      hint.textContent = firstLine(a.command, 80);
      hint.classList.add('tr-item-cmd');
    } else if (a && a.verb === 'ran') {
      name.textContent = firstLine(a.command, 80);
      name.classList.add('tr-item-cmd');
    } else if (a && (a.verb === 'edited' || a.verb === 'wrote' || a.verb === 'read')) {
      const f = fileParts(a.path, opts.session);
      name.textContent = f.base;
      hint.textContent = f.dir;
      s.title = a.path;
      if (a.verb !== 'read' && e.error !== true) delta = deltaEl(a.added, a.removed, 'tr-item-delta');
    } else {
      name.textContent = e.name || 'tool';
      hint.textContent = e.label || e.summary || '';
    }
  } else if (e.kind === 'tool_result') {
    name.textContent = 'result';
    hint.textContent = firstLine(e.text, 80);
  } else if (e.kind === 'thinking') {
    name.textContent = 'thinking';
    hint.textContent = firstLine(e.text, 80);
  } else if (e.sidechain) {
    name.textContent = 'sub-agent';
    hint.textContent = firstLine(e.text, 80);
  } else {
    name.textContent = e.label || 'system';
    hint.textContent = firstLine(e.text, 80);
  }
  // Name, then the hint taking what room is left; the failed chip and the
  // counts trail it, non-shrinking, so the ellipsis only ever cuts the hint.
  s.append(name, hint);
  if (e.error === true) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-chip';
    chip.textContent = 'failed';
    s.appendChild(chip);
  }
  if (delta) s.appendChild(delta);
  d.appendChild(s);
  const body = document.createElement('div');
  body.className = 'tr-item-body';
  if (e.kind === 'tool_call') {
    // An edit opens as its diff and nothing else: the tool's own
    // "file has been updated" line was a second box saying less.
    // Anything else opens as a card: header, input, output.
    const diff = stepDiff(e, false, opts.session);
    if (diff) {
      body.appendChild(diff);
    } else {
      body.appendChild(stepCard(e, opts.session));
      const shots = e.result != null ? thumbs(e.result_images, opts.session) : null;
      if (shots) body.appendChild(shots);
    }
  } else {
    body.appendChild(pre(e.text, e.truncated));
    const shots = thumbs(e.images, opts.session);
    if (shots) body.appendChild(shots);
  }
  // The honest third state: this harness cannot say whether the call
  // failed. Said here, in the body, rather than as a marker on every row —
  // it is the answer to "did this work?", and that question is asked at the
  // moment the card is opened, not while it is folded.
  const note = toolOutcomeNote(e, opts.toolErrors);
  if (note) {
    const unknown = document.createElement('div');
    unknown.className = 'tr-outcome-unknown';
    unknown.textContent = note;
    body.appendChild(unknown);
  }
  d.appendChild(body);
  return d;
}

// A run in plain words: [verb, object] pairs that read
// "Edited main.js, ran 2 commands, read 3 files", plus the edits' total +N −M.
// The total is not label text: the step line gives it its own element,
// which the label's ellipsis can't reach. Built from each call's server-side
// `action`; a call without one is counted as before.
function runSummary(run) {
  let ran = 0;
  let other = 0;
  let thinking = 0;
  let system = 0;
  const edited = {};
  const read = {};
  run.forEach(function (e) {
    const a = e.kind === 'tool_call' ? e.action : null;
    if (a && a.verb === 'ran') {
      ran += 1;
    } else if (a && (a.verb === 'edited' || a.verb === 'wrote') && e.error !== true) {
      // A failed edit changed nothing: it counts as a call, never toward the
      // +N −M, the rule the Changed files panel's totals follow.
      const sum = edited[a.path] || (edited[a.path] = { added: 0, removed: 0 });
      sum.added += a.added || 0;
      sum.removed += a.removed || 0;
    } else if (a && a.verb === 'read') {
      read[a.path] = true;
    } else if (e.kind === 'tool_call' || e.kind === 'tool_result') {
      other += 1;
    } else if (e.kind === 'thinking') {
      thinking += 1;
    } else {
      system += 1;
    }
  });
  // Edits lead: they are what the trailing +N −M counts, and at phone width
  // the label's start is the part that survives the ellipsis.
  const parts = [];
  const edits = Object.keys(edited);
  let total = null;
  if (edits.length) {
    total = edits.reduce(function (s, path) {
      return { added: s.added + edited[path].added, removed: s.removed + edited[path].removed };
    }, { added: 0, removed: 0 });
    parts.push(['edited', edits.length === 1 ? baseName(edits[0]) : edits.length + ' files']);
  }
  if (ran) parts.push(['ran', ran === 1 ? 'a command' : ran + ' commands']);
  const reads = Object.keys(read);
  if (reads.length) parts.push(['read', reads.length === 1 ? baseName(reads[0]) : reads.length + ' files']);
  if (other) {
    const word = parts.length ? ' other tool call' : ' tool call';
    parts.push(['', other + word + (other === 1 ? '' : 's')]);
  }
  if (thinking) parts.push(['', thinking + ' thinking']);
  if (system) parts.push(['', system + ' system']);
  return { parts: parts, total: total };
}

// Whether a call carries its own description: the server's `label`
// is the description when the harness recorded one, else the summary.
function described(e) {
  return !!e.label && e.label !== e.summary;
}

// One step in plain words: its description, else what it did
// ("Ran pytest -q", "Edited main.js"), else the tool and its summary.
function stepText(e) {
  if (described(e)) return e.label;
  const a = e.action;
  if (a && a.verb === 'ran') return firstLine(a.command, 80);
  if (a && a.path && VERB_ICON[a.verb]) {
    return a.verb.charAt(0).toUpperCase() + a.verb.slice(1) + ' ' + baseName(a.path);
  }
  const what = firstLine(e.label || e.summary, 80);
  return (e.name || 'tool') + (what ? ' ' + what : '');
}

// The newest tool call a run holds, or null.
function latestCall(run) {
  for (let i = run.length - 1; i >= 0; i -= 1) {
    if (run[i].kind === 'tool_call') return run[i];
  }
  return null;
}

// The label: the newest step in plain words, then, for a run of
// more than one entry, the counts in muted secondary text. A run with no
// tool call keeps the counts alone. Counts: verbs muted, what they acted on
// in the text colour while they lead the line.
function fillStepLabel(el, parts, latest) {
  el.textContent = '';
  let counts = el;
  if (latest) {
    const now = document.createElement('span');
    now.className = 'tr-step-now';
    now.textContent = stepText(latest);
    el.appendChild(now);
    if (!parts.length) return;
    counts = document.createElement('span');
    counts.className = 'tr-step-counts';
    counts.append(' · ');
    el.appendChild(counts);
  }
  parts.forEach(function (p, i) {
    if (i) counts.append(', ');
    const verb = i === 0 ? p[0].charAt(0).toUpperCase() + p[0].slice(1) : p[0];
    if (verb) counts.append(verb + ' ');
    const what = document.createElement('b');
    what.textContent = p[1];
    counts.appendChild(what);
  });
}

function failedCount(run) {
  return run.reduce(function (n, e) { return n + (e.error === true ? 1 : 0); }, 0);
}

// "14s", "1m 52s", "1h 5m"; empty under a second.
function fmtSpan(ms) {
  const s = Math.round(ms / 1000);
  if (!(s >= 1)) return '';
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

// How long a run took: from its first entry to the reply that follows it in
// the turn, or, with none yet, to its own last entry. Empty when either end
// has no usable timestamp.
function runSpan(li) {
  const run = li._trRun;
  const next = li.nextElementSibling;
  const end = next && next._trEntry ? next._trEntry : run[run.length - 1];
  return fmtSpan(Date.parse(end.timestamp) - Date.parse(run[0].timestamp));
}

// "07:30", or "Oct 30, 07:30" when the day is not today.
function fmtWhen(ts) {
  const d = new Date(ts);
  const time = fmtTime(ts);
  if (!time) return '';
  if (d.toDateString() === new Date().toDateString()) return time;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + time;
}

// Where an opened run sits: when it started, how long it ran, and how far
// into the session that was. Each part is left out when its timestamps don't
// parse, so the line never reads "NaN" or a made-up zero. `opts.sessionStart`
// (ms, or null) is the open session's; the Life OS viewer has none, and its
// line carries no offset.
function runWhen(li) {
  const first = li._trRun[0];
  const start = Date.parse(first.timestamp);
  if (Number.isNaN(start)) return '';
  const parts = [fmtWhen(first.timestamp)];
  const span = runSpan(li);
  if (span) parts.push('lasted ' + span);
  const origin = li._trSessionStart ? li._trSessionStart() : null;
  if (origin != null && start >= origin) {
    const into = fmtSpan(start - origin);
    parts.push(into ? '+' + into + ' into the session' : 'at the session start');
  }
  return parts.join(' · ');
}

// Filled only while the run is open: the session start it measures from can
// move (older pages joining the list), and a folded run shows nothing anyway.
function syncRunWhen(li) {
  const d = li.querySelector(':scope > .tr-step');
  const el = d.querySelector(':scope > .tr-step-items > .tr-run-when');
  const text = d.open ? runWhen(li) : '';
  el.textContent = text;
  el.hidden = !text;
}

// A run of consecutive folded entries → one quiet step line: a
// chevron, the label, then the parts no ellipsis may cut. Open, its items
// hang off a rail beneath it.
export function renderRun(run, opts) {
  const li = document.createElement('li');
  li.className = 'tr-run';
  // The entries this line holds, kept on the node so a later tick can merge
  // more into it and recompute the summary.
  li._trRun = run.slice();
  li._trSessionStart = opts.sessionStart || null;
  const d = document.createElement('details');
  d.className = 'tr-step';
  tagKey(d, run[0], 'step');
  const s = document.createElement('summary');
  s.className = 'tr-step-line';
  s.innerHTML = icon('chevron-right', 'tr-step-chevron');
  const label = document.createElement('span');
  label.className = 'tr-step-label';
  s.appendChild(label);
  d.appendChild(s);
  const body = document.createElement('div');
  body.className = 'tr-step-items';
  const when = document.createElement('div');
  when.className = 'tr-run-when';
  when.hidden = true;
  body.appendChild(when);
  run.forEach(function (e) { body.appendChild(renderItem(e, opts)); });
  d.appendChild(body);
  d.addEventListener('toggle', function () { syncRunWhen(li); });
  li.appendChild(d);
  syncRunSummary(li, opts.live);
  return li;
}

// --- live refresh rendering ---------------------------------------
//
// Every disclosure in a live region is tagged with the byte offset of the
// entry it renders, so an open card can be found again after the region is
// rebuilt. Offsets are stable identities: an entry keeps the offset of the
// line it started at for as long as that file is not rewritten.
//
// The key is namespaced by what kind of disclosure it is: a turn that
// opens with a tool call, its step line and the call's own item all render
// the same entry, so a bare offset let a folded step line's state be restored
// onto the open turn and a new reply arrived folded.

export function tagKey(el, entry, kind) {
  if (entry && entry.offset != null) el.dataset.trKey = kind + ':' + entry.offset;
}

// A step line, recomputed from the entries it now holds and from what
// follows it — called on every merge and every turn-head sync, so a run
// that grew keeps an honest count and its duration ends at the reply that
// arrived after it. The trailing parts (total +N −M, "N failed", duration)
// are rebuilt in that order; a failure has to read while the line is
// closed, and none of them may be cut by the label's ellipsis. `live` is
// the step in progress (null: none).
export function syncRunSummary(li, live) {
  const run = li._trRun || [];
  const line = li.querySelector('.tr-step-line');
  syncRunWhen(li);
  const sum = runSummary(run);
  const latest = latestCall(run);
  fillStepLabel(line.querySelector('.tr-step-label'), latest && run.length < 2 ? [] : sum.parts, latest);
  line.querySelectorAll(':scope > .tr-step-delta, :scope > .tr-fail-count, :scope > .tr-step-dur, :scope > .tr-step-live')
    .forEach(function (n) { n.remove(); });
  if (sum.total) line.appendChild(deltaEl(sum.total.added, sum.total.removed, 'tr-step-delta'));
  const failed = failedCount(run);
  if (failed) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-count';
    chip.textContent = failed + ' failed';
    line.appendChild(chip);
  }
  // The step in progress counts up in the duration's place.
  if (live && run.indexOf(live) !== -1) {
    line.appendChild(liveStepEl(live));
    return;
  }
  const span = runSpan(li);
  if (span) {
    const dur = document.createElement('span');
    dur.className = 'tr-step-dur';
    dur.textContent = span;
    line.appendChild(dur);
  }
}
