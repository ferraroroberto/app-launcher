/* Mermaid diagrams for rendered Markdown: the one shared renderer.
 *
 * markdown.js turns a ```mermaid fence into an ordinary code block (`.md-block`
 * whose header says `mermaid`, its copy button carrying the exact source) and
 * stays DOM-free. A view that wants diagrams calls renderMermaidIn(root) once
 * its HTML is in the page: each such block is swapped for the drawn diagram.
 * Chat replies never call this, so a Mermaid fence there stays a code block.
 *
 * The library is one pinned, vendored build loaded by a <script> the first
 * time a block needs drawing, never at startup. It runs in `strict` security
 * mode with no auto-start, so diagram text cannot inject HTML or script. Its
 * colours come from the app's own tokens at draw time, and a diagram is drawn
 * again from its kept source when the theme flips.
 *
 * A block that will not parse, or a script that will not load, keeps its code
 * listing and gains a one-line note: never a blank gap.
 */

// The version is in the file name: vendor files are not content-hashed, so a
// bump must change the URL or a phone keeps the old build from its cache.
const SCRIPT_URL = '/static/vendor/mermaid-12.1.0.min.js';
export const FAILED_NOTE = "Diagram couldn't be drawn";

// A diagram narrower than this on its own is left at its natural size; a wider
// one is scaled to the sheet down to DIAGRAM_FLOOR of its natural width, and
// scrolls sideways inside its box below that.
const DIAGRAM_FLOOR = 0.8;
const DIAGRAM_FLOOR_MAX_PX = 900;

let loading = null;
let counter = 0;
let observing = false;
const sources = new WeakMap();

// The tokens a diagram reads, with the fallback used when one is unset.
const TOKENS = {
  card: ['--card', '#ffffff'],
  card2: ['--card-2', '#f6f8fa'],
  cardOff: ['--card-off', '#f6f8fa'],
  fg: ['--fg', '#1f2328'],
  muted: ['--muted', '#656d76'],
  line: ['--line', '#d1d9e0'],
  accent: ['--accent', '#0969da'],
  font: ['--font', 'system-ui, sans-serif'],
};

/**
 * Mermaid's `base` theme variables from the app's tokens. `read(name)` returns
 * a token's text ('' when unset). Only literal colours are read: Mermaid's
 * colour maths cannot parse a computed `color-mix()` or relative colour.
 */
export function themeVariables(read) {
  const t = {};
  Object.keys(TOKENS).forEach(function (key) {
    t[key] = String(read(TOKENS[key][0]) || '').trim() || TOKENS[key][1];
  });
  return {
    background: t.card,
    mainBkg: t.card2,
    primaryColor: t.card2,
    primaryTextColor: t.fg,
    primaryBorderColor: t.accent,
    secondaryColor: t.cardOff,
    tertiaryColor: t.card,
    lineColor: t.muted,
    textColor: t.fg,
    titleColor: t.fg,
    nodeBorder: t.accent,
    clusterBkg: t.cardOff,
    clusterBorder: t.line,
    edgeLabelBackground: t.card,
    noteBkgColor: t.cardOff,
    noteTextColor: t.fg,
    noteBorderColor: t.line,
    fontFamily: t.font,
  };
}

/** The smallest width (px) a diagram of natural width `natural` may be scaled to. */
export function diagramMinWidth(natural) {
  const n = Number(natural);
  if (!(n > 0)) return 0;
  return Math.round(Math.min(n, DIAGRAM_FLOOR_MAX_PX) * DIAGRAM_FLOOR);
}

/**
 * The Mermaid fences under `root` not yet handled: `{block, source}` for each
 * `.md-block` whose header names `mermaid`. The source is the copy button's
 * `data-copy`, which markdown.js sets to the fence exactly as written.
 */
export function findMermaidBlocks(root) {
  const found = [];
  root.querySelectorAll('.md-block').forEach(function (block) {
    const lang = block.querySelector('.md-block-lang');
    const copy = block.querySelector('.md-block-copy');
    if (!lang || !copy || block.dataset.diagram) return;
    if (String(lang.textContent).trim().toLowerCase() !== 'mermaid') return;
    found.push({ block: block, source: copy.getAttribute('data-copy') || '' });
  });
  return found;
}

function readToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name);
}

function addScript() {
  return new Promise(function (resolve, reject) {
    const script = document.createElement('script');
    script.src = SCRIPT_URL;
    script.async = true;
    script.addEventListener('load', function () { resolve(); }, { once: true });
    script.addEventListener('error', function () {
      script.remove();
      reject(new Error('Could not load Mermaid'));
    }, { once: true });
    document.body.appendChild(script);
  });
}

// Repeat calls share one load; after a failure the next call tries again.
function loadMermaid() {
  if (globalThis.mermaid) return Promise.resolve(globalThis.mermaid);
  if (!loading) {
    loading = addScript().then(function () {
      if (!globalThis.mermaid) throw new Error('Mermaid did not start');
      return globalThis.mermaid;
    }).catch(function (exc) {
      loading = null;
      throw exc;
    });
  }
  return loading;
}

// Drawn on every pass because the tokens differ between light and dark.
function configure(mermaid) {
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'base',
    themeVariables: themeVariables(readToken),
    fontFamily: readToken('--font') || undefined,
    suppressErrorRendering: true,
  });
}

// The SVG for `source`, or null when it does not parse. Mermaid may leave a
// scratch element in the body when it fails; it is removed here.
async function draw(mermaid, source) {
  const id = 'mmd-' + (counter += 1);
  try {
    return (await mermaid.render(id, source)).svg;
  } catch (_) {
    document.getElementById('d' + id)?.remove();
    document.getElementById(id)?.remove();
    return null;
  }
}

function fit(box) {
  const svg = box.querySelector('svg');
  if (!svg) return;
  const view = (svg.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
  const floor = diagramMinWidth(view[2]);
  if (floor) svg.style.minWidth = floor + 'px';
}

function fill(box, svg) {
  box.innerHTML = svg;
  fit(box);
}

function failNote(block) {
  block.dataset.diagram = 'failed';
  const note = document.createElement('p');
  note.className = 'md-diagram-note muted small';
  note.textContent = FAILED_NOTE;
  block.after(note);
}

function diagramBox(source) {
  const box = document.createElement('div');
  box.className = 'md-diagram';
  box.setAttribute('role', 'img');
  box.setAttribute('aria-label', 'Diagram');
  sources.set(box, source);
  return box;
}

async function redrawAll() {
  const boxes = Array.from(document.querySelectorAll('.md-diagram'));
  if (!boxes.length || !globalThis.mermaid) return;
  configure(globalThis.mermaid);
  for (const box of boxes) {
    const svg = await draw(globalThis.mermaid, sources.get(box) || '');
    // A diagram that drew once keeps its picture if the second draw fails.
    if (svg && box.isConnected) fill(box, svg);
  }
}

// One watcher for the page's life: the theme toggle sets html[data-theme].
function watchTheme() {
  if (observing) return;
  observing = true;
  new MutationObserver(function () { redrawAll(); }).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });
}

/**
 * Replace every Mermaid code block under `root` with its diagram. Resolves once
 * all are settled; never rejects. Call it after `root.innerHTML` is set, and
 * leave `root` alone until it resolves.
 */
export async function renderMermaidIn(root) {
  const blocks = findMermaidBlocks(root);
  if (!blocks.length) return;
  let mermaid;
  try {
    mermaid = await loadMermaid();
  } catch (_) {
    blocks.forEach(function (b) { if (b.block.isConnected) failNote(b.block); });
    return;
  }
  configure(mermaid);
  for (const { block, source } of blocks) {
    if (!block.isConnected) continue;       // the view closed meanwhile
    const svg = await draw(mermaid, source);
    if (!block.isConnected) continue;
    if (svg === null) { failNote(block); continue; }
    const box = diagramBox(source);
    fill(box, svg);
    block.replaceWith(box);
  }
  watchTheme();
}
