/* The mic button's tap handling: what each tap would do, as the usage log's
 * `data-track` id, and the fallback for a touch iOS sends no click for.
 * Takes the dictation's state `d` (voice.js) and reads only its recording,
 * arming and finalizing fields.
 */

import { usage } from '../usage.js';

// What a tap on the mic would do right now, as the usage log's `data-track`
// control id. The log's click listener reads the attribute when the tap lands,
// before toggle() runs, so the id is kept in step with the state: each tap is
// then logged under what it did. A phone's console is out of reach; the
// server-side log is not. Constant ids only (the usage-log privacy contract):
// nothing about the take is recorded.
function tapState(d) {
  if (d.lateClick) return 'late-click';
  if (d.arming) return 'arming';
  if (d.finishOp) return 'finalizing';
  if (d.recorder && d.recorder.state === 'recording') return 'stop';
  return 'start';
}

export function syncTrackId(d) {
  const button = d.button;
  if (!button) return;
  switch (tapState(d)) {
    case 'late-click': button.setAttribute('data-track', 'mic.late-click'); break;
    case 'arming': button.setAttribute('data-track', 'mic.ignored-arming'); break;
    case 'finalizing': button.setAttribute('data-track', 'mic.cancel-finalizing'); break;
    case 'stop': button.setAttribute('data-track', 'mic.stop'); break;
    default: button.setAttribute('data-track', 'mic.start');
  }
}

// A touch that ended on the mic with no click after it. iOS can withhold the
// click (its hover heuristics), and a withheld click reaches neither toggle()
// nor the usage log's click listener, so the tap was lost and the mic needed
// a second one. When no click follows in time the tap is run here instead,
// logged as `mic.no-click` plus what it did.
// iOS fires a click it means to send within tens of ms of touchend; half a
// second is past it yet still reads as one tap.
const _NO_CLICK_MS = 500;
// A click iOS delivers after the fallback already ran belongs to the same
// touch; swallowing clicks this long after touchend keeps it from undoing it.
const _LATE_CLICK_MS = 1500;

// The fallback tap's outcome, one row per tap as the click path logs it.
function trackFallbackTap(d) {
  switch (tapState(d)) {
    case 'arming': usage.track('mic.ignored-arming'); break;
    case 'finalizing': usage.track('mic.cancel-finalizing'); break;
    case 'stop': usage.track('mic.stop'); break;
    default: usage.track('mic.start');
  }
}

export function watchWithheldClick(d, toggle) {
  const button = d.button;
  let timer = null;
  let lateTimer = null;
  function endLateWindow() {
    clearTimeout(lateTimer);
    lateTimer = null;
    if (!d.lateClick) return;
    d.lateClick = false;
    syncTrackId(d);
  }
  button.addEventListener('pointerup', function (ev) {
    if (ev.pointerType !== 'touch' || button.disabled) return;
    // A touch pointer stays captured by the button, so a finger that slid off
    // still ends here; no click is due then.
    const hit = document.elementFromPoint(ev.clientX, ev.clientY);
    if (!hit || !button.contains(hit)) return;
    clearTimeout(timer);
    timer = setTimeout(function () {
      timer = null;
      // The composer may have closed or locked the mic in the meantime.
      if (button.disabled || !button.isConnected) return;
      console.info('[dictation] the touch ended on the mic but the browser sent no click: running the tap');
      usage.track('mic.no-click');
      trackFallbackTap(d);
      d.lateClick = true;
      syncTrackId(d);
      lateTimer = setTimeout(endLateWindow, _LATE_CLICK_MS - _NO_CLICK_MS);
      toggle(d);
    }, _NO_CLICK_MS);
  });
  // Registered before any caller's click handler, so it runs first.
  button.addEventListener('click', function (ev) {
    clearTimeout(timer);
    timer = null;
    if (!d.lateClick) return;
    endLateWindow();
    console.info('[dictation] late click ignored: the fallback already ran this tap');
    ev.stopImmediatePropagation();
  }, true);
}
