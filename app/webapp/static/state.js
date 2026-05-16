/* Shared singletons: app state, DOM-element references, constants.
 *
 * State (mutable):
 *   state.tab          — 'claude' | 'apps'
 *   state.config       — /api/config payload (claude flags + scan paths)
 *   state.apps         — array from /api/apps (each entry carries its own .health)
 *   state.sessions     — array from /api/claude-code/sessions
 *   state.pendingScan  — array from /api/apps/scan, surfaced in scan dialog
 *   state.genPreview   — { workspaces, orphans } for the generate dialog
 *   state.webauthn     — { configured, enrollment_open, devices[] }
 *   state.terminal     — null when overlay closed, else { sid, ws, term, fit, onWindowResize }
 *   state.status       — /api/status payload (incl. terminal reachability)
 *   state.editMode     — boolean, persisted to localStorage (launcher.editMode)
 *
 * Auth: a bearer token is stored in localStorage. The page extracts it
 * from ?token=… on first load and strips it from the URL. On 401, the
 * login overlay shows; password → /api/login → bearer token.
 */

export const TOKEN_KEY = 'launcher.token';
export const TT_KEY = 'launcher.tt';
export const TT_EXP_KEY = 'launcher.tt.exp';

export const TUNNEL_POLL_MS = 4000;       // refresh tunnel-kind URLs + health
export const SESSIONS_POLL_MS = 5000;     // refresh running Claude Code sessions
export const LISTENERS_POLL_MS = 5000;    // refresh running apps (port listeners)
export const WEBAUTHN_POLL_MS = 15000;

export const state = {
  tab: 'claude',
  config: null,
  apps: [],
  sessions: [],
  pendingScan: [],
  genPreview: null,
  webauthn: { configured: false, enrollment_open: false, devices: [] },
  terminal: null,   // { sid, ws, term, fit, onWindowResize }
  status: null,     // /api/status payload (incl. terminal reachability)
  // Edit mode (Settings toggle) reveals rename + remove on every row,
  // so the lists stay icon-free in normal use. Persisted across reloads.
  editMode: localStorage.getItem('launcher.editMode') === '1',
};

// ES modules are deferred — they execute after DOMContentLoaded, so
// document.getElementById is safe to call at module top level.
export const els = {
  tabClaude: document.getElementById('tabClaude'),
  tabApps: document.getElementById('tabApps'),
  paneClaude: document.getElementById('paneClaude'),
  paneApps: document.getElementById('paneApps'),

  claudeModel: document.getElementById('claudeModel'),
  claudeEffort: document.getElementById('claudeEffort'),
  claudeVerbose: document.getElementById('claudeVerbose'),
  claudeDebug: document.getElementById('claudeDebug'),
  claudeDetached: document.getElementById('claudeDetached'),
  claudeFlagsPreview: document.getElementById('claudeFlagsPreview'),
  claudeList: document.getElementById('claudeList'),
  claudeEmpty: document.getElementById('claudeEmpty'),
  sessionsList: document.getElementById('sessionsList'),
  sessionsEmpty: document.getElementById('sessionsEmpty'),
  refreshSessions: document.getElementById('refreshSessions'),
  appsList: document.getElementById('appsList'),
  appsEmpty: document.getElementById('appsEmpty'),

  rescanBtn: document.getElementById('rescanBtn'),
  settingsPanel: document.getElementById('settingsPanel'),
  editMode: document.getElementById('editMode'),
  projectsDir: document.getElementById('projectsDir'),
  appsScanRoot: document.getElementById('appsScanRoot'),
  saveSettings: document.getElementById('saveSettings'),
  listenersList: document.getElementById('listenersList'),
  listenersEmpty: document.getElementById('listenersEmpty'),
  refreshListeners: document.getElementById('refreshListeners'),
  statusReadout: document.getElementById('statusReadout'),

  scanDialog: document.getElementById('scanDialog'),
  scanResults: document.getElementById('scanResults'),
  scanCancel: document.getElementById('scanCancel'),
  scanSave: document.getElementById('scanSave'),

  genBatBtn: document.getElementById('genBatBtn'),
  genDialog: document.getElementById('genDialog'),
  genResults: document.getElementById('genResults'),
  genCancel: document.getElementById('genCancel'),
  genRun: document.getElementById('genRun'),

  renameDialog: document.getElementById('renameDialog'),
  renameForm: document.getElementById('renameForm'),
  renameInput: document.getElementById('renameInput'),
  renameCancel: document.getElementById('renameCancel'),

  toast: document.getElementById('toast'),

  loginOverlay: document.getElementById('loginOverlay'),
  loginForm: document.getElementById('loginForm'),
  loginPassword: document.getElementById('loginPassword'),
  loginError: document.getElementById('loginError'),

  terminalOverlay: document.getElementById('terminalOverlay'),
  terminalBar: document.querySelector('.terminal-bar'),
  terminalBack: document.getElementById('terminalBack'),
  terminalTitle: document.getElementById('terminalTitle'),
  terminalHost: document.getElementById('terminalHost'),
  terminalStatus: document.getElementById('terminalStatus'),
  terminalImage: document.getElementById('terminalImage'),
  terminalImageInput: document.getElementById('terminalImageInput'),
  terminalPaste: document.getElementById('terminalPaste'),
  terminalJumpEnd: document.getElementById('terminalJumpEnd'),
  terminalCtrlC: document.getElementById('terminalCtrlC'),
  terminalQuit: document.getElementById('terminalQuit'),

  webauthnStatus: document.getElementById('webauthnStatus'),
  webauthnDevices: document.getElementById('webauthnDevices'),
  enrollDeviceBtn: document.getElementById('enrollDeviceBtn'),
};
