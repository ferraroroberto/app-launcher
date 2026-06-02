/* Shared singletons: app state, DOM-element references, constants.
 *
 * State (mutable):
 *   state.tab          — 'claude' | 'apps'
 *   state.config       — /api/config payload (claude flags + scan paths)
 *   state.apps         — array from /api/apps (each entry carries its own .health)
 *   state.agents       — array from /api/agents ({id,label,available} per agent)
 *   state.runningApps  — array from /api/apps/running (launcher-spawned apps)
 *   state.sessions     — array from /api/claude-code/sessions
 *   state.pendingScan  — array from /api/apps/scan, surfaced in scan dialog
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
export const LISTENERS_POLL_MS = 5000;    // refresh port listeners
export const RUNNING_APPS_POLL_MS = 4000; // refresh launcher-spawned apps
export const JOBS_POLL_MS = 4000;         // refresh Jobs tab while it's visible
export const WEBAUTHN_POLL_MS = 15000;

export const state = {
  tab: 'claude',
  config: null,
  apps: [],
  // Coding agents — overwritten by /api/agents at boot. The fallback
  // keeps the Coding tab usable if that fetch fails: Claude Code is the
  // launcher's core agent so it's assumed present; the other agents
  // stay disabled until detection confirms their CLI is on PATH.
  agents: [
    { id: 'claude', label: 'Claude Code', available: true },
    { id: 'antigravity', label: 'Antigravity CLI', available: false },
    { id: 'copilot', label: 'GitHub Copilot CLI', available: false },
  ],
  runningApps: [],
  jobs: [],
  jobRuns: {},      // job_id → array of recent runs (lazy)
  expandedJob: null, // job_id currently expanded inline (history visible)
  selectedRun: null, // { jobId, runId } — which run's log is in the panel
  sessions: [],
  // Life OS tab (issue #102): skills from /api/life-os/skills, plus the
  // read-only content browser's current skill + loaded files.
  lifeOsSkills: [],
  lifeOsAvailable: false,
  lifeOsBrowser: null,   // { skillId, name, files } while the browser is open
  pendingScan: [],
  webauthn: { configured: false, enrollment_open: false, devices: [] },
  terminal: null,   // { sid, ws, term, fit, onWindowResize }
  status: null,     // /api/status payload (incl. terminal reachability)
  // Edit mode (Settings toggle) reveals rename + remove on Apps tab
  // rows only — Coding tab rows are disk-scanned and never editable.
  // Persisted across reloads.
  editMode: localStorage.getItem('launcher.editMode') === '1',
};

// ES modules are deferred — they execute after DOMContentLoaded, so
// document.getElementById is safe to call at module top level.
export const els = {
  tabClaude: document.getElementById('tabClaude'),
  tabApps: document.getElementById('tabApps'),
  tabJobs: document.getElementById('tabJobs'),
  tabLifeOS: document.getElementById('tabLifeOS'),
  paneClaude: document.getElementById('paneClaude'),
  paneApps: document.getElementById('paneApps'),
  paneJobs: document.getElementById('paneJobs'),
  paneLifeOS: document.getElementById('paneLifeOS'),

  lifeOsOptions: document.getElementById('lifeOsOptions'),
  lifeOsOpus: document.getElementById('lifeOsOpus'),
  lifeOsDetached: document.getElementById('lifeOsDetached'),
  lifeOsList: document.getElementById('lifeOsList'),
  lifeOsEmpty: document.getElementById('lifeOsEmpty'),
  lifeOsDir: document.getElementById('lifeOsDir'),
  lifeOsBrowser: document.getElementById('lifeOsBrowser'),
  lifeOsBrowserBack: document.getElementById('lifeOsBrowserBack'),
  lifeOsBrowserTitle: document.getElementById('lifeOsBrowserTitle'),
  lifeOsBrowserRefresh: document.getElementById('lifeOsBrowserRefresh'),
  lifeOsDocClose: document.getElementById('lifeOsDocClose'),
  lifeOsDocDelete: document.getElementById('lifeOsDocDelete'),
  lifeOsBrowserStatus: document.getElementById('lifeOsBrowserStatus'),
  lifeOsFileList: document.getElementById('lifeOsFileList'),
  lifeOsFileContent: document.getElementById('lifeOsFileContent'),

  jobsList: document.getElementById('jobsList'),
  jobsEmpty: document.getElementById('jobsEmpty'),
  jobsAddBtn: document.getElementById('jobsAddBtn'),
  refreshJobs: document.getElementById('refreshJobs'),
  jobDialog: document.getElementById('jobDialog'),
  jobForm: document.getElementById('jobForm'),
  jobDialogTitle: document.getElementById('jobDialogTitle'),
  jobIdField: document.getElementById('jobIdField'),
  jobNameInput: document.getElementById('jobNameInput'),
  jobScriptInput: document.getElementById('jobScriptInput'),
  jobArgsInput: document.getElementById('jobArgsInput'),
  jobScheduleType: document.getElementById('jobScheduleType'),
  jobScheduleEveryRow: document.getElementById('jobScheduleEveryRow'),
  jobScheduleEvery: document.getElementById('jobScheduleEvery'),
  jobScheduleAtRow: document.getElementById('jobScheduleAtRow'),
  jobScheduleAt: document.getElementById('jobScheduleAt'),
  jobScheduleTimesRow: document.getElementById('jobScheduleTimesRow'),
  jobScheduleTimes: document.getElementById('jobScheduleTimes'),
  jobScheduleDayRow: document.getElementById('jobScheduleDayRow'),
  jobScheduleDay: document.getElementById('jobScheduleDay'),
  jobScheduleOnceRow: document.getElementById('jobScheduleOnceRow'),
  jobScheduleOnceAt: document.getElementById('jobScheduleOnceAt'),
  jobCooldownInput: document.getElementById('jobCooldownInput'),
  jobMutexGroupInput: document.getElementById('jobMutexGroupInput'),
  jobConfirmInput: document.getElementById('jobConfirmInput'),
  jobOnSuccessList: document.getElementById('jobOnSuccessList'),
  jobOnFailureList: document.getElementById('jobOnFailureList'),
  jobParamsList: document.getElementById('jobParamsList'),
  jobParamsAdd: document.getElementById('jobParamsAdd'),
  jobPreflightProblems: document.getElementById('jobPreflightProblems'),
  jobSaveAnyway: document.getElementById('jobSaveAnyway'),
  jobSaveBtn: document.getElementById('jobSaveBtn'),
  jobCancel: document.getElementById('jobCancel'),
  jobRunDialog: document.getElementById('jobRunDialog'),
  jobRunForm: document.getElementById('jobRunForm'),
  jobRunDialogTitle: document.getElementById('jobRunDialogTitle'),
  jobRunDialogStaleNote: document.getElementById('jobRunDialogStaleNote'),
  jobRunDialogFields: document.getElementById('jobRunDialogFields'),
  jobRunDialogDryRun: document.getElementById('jobRunDialogDryRun'),
  jobRunCancel: document.getElementById('jobRunCancel'),

  codingOptions: document.getElementById('codingOptions'),
  claudeModel: document.getElementById('claudeModel'),
  claudeEffort: document.getElementById('claudeEffort'),
  claudePermission: document.getElementById('claudePermission'),
  claudeVerbose: document.getElementById('claudeVerbose'),
  claudeDebug: document.getElementById('claudeDebug'),
  claudeDetached: document.getElementById('claudeDetached'),
  claudeFlagsPreview: document.getElementById('claudeFlagsPreview'),
  antigravitySkipPerms: document.getElementById('antigravitySkipPerms'),
  antigravitySandbox: document.getElementById('antigravitySandbox'),
  antigravityFlagsPreview: document.getElementById('antigravityFlagsPreview'),
  copilotModel: document.getElementById('copilotModel'),
  copilotSkipPerms: document.getElementById('copilotSkipPerms'),
  copilotFlagsPreview: document.getElementById('copilotFlagsPreview'),
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
  projectsIgnore: document.getElementById('projectsIgnore'),
  appsScanRoot: document.getElementById('appsScanRoot'),
  saveSettings: document.getElementById('saveSettings'),
  listenersList: document.getElementById('listenersList'),
  listenersEmpty: document.getElementById('listenersEmpty'),
  refreshListeners: document.getElementById('refreshListeners'),
  runningAppsList: document.getElementById('runningAppsList'),
  runningAppsEmpty: document.getElementById('runningAppsEmpty'),
  refreshRunningApps: document.getElementById('refreshRunningApps'),
  statusReadout: document.getElementById('statusReadout'),
  buildReadout: document.getElementById('buildReadout'),

  scanDialog: document.getElementById('scanDialog'),
  scanResults: document.getElementById('scanResults'),
  scanCancel: document.getElementById('scanCancel'),
  scanSave: document.getElementById('scanSave'),

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
  terminalKeys: document.getElementById('terminalKeys'),
  terminalKeysPopover: document.getElementById('terminalKeysPopover'),
  terminalCompose: document.getElementById('terminalCompose'),
  terminalComposeBar: document.getElementById('terminalComposeBar'),
  terminalComposeInput: document.getElementById('terminalComposeInput'),
  terminalComposeSend: document.getElementById('terminalComposeSend'),

  webauthnStatus: document.getElementById('webauthnStatus'),
  webauthnDevices: document.getElementById('webauthnDevices'),
  enrollDeviceBtn: document.getElementById('enrollDeviceBtn'),
};
