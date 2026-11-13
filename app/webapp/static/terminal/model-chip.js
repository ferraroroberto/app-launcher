/* The session overlay bar's model chip: names the session's model and, for an
 * agent with an in-session switch, opens the model picker. A pick goes to
 * POST /api/claude-code/sessions/<sid>/model, which types the agent's own
 * /model for that session only (new sessions keep the owner's default),
 * answers Claude Code's "Switch model?" dialog, and waits for the agent to
 * report the new model. The chip changes model only on that report,
 * never optimistically: a switch the server could not confirm leaves the old
 * model showing and says why.
 */

import { els, state } from '../state.js';
import { apiFailToast, sessionRoute, toast } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { bindLongPressHint } from '../long-press-hint.js';
import { modelChoices, modelPickRow, modelValueForLabel } from '../shared/model-choices.js';

// Agents with an in-session /model the server can drive, and what the switch
// does there, shown on the sheet (each agent's own wording, probed live).
const SWITCH_NOTES = {
  claude: 'Claude switches for this session only and re-reads the whole conversation on the new model.',
  codex: 'Codex switches for this session only.',
};
const AGENT_NAMES = {
  pi: 'Pi', copilot: 'Copilot', grok: 'Grok', antigravity: 'Antigravity', agy: 'Antigravity',
};

// The switch in flight, or the last one the server could not confirm:
// { sid, phase: 'switching' | 'unconfirmed', target, reason }.
let pending = null;

function agentOf(s) {
  return String((s && s.agent) || 'claude').toLowerCase();
}

function viewSession() {
  return state.sessionView ? state.sessionView.session : null;
}

// Why this session's chip can't switch, or null when it can.
export function modelSwitchReason(s) {
  const agent = agentOf(s);
  if (!SWITCH_NOTES[agent]) {
    return (AGENT_NAMES[agent] || 'This agent') + " can't switch model inside a session";
  }
  if (s && s.kind === 'remote') {
    return 'Detached session: switch the model with /model in the PC console';
  }
  return null;
}

function chipTitle(model, mine, reason) {
  if (mine && mine.phase === 'switching') return 'Switching to ' + mine.target + '…';
  if (mine) return 'Switch to ' + mine.target + ' not confirmed: ' + mine.reason;
  if (reason) return 'Model: ' + model + '. ' + reason;
  return 'Model: ' + model + '. Tap to switch';
}

// Paint the chip for `session`: its model, and whether a tap can switch it.
export function renderModelChip(session) {
  const chip = els.terminalModel;
  if (!chip) return;
  const model = String((session && session.model) || '').trim();
  const sid = session && session.session_id;
  if (pending && pending.sid === sid && pending.phase === 'unconfirmed' && model === pending.target) {
    pending = null; // the agent reported it after all
  }
  const mine = pending && pending.sid === sid ? pending : null;
  (els.terminalModelLabel || chip).textContent = model;
  chip.hidden = !model;
  if (mine) chip.dataset.switch = mine.phase;
  else delete chip.dataset.switch;
  chip.setAttribute('aria-busy', String(!!mine && mine.phase === 'switching'));
  if (!model) {
    chip.removeAttribute('title');
    chip.removeAttribute('aria-label');
    return;
  }
  const reason = modelSwitchReason(session);
  if (reason) chip.setAttribute('aria-disabled', 'true');
  else chip.removeAttribute('aria-disabled');
  chip.title = chipTitle(model, mine, reason);
  chip.setAttribute('aria-label', chip.title);
}

function openModelPicker(s) {
  const sheet = els.modelSwitchSheet;
  if (!sheet) return;
  const agent = agentOf(s);
  // The session reports its model by label; its own model is the running one.
  const own = modelValueForLabel(agent, state.config, String(s.model || ''));
  els.modelSwitchSheetNote.textContent = SWITCH_NOTES[agent];
  els.modelSwitchList.replaceChildren.apply(
    els.modelSwitchList,
    modelChoices(agent, state.config, own).map(function (m) {
      return modelPickRow(m, {
        own: own,
        current: own,
        track: 'session.model-pick',
        onPick: function (choice) {
          els.modelSwitchSheet.close();
          if (choice.value !== own) switchModel(s, choice);
        },
      });
    }),
  );
  if (!sheet.open) sheet.showModal();
}

function repaintIfShowing(sid) {
  const s = viewSession();
  if (s && s.session_id === sid) renderModelChip(s);
}

async function switchModel(s, m) {
  const sid = s.session_id;
  pending = { sid: sid, phase: 'switching', target: m.label, reason: '' };
  repaintIfShowing(sid);
  toast('Switching to ' + m.label + '…');
  let res;
  try {
    res = await terminalJsonApi(sessionRoute(sid, '/model'), {
      method: 'POST',
      body: { model: m.id },
    });
  } catch (exc) {
    pending = { sid: sid, phase: 'unconfirmed', target: m.label,
      reason: (exc && exc.message) || 'the switch failed' };
    repaintIfShowing(sid);
    apiFailToast('Switch to ' + m.label + ' failed', exc);
    return;
  }
  if (res && res.outcome === 'switched') {
    pending = null;
    // The server's answer is the agent's own report of the model.
    s.model = res.model;
    repaintIfShowing(sid);
    toast('Switched to ' + res.model, '', { icon: 'circle-check' });
    return;
  }
  const reason = (res && res.detail) || 'the agent did not report it';
  pending = { sid: sid, phase: 'unconfirmed', target: m.label, reason: reason };
  repaintIfShowing(sid);
  toast('Switch to ' + m.label + ' not confirmed: ' + reason, 'error');
}

export function wireModelChip() {
  const chip = els.terminalModel;
  if (!chip) return;
  bindLongPressHint(chip);
  chip.addEventListener('click', function () {
    const s = viewSession();
    if (!s) return;
    const reason = modelSwitchReason(s);
    if (reason) {
      toast(reason, '', { icon: 'info' });
      return;
    }
    if (pending && pending.sid === s.session_id && pending.phase === 'switching') {
      toast('Switching to ' + pending.target + '…');
      return;
    }
    openModelPicker(s);
  });
  [els.modelSwitchSheetClose, els.modelSwitchSheetDone].forEach(function (b) {
    if (b) b.addEventListener('click', function () { els.modelSwitchSheet.close(); });
  });
  if (els.modelSwitchSheet) {
    els.modelSwitchSheet.addEventListener('click', function (ev) {
      if (ev.target === els.modelSwitchSheet) els.modelSwitchSheet.close();
    });
  }
}
