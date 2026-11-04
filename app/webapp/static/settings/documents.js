/* Settings > Documents: the fleet's sources of truth, read on the phone.
 *
 * GET /api/documents lists the configured documents (title, repo, last
 * changed); a tap opens the reading sheet, which fetches
 * GET /api/documents/{id} behind the passkey gate and renders it with the
 * shared Markdown renderer in the same `.tr-md` box the Changed files Preview
 * uses, then draws its Mermaid blocks. The browser only ever sends an id from
 * the list, never a path.
 *
 * The group is hidden when nothing is configured. A list that could not be
 * read is a muted row with Retry, never an empty group.
 */

import { els } from '../state.js';
import { apiFailToast, jsonApi } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { renderMarkdown } from '../markdown.js';
import { renderMermaidIn } from '../shared/mermaid-render.js';
import { agoText } from '../board/chief-plan.js';
import { showPath } from './settings-sheets.js';

let documents = [];
let listed = false;
// The open document's request number: a slower answer for an earlier tap, or
// for a sheet since closed, is ignored.
let openSeq = 0;
let openId = null;
// The open document's page on github.com, or null when its repo has no GitHub
// remote (the button is then hidden). Only https://github.com links are kept.
let githubUrl = null;

function changedText(iso) {
  const ago = agoText(iso);
  return ago ? 'Updated ' + ago : 'Unavailable';
}

function rowFor(doc) {
  const row = els.documentRowTemplate.content.firstElementChild.cloneNode(true);
  row.dataset.docId = doc.id;
  row.querySelector('.settings-row-label').textContent = doc.title;
  row.querySelector('.settings-row-sub').textContent = doc.repo + ' · ' + changedText(doc.updated);
  return row;
}

function renderList() {
  els.documentsGroup.replaceChildren(...documents.map(rowFor));
  els.documentsSection.hidden = !documents.length;
}

async function fetchList(quiet) {
  try {
    const body = await jsonApi('/api/documents');
    documents = (body && body.documents) || [];
    listed = true;
    els.documentsError.hidden = true;
    renderList();
  } catch (exc) {
    // Rows already on screen stay; a list never read shows the Retry row.
    if (!listed) {
      els.documentsGroup.replaceChildren();
      els.documentsSection.hidden = false;
      els.documentsError.hidden = false;
    }
    if (!quiet) apiFailToast('Documents', exc);
  }
}

function setGithub(url) {
  githubUrl = typeof url === 'string' && url.startsWith('https://github.com/') ? url : null;
  els.documentGithub.hidden = !githubUrl;
}

function showState(message, withRetry) {
  els.documentBody.replaceChildren();
  els.documentState.hidden = false;
  els.documentStateMessage.textContent = message;
  els.documentRetry.hidden = !withRetry;
}

async function loadDocument(id) {
  const mine = (openSeq += 1);
  openId = id;
  const doc = documents.find(function (d) { return d.id === id; });
  els.documentSheetTitle.textContent = doc ? doc.title : 'Document';
  els.documentUpdated.textContent = '';
  setGithub(null);
  showState('Loading…', false);
  let body;
  try {
    body = await terminalJsonApi('/api/documents/' + encodeURIComponent(id));
  } catch (exc) {
    if (mine !== openSeq) return;
    showState("Couldn't read this document: " + (exc.message || exc), true);
    return;
  }
  if (mine !== openSeq) return;
  els.documentState.hidden = true;
  els.documentSheetTitle.textContent = body.title || els.documentSheetTitle.textContent;
  els.documentUpdated.textContent = changedText(body.updated) + ' · ' + body.repo;
  setGithub(body.github_url);
  els.documentBody.innerHTML = renderMarkdown(body.content || '', { hideComments: true });
  els.documentBody.scrollTop = 0;
  await renderMermaidIn(els.documentBody);
}

function closeDocument() {
  openSeq += 1;
  openId = null;
  setGithub(null);
  els.documentBody.replaceChildren();
}

export function wireDocuments() {
  if (!els.documentsGroup || !els.documentSheet) return;
  els.documentsGroup.addEventListener('click', function (ev) {
    const row = ev.target.closest('[data-doc-id]');
    if (!row) return;
    showPath(['documentSheet']);
    loadDocument(row.dataset.docId);
  });
  els.documentRetry.addEventListener('click', function () {
    if (openId) loadDocument(openId);
  });
  els.documentGithub.addEventListener('click', function () {
    if (githubUrl) window.open(githubUrl, '_blank', 'noopener,noreferrer');
  });
  els.documentsRetry.addEventListener('click', function () { fetchList(false); });
  els.documentSheet.addEventListener('close', closeDocument);
  // Read on each visit to Settings, so a row's time is never older than the file.
  document.addEventListener('launcher:tab', function (e) {
    if (e.detail && e.detail.tab === 'settings') fetchList(true);
  });
}
