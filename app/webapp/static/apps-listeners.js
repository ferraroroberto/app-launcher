/* Apps tab port-listeners panel: what is bound on this machine right now,
 * with helper services collapsed under their parent app's row (#224/#480)
 * and a per-port kill.
 *
 * Split out of apps.js in issue #723. Self-contained — it owns its own
 * fetch, its own expand state, and imports nothing from apps.js.
 */

import { els } from './state.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { icon } from './_vendored/icons/icons.js';

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
}

function buildListenerRow(l, isChild, hasChildren) {
  const row = document.createElement('div');
  row.className = isChild ? 'listener-row child' : 'listener-row';

  const meta = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = isChild
    ? ('↳ ' + (l.service || l.name || ('port ' + l.port)))
    : (l.app || l.name || ('port ' + l.port));
  const sub = document.createElement('span');
  sub.className = 'meta';
  sub.textContent = ' :' + l.port + ' · pid ' + l.pid + ' · ' + (l.name || '?');
  meta.appendChild(strong);
  meta.appendChild(sub);
  row.appendChild(meta);

  if (hasChildren) {
    // Collapsed by default (#480): the whole parent row is the tap target;
    // the chevron rotates open like the panel-level disclosure idiom.
    row.classList.add('expandable');
    row.setAttribute('aria-expanded', expandedListenerPorts.has(l.port) ? 'true' : 'false');
    const chev = document.createElement('span');
    chev.className = 'listener-chevron';
    chev.setAttribute('aria-hidden', 'true');
    chev.textContent = '›';
    row.appendChild(chev);
    row.addEventListener('click', function () {
      if (expandedListenerPorts.has(l.port)) expandedListenerPorts.delete(l.port);
      else expandedListenerPorts.add(l.port);
      renderListeners(lastListenerItems);
    });
  }

  const kill = document.createElement('button');
  kill.type = 'button';
  kill.innerHTML = icon('octagon-x') + ' Kill';
  kill.addEventListener('click', async function (ev) {
    // Kill on a parent row must never toggle the collapse (#480).
    ev.stopPropagation();
    const label = (isChild ? l.service : l.app) || ('port ' + l.port);
    if (!confirm('Kill ' + label + '?\n\npid ' + l.pid + ' on :' + l.port)) return;
    try {
      const r = await jsonApi('/api/ports/' + l.port + '/kill', { method: 'POST' });
      toast('Killed ' + (r.killed || []).length + ' pid(s) on :' + l.port + '.', 'good');
      fetchListeners();
    } catch (exc) {
      apiFailToast('Kill failed', exc);
    }
  });
  row.appendChild(kill);
  return row;
}
