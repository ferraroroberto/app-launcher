// Pure-function pins for how a session is named, under plain Node: the
// Coding row / Board card title precedence (sessions.js sessionTitle) and the
// PC mirror window's OS title (terminal.js mirrorDocTitle).
// Invoked by tests/unit/static_ui/test_node_module_helpers.py.

import './_browser_stubs.mjs';
import assert from 'node:assert/strict';

globalThis.location = { protocol: 'http:', host: 'test.invalid' };

const { sessionTitle } = await import('../../app/webapp/static/sessions.js');
const { mirrorDocTitle } = await import('../../app/webapp/static/terminal.js');

// sessionTitle: a manual rename wins outright, then a genuine shared title,
// then a real live summary; a folder-echo live title (Codex "<folder> |
// <model>", Pi "π - <folder>") yields to the first-prompt title; a derived
// shared name ("<project>-N") beats a bare folder echo; the launch name last.
// Only Claude emits a genuine per-conversation OSC title; Antigravity and
// Copilot emit none.
{
  const dir = 'C:/code/app-launcher';   // basename 'app-launcher'
  assert.equal(sessionTitle({
    live_title: '✳ Fixing the login flow bug',
    prompt_title: 'something else', project_dir: dir, name: 'app-launcher',
  }), 'Fixing the login flow bug', "a real Claude summary wins over the first-prompt title");
  assert.equal(sessionTitle({
    live_title: 'app-launcher | gpt-5.5',
    prompt_title: 'add dark mode toggle', project_dir: dir, name: 'app-launcher',
  }), 'add dark mode toggle', "Codex's folder echo yields to the first-prompt title");
  assert.equal(sessionTitle({
    live_title: 'π - app-launcher',
    prompt_title: 'refactor the parser', project_dir: dir, name: 'app-launcher',
  }), 'refactor the parser', "Pi's folder echo yields to the first-prompt title");
  assert.equal(sessionTitle({
    live_title: '', prompt_title: 'wire up the API', project_dir: dir, name: 'app-launcher',
  }), 'wire up the API', 'no agent title at all: the first-prompt title shows');
  assert.equal(sessionTitle({
    live_title: '', prompt_title: '', project_dir: 'C:/code/myproj', name: 'myproj',
  }), 'myproj', 'nothing derived yet: the launch name');
  assert.equal(sessionTitle({
    live_title: 'app-launcher | gpt-5.5', prompt_title: '', project_dir: dir, name: 'app-launcher',
  }), 'app-launcher | gpt-5.5', 'a folder echo with no prompt yet keeps the echo');
  assert.equal(sessionTitle({
    shared_name: 'Fixing the chunk merge bug', shared_name_source: null,
    live_title: '✳ Some other in-terminal summary', prompt_title: 'x',
    project_dir: dir, name: 'app-launcher',
  }), 'Fixing the chunk merge bug', 'a genuine shared title wins over a live summary');
  assert.equal(sessionTitle({
    shared_name: 'app-launcher-2', shared_name_source: 'derived',
    live_title: '✳ Fixing the login flow bug', prompt_title: '',
    project_dir: dir, name: 'app-launcher',
  }), 'Fixing the login flow bug', 'a derived shared name yields to a real live summary');
  assert.equal(sessionTitle({
    shared_name: 'app-launcher-2', shared_name_source: 'derived',
    live_title: '', prompt_title: '', project_dir: dir, name: 'app-launcher',
  }), 'app-launcher-2', 'a derived shared name beats the bare project echo');
  assert.equal(sessionTitle({
    shared_name: null, shared_name_source: null,
    live_title: '', prompt_title: 'wire up the API', project_dir: dir, name: 'app-launcher',
  }), 'wire up the API', 'no shared name: the precedence below is untouched');
  assert.equal(sessionTitle({
    manual_title: 'My custom rename',
    shared_name: 'Fixing the chunk merge bug', shared_name_source: null,
    live_title: '✳ Some other in-terminal summary', prompt_title: 'x',
    project_dir: dir, name: 'app-launcher',
  }), 'My custom rename', 'a manual rename wins over every auto-derived source');
  assert.equal(sessionTitle({
    manual_title: '', live_title: '', prompt_title: 'wire up the API',
    project_dir: dir, name: 'app-launcher',
  }), 'wire up the API', 'an empty manual title does not shadow the precedence below it');
}

// mirrorDocTitle: the human title leads the Windows title bar, and the
// app-launcher-mirror-<sid> marker the launcher's EnumWindows scan matches
// (as a substring) to close and reconcile the window always trails it.
{
  const marker = 'app-launcher-mirror-abc123';
  const withTitle = mirrorDocTitle('abc123', 'Fixing the login bug');
  assert.equal(withTitle, `Fixing the login bug — ${marker}`, 'the human title leads, the marker trails');
  assert.ok(withTitle.includes(marker), 'the close marker survives in the mirror title');
  assert.equal(mirrorDocTitle('abc123', ''), marker, 'no human title: the bare marker');
}

console.log('OK');
