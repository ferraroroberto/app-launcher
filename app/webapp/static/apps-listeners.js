/* Apps tab "Other ports" (#1437): what is bound on this machine right now,
 * with helper services collapsed under their parent app's row (#224/#480)
 * and a per-port Stop process in each row's ⋮ menu (#1129). The list lives
 * in a sheet opened from the Running card's last row, which carries the
 * count of listeners the launcher did not start (the Port listeners card
 * merged into Running).
 *
 * Split out of apps.js in issue #723. It owns its own fetch and expand
 * state, and imports nothing from apps.js (apps.js imports copyUrl and the
 * row's repaint from here).
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { actionRow } from './action-rows.js';
import { createRowMenu } from './row-menu.js';
import { confirmDialog } from './confirm-dialog.js';

const listenerMenu = createRowMenu('project-menu');

// Parent rows with dependent children keep them collapsed behind a tap
// (#480). Module-level so the expand state survives the poll's re-renders.
const expandedListenerPorts = new Set();

// The one Copy URL every Apps-tab menu uses (Running, Apps, Other ports).
export async function copyUrl(url) {
  try {
    await navigator.clipboard.writeText(url);
    toast('Link copied', 'good');
  } catch (exc) {
    apiFailToast('Could not copy the link', exc);
  }
}

export async function fetchListeners() {
  try {
    const body = await jsonApi('/api/ports/probe');
    state.listeners = body.listeners || [];
    renderListeners();
  } catch (exc) {
    // Best-effort poll — don't spam toasts.
    logPollFailure('listeners fetch failed', exc);
  }
}

// Group helper services (parent_port set, parent present) under their
// parent app's row so one app reads as one top-level entry — see #224.
function groupListeners(items) {
  const byPort = {};
  items.forEach(function (l) { byPort[l.port] = l; });
  const childrenOf = {};
  const topLevel = [];
  items.forEach(function (l) {
    if (l.parent_port != null && byPort[l.parent_port]) {
      (childrenOf[l.parent_port] = childrenOf[l.parent_port] || []).push(l);
    } else {
      topLevel.push(l);
    }
  });
  return { topLevel: topLevel, childrenOf: childrenOf };
}

// The Running card's last row: how many top-level listeners (an app with
// its helpers counts once) are on a port no app launched from here holds.
// The running list comes from the same tab's poll, so both are current
// together; before the first probe the row says it is still checking.
export function renderOtherPortsRow() {
  if (!els.otherPortsMeta) return;
  if (state.listeners == null) {
    els.otherPortsMeta.textContent = 'Checking…';
    return;
  }
  const ours = new Set();
  state.runningApps.forEach(function (r) { if (r.port) ours.add(r.port); });
  const n = groupListeners(state.listeners).topLevel
    .filter(function (l) { return !ours.has(l.port); }).length;
  els.otherPortsMeta.textContent = n
    ? n + (n === 1 ? ' listener' : ' listeners') + ' not started here'
    : 'None besides these';
}

function renderListeners() {
  renderOtherPortsRow();
  const items = state.listeners || [];
  const host = els.listenersList;
  host.innerHTML = '';
  els.listenersEmpty.hidden = items.length !== 0;

  const groups = groupListeners(items);
  groups.topLevel.forEach(function (l) {
    const kids = groups.childrenOf[l.port] || [];
    host.appendChild(buildListenerRow(l, false, kids.length > 0));
    if (expandedListenerPorts.has(l.port)) {
      kids.forEach(function (c) {
        host.appendChild(buildListenerRow(c, true, false));
      });
    }
  });
  listenerMenu.endRender();
}

async function killListener(l, label) {
  const ok = await confirmDialog({
    title: 'Stop ' + label + '?',
    message: 'This ends pid ' + l.pid + ' on :' + l.port + '.',
    action: 'Stop process',
  });
  if (!ok) return;
  try {
    const r = await jsonApi('/api/ports/' + l.port + '/kill', { method: 'POST' });
    toast('Killed ' + (r.killed || []).length + ' pid(s) on :' + l.port + '.', 'good');
    fetchListeners();
  } catch (exc) {
    apiFailToast('Kill failed', exc);
  }
}

// One listener as an action-row (#1129). A parent whose helper services
// are folded under it (#480) toggles them on tap; any other row opens the
// app, like a running app's Open. The destructive Stop process lives in the
// ⋮ menu, last and confirmed — nine stacked red Kill buttons were the
// loudest thing in the app.
function buildListenerRow(l, isChild, hasChildren) {
  const label = (isChild ? (l.service || l.name) : (l.app || l.name)) || ('port ' + l.port);
  const expanded = expandedListenerPorts.has(l.port);
  const row = actionRow({
    id: l.port,
    className: 'listener-row' + (isChild ? ' child' : ''),
    title: label,
    meta: (l.name || '?') + ' · pid ' + l.pid,
    label: hasChildren
      ? (expanded ? 'Hide' : 'Show') + ' the helper services of ' + label
      : 'Open ' + label,
    disabled: !hasChildren && !l.url,
    hint: 'Set tailnet_host in config/config.json to enable Open',
    onMain: hasChildren
      ? function () {
        if (expandedListenerPorts.has(l.port)) expandedListenerPorts.delete(l.port);
        else expandedListenerPorts.add(l.port);
        renderListeners();
      }
      : function () { window.open(l.url, '_blank', 'noopener,noreferrer'); },
    kebabClass: 'listener-menu-anchor',
    kebabLabel: label + ' actions',
  });
  // The port is the scannable datum: it leads the context line as a chip.
  const port = document.createElement('span');
  port.className = 'listener-port';
  port.textContent = ':' + l.port;
  row.meta.insertBefore(port, row.meta.firstChild);

  if (hasChildren) {
    // Collapsed by default (#480); the chevron rotates open like the
    // panel-level disclosure idiom.
    row.li.classList.add('expandable');
    row.li.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    const chev = document.createElement('span');
    chev.className = 'listener-chevron';
    chev.setAttribute('aria-hidden', 'true');
    chev.textContent = '›';
    row.li.insertBefore(chev, row.kebab);
  }

  row.li.appendChild(listenerMenu.attach('port:' + l.port, row.kebab, [
    {
      className: 'listener-open-btn', glyph: 'globe',
      label: l.url ? 'Open ' + l.url : 'Set tailnet_host in config/config.json to enable Open',
      text: 'Open',
      disabled: !l.url,
      title: 'Set tailnet_host in config/config.json to enable Open',
      onTap: function () { window.open(l.url, '_blank', 'noopener,noreferrer'); },
    },
    {
      className: 'listener-copy-btn', glyph: 'copy',
      label: 'Copy ' + label + ' URL', text: 'Copy URL',
      disabled: !l.url,
      title: 'Set tailnet_host in config/config.json to enable Open',
      onTap: function () { copyUrl(l.url); },
    },
    {
      // `power` reads as "shut this down" (#782); `octagon-x` is this app's
      // *failure* mark (a killed job run).
      className: 'listener-kill', glyph: 'power', danger: true,
      label: 'Stop ' + label + ' (pid ' + l.pid + ')', text: 'Stop process',
      onTap: function () { killListener(l, label); },
    },
  ]));
  return row.li;
}

// The Running card's Other ports row opens the sheet, which re-probes on
// open so the list is never a poll old; Check again is its one action.
export function wireListeners() {
  els.otherPortsRow.addEventListener('click', function () {
    els.otherPortsSheet.showModal();
    fetchListeners().catch(function () {});
  });
  els.otherPortsSheetClose.addEventListener('click', function () {
    els.otherPortsSheet.close();
  });
  els.listenersCheckAgain.addEventListener('click', function () {
    fetchListeners().catch(function () {});
  });
}
