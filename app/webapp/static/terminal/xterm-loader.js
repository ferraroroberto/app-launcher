/* Loads the xterm stack (core, fit, web-links, WebGL addons + xterm.css)
 * on the first terminal open instead of at boot.
 *
 * The four UMD scripts are ~390 KB raw (~95 KB gzipped) and only a
 * terminal needs them, so a cold launch that never opens one no longer
 * downloads or parses them. They set the `window.Terminal`,
 * `window.FitAddon`, `window.WebLinksAddon` and `window.WebglAddon`
 * globals terminal.js reads, exactly as the classic <script> tags did.
 *
 * xterm measures its character cell from xterm.css when `term.open()`
 * runs, so the promise settles only once the stylesheet has loaded too.
 */

const VENDOR = '/static/vendor/';
// Each script and the global it defines, in execution order.
const SCRIPTS = [
  ['xterm.js', 'Terminal'],
  ['addon-fit.js', 'FitAddon'],
  ['addon-web-links.js', 'WebLinksAddon'],
  ['addon-webgl.js', 'WebglAddon'],
];
const STYLESHEET = VENDOR + 'xterm.css';

let _loading = null;

function stylesheetPresent() {
  return !!document.querySelector(`link[href="${STYLESHEET}"]`);
}

function stackPresent() {
  return stylesheetPresent() && SCRIPTS.every(function (s) { return window[s[1]]; });
}

function whenLoaded(el, what) {
  return new Promise(function (resolve, reject) {
    el.addEventListener('load', function () { resolve(); }, { once: true });
    el.addEventListener('error', function () {
      el.remove();
      reject(new Error(`Could not load ${what}`));
    }, { once: true });
  });
}

// xterm.css goes first in the head, ahead of the app's bundled stylesheet, so
// the app's own terminal rules still override it.
function addStylesheet() {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = STYLESHEET;
  const loaded = whenLoaded(link, 'xterm.css');
  document.head.prepend(link);
  return loaded;
}

// `async = false` keeps the scripts executing in list order (the addons
// after the core) while they still download in parallel.
function addScript(name) {
  const script = document.createElement('script');
  script.src = VENDOR + name;
  script.async = false;
  const loaded = whenLoaded(script, name);
  document.body.appendChild(script);
  return loaded;
}

/**
 * Resolve once the xterm globals and stylesheet are in the page.
 * Repeat calls share one load; after a failure the next call fetches only
 * the parts that did not arrive.
 *
 * @returns {Promise<void>}
 */
export function loadXterm() {
  if (stackPresent()) return Promise.resolve();
  if (!_loading) {
    const parts = SCRIPTS
      .filter(function (s) { return !window[s[1]]; })
      .map(function (s) { return addScript(s[0]); });
    if (!stylesheetPresent()) parts.push(addStylesheet());
    _loading = Promise.all(parts)
      .then(function () {})
      .catch(function (exc) {
        _loading = null;
        throw exc;
      });
  }
  return _loading;
}
