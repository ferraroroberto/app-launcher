// The composer's attach queue (#1354): the DOM-free half of `attachFiles`,
// kept apart so it runs under plain Node (tests/js/attach_batch.test.mjs).
//
// One queue per composer. Files upload strictly one after another — the
// append reads then writes the textarea, so they never overlap (#41/#448) —
// and a pick made while a batch is running is queued behind it, never
// interleaved: the append order is the pick order. Progress is reported as
// "N of M" over everything queued since the queue last went idle, so a second
// pick just grows M. A failure is recorded with its reason and the queue moves
// on; the summary is one message for the whole run, not one per file.

// The session-host rejects anything larger (`_MAX_IMAGE_BYTES` in
// app/session_host/server.py; tests/test_attach_batch.py pins the two equal).
// Checked here so an oversize file costs no upload at all: the host only
// answers after it has received every byte.
export const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;
const MAX_NAMED_FAILURES = 3;

// "14.3 MB", "12 MB": one decimal, dropped when it is .0.
export function formatMB(bytes) {
  const mb = Math.round((bytes / (1024 * 1024)) * 10) / 10;
  return mb + ' MB';
}

// Why a file may not be uploaded, or '' when it may.
export function oversizeReason(file) {
  return file && file.size > MAX_UPLOAD_BYTES
    ? formatMB(file.size) + ', limit ' + formatMB(MAX_UPLOAD_BYTES)
    : '';
}

// The one toast a run ends with: `{text, tone}`. All good keeps the wording
// the composer has always used; anything else counts and names what failed.
export function summarize(ok, total, failures) {
  if (!failures.length) {
    const plural = ok > 1;
    return {
      text: 'Uploaded ' + ok + ' file' + (plural ? 's' : '') +
        ' — path' + (plural ? 's' : '') + ' added to the message.',
      tone: 'good',
    };
  }
  const named = failures.slice(0, MAX_NAMED_FAILURES).map(function (f) {
    return f.name + ' (' + f.reason + ')';
  });
  const more = failures.length - named.length;
  return {
    text: 'Uploaded ' + ok + ' of ' + total + ' — ' + failures.length + ' failed: ' +
      named.join(', ') + (more > 0 ? ', and ' + more + ' more' : ''),
    tone: 'error',
  };
}

// opts:
//   upload(file)  → path, or null; a throw carries the reason
//   onAppend(path)  each successful path, in order
//   onProgress({index, total, name})  before each file; null when idle again
//   onSettle({ok, total, failures})  once the queue has drained
//   log(entry)  one per upload attempt: {name, bytes, ms, ok, reason?}
// Returns {enqueue(files) → promise for the drain, busy()}.
export function createAttachQueue(opts) {
  const log = opts.log || function () {};
  let pending = [];
  let running = null;
  let total = 0;
  let done = 0;
  let current = '';
  let ok = 0;
  let failures = [];

  async function attempt(file) {
    const name = file.name || 'file';
    const started = Date.now();
    let reason = oversizeReason(file);
    if (!reason) {
      try {
        const path = await opts.upload(file);
        if (path) {
          opts.onAppend(path);
          ok++;
          log({ name: name, bytes: file.size, ms: Date.now() - started, ok: true });
          return;
        }
        reason = 'upload failed';
      } catch (exc) {
        reason = (exc && exc.message) || 'upload failed';
      }
    }
    failures.push({ name: name, reason: reason });
    log({ name: name, bytes: file.size, ms: Date.now() - started, ok: false, reason: reason });
  }

  async function drain() {
    while (pending.length) {
      const file = pending.shift();
      done++;
      current = file.name || 'file';
      opts.onProgress({ index: done, total: total, name: current });
      await attempt(file);
    }
    const result = { ok: ok, total: total, failures: failures };
    pending = []; running = null; total = 0; done = 0; current = ''; ok = 0; failures = [];
    opts.onProgress(null);
    opts.onSettle(result);
  }

  return {
    enqueue: function (files) {
      const list = Array.prototype.slice.call(files || []);
      if (!list.length) return running || Promise.resolve();
      total += list.length;
      pending = pending.concat(list);
      // A pick joining a running batch grows M at once, not at the next file.
      if (running) opts.onProgress({ index: done, total: total, name: current });
      else running = drain();
      return running;
    },
    busy: function () { return !!running; },
  };
}
