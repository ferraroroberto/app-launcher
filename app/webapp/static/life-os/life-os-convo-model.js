/* The conversation sheet's Model row: the model one resume runs on.
 *
 * The row names the model the conversation last ran on (read from its own
 * transcript when the sheet opens) and opens a picker for this resume only.
 * Resuming on the conversation's own model sends `keep_model`; any other pick
 * sends that `model` and no `keep_model`, so the server resumes the same
 * session with `--model` for that one session. The pick lives in this module
 * and nowhere else: it is cleared when the sheet closes, and nothing here
 * calls a settings route.
 */

import { els, state } from '../state.js';
import { jsonApi } from '../api.js';
import { modelChoices, modelPickRow } from '../shared/model-choices.js';

const AGENTS = ['claude', 'codex'];

// What the sheet last showed: { row, own, pick } while a conversation is
// open, null once it closes. `own` is the recorded model id, '' when unknown,
// null while the read is in flight; `pick` is the { value, label } chosen.
let shown = null;

// 'claude-opus-5-5' is the Opus family; a Codex id is its own value.
function ownValue(agent, raw) {
  if (!raw) return '';
  if (agent !== 'claude') return agent + ':' + raw;
  const family = /^claude-([a-z]+)/.exec(raw);
  return family ? 'claude:' + family[1] : '';
}

function ownLabel(row, own) {
  const hit = modelChoices(row.agent, state.config, own).find(function (c) { return c.value === own; });
  return hit ? hit.label : '';
}

function offered(row) {
  return !!(row && row.resumable && row.skill && AGENTS.includes(row.agent));
}

// What the row says when the transcript names no model: a Claude resume still
// keeps its own natively, a Codex one runs on the Codex default.
function unknownText(row) {
  return (row.agent === 'codex' ? 'Codex default' : 'Its own') + ' · not recorded';
}

function paintRow() {
  const row = els.lifeOsConvoModelRow;
  if (!row) return;
  const open = shown && offered(shown.row);
  row.parentElement.hidden = !open;
  if (!open) return;
  const own = ownValue(shown.row.agent, shown.own);
  const text = shown.pick ? shown.pick.label + ' · this resume'
    : (ownLabel(shown.row, own) || (shown.own === null ? '…' : unknownText(shown.row)));
  els.lifeOsConvoModelValue.textContent = text;
}

// Called whenever the conversation sheet is filled: a new conversation reads
// its own model once; a repaint of the same one (a rename) keeps the pick.
export function showConvoModel(row) {
  if (!shown || shown.row !== row) {
    shown = { row: row, own: null, pick: null };
    if (offered(row)) readOwnModel(shown);
  }
  paintRow();
}

async function readOwnModel(mine) {
  const url = '/api/life-os/skills/' + encodeURIComponent(mine.row.skill) +
    '/conversations/model?agent=' + encodeURIComponent(mine.row.agent) +
    '&sid=' + encodeURIComponent(mine.row.sid || '');
  let model = '';
  try {
    model = String((await jsonApi(url)).model || '');
  } catch (exc) {
    model = ''; // not known: the row says "Its own" and the resume still keeps it
  }
  if (shown !== mine) return; // the sheet closed or moved on
  mine.own = model;
  paintRow();
}

export function resetConvoModel() {
  shown = null;
}

// The resume's model fields: the conversation's own model kept, or the pick.
export function resumeModelFields(row) {
  const mine = shown && shown.row === row ? shown : null;
  return mine && mine.pick ? { model: mine.pick.value } : { keep_model: true };
}

function openPicker() {
  const sheet = els.lifeOsConvoModelSheet;
  if (!sheet || !shown || !offered(shown.row)) return;
  const own = ownValue(shown.row.agent, shown.own);
  const current = shown.pick ? shown.pick.value : own;
  els.lifeOsConvoModelList.replaceChildren.apply(
    els.lifeOsConvoModelList,
    modelChoices(shown.row.agent, state.config, own).map(function (c) {
      return modelPickRow(c, {
        own: own,
        current: current,
        track: 'life-os-convo.model-pick',
        onPick: function (choice) {
          if (shown) {
            shown.pick = choice.value === own ? null : choice;
            paintRow();
          }
          sheet.close();
        },
      });
    }),
  );
  if (!sheet.open) sheet.showModal();
}

export function wireConvoModel() {
  const sheet = els.lifeOsConvoModelSheet;
  if (!els.lifeOsConvoModelRow || !sheet) return;
  els.lifeOsConvoModelRow.addEventListener('click', openPicker);
  [els.lifeOsConvoModelClose, els.lifeOsConvoModelDone].forEach(function (b) {
    if (b) b.addEventListener('click', function () { sheet.close(); });
  });
  sheet.addEventListener('click', function (ev) {
    if (ev.target === sheet) sheet.close();
  });
}
