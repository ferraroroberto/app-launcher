/* Tab views imported on first open, not at boot.
 *
 * A launch shows one tab, but every static import is a request, and the
 * phone has six HTTP/1.1 connections to queue them on: the Board (22
 * modules) and Jobs (7) left the boot graph by loading here when their tab
 * first opens. Code, Life OS and Apps stay static: Code is the default
 * tab, and the other two serve every tab (the project list, the skills and
 * the Telegram settings).
 *
 * The specifiers stay literal `import('./x.js')` so the server stamps
 * `?v=<hash>` onto them like any static import (src/static_versioning.py):
 * an unstamped URL would load a second copy of the module, state and all.
 *
 * Nothing here imports a lazy view statically: a caller that needs one
 * (the chief row's Start, a Settings change to the Board's model list)
 * goes through loadView() or ifViewLoaded(). */

import { state } from './state.js';

// Each view's loader wires it once; open() is its first data read, which
// its own tab-click listener repeats on later visits.
const VIEWS = {
  board: {
    pane: 'paneBoard',
    load: function () {
      return Promise.all([import('./board.js'), import('./board/board-dispatch.js')])
        .then(function (mods) {
          const view = { board: mods[0], dispatch: mods[1] };
          view.board.wireBoard();
          // What this device last saw, while the live read is in flight.
          if (!state.board) view.board.paintLastKnownBoard();
          return view;
        });
    },
    open: function (view) { return view.board.fetchBoard(); },
  },
  jobs: {
    pane: 'paneJobs',
    load: function () {
      return import('./jobs.js').then(function (jobs) {
        jobs.wireJobs();
        return { jobs: jobs };
      });
    },
    open: function (view) { return view.jobs.fetchJobs(); },
  },
};

const pending = {};
const loaded = {};

// The pane says where its view is: `data-view-load` is "loading" from the
// tap until the view is wired, then "ready". A control tapped in between
// would race the wiring (the desktop Board opens its lanes as it wires), so
// the e2e tab helper waits on it.
function markPane(name, value) {
  const pane = document.getElementById(VIEWS[name].pane);
  if (!pane) return;
  if (value) pane.dataset.viewLoad = value;
  else delete pane.dataset.viewLoad;
}

// The view's modules, imported and wired once. A failed import (a dropped
// connection) is forgotten, so the next open tries again.
export function loadView(name) {
  if (!pending[name]) {
    markPane(name, 'loading');
    pending[name] = VIEWS[name].load().then(function (view) {
      loaded[name] = view;
      markPane(name, 'ready');
      return view;
    }, function (exc) {
      delete pending[name];
      markPane(name, null);
      throw exc;
    });
  }
  return pending[name];
}

// `fn(view)` only once the view is in; a view never opened has nothing to
// repaint or poll, so this returns undefined without importing it.
export function ifViewLoaded(name, fn) {
  return loaded[name] ? fn(loaded[name]) : undefined;
}

// Load the tab's view if it is a lazy one, then read its data. Resolves at
// once for a static tab. Boot calls it for the tab it lands on.
export function openView(tab) {
  if (!VIEWS[tab]) return Promise.resolve();
  return loadView(tab).then(function (view) { return VIEWS[tab].open(view); });
}

// The first switch to a lazy tab loads it (a second tap while it loads
// waits on the same import); later switches are the view's
// own (its tab listener reads fresh data). The tab the page boots on is
// boot()'s, since the nav announces it before this listener exists.
export function wireLazyViews() {
  document.addEventListener('launcher:tab', function (ev) {
    const tab = ev.detail && ev.detail.tab;
    if (!VIEWS[tab] || pending[tab]) return;
    openView(tab).catch(function (exc) {
      console.warn('lazy view: opening ' + tab + ' failed', exc);
    });
  });
}
