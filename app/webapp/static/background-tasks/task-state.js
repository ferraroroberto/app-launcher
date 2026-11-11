/* A session background task's state, the browser copy of
 * src/session_view/task_state.py.
 *
 * The server sends `status: 'running'` until Claude Code reports how the task
 * ended, then that report (`completed`, `failed`, `killed`, ...), or `finished`
 * for one that is not a plain word. statusChip() is the one rule for the chip a
 * row shows: none while running or for a plain success, otherwise the status
 * in its tone. tests/unit/transcript/test_task_state.py holds the values equal
 * to the server's.
 */

export const TaskPhase = Object.freeze({
  RUNNING: 'running',
  FINISHED: 'finished',
});

// The status Claude Code reports for a plain success.
export const COMPLETED = 'completed';

// Tone per ended status that is not a plain success; any other word is neutral.
const STATUS_TONE = Object.freeze({
  failed: 'danger', killed: 'attention', stopped: 'attention', timeout: 'attention',
});

// The chip a task row shows, `{ label, tone }`, or null for none.
export function statusChip(task) {
  if (!task || task.status === TaskPhase.RUNNING || task.status === COMPLETED) return null;
  return { label: task.status, tone: STATUS_TONE[task.status] || 'neutral' };
}
