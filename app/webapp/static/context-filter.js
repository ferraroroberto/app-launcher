/* Context filter (issue #713) — the fleet's PreToolUse token-reduction hook
 * (fleet-config#392/#541/#544): the Settings-tab "Context filter" card
 * (mode switch + harness matrix + savings stats) and the Coding-tab "filter
 * saved" badge beside #codingUsage. One module owns the whole feature, per
 * the issue's self-containment constraint — it gets ported to
 * app-launcher-lite alongside src/context_filter_state.py and
 * routers/context_filter.py later.
 *
 * GET /api/context-filter -> {mode, harnesses, stats}. The mode control is a
 * three-way segmented switch built from the app's own .segmented styling
 * (same shape as claude-options.js's model/effort rows) — no vendored
 * segmented component exists, and none is added here. A click PUTs then
 * re-fetches; never an optimistic flip (main.js's toggleBootAutostart
 * precedent).
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';

const MODES = ['off', 'shadow', 'rewrite'];
const MODE_LABELS = { off: 'Off', shadow: 'Shadow', rewrite: 'Rewrite' };
const STATUS_LABELS = { active: 'Active', unsupported: 'Unsupported', planned: 'Planned' };

// Compact k/M abbreviation for a token count — 12300 -> "12.3k".
function fmtTokens(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(v));
}

function renderModeControl(mode) {
  if (!els.contextFilterMode) return;
  els.contextFilterMode.innerHTML = '';
  MODES.forEach(function (m) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = MODE_LABELS[m];
    b.dataset.value = m;
    if (m === mode) b.classList.add('active');
    b.addEventListener('click', function () {
      if (m !== mode) putContextFilterMode(m);
    });
    els.contextFilterMode.appendChild(b);
  });
}

function renderHarnesses(harnesses) {
  if (!els.contextFilterHarnesses) return;
  els.contextFilterHarnesses.innerHTML = '';
  (harnesses || []).forEach(function (h) {
    const li = document.createElement('li');
    li.className = 'row context-filter-harness';
    const meta = document.createElement('span');
    meta.className = 'context-filter-harness-meta';
    const label = document.createElement('strong');
    label.textContent = h.label || h.id;
    meta.appendChild(label);
    if (h.note) {
      const note = document.createElement('span');
      note.className = 'muted small';
      note.textContent = h.note;
      meta.appendChild(note);
    }
    li.appendChild(meta);
    const status = document.createElement('span');
    status.className = 'context-filter-status context-filter-status-' + (h.status || 'planned');
    status.textContent = STATUS_LABELS[h.status] || h.status || '';
    li.appendChild(status);
    els.contextFilterHarnesses.appendChild(li);
  });
}

function renderStats(stats) {
  if (!els.contextFilterStats) return;
  if (!stats || !stats.available) {
    els.contextFilterStats.hidden = true;
    if (els.contextFilterStatsEmpty) els.contextFilterStatsEmpty.hidden = false;
    return;
  }
  els.contextFilterStats.hidden = false;
  if (els.contextFilterStatsEmpty) els.contextFilterStatsEmpty.hidden = true;

  const totals = stats.totals || {};
  const week = stats.last_7_days || {};
  const today = stats.today || {};

  if (els.contextFilterSavedToday) {
    els.contextFilterSavedToday.textContent =
      'Today: ' + fmtTokens(today.tokens_saved) + ' saved · ' + (today.rows || 0) + ' calls';
  }
  if (els.contextFilterSavedWeek) {
    els.contextFilterSavedWeek.textContent =
      'Last 7 days: ' + fmtTokens(week.tokens_saved) + ' saved · ' + (week.rows || 0) + ' calls';
  }
  if (els.contextFilterSavedTotal) {
    els.contextFilterSavedTotal.textContent =
      'All time: ' + fmtTokens(totals.tokens_saved) + ' saved · ' + (totals.rows || 0) + ' calls';
  }
  if (els.contextFilterPerAgent) {
    els.contextFilterPerAgent.innerHTML = '';
    const perAgent = stats.per_agent || {};
    Object.keys(perAgent).sort().forEach(function (agent) {
      const bucket = perAgent[agent] || {};
      const li = document.createElement('li');
      li.textContent =
        agent + ': ' + fmtTokens(bucket.tokens_saved) + ' saved · ' + (bucket.rows || 0) + ' calls';
      els.contextFilterPerAgent.appendChild(li);
    });
  }
}

function renderBadge(stats) {
  if (!els.codingFilterBadge || !els.codingFilterSavedBadge) return;
  if (!stats || !stats.available) {
    els.codingFilterBadge.hidden = true;
    return;
  }
  const todaySaved = (stats.today && stats.today.tokens_saved) || 0;
  const weekSaved = (stats.last_7_days && stats.last_7_days.tokens_saved) || 0;
  const useToday = todaySaved > 0;
  const saved = useToday ? todaySaved : weekSaved;
  if (!saved) {
    els.codingFilterBadge.hidden = true;
    return;
  }
  els.codingFilterBadge.hidden = false;
  els.codingFilterSavedBadge.hidden = false;
  els.codingFilterSavedBadge.className = 'usage-badge good';
  els.codingFilterSavedBadge.textContent =
    fmtTokens(saved) + ' tok saved · filter' + (useToday ? '' : ' (7d)');
}

export async function fetchContextFilter() {
  try {
    const body = await jsonApi('/api/context-filter');
    state.contextFilter = body;
    const mode = body.mode && body.mode.available ? body.mode.mode : null;
    renderModeControl(mode);
    renderHarnesses(body.harnesses);
    renderStats(body.stats);
    renderBadge(body.stats);
  } catch (exc) {
    console.warn('context-filter fetch failed', exc);
  }
}

async function putContextFilterMode(mode) {
  try {
    await jsonApi('/api/context-filter/mode', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode }),
    });
    await fetchContextFilter();
    toast('Context filter: ' + MODE_LABELS[mode] + '.', 'good');
  } catch (exc) {
    apiFailToast('Context filter mode change failed', exc);
  }
}
