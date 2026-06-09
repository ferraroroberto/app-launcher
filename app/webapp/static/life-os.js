/* Life OS tab (issue #102): skill tiles + one-tap launch + a read-only
 * private-content browser.
 *
 * ~80% a clone of the Coding tab. A tile launches a Claude session in the
 * life-os repo that auto-invokes the bare /<skill> slash-command; the
 * ☁️ Detached + opus toggles live in the Life OS options card (same UX as
 * the Coding-options Detached toggle). The 📖 Browse button opens an
 * overlay that reads each skill's files — public SKILL.md/description.md
 * plus the private context/memory/examples/conversations + shared
 * identity. Those content endpoints are Tailscale + passkey gated
 * server-side, so a fetch may 403; we surface the reason rather than a
 * blank pane.
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { fetchSessions } from './sessions.js';
import { openTerminal } from './terminal.js';

// ----------------------------------------------------------- skills list
export async function fetchSkills() {
  try {
    const body = await jsonApi('/api/life-os/skills');
    state.lifeOsSkills = body.skills || [];
    state.lifeOsAvailable = !!body.available;
    renderSkills();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      console.warn('life-os skills fetch failed', exc);
    }
  }
}

export function renderSkills() {
  const host = els.lifeOsList;
  if (!host) return;
  host.innerHTML = '';
  const skills = state.lifeOsSkills;
  els.lifeOsEmpty.hidden = skills.length !== 0;

  skills.forEach(function (s) {
    const li = document.createElement('li');
    li.className = 'app-item coding-item lifeos-item';
    li.dataset.id = s.id;

    const main = document.createElement('div');
    main.className = 'app-main';
    const name = document.createElement('div');
    name.className = 'coding-name';
    name.textContent = s.name;   // name only — one line per tile
    main.appendChild(name);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions agent-actions';

    // 📖 Browse — open the read-only content browser for this skill.
    const browseBtn = document.createElement('button');
    browseBtn.type = 'button';
    browseBtn.className = 'icon-btn agent-btn';
    browseBtn.textContent = '📖';
    browseBtn.title = 'Browse what this skill knows';
    browseBtn.setAttribute('aria-label', 'Browse ' + s.name);
    browseBtn.addEventListener('click', function () { openBrowser(s); });
    actions.appendChild(browseBtn);

    // Launch — fires a fresh Claude session that auto-invokes /<skill>.
    const launchBtn = document.createElement('button');
    launchBtn.type = 'button';
    launchBtn.className = 'icon-btn agent-btn lifeos-launch';
    launchBtn.textContent = '🚀';
    launchBtn.title = 'Launch ' + s.name;
    launchBtn.setAttribute('aria-label', 'Launch ' + s.name);
    launchBtn.addEventListener('click', function () { launchSkill(s); });
    actions.appendChild(launchBtn);

    li.appendChild(actions);
    host.appendChild(li);
  });
}

async function launchSkill(s) {
  // Resume (issue #151) reopens Claude's session picker (dropping the
  // /<skill> prompt) in a streamed PTY — it wins over Detached.
  const resume = !!(els.lifeOsResume && els.lifeOsResume.checked);
  const mode = (!resume && els.lifeOsDetached && els.lifeOsDetached.checked)
    ? 'remote' : 'pty';
  const opus = !!(els.lifeOsOpus && els.lifeOsOpus.checked);
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(s.id) + '/launch',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode, opus: opus, resume: resume }),
      }
    );
    toast(
      (resume ? '↺ Resumed ' : '🌱 Launched ') + s.name +
        (opus ? ' (Opus)' : '') + (mode === 'remote' ? ' (detached)' : ''),
      'good'
    );
    if (body.session) {
      fetchSessions().catch(function () {});
      // Full-control sessions drop straight into the terminal; detached
      // ones only appear in the Coding tab's running-sessions list.
      if (body.session.kind !== 'remote') openTerminal(body.session);
    }
  } catch (exc) {
    toast('Launch failed: ' + (exc.message || exc), 'error');
  }
}

// --------------------------------------------------- content browser
// The file currently shown in the doc view — drives the toolbar 🗑️ (which
// deletes conversation logs only). Null while we're on the file list.
let openDocFile = null;

async function openBrowser(s) {
  state.lifeOsBrowser = { skillId: s.id, name: s.name, files: [] };
  els.lifeOsBrowserTitle.textContent = s.name;
  els.lifeOsBrowserStatus.hidden = true;
  closeDoc();                       // start on the full-screen file list
  els.lifeOsBrowser.hidden = false;
  await loadFileList();
}

// (Re)load the current skill's file list — used on open and by 🔄 refresh
// (a new conversation log won't appear until the list is reloaded).
async function loadFileList() {
  const b = state.lifeOsBrowser;
  if (!b) return;
  els.lifeOsFileList.innerHTML = '<li class="muted small">Loading…</li>';
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(b.skillId) + '/files'
    );
    b.files = body.files || [];
    renderFileList(b.files);
  } catch (exc) {
    // The content endpoints are Tailscale + passkey gated — a 403 here
    // means this connection can't reach them. Say so plainly, in the
    // (full-screen) list area.
    const msg = (exc && exc.status === 403)
      ? 'The content browser is Tailscale-only (and passkey-gated). Open the ' +
        'launcher over your Tailscale URL on an enrolled device.'
      : 'Could not load files: ' + (exc.message || exc);
    els.lifeOsFileList.innerHTML = '';
    const li = document.createElement('li');
    li.className = 'muted small';
    li.textContent = msg;
    els.lifeOsFileList.appendChild(li);
  }
}

function renderFileList(files) {
  const host = els.lifeOsFileList;
  host.innerHTML = '';
  if (!files.length) {
    const p = document.createElement('li');
    p.className = 'muted small';
    p.textContent = 'No readable files.';
    host.appendChild(p);
    return;
  }
  let lastCat = null;
  files.forEach(function (f) {
    if (f.category !== lastCat) {
      const h = document.createElement('li');
      h.className = 'lifeos-file-cat';
      h.textContent = f.category;
      host.appendChild(h);
      lastCat = f.category;
    }
    const li = document.createElement('li');
    li.className = 'lifeos-file-row';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lifeos-file-btn';
    btn.textContent = f.name;
    btn.title = f.path;
    btn.addEventListener('click', function () {
      Array.prototype.forEach.call(
        host.querySelectorAll('.lifeos-file-btn.active'),
        function (b) { b.classList.remove('active'); }
      );
      btn.classList.add('active');
      loadFile(f);
    });
    li.appendChild(btn);
    // No delete control in the list — the list is navigation only. The 🗑️
    // for a disposable conversation log lives in the document toolbar and
    // appears once the log is open (see openDoc / loadFile below).
    host.appendChild(li);
  });
}

async function deleteFile(f) {
  if (!confirm(
    'Delete this conversation log?\n\n' + f.name +
    '\n\nThe file is removed from disk — this cannot be undone.'
  )) return;
  try {
    await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path),
      { method: 'DELETE' }
    );
    toast('🗑️ Deleted ' + f.name, 'good');
    closeDoc();             // in case the deleted file was the open one
    await loadFileList();
  } catch (exc) {
    toast('Delete failed: ' + (exc.message || exc), 'error');
  }
}

// Lower-case, spaces (and any other punctuation) → single dashes, trimmed —
// the same shape the capture hook's slugs already have.
function slugify(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

async function renameFile(f) {
  const proposed = window.prompt(
    'Rename this conversation log.\n\n' +
    'The date keeps unchanged — type the new name (spaces become dashes, ' +
    'lower-cased):',
    ''
  );
  if (proposed === null) return;            // cancelled
  const slug = slugify(proposed);
  if (!slug) { toast('Name cannot be empty', 'error'); return; }
  try {
    const body = await jsonApi('/api/life-os/file/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: f.path, slug: slug }),
    });
    toast('✏️ Renamed to ' + (body.name || slug), 'good');
    closeDoc();             // name (and path) changed — back to the list
    await loadFileList();
  } catch (exc) {
    toast('Rename failed: ' + (exc.message || exc), 'error');
  }
}

async function loadFile(f) {
  // The file view is a full-screen layer over the list; the ✕ close-doc
  // button in the bar appears only while it's open.
  openDocFile = f;
  openDoc(f);
  els.lifeOsFileContent.innerHTML = '<p class="muted small">Loading…</p>';
  try {
    const body = await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path)
    );
    els.lifeOsFileContent.innerHTML = renderMarkdown(body.content || '');
    if (body.truncated) {
      const note = document.createElement('p');
      note.className = 'muted small';
      note.textContent = '… (truncated)';
      els.lifeOsFileContent.appendChild(note);
    }
    els.lifeOsFileContent.scrollTop = 0;
  } catch (exc) {
    els.lifeOsFileContent.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'Could not load: ' + (exc.message || exc);
    els.lifeOsFileContent.appendChild(p);
  }
}

// A conversation log the toolbar may act on — any file under a skill's
// conversations/ EXCEPT the .gitkeep placeholder that keeps the (otherwise
// empty) dir tracked in git. Deleting/renaming that would untrack the dir,
// so it stays off-limits (the server refuses it too — defence in depth).
function isEditableLog(f) {
  return !!f && f.category === 'conversations' &&
    !/(^|\/)\.gitkeep$/.test(f.name || '');
}

// Reveal the full-screen file view (overlaying the list) + the ✕ button.
// The 🗑️ delete and ✏️ rename show only for a conversation log — disposable
// run transcripts, editable while you read them. Every other category (and
// the .gitkeep placeholder) keeps both hidden.
function openDoc(f) {
  els.lifeOsFileContent.hidden = false;
  if (els.lifeOsDocClose) els.lifeOsDocClose.hidden = false;
  const editable = isEditableLog(f);
  if (els.lifeOsDocDelete) els.lifeOsDocDelete.hidden = !editable;
  if (els.lifeOsDocRename) els.lifeOsDocRename.hidden = !editable;
}

// Close the open file → back to the full-screen file list.
function closeDoc() {
  openDocFile = null;
  els.lifeOsFileContent.hidden = true;
  els.lifeOsFileContent.innerHTML = '';
  if (els.lifeOsDocClose) els.lifeOsDocClose.hidden = true;
  if (els.lifeOsDocDelete) els.lifeOsDocDelete.hidden = true;
  if (els.lifeOsDocRename) els.lifeOsDocRename.hidden = true;
  Array.prototype.forEach.call(
    els.lifeOsFileList.querySelectorAll('.lifeos-file-btn.active'),
    function (b) { b.classList.remove('active'); }
  );
}

// Close the whole browser → back to the skill tiles.
function closeBrowser() {
  state.lifeOsBrowser = null;
  closeDoc();
  els.lifeOsBrowser.hidden = true;
}

// ------------------------------------------------ minimal markdown render
// Escape-first, then apply a small, safe subset (headings, bold, italic,
// inline code, fenced code, links, unordered lists, paragraphs). Content
// comes from the user's own private files over a passkey-gated tailnet
// link, but we still escape every byte before formatting so a stray
// `<script>` in a note can never execute.
function escapeHtml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

export function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  const out = [];
  let inCode = false;
  let inList = false;
  let para = [];

  function flushPara() {
    if (para.length) {
      out.push('<p>' + inlineMd(para.join(' ')) + '</p>');
      para = [];
    }
  }
  function flushList() {
    if (inList) { out.push('</ul>'); inList = false; }
  }

  lines.forEach(function (line) {
    if (line.trim().startsWith('```')) {
      flushPara(); flushList();
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { out.push('<pre class="md-code"><code>'); inCode = true; }
      return;
    }
    if (inCode) { out.push(line); return; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const level = h[1].length;
      out.push('<h' + level + '>' + inlineMd(h[2]) + '</h' + level + '>');
      return;
    }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      flushPara();
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push('<li>' + inlineMd(li[1]) + '</li>');
      return;
    }
    if (!line.trim()) { flushPara(); flushList(); return; }
    para.push(line.trim());
  });
  if (inCode) out.push('</code></pre>');
  flushPara(); flushList();
  return out.join('\n');
}

// --------------------------------------------------------------- wire
export function wireLifeOs() {
  if (els.lifeOsBrowserBack) {
    els.lifeOsBrowserBack.addEventListener('click', closeBrowser);
  }
  if (els.lifeOsDocClose) {
    els.lifeOsDocClose.addEventListener('click', closeDoc);
  }
  if (els.lifeOsDocDelete) {
    // Delete the open conversation log → confirm, DELETE, back to the list
    // (deleteFile closeDoc()s, exactly like ✕).
    els.lifeOsDocDelete.addEventListener('click', function () {
      if (openDocFile) deleteFile(openDocFile);
    });
  }
  if (els.lifeOsDocRename) {
    // Rename the open conversation log → prompt, POST, back to the list.
    els.lifeOsDocRename.addEventListener('click', function () {
      if (openDocFile) renameFile(openDocFile);
    });
  }
  if (els.lifeOsBrowserRefresh) {
    els.lifeOsBrowserRefresh.addEventListener('click', function () {
      loadFileList().catch(function () {});
    });
  }
  // Refresh skills the moment the tab opens (cheap, live directory scan).
  if (els.tabLifeOS) {
    els.tabLifeOS.addEventListener('click', function () {
      fetchSkills().catch(function () {});
    });
  }
}
