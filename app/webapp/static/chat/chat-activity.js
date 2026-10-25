/* The Chat pane's live activity: the strip's line for the turn in
 * progress, and the counter on the step whose call is still running.
 */

import { els } from '../state.js';
import { icon } from '../_vendored/icons/icons.js';
import { fmtElapsed, syncRunSummary } from './transcript-steps.js';
import { ACTIVITY_TICK_MS, liveAllowed, view } from './session-transcript.js';

// --- the strip's live line -----------------------------------------
//
// "[timer icon] 15:02 · 10 actions · Running Bash": the turn in progress, from
// the `activity` the transcript responses carry (src/session_transcript.py).
// Everything but the elapsed counter is the last read's word; the counter is
// redrawn here once a second from `since`. The timer is the vendored `i-timer`
// icon, never the emoji: iOS draws a colour stopwatch for it and
// desktop another glyph, and the emoji's fallback font metrics inflate the
// line box, which is what let the line sit low on the phone.

// The line, or null when nothing is mid-turn or the turn's start is unknown.
// `actions` is null for a harness that records no tool calls: omitted, never 0.
function activityLine(a) {
  if (!a || !a.working) return null;
  const since = new Date(a.since).getTime();
  if (Number.isNaN(since)) return null;
  const parts = [fmtElapsed(Date.now() - since)];
  if (a.actions != null) parts.push(a.actions + (a.actions === 1 ? ' action' : ' actions'));
  if (a.last) parts.push(a.last);
  return parts.join(' · ');
}

let activityTimer = null;

// The line's two children, built once: the timer icon (shown for the live
// line only) and the text, which alone is rewritten on every tick. The line
// is a centring flex row (styles/terminal.css), so neither child can lift or sink it.
function stripLineParts(el) {
  let text = el.querySelector('.terminal-activity-text');
  if (!text) {
    el.innerHTML = icon('timer', 'terminal-activity-icon');
    text = document.createElement('span');
    text.className = 'terminal-activity-text';
    el.appendChild(text);
  }
  return { icon: el.querySelector('.terminal-activity-icon'), text };
}

// Writes the strip's line: a forced read's note, else the activity line, and
// nothing outside Chat mode. Connection status outranks both in CSS (it hides
// this element while #terminalStatus shows). The counter's tick lives exactly
// as long as a live activity line does.
export function renderStripLine() {
  const el = els.terminalActivity;
  if (!el) return;
  const note = view && view.note;
  const live = !note && view && liveAllowed() ? activityLine(view.activity) : null;
  const text = note || live;
  const parts = stripLineParts(el);
  // The ticking counter must not be announced every second; a note should be.
  el.setAttribute('aria-live', note ? 'polite' : 'off');
  parts.text.textContent = text || '';
  parts.icon.style.display = live ? '' : 'none';
  el.hidden = !text;
  if (live && !activityTimer) {
    activityTimer = window.setInterval(renderStripLine, ACTIVITY_TICK_MS);
  } else if (!live && activityTimer) {
    window.clearInterval(activityTimer);
    activityTimer = null;
  }
  syncLiveStep();
}

// --- the step in progress ------------------------------------------
//
// While the agent is working and its newest tool call has no result yet, that
// call's step line counts up from the call's own timestamp in place of the
// run's duration. The result landing, the turn ending or the pane leaving
// Chat puts the duration back. An activity the server could not establish
// (null) never ticks: a counter that never stops would be a guess.

let stepTimer = null;

// The newest tool call, when it is in progress; else null.
export function liveStep() {
  if (!view || !view.entries || !liveAllowed() || !(view.activity && view.activity.working)) return null;
  for (let i = view.entries.length - 1; i >= 0; i -= 1) {
    const e = view.entries[i];
    if (e.kind !== 'tool_call') continue;
    const since = Date.parse(e.timestamp);
    return e.result == null && e.error !== true && !Number.isNaN(since) ? e : null;
  }
  return null;
}

// Put the counter on the step in progress, take it off one that no longer
// is, and redraw it; the tick lives exactly as long as a counter shows.
export function syncLiveStep() {
  const list = els.transcriptList;
  const live = liveStep();
  let shown = list ? list.querySelector('.tr-step-live') : null;
  if (list && (shown ? shown._trEntry : null) !== live) {
    const runs = Array.prototype.filter.call(list.querySelectorAll('.tr-run'), function (li) {
      return (shown && li.contains(shown)) || (live && (li._trRun || []).indexOf(live) !== -1);
    });
    runs.forEach(function (li) { syncRunSummary(li, live); });
    shown = list.querySelector('.tr-step-live');
  }
  if (shown) {
    shown.querySelector('.tr-step-live-text').textContent =
      fmtElapsed(Date.now() - Date.parse(shown._trEntry.timestamp));
  }
  if (shown && !stepTimer) {
    stepTimer = window.setInterval(syncLiveStep, ACTIVITY_TICK_MS);
  } else if (!shown && stepTimer) {
    window.clearInterval(stepTimer);
    stepTimer = null;
  }
}
