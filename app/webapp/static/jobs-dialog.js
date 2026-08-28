/* Jobs tab: the edit/add dialog (schedule + params + chain checklist +
 * pre-flight), and the run-now dialog for parametrized jobs (issue #67).
 *
 * Split out of jobs.js (audit #315) — these two <dialog> forms are each
 * their own self-contained surface (open/populate/build-payload/submit)
 * independent of the list render + poller that jobs.js keeps.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { fetchJobs, runJobNow } from './jobs.js';
import { icon } from './_vendored/icons/icons.js';
import { setSwitch, switchEl } from './_vendored/switch/switch.js';

// --------------------------------------------------- chain checklist (dialog)
//
// Two <ul>s in the job dialog, one for on_success and one for on_failure.
// Each list is populated from state.jobs minus the currently-edited job
// (a job can't chain to itself — the server validates this too, but the UI
// just hides the row so the user can't even try). The cycle check is
// strictly server-side; the toast surfaces the server's precise error.

function populateChainList(host, selected, currentId, kind) {
  if (!host) return;
  host.innerHTML = '';
  const want = new Set(selected || []);
  const all = (state.jobs || []).slice().sort(function (a, b) {
    return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
  });
  let rendered = 0;
  all.forEach(function (j) {
    if (currentId && j.id === currentId) return;
    const li = document.createElement('li');
    li.className = 'job-chain-row';
    const row = document.createElement('div');
    row.className = 'job-chain-row-btn switch-row';
    const text = document.createElement('span');
    text.textContent = j.name + '  ·  ' + j.id;
    const btn = switchEl(want.has(j.id), {
      label: 'Run ' + j.name + ' ' + kind.replace('_', ' '),
      onToggle: function (next, switchBtn) { setSwitch(switchBtn, next); },
    });
    btn.dataset.value = j.id;
    btn.dataset.role = 'chain-' + kind;
    row.appendChild(text);
    row.appendChild(btn);
    li.appendChild(row);
    host.appendChild(li);
    rendered += 1;
  });
  if (rendered === 0) {
    const li = document.createElement('li');
    li.className = 'job-chain-row muted small';
    li.textContent = '(no other jobs to choose from)';
    host.appendChild(li);
  }
}

function readChainList(host, kind) {
  if (!host) return [];
  const selector = '.job-chain-row .toggle[data-role="chain-' + kind + '"][aria-checked="true"]';
  const checked = Array.from(host.querySelectorAll(selector));
  return checked.map(function (btn) { return btn.dataset.value; });
}

// -------------------------------------------------- params editor (dialog)

const PARAM_KINDS = ['string', 'int', 'enum', 'bool', 'date'];

function renderParamRow(param) {
  const li = document.createElement('li');
  li.className = 'job-param-row';
  li.dataset.role = 'job-param-row';

  const head = document.createElement('div');
  head.className = 'job-param-row-head';

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'input-native';
  nameInput.placeholder = 'name (snake_case)';
  nameInput.dataset.role = 'param-name';
  nameInput.value = (param && param.name) || '';
  head.appendChild(nameInput);

  const kindSel = document.createElement('select');
  kindSel.className = 'input-native';
  kindSel.dataset.role = 'param-kind';
  PARAM_KINDS.forEach(function (k) {
    const opt = document.createElement('option');
    opt.value = k;
    opt.textContent = k;
    kindSel.appendChild(opt);
  });
  kindSel.value = (param && param.kind) || 'string';
  head.appendChild(kindSel);

  const rmBtn = document.createElement('button');
  rmBtn.type = 'button';
  rmBtn.className = 'icon-btn danger';
  rmBtn.innerHTML = icon('x');
  rmBtn.title = 'Remove parameter';
  rmBtn.setAttribute('aria-label', 'Remove parameter');
  rmBtn.addEventListener('click', function () { li.remove(); });
  head.appendChild(rmBtn);

  li.appendChild(head);

  const grid = document.createElement('div');
  grid.className = 'job-param-row-grid';

  function makeField(role, placeholder, value) {
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'input-native';
    inp.placeholder = placeholder;
    inp.dataset.role = role;
    inp.value = value == null ? '' : String(value);
    return inp;
  }

  grid.appendChild(makeField('param-flag', '--flag (optional)',
    param && param.flag));
  grid.appendChild(makeField('param-env', 'ENV_VAR (optional)',
    param && param.env));
  grid.appendChild(makeField('param-default', 'default (optional)',
    param && (param.default == null ? '' : param.default)));
  grid.appendChild(makeField('param-options',
    'enum options, comma-separated',
    param && param.options ? param.options.join(', ') : ''));

  li.appendChild(grid);
  return li;
}

function setParamsEditor(params) {
  if (!els.jobParamsList) return;
  els.jobParamsList.innerHTML = '';
  (params || []).forEach(function (p) {
    els.jobParamsList.appendChild(renderParamRow(p));
  });
}

function readParamsEditor() {
  if (!els.jobParamsList) return [];
  const rows = Array.from(els.jobParamsList.querySelectorAll('[data-role="job-param-row"]'));
  const seen = new Set();
  return rows.map(function (row) {
    const name = (row.querySelector('[data-role="param-name"]').value || '').trim();
    if (!name) throw new Error('Parameter name is required');
    if (!/^[a-z][a-z0-9_]*$/.test(name)) {
      throw new Error('Parameter name ' + JSON.stringify(name) + ' must be snake_case');
    }
    if (seen.has(name)) throw new Error('Duplicate parameter name: ' + name);
    seen.add(name);
    const kind = row.querySelector('[data-role="param-kind"]').value;
    const flag = (row.querySelector('[data-role="param-flag"]').value || '').trim();
    const env = (row.querySelector('[data-role="param-env"]').value || '').trim();
    const defaultRaw = (row.querySelector('[data-role="param-default"]').value || '').trim();
    const optionsRaw = (row.querySelector('[data-role="param-options"]').value || '').trim();
    if (flag && env) {
      throw new Error('Parameter ' + name + ': flag and env are mutually exclusive');
    }
    const out = { name: name, kind: kind };
    if (flag) out.flag = flag;
    if (env) out.env = env;
    if (kind === 'enum') {
      const options = optionsRaw.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      if (!options.length) {
        throw new Error('Parameter ' + name + ': enum needs at least one option');
      }
      out.options = options;
    }
    if (defaultRaw !== '') {
      if (kind === 'int') {
        const n = parseInt(defaultRaw, 10);
        if (!Number.isFinite(n) || String(n) !== defaultRaw) {
          throw new Error('Parameter ' + name + ': default must be an integer');
        }
        out.default = n;
      } else if (kind === 'bool') {
        if (defaultRaw !== 'true' && defaultRaw !== 'false') {
          throw new Error('Parameter ' + name + ': default must be true or false');
        }
        out.default = defaultRaw === 'true';
      } else {
        out.default = defaultRaw;
      }
    }
    return out;
  });
}

// ------------------------------------------------------ webhook editor (#73)

function renderMappingRow(name, path) {
  const li = document.createElement('li');
  li.className = 'job-param-row';
  li.dataset.role = 'job-webhook-mapping-row';

  const head = document.createElement('div');
  head.className = 'job-param-row-head';

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'input-native';
  nameInput.placeholder = 'param name';
  nameInput.dataset.role = 'mapping-name';
  nameInput.value = name || '';
  head.appendChild(nameInput);

  const pathInput = document.createElement('input');
  pathInput.type = 'text';
  pathInput.className = 'input-native';
  pathInput.placeholder = '$.repository.full_name';
  pathInput.dataset.role = 'mapping-path';
  pathInput.value = path || '';
  head.appendChild(pathInput);

  const rmBtn = document.createElement('button');
  rmBtn.type = 'button';
  rmBtn.className = 'icon-btn danger';
  rmBtn.innerHTML = icon('x');
  rmBtn.title = 'Remove mapping';
  rmBtn.setAttribute('aria-label', 'Remove mapping');
  rmBtn.addEventListener('click', function () { li.remove(); });
  head.appendChild(rmBtn);

  li.appendChild(head);
  return li;
}

function setMappingEditor(mapping) {
  if (!els.jobWebhookMappingList) return;
  els.jobWebhookMappingList.innerHTML = '';
  Object.keys(mapping || {}).forEach(function (name) {
    els.jobWebhookMappingList.appendChild(renderMappingRow(name, mapping[name]));
  });
}

function readMappingEditor() {
  if (!els.jobWebhookMappingList) return {};
  const rows = Array.from(
    els.jobWebhookMappingList.querySelectorAll('[data-role="job-webhook-mapping-row"]'),
  );
  const out = {};
  rows.forEach(function (row) {
    const name = (row.querySelector('[data-role="mapping-name"]').value || '').trim();
    const path = (row.querySelector('[data-role="mapping-path"]').value || '').trim();
    if (!name && !path) return;  // ignore a fully-blank row
    if (!name) throw new Error('Webhook mapping: parameter name is required');
    if (!path) throw new Error('Webhook mapping ' + name + ': JSONPath is required');
    out[name] = path;
  });
  return out;
}

// Show/hide provider-specific rows: secret applies to every provider;
// the event allowlist and mapping editor only make sense once a provider
// is chosen (mapping is meaningless with no payload shape to map from).
function syncWebhookFields() {
  if (!els.jobWebhookProvider) return;
  const provider = els.jobWebhookProvider.value;
  const enabled = !!provider;
  if (els.jobWebhookSecretRow) els.jobWebhookSecretRow.hidden = !enabled;
  if (els.jobWebhookEventsRow) els.jobWebhookEventsRow.hidden = provider !== 'github';
  if (els.jobWebhookMappingSection) els.jobWebhookMappingSection.hidden = !enabled;
}

// Returns null when disabled (provider unset) so buildJobPayload can clear
// the job's webhook on save; otherwise the {provider, secret, mapping,
// events} object the server expects.
function buildWebhookField() {
  const provider = els.jobWebhookProvider ? els.jobWebhookProvider.value : '';
  if (!provider) return null;
  const secret = els.jobWebhookSecretInput ? els.jobWebhookSecretInput.value.trim() : '';
  if (!secret) throw new Error('Webhook: secret is required');
  const mapping = readMappingEditor();  // may throw
  const out = { provider: provider, secret: secret, mapping: mapping };
  if (provider === 'github') {
    const eventsRaw = els.jobWebhookEventsInput ? els.jobWebhookEventsInput.value.trim() : '';
    if (eventsRaw) {
      out.events = eventsRaw.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    }
  }
  return out;
}

// ------------------------------------------------------ run-now dialog (#67)

let runDialogJob = null;

export function openRunDialog(job, prefill, staleKeys) {
  runDialogJob = job;
  els.jobRunDialogTitle.innerHTML = icon('play');
  els.jobRunDialogTitle.appendChild(document.createTextNode(' ' + job.name));
  if (staleKeys && staleKeys.length) {
    els.jobRunDialogStaleNote.hidden = false;
    els.jobRunDialogStaleNote.textContent =
      'Note: ' + staleKeys.join(', ') + ' from the previous run ' +
      (staleKeys.length === 1 ? 'was' : 'were') +
      ' dropped (no longer declared on this job).';
  } else {
    els.jobRunDialogStaleNote.hidden = true;
    els.jobRunDialogStaleNote.textContent = '';
  }

  const host = els.jobRunDialogFields;
  host.innerHTML = '';
  (job.params || []).forEach(function (p) {
    host.appendChild(renderRunDialogField(p, prefill));
  });

  if (els.jobRunDialogDryRun) setSwitch(els.jobRunDialogDryRun, false);
  if (els.jobRunDialog.showModal) els.jobRunDialog.showModal();
}

function renderRunDialogField(param, prefill) {
  const label = document.createElement('label');
  label.className = 'stacked';

  const span = document.createElement('span');
  let title = param.name;
  if (param.flag) title += ' (' + param.flag + ')';
  else if (param.env) title += ' ($' + param.env + ')';
  if (param.required && (param.default === undefined || param.default === null)) {
    title += ' *';
  }
  span.textContent = title;
  label.appendChild(span);

  const initial = (prefill && Object.prototype.hasOwnProperty.call(prefill, param.name))
    ? prefill[param.name]
    : (param.default !== undefined && param.default !== null ? param.default : null);

  let input;
  if (param.kind === 'enum') {
    input = document.createElement('select');
    input.className = 'input-native';
    (param.options || []).forEach(function (opt) {
      const o = document.createElement('option');
      o.value = opt;
      o.textContent = opt;
      input.appendChild(o);
    });
    if (initial != null) input.value = String(initial);
  } else if (param.kind === 'bool') {
    input = switchEl(initial === true || initial === 'true', {
      label: param.name,
      onToggle: function (next, btn) { setSwitch(btn, next); },
    });
  } else {
    input = document.createElement('input');
    input.type = param.kind === 'date' ? 'date'
      : param.kind === 'int' ? 'number'
      : 'text';
    input.className = 'input-native';
    if (initial != null) input.value = String(initial);
    if (param.required) input.required = true;
  }
  input.dataset.paramName = param.name;
  input.dataset.paramKind = param.kind;
  label.appendChild(input);
  return label;
}

function readRunDialogValues() {
  const out = {};
  const inputs = els.jobRunDialogFields.querySelectorAll('[data-param-name]');
  inputs.forEach(function (el) {
    const name = el.dataset.paramName;
    const kind = el.dataset.paramKind;
    if (kind === 'bool') {
      out[name] = el.getAttribute('aria-checked') === 'true';
      return;
    }
    const raw = (el.value || '').trim();
    if (raw === '') return;  // server applies defaults / enforces required
    if (kind === 'int') {
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n)) throw new Error(name + ' must be an integer');
      out[name] = n;
    } else {
      out[name] = raw;
    }
  });
  return out;
}

async function submitRunDialog(ev) {
  ev.preventDefault();
  if (!runDialogJob) return;
  let values;
  try { values = readRunDialogValues(); } catch (exc) {
    apiFailToast('', exc);
    return;
  }
  const job = runDialogJob;
  const dry = !!(els.jobRunDialogDryRun && els.jobRunDialogDryRun.getAttribute('aria-checked') === 'true');
  if (els.jobRunDialog.close) els.jobRunDialog.close();
  runDialogJob = null;
  await runJobNow(job, {
    params: values,
    skipDialog: true,
    dryRun: dry ? 'execute' : undefined,
  });
}

export async function removeJob(job) {
  if (!confirm('Remove ' + job.name + ' from the jobs registry?')) return;
  try {
    await jsonApi('/api/jobs/' + encodeURIComponent(job.id), { method: 'DELETE' });
    toast('Removed ' + job.name, 'good');
    await fetchJobs();
  } catch (exc) {
    apiFailToast('Remove failed', exc);
  }
}

// ------------------------------------------------------------ dialog form

let dialogTargetId = null;

export function openJobDialog(job) {
  dialogTargetId = job ? job.id : null;
  els.jobDialogTitle.textContent = job ? 'Edit job' : 'Add job';
  els.jobIdField.value = job ? job.id : '';
  els.jobNameInput.value = job ? job.name : '';
  els.jobScriptInput.value = job ? job.script_path : '';
  els.jobArgsInput.value = job ? (job.args || '') : '';

  // Job-kind registry (issue #70). Empty kind = "auto (from extension)",
  // the back-compat default every pre-existing job round-trips through.
  const kindConfig = (job && job.kind_config) || {};
  if (els.jobKindInput) els.jobKindInput.value = (job && job.kind) || '';
  if (els.jobInlineExtInput) {
    els.jobInlineExtInput.value = kindConfig.ext || '.ps1';
  }
  if (els.jobInlineBodyInput) {
    els.jobInlineBodyInput.value = kindConfig.script_body || '';
  }
  if (els.jobHttpUrlInput) els.jobHttpUrlInput.value = kindConfig.url || '';
  if (els.jobHttpMethodInput) {
    els.jobHttpMethodInput.value = kindConfig.method || 'GET';
  }
  if (els.jobHttpExpectStatusInput) {
    els.jobHttpExpectStatusInput.value = kindConfig.expect_status || '';
  }
  if (els.jobHttpTimeoutInput) {
    els.jobHttpTimeoutInput.value = kindConfig.timeout || '';
  }
  syncKindFields();

  const sched = (job && job.schedule) || { type: 'none' };
  els.jobScheduleType.value = sched.type || 'none';
  if (sched.type === 'minutes' || sched.type === 'hourly') {
    els.jobScheduleEvery.value = sched.every || 1;
  } else {
    els.jobScheduleEvery.value = 1;
  }
  if (sched.type === 'daily' || sched.type === 'weekly') {
    els.jobScheduleAt.value = typeof sched.at === 'string' ? sched.at : '';
  } else {
    els.jobScheduleAt.value = '';
  }
  if (sched.type === 'daily_times' && Array.isArray(sched.at)) {
    els.jobScheduleTimes.value = sched.at.join(', ');
  } else {
    els.jobScheduleTimes.value = '';
  }
  els.jobScheduleDay.value = sched.day || 'MON';
  if (els.jobScheduleOnceAt) {
    els.jobScheduleOnceAt.value =
      (sched.type === 'once' && typeof sched.at === 'string') ? sched.at : '';
  }
  syncScheduleFields();

  if (els.jobCooldownInput) {
    const cd = job && Number.isFinite(job.cooldown_seconds) ? job.cooldown_seconds : 0;
    els.jobCooldownInput.value = cd > 0 ? String(cd) : '';
  }
  if (els.jobMutexGroupInput) {
    els.jobMutexGroupInput.value = (job && job.mutex_group) || '';
  }
  if (els.jobAlertOnFailureInput) {
    setSwitch(els.jobAlertOnFailureInput, !!(job && job.alert_on_failure));
  }
  if (els.jobConfirmInput) {
    setSwitch(els.jobConfirmInput, !!(job && job.confirm));
  }
  populateChainList(
    els.jobOnSuccessList,
    job ? (job.on_success || []) : [],
    job ? job.id : null,
    'on_success',
  );
  populateChainList(
    els.jobOnFailureList,
    job ? (job.on_failure || []) : [],
    job ? job.id : null,
    'on_failure',
  );

  setParamsEditor(job ? job.params : []);

  const webhook = job && job.webhook;
  if (els.jobWebhookProvider) els.jobWebhookProvider.value = webhook ? webhook.provider : '';
  if (els.jobWebhookSecretInput) els.jobWebhookSecretInput.value = webhook ? webhook.secret : '';
  if (els.jobWebhookEventsInput) {
    els.jobWebhookEventsInput.value = (webhook && webhook.events) ? webhook.events.join(', ') : '';
  }
  setMappingEditor(webhook ? webhook.mapping : {});
  syncWebhookFields();

  clearPreflightProblems();
  if (els.jobDialog.showModal) els.jobDialog.showModal();
}

function syncScheduleFields() {
  const t = els.jobScheduleType.value;
  els.jobScheduleEveryRow.hidden = !(t === 'minutes' || t === 'hourly');
  els.jobScheduleAtRow.hidden = !(t === 'daily' || t === 'weekly');
  els.jobScheduleTimesRow.hidden = t !== 'daily_times';
  els.jobScheduleDayRow.hidden = t !== 'weekly';
  if (els.jobScheduleOnceRow) els.jobScheduleOnceRow.hidden = t !== 'once';
}

// Job-kind registry (issue #70). "" (auto) and every file-kind
// (python/batch/powershell/shell-wsl) show the plain script-path row;
// inline-shell swaps it for a body textarea + extension picker; http-check
// swaps it for url/method/expect-status/timeout. Args apply to every kind
// except http-check (its build_argv never appends the composed tail).
function syncKindFields() {
  if (!els.jobKindInput) return;
  const k = els.jobKindInput.value;
  const isInline = k === 'inline-shell';
  const isHttpCheck = k === 'http-check';
  if (els.jobScriptRow) els.jobScriptRow.hidden = isInline || isHttpCheck;
  if (els.jobInlineShellFields) els.jobInlineShellFields.hidden = !isInline;
  if (els.jobHttpCheckFields) els.jobHttpCheckFields.hidden = !isHttpCheck;
  if (els.jobArgsRow) els.jobArgsRow.hidden = isHttpCheck;
}

function buildSchedule() {
  const t = els.jobScheduleType.value;
  if (t === 'none') return { type: 'none' };
  if (t === 'minutes' || t === 'hourly') {
    const every = parseInt(els.jobScheduleEvery.value, 10);
    if (!Number.isFinite(every) || every <= 0) throw new Error('Every must be > 0');
    return { type: t, every: every };
  }
  if (t === 'daily') {
    const at = els.jobScheduleAt.value.trim();
    if (!/^[0-2]\d:[0-5]\d$/.test(at)) throw new Error('At must be HH:MM');
    return { type: 'daily', at: at };
  }
  if (t === 'daily_times') {
    const list = els.jobScheduleTimes.value
      .split(',')
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    if (!list.length) throw new Error('Provide at least one HH:MM');
    list.forEach(function (s) {
      if (!/^[0-2]\d:[0-5]\d$/.test(s)) throw new Error('Each time must be HH:MM');
    });
    return { type: 'daily_times', at: list };
  }
  if (t === 'weekly') {
    const at = els.jobScheduleAt.value.trim();
    if (!/^[0-2]\d:[0-5]\d$/.test(at)) throw new Error('At must be HH:MM');
    return { type: 'weekly', day: els.jobScheduleDay.value, at: at };
  }
  if (t === 'once') {
    const at = (els.jobScheduleOnceAt && els.jobScheduleOnceAt.value || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}T[0-2]\d:[0-5]\d$/.test(at)) {
      throw new Error('Once: pick a date and time');
    }
    return { type: 'once', at: at };
  }
  return { type: 'none' };
}

// Pre-flight problems (issue #69). The last-submitted payload is held so
// "Save anyway" can re-POST it with acknowledge_warnings:true once the
// user has seen the warnings.
let lastJobPayload = null;

function clearPreflightProblems() {
  if (els.jobPreflightProblems) {
    els.jobPreflightProblems.innerHTML = '';
    els.jobPreflightProblems.hidden = true;
  }
  if (els.jobSaveAnyway) els.jobSaveAnyway.hidden = true;
  if (els.jobSaveBtn) {
    els.jobSaveBtn.textContent = dialogTargetId ? 'Save and verify' : 'Add and verify';
    els.jobSaveBtn.hidden = false;
  }
}

function renderPreflightProblems(problems) {
  const host = els.jobPreflightProblems;
  if (!host) return;
  host.innerHTML = '';
  (problems || []).forEach(function (p) {
    const li = document.createElement('li');
    li.className = 'job-preflight-problem ' + (p.level === 'error' ? 'error' : 'warning');
    const tag = document.createElement('span');
    tag.className = 'job-preflight-tag';
    tag.innerHTML = icon(p.level === 'error' ? 'octagon-x' : 'triangle-alert');
    li.appendChild(tag);
    const text = document.createElement('span');
    text.textContent = (p.field ? p.field + ': ' : '') + p.message;
    li.appendChild(text);
    host.appendChild(li);
  });
  host.hidden = (problems || []).length === 0;
}

// Job-kind registry (issue #70). Returns {kind, script_path, kind_config}
// for whichever kind group is currently visible — the two non-file kinds
// carry their settings in kind_config and leave script_path empty; every
// other kind (including "" / auto) is the plain script_path path.
function buildKindFields() {
  const kind = els.jobKindInput ? els.jobKindInput.value : '';
  if (kind === 'inline-shell') {
    const ext = els.jobInlineExtInput ? els.jobInlineExtInput.value : '.ps1';
    const body = els.jobInlineBodyInput ? els.jobInlineBodyInput.value : '';
    if (!body.trim()) throw new Error('Inline-shell needs a script body');
    return { kind: kind, script_path: '', kind_config: { script_body: body, ext: ext } };
  }
  if (kind === 'http-check') {
    const url = els.jobHttpUrlInput ? els.jobHttpUrlInput.value.trim() : '';
    if (!url) throw new Error('HTTP check needs a URL');
    const kindConfig = { url: url };
    const method = els.jobHttpMethodInput ? els.jobHttpMethodInput.value : '';
    if (method && method !== 'GET') kindConfig.method = method;
    const statusRaw = els.jobHttpExpectStatusInput ? els.jobHttpExpectStatusInput.value.trim() : '';
    if (statusRaw) {
      const status = parseInt(statusRaw, 10);
      if (!Number.isFinite(status)) throw new Error('Expected status must be a number');
      kindConfig.expect_status = status;
    }
    const timeoutRaw = els.jobHttpTimeoutInput ? els.jobHttpTimeoutInput.value.trim() : '';
    if (timeoutRaw) {
      const timeout = parseFloat(timeoutRaw);
      if (!Number.isFinite(timeout)) throw new Error('Timeout must be a number');
      kindConfig.timeout = timeout;
    }
    return { kind: kind, script_path: '', kind_config: kindConfig };
  }
  // "" (auto) or an explicit file-kind: plain script_path, no kind_config.
  return { kind: kind, script_path: els.jobScriptInput.value.trim(), kind_config: {} };
}

function buildJobPayload() {
  const schedule = buildSchedule();      // may throw
  const params = readParamsEditor();     // may throw
  const kindFields = buildKindFields();  // may throw
  const payload = {
    name: els.jobNameInput.value.trim(),
    script_path: kindFields.script_path,
    kind: kindFields.kind,
    kind_config: kindFields.kind_config,
    args: els.jobArgsInput.value,
    schedule: schedule,
    params: params,
  };
  // Empty → omit on create (server stores null); on edit, send an explicit
  // null so blanking the field actually clears a previously-set value —
  // edit_job only patches keys present in the body, so omitting the key
  // means "leave unchanged," not "clear" (issue #409). "0" → same as empty
  // (treated as off). Negative or non-numeric → tell the user; the server
  // cap (>86400) we let the server reject so the limit lives in one place.
  const cdRaw = els.jobCooldownInput ? els.jobCooldownInput.value.trim() : '';
  if (cdRaw) {
    const cd = parseInt(cdRaw, 10);
    if (!Number.isFinite(cd) || cd < 0) {
      throw new Error('Cooldown must be a non-negative integer');
    }
    if (cd > 0) payload.cooldown_seconds = cd;
    else if (dialogTargetId) payload.cooldown_seconds = null;  // clear on edit
  } else if (dialogTargetId) {
    payload.cooldown_seconds = null;  // clear on edit
  }
  // Empty → omit (server treats as null); the server validates the shape
  // (lowercase alnum + _/-, starts with letter, <=32 chars) so the
  // 400 surfaces here as a normal toast.
  const mg = els.jobMutexGroupInput ? els.jobMutexGroupInput.value.trim() : '';
  if (mg) payload.mutex_group = mg;
  else if (dialogTargetId) payload.mutex_group = null;  // clear on edit
  // Chain edges (issue #68 PR #3). Always send both keys on submit so a
  // user un-checking the last entry actually clears it server-side.
  payload.on_success = readChainList(els.jobOnSuccessList, 'on_success');
  payload.on_failure = readChainList(els.jobOnFailureList, 'on_failure');
  // Alert-to-Telegram-on-failure (issue #597). Always send so unchecking clears it.
  payload.alert_on_failure = !!(els.jobAlertOnFailureInput && els.jobAlertOnFailureInput.getAttribute('aria-checked') === 'true');
  // Confirm-on-fire (issue #69). Always send so unchecking clears it.
  payload.confirm = !!(els.jobConfirmInput && els.jobConfirmInput.getAttribute('aria-checked') === 'true');
  // Webhook trigger (issue #73). null clears an existing config when the
  // provider is set back to "None" on edit.
  payload.webhook = buildWebhookField();  // may throw
  return payload;
}

async function postJobPayload(payload) {
  try {
    const opts = {
      method: dialogTargetId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    };
    const path = dialogTargetId
      ? '/api/jobs/' + encodeURIComponent(dialogTargetId)
      : '/api/jobs';
    const res = await jsonApi(path, opts);
    // Warnings-only, not acknowledged: the server didn't save. Keep the
    // dialog open, show the warnings, and offer "Save anyway".
    if (res && res.saved === false) {
      renderPreflightProblems(res.warnings || []);
      // Modal contract (#545): exactly one full-width primary action —
      // swap the submit button out for "Save anyway" rather than stacking both.
      if (els.jobSaveAnyway) els.jobSaveAnyway.hidden = false;
      if (els.jobSaveBtn) els.jobSaveBtn.hidden = true;
      return;
    }
    if (els.jobDialog.close) els.jobDialog.close();
    clearPreflightProblems();
    const warned = res && Array.isArray(res.warnings) && res.warnings.length;
    toast(
      (dialogTargetId ? 'Job updated.' : 'Job added.') +
        (warned ? ' (saved with warnings)' : ''),
      'good',
    );
    await fetchJobs();
  } catch (exc) {
    // Pre-flight errors come back as a 400 with a structured problems
    // list — render them inline (red) and keep the dialog open.
    const detail = exc && exc.body && exc.body.detail;
    if (exc && exc.status === 400 && detail && detail.reason === 'preflight') {
      renderPreflightProblems(detail.problems || []);
      if (els.jobSaveAnyway) els.jobSaveAnyway.hidden = true;
      return;
    }
    apiFailToast('Save failed', exc);
  }
}

async function submitJobDialog(ev) {
  ev.preventDefault();
  let payload;
  try { payload = buildJobPayload(); } catch (exc) {
    apiFailToast('', exc);
    return;
  }
  clearPreflightProblems();
  lastJobPayload = payload;
  await postJobPayload(payload);
}

async function saveJobAnyway() {
  if (!lastJobPayload) return;
  const payload = Object.assign({}, lastJobPayload, { acknowledge_warnings: true });
  await postJobPayload(payload);
}

// -------------------------------------------------------------- wiring

export function wireJobDialogs() {
  if (els.jobsAddBtn) {
    els.jobsAddBtn.addEventListener('click', function () { openJobDialog(null); });
    // The ➕ Add job button lives in the Registered-jobs card's <summary>, so a
    // click there would also toggle the <details>. Stop the click at the actions
    // container — same trick the Running-sessions card uses (sessions.js).
    const headerActions = els.jobsAddBtn.closest('.jobs-header-actions');
    if (headerActions) {
      headerActions.addEventListener('click', function (ev) { ev.stopPropagation(); });
    }
  }
  if (els.jobForm) {
    els.jobForm.addEventListener('submit', submitJobDialog);
  }
  if (els.jobSaveAnyway) {
    els.jobSaveAnyway.addEventListener('click', function () {
      saveJobAnyway().catch(function () {});
    });
  }
  if (els.jobCancel) {
    els.jobCancel.addEventListener('click', function () {
      if (els.jobDialog.close) els.jobDialog.close();
    });
  }
  if (els.jobScheduleType) {
    els.jobScheduleType.addEventListener('change', syncScheduleFields);
  }
  if (els.jobKindInput) {
    els.jobKindInput.addEventListener('change', syncKindFields);
  }
  if (els.jobParamsAdd) {
    els.jobParamsAdd.addEventListener('click', function () {
      if (els.jobParamsList) {
        els.jobParamsList.appendChild(renderParamRow(null));
      }
    });
  }
  if (els.jobWebhookProvider) {
    els.jobWebhookProvider.addEventListener('change', syncWebhookFields);
  }
  if (els.jobWebhookMappingAdd) {
    els.jobWebhookMappingAdd.addEventListener('click', function () {
      if (els.jobWebhookMappingList) {
        els.jobWebhookMappingList.appendChild(renderMappingRow(null, null));
      }
    });
  }
  if (els.jobRunForm) {
    els.jobRunForm.addEventListener('submit', submitRunDialog);
  }
  if (els.jobRunCancel) {
    els.jobRunCancel.addEventListener('click', function () {
      if (els.jobRunDialog && els.jobRunDialog.close) els.jobRunDialog.close();
      runDialogJob = null;
    });
  }
  // Alert-on-failure + Require-confirmation + Dry-run role="switch"
  // buttons (issue #355) — no native checkbox toggling any more, so the
  // click has to flip aria-checked.
  [els.jobAlertOnFailureInput, els.jobConfirmInput, els.jobRunDialogDryRun].forEach(function (btn) {
    if (!btn) return;
    btn.addEventListener('click', function () {
      setSwitch(btn, btn.getAttribute('aria-checked') !== 'true');
    });
  });
}
