/* Apps tab port-listeners panel: what is bound on this machine right now,
 * with helper services collapsed under their parent app's row (#224/#480)
 * and a per-port Stop process in each row's ⋮ menu (#1129).
 *
 * Split out of apps.js in issue #723. Self-contained — it owns its own
 * fetch, its own expand state, and imports nothing from apps.js.
 */

import { els } from './state.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { actionRow } from './action-rows.js';
import { createRowMenu } from './row-menu.js';

const listenerMenu = createRowMenu('project-menu');

// ----------------------------------------------------------- listeners panel (Apps tab)
// Parent rows with dependent children keep them collapsed behind a tap
// (#480). Module-level so the expand state survives the poll's re-renders.
const expandedListenerPorts = new Set();
let lastListenerItems = [];

export async function fetchListeners() {
  try {
    const body = await jsonApi('/api/ports/probe');
    renderListeners(body.listeners || []);
  } catch (exc) {
    // Best-effort poll — don't spam toasts.
    logPollFailure('listeners fetch failed', exc);
  }
}

function renderListeners(items) {
  lastListenerItems = items;
  const host = els.listenersList;
  host.innerHTML = '';
  els.listenersEmpty.hidden = items.length !== 0;

  // Group helper services (parent_port set, parent present) under their
  // parent app's row so one app reads as one top-level entry — see #224.
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

  topLevel.forEach(function (l) {
    const kids = childrenOf[l.port] || [];
    host.appendChild(buildListenerRow(l, false, kids.length > 0));
    if (expandedListenerPorts.has(l.port)) {
      kids.forEach(function (c) {
        host.appendChild(buildListenerRow(c, true, false));
      });
    }
  });
  listenerMenu.endRender();
}

async function copyUrl(url) {
  try {
    await navigator.clipboard.writeText(url);
    toast('Link copied', 'good');
  } catch (exc) {
    apiFailToast('Could not copy the link', exc);
  }
}

async function killListener(l, label) {
  if (!confirm('Stop ' + label + '?\n\npid ' + l.pid + ' on :' + l.port)) return;
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
        renderListeners(lastListenerItems);
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
