/* A session's Background tasks sheet (⋮ → Background tasks).
 *
 * Opens over either menu's session, reads GET .../background-tasks once per
 * open and lists what the agent launched in the background: Running first
 * (newest first, with its elapsed time), then Finished N with the status
 * Claude Code reported. A row expands to the task's output tail, read on
 * demand through the same route (?task=<id>); the server only ever reads
 * the session's own task files. Read-only: Claude Code has no way to stop
 * a task from outside its session, so there is no stop control to show.
 *
 * Claude only. `hasBackgroundTasks` is what both ⋮ menus gate the item on.
 */

import { els } from '../state.js';
import { terminalJsonApi } from '../webauthn.js';
import { AuthRequiredError, sessionRoute } from '../api.js';
import { setAsyncState } from '../async-state.js';
import { chip } from '../glance.js';
import { fmtDuration } from '../format.js';
import { icon } from '../_vendored/icons/icons.js';

// Why the server could not read, in the words the sheet shows.
const REASON = {
  unsupported_agent: 'Only Claude sessions list background tasks',
  no_transcript: 'This session has no transcript to read yet',
  session_not_found: 'This session is no longer running',
  session_host_unreachable: 'The session host is not answering',
  read_failed: 'Couldn’t read the transcript',
};
const NO_OUTPUT = 'No output was recorded for this task';

// Chip tone per status that is not a plain success; `completed` shows none.
const STATUS_TONE = { failed: 'danger', killed: 'attention', stopped: 'attention', timeout: 'attention' };

let view = null;   // { sid, seq } while the sheet is open

// A missing agent reads as Claude, as canCompact does in sessions.js.
export function hasBackgroundTasks(s) {
  return !!s && (s.agent || 'claude') === 'claude';
}

function tasksPath(query) {
  return sessionRoute(view.sid, '/background-tasks' + (query || ''));
}

function elapsedText(task) {
  return fmtDuration(task.elapsed_seconds, { granular: true });
}

function note(text) {
  const p = document.createElement('p');
  p.className = 'bg-task-note';
  p.textContent = text;
  return p;
}

function taskRow(task, running) {
  const row = document.createElement('details');
  row.className = 'bg-task';
  row.dataset.task = task.id;
  row.dataset.status = task.status;

  const head = document.createElement('summary');
  head.className = 'bg-task-head';
  const text = document.createElement('span');
  text.className = 'bg-task-text';
  const title = document.createElement('span');
  title.className = 'bg-task-title';
  title.textContent = task.label;
  title.title = task.label;
  text.appendChild(title);
  const meta = document.createElement('span');
  meta.className = 'bg-task-meta';
  const bits = [];
  const took = elapsedText(task);
  if (running) bits.push('Running ' + took);
  else bits.push(task.status, took && 'took ' + took);
  meta.textContent = bits.filter(Boolean).join(' · ');
  text.appendChild(meta);
  head.appendChild(text);
  if (!running && task.status !== 'completed') {
    head.appendChild(chip(task.status, STATUS_TONE[task.status] || 'neutral'));
  }
  head.insertAdjacentHTML('beforeend', icon('chevron-down', 'bg-task-chevron'));
  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'bg-task-body';
  row.appendChild(body);
  let loaded = false;
  row.addEventListener('toggle', function () {
    if (!row.open || loaded) return;
    loaded = true;
    fillTaskBody(body, task);
  });
  return row;
}

async function fillTaskBody(body, task) {
  if (task.summary) body.appendChild(note(task.summary));
  if (task.command && task.command !== task.label) {
    const cmd = document.createElement('pre');
    cmd.className = 'bg-task-command';
    cmd.textContent = task.command;
    body.appendChild(cmd);
  }
  if (!task.has_output) {
    body.appendChild(note(NO_OUTPUT));
    return;
  }
  const seq = view && view.seq;
  const slot = note('Reading output…');
  body.appendChild(slot);
  let out = null;
  try {
    out = await terminalJsonApi(tasksPath('?task=' + encodeURIComponent(task.id)));
  } catch (exc) {
    out = null;
  }
  if (!view || view.seq !== seq) return;
  if (!out || !out.available) {
    slot.textContent = out && out.reason === 'output_not_found' ? NO_OUTPUT : 'Couldn’t read the output';
    return;
  }
  const pre = document.createElement('pre');
  pre.className = 'bg-task-output';
  pre.textContent = out.text || '(empty)';
  slot.replaceWith(pre);
  if (out.truncated) body.appendChild(note('Showing the end of the output'));
}

function group(titleText, tasks, running) {
  const section = document.createElement('section');
  section.className = 'bg-tasks-group';
  section.dataset.group = running ? 'running' : 'finished';
  const h = document.createElement('h3');
  h.className = 'bg-tasks-group-title';
  h.textContent = titleText;
  section.appendChild(h);
  tasks.forEach(function (t) { section.appendChild(taskRow(t, running)); });
  return section;
}

function render(data) {
  els.bgTasksBody.replaceChildren();
  const running = data.running || [];
  const finished = data.finished || [];
  if (!running.length && !finished.length) {
    setAsyncState(els.bgTasksState, 'empty', { message: 'No background tasks', glyph: 'clipboard-list' });
    return;
  }
  setAsyncState(els.bgTasksState, 'ready');
  if (running.length) els.bgTasksBody.appendChild(group('Running', running, true));
  if (finished.length) {
    els.bgTasksBody.appendChild(group('Finished ' + data.finished_total, finished, false));
    if (data.finished_total > finished.length) {
      els.bgTasksBody.appendChild(note('The newest ' + finished.length + ' of ' + data.finished_total));
    }
  }
}

async function load() {
  const seq = ++view.seq;
  els.bgTasksBody.replaceChildren();
  setAsyncState(els.bgTasksState, 'loading', { message: 'Reading background tasks…', glyph: 'hourglass' });
  let data = null;
  try {
    data = await terminalJsonApi(tasksPath());
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return;
    data = null;
  }
  if (!view || view.seq !== seq) return;
  if (!data) {
    setAsyncState(els.bgTasksState, 'error', { message: 'Could not load background tasks.', retry: load });
  } else if (!data.available) {
    setAsyncState(els.bgTasksState, 'empty', {
      message: REASON[data.reason] || REASON.read_failed, glyph: 'clipboard-list',
    });
  } else {
    render(data);
  }
}

// `s` is a session as the list knows it (only `session_id` is read).
export function openBackgroundTasks(s) {
  if (!els.bgTasksSheet || !s) return;
  view = { sid: s.session_id, seq: 0 };
  if (!els.bgTasksSheet.open) els.bgTasksSheet.showModal();
  load();
}

function closeSheet() {
  if (els.bgTasksSheet.open) els.bgTasksSheet.close();
}

export function wireBackgroundTasks() {
  if (!els.bgTasksSheet) return;
  els.bgTasksClose.addEventListener('click', closeSheet);
  els.bgTasksDone.addEventListener('click', closeSheet);
  // Esc, the back gesture and both buttons end here: a response still in
  // flight lands nowhere.
  els.bgTasksSheet.addEventListener('close', function () {
    if (view) view.seq += 1;
    view = null;
  });
}
