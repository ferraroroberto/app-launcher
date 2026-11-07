/* Files a Job run sent to the owner through its outbox
 * (POST /api/jobs/{job}/runs/{run}/outbox), listed under the selected run's
 * output, each drawn as the Chat's own file card. The run detail carries only
 * the count; names, captions and bytes are passkey-gated, so the list loads
 * on a tap (a passkey prompt needs the user's gesture), never from a poll.
 */

import { renderRunFile, resetRunBlobs } from '../chat/transcript-media.js';
import { terminalJsonApi } from '../webauthn.js';

function outboxPath(jobId, runId) {
  return '/api/jobs/' + encodeURIComponent(jobId) + '/runs/' + encodeURIComponent(runId) + '/outbox';
}

async function loadFiles(host, jobId, runId, shown) {
  const key = 'run:' + jobId + '/' + runId;
  let body;
  try {
    body = await terminalJsonApi(outboxPath(jobId, runId));
  } catch (exc) {
    if (host.dataset.shown !== shown) return;
    const note = host.querySelector('.jobs-sent-files-note');
    if (note) note.textContent = 'Couldn’t load them: ' + (exc && exc.message ? exc.message : 'request failed');
    return;
  }
  // The panel moved to another run, or a new file arrived, while this loaded.
  if (host.dataset.shown !== shown) return;
  resetRunBlobs(key);
  const list = document.createElement('ul');
  list.className = 'jobs-sent-files-list';
  (body.entries || []).forEach(function (file) {
    if (!file || !file.id) return;
    list.appendChild(renderRunFile(file, key, outboxPath(jobId, runId) + '/' + encodeURIComponent(file.id)));
  });
  host.querySelector('.jobs-sent-files-body').replaceChildren(list);
}

// Draw the run's sent-files section into `host`: hidden for none, else a
// heading and a Show button that loads the cards. Unchanged run and count
// leave the section as it is, so the running run's poll never redraws it.
export function renderRunSentFiles(host, jobId, runId, count) {
  if (!host) return;
  const shown = jobId + '/' + runId + '#' + (count || 0);
  if (host.dataset.shown === shown) return;
  host.dataset.shown = shown;
  if (!count) {
    host.hidden = true;
    host.replaceChildren();
    return;
  }
  const heading = document.createElement('h4');
  heading.textContent = count === 1 ? '1 sent file' : count + ' sent files';
  const bodyEl = document.createElement('div');
  bodyEl.className = 'jobs-sent-files-body';
  const show = document.createElement('button');
  show.type = 'button';
  show.className = 'button-tint jobs-sent-files-show';
  show.dataset.track = 'jobs.sent-files-show';
  show.textContent = 'Show';
  show.addEventListener('click', function () {
    show.disabled = true;
    const note = document.createElement('div');
    note.className = 'jobs-sent-files-note';
    note.textContent = 'Loading…';
    bodyEl.replaceChildren(note);
    loadFiles(host, jobId, runId, shown);
  });
  bodyEl.appendChild(show);
  host.replaceChildren(heading, bodyEl);
  host.hidden = false;
}
