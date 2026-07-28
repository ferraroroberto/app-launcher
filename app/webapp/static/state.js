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
export const BOARD_POLL_MS = 5000;        // refresh Board tab while it's visible
export const WEBAUTHN_POLL_MS = 15000;
// Git-status refresh (#496, reversing #115's tap-only contract): slow on
// purpose — one git run per project per tick, gated to the tabs that show
// the flags (Coding tiles, Board backlog) and to a foreground page.
export const GIT_STATUS_POLL_MS = 45000;

export const state = {
  tab: 'claude',
  config: null,
  apps: [],
  // Coding agents — overwritten by /api/agents at boot. The fallback
  // keeps the Coding tab usable if that fetch fails: Claude Code is the
  // launcher's core agent so it's assumed present; the other agents
  // stay disabled until detection confirms their CLI is on PATH. The
  // fullscreen flags mirror src/agents.py so a terminal opened while the
  // fetch is degraded still takes the pan-not-reflow path (#264/#430) —
  // without them a ratatui agent would silently fall onto Claude's
  // reflow path and replay-scroll on every keyboard toggle.
  agents: [
    { id: 'claude', label: 'Claude Code', available: true, fullscreen: false },
    { id: 'codex', label: 'Codex CLI', available: false, fullscreen: true },
    { id: 'antigravity', label: 'Antigravity CLI', available: false, fullscreen: true },
    { id: 'copilot', label: 'GitHub Copilot CLI', available: false, fullscreen: true },
    { id: 'pi', label: 'Pi', available: false, fullscreen: true },
    { id: 'grok', label: 'Grok Build', available: false, fullscreen: true },
  ],
  runningApps: [],
  // Git flags for the Coding tiles + Board backlog (issue #115, always-on
  // since #496). null only until the boot fetch lands; then a map of
  // project id → { is_git, branch, default_branch, on_default_branch,
  // dirty }, refreshed by the GIT_STATUS_POLL_MS poll while the Coding or
  // Board tab is visible. Cached: the 4-5 s tab polls re-render from this
  // map but never re-run the git check themselves.
  gitStatus: null,
  // Coding-tab favorites filter (issue #250). false = show all projects
  // (favorites pinned to the top); true = show only starred projects. A
  // client-side view toggle, persisted across reloads like editMode so the
  // 4 s apps poll re-renders without dropping it.
  codingFavFilter: localStorage.getItem('launcher.codingFavFilter') === '1',
  jobs: [],
  // Jobs-list ordering (issue #229). 'next' = ascending by computed next
  // fire (imminent dailies above weeklies; manual/paused sink to the
  // bottom); 'name' = A–Z. Persisted across reloads like editMode.
  jobsSort: localStorage.getItem('launcher.jobsSort') === 'name' ? 'name' : 'next',
  jobsSearchQuery: '',
  jobsSearchMatches: [],
  jobRuns: {},      // job_id → array of recent runs (lazy)
  expandedJob: null, // job_id currently expanded inline (history visible)
  selectedRun: null, // { jobId, runId } — which run's log is in the panel
  sessions: [],
  // Board tab (issue #300 / #164): the /api/board payload, null until the
  // tab's first fetch. The 5 s poll self-gates on the tab being visible;
  // GitHub data inside it comes from the server-side gh cache and is only
  // refreshed on demand (the ↻ button / first activation), never per poll.
  board: null,
  // Which board column the phone carousel has in view.
  boardCol: 'your_turn',
  // Session id whose drawer is open (#301). The board poll pauses while
  // set, so a re-render can never wipe a reply being typed.
  boardExpanded: null,
  // Life OS tab (issue #102): skills from /api/life-os/skills, plus the
  // read-only content browser's current skill + loaded files.
  lifeOsSkills: [],
  lifeOsBrowser: null,   // { skillId, name, files } while the browser is open
  systemMapAvailable: false, // /api/system-map/status → show/hide the section
  systemMapObjectUrl: null,  // object URL of the loaded map blob (revoked on reload)
  pendingScan: [],
  webauthn: { configured: false, enrollment_open: false, devices: [] },
  terminal: null,   // { sid, ws, term, fit, onWindowResize }
  status: null,     // /api/status payload (incl. terminal reachability)
  // True only when this page was opened as the launcher-spawned PC mirror
  // window — i.e. via the ?terminal=<sid> deep-link (set at boot, issue
  // #241). A human's own desktop browser over loopback is NOT a mirror, so
  // the connection's loopback reason alone must never flip isMirror — that
  // mis-classification made Stop & Close window.close() the user's Chrome.
  isMirrorWindow: false,
  // Edit mode (Settings toggle) reveals rename + remove on Apps tab
  // rows only — Coding tab rows are disk-scanned and never editable.
  // Persisted across reloads.
  editMode: localStorage.getItem('launcher.editMode') === '1',
};

// ES modules are deferred — they execute after DOMContentLoaded, so
// document.getElementById is safe to call at module top level.
export const els = {
  themeToggle: document.getElementById('themeToggle'),
  homeHeadStatus: document.getElementById('homeHeadStatus'),
  tabClaude: document.getElementById('tabClaude'),
  tabApps: document.getElementById('tabApps'),
  tabJobs: document.getElementById('tabJobs'),
  tabLifeOS: document.getElementById('tabLifeOS'),
  tabBoard: document.getElementById('tabBoard'),
  paneClaude: document.getElementById('paneClaude'),
  paneApps: document.getElementById('paneApps'),
  paneJobs: document.getElementById('paneJobs'),
  paneLifeOS: document.getElementById('paneLifeOS'),
  paneBoard: document.getElementById('paneBoard'),

  boardColumns: document.getElementById('boardColumns'),
  boardStatus: document.getElementById('boardStatus'),
  boardUsage: document.getElementById('boardUsage'),
  boardUsageSession: document.getElementById('boardUsageSession'),
  boardUsageWeekly: document.getElementById('boardUsageWeekly'),
  boardRefresh: document.getElementById('boardRefresh'),
  boardColBacklog: document.getElementById('boardColBacklog'),
  boardColClaude: document.getElementById('boardColClaude'),
  boardColYours: document.getElementById('boardColYours'),
  boardColOther: document.getElementById('boardColOther'),
  boardColDone: document.getElementById('boardColDone'),
  boardDispatchGoal: document.getElementById('boardDispatchGoal'),
  boardDispatchRepo: document.getElementById('boardDispatchRepo'),
  boardDispatchRepoBtn: document.getElementById('boardDispatchRepoBtn'),
  boardDispatchRepoList: document.getElementById('boardDispatchRepoList'),
  boardDispatchMode: document.getElementById('boardDispatchMode'),
  boardDispatchModel: document.getElementById('boardDispatchModel'),
  boardDispatchRecord: document.getElementById('boardDispatchRecord'),
  boardDispatchClear: document.getElementById('boardDispatchClear'),
  boardDispatchSend: document.getElementById('boardDispatchSend'),
  boardChiefStatus: document.getElementById('boardChiefStatus'),
  boardChiefStatusText: document.getElementById('boardChiefStatusText'),
  boardChiefStart: document.getElementById('boardChiefStart'),
  boardChiefResume: document.getElementById('boardChiefResume'),
  boardChiefRestart: document.getElementById('boardChiefRestart'),
  boardChiefSettings: document.getElementById('boardChiefSettings'),
  chiefSettingsDialog: document.getElementById('chiefSettingsDialog'),
  chiefSettingsForm: document.getElementById('chiefSettingsForm'),
  chiefModelSelect: document.getElementById('chiefModelSelect'),
  chiefWorkerCap: document.getElementById('chiefWorkerCap'),
  chiefSettingsCancel: document.getElementById('chiefSettingsCancel'),

  lifeOsDetached: document.getElementById('lifeOsDetached'),
  lifeOsResume: document.getElementById('lifeOsResume'),
  lifeOsList: document.getElementById('lifeOsList'),
  lifeOsEmpty: document.getElementById('lifeOsEmpty'),
  lifeOsRecap: document.getElementById('lifeOsRecap'),
  lifeOsRecapBadge: document.getElementById('lifeOsRecapBadge'),
  lifeOsRecapLaunch: document.getElementById('lifeOsRecapLaunch'),
  lifeOsDir: document.getElementById('lifeOsDir'),
  claudeConfigDir: document.getElementById('claudeConfigDir'),
  lifeOsBrowser: document.getElementById('lifeOsBrowser'),
  lifeOsBrowserBack: document.getElementById('lifeOsBrowserBack'),
  lifeOsBrowserTitle: document.getElementById('lifeOsBrowserTitle'),
  lifeOsDocClose: document.getElementById('lifeOsDocClose'),
  lifeOsDocDelete: document.getElementById('lifeOsDocDelete'),
  lifeOsDocRename: document.getElementById('lifeOsDocRename'),
  lifeOsBrowserStatus: document.getElementById('lifeOsBrowserStatus'),
  lifeOsFileList: document.getElementById('lifeOsFileList'),
  lifeOsFileContent: document.getElementById('lifeOsFileContent'),

  jobsList: document.getElementById('jobsList'),
  jobsEmpty: document.getElementById('jobsEmpty'),
  jobsAddBtn: document.getElementById('jobsAddBtn'),
  jobsSortBtn: document.getElementById('jobsSortBtn'),
  jobsSearchInput: document.getElementById('jobsSearchInput'),
  jobsSearchClear: document.getElementById('jobsSearchClear'),
  jobsEditBtn: document.getElementById('jobsEditBtn'),
  jobsAgendaCard: document.getElementById('jobsAgendaCard'),
  jobsAgendaBody: document.getElementById('jobsAgendaBody'),
  jobDialog: document.getElementById('jobDialog'),
  jobForm: document.getElementById('jobForm'),
  jobDialogTitle: document.getElementById('jobDialogTitle'),
  jobIdField: document.getElementById('jobIdField'),
  jobNameInput: document.getElementById('jobNameInput'),
  jobKindInput: document.getElementById('jobKindInput'),
  jobScriptRow: document.getElementById('jobScriptRow'),
  jobScriptInput: document.getElementById('jobScriptInput'),
  jobInlineShellFields: document.getElementById('jobInlineShellFields'),
  jobInlineExtInput: document.getElementById('jobInlineExtInput'),
  jobInlineBodyInput: document.getElementById('jobInlineBodyInput'),
  jobHttpCheckFields: document.getElementById('jobHttpCheckFields'),
  jobHttpUrlInput: document.getElementById('jobHttpUrlInput'),
  jobHttpMethodInput: document.getElementById('jobHttpMethodInput'),
  jobHttpExpectStatusInput: document.getElementById('jobHttpExpectStatusInput'),
  jobHttpTimeoutInput: document.getElementById('jobHttpTimeoutInput'),
  jobArgsRow: document.getElementById('jobArgsRow'),
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
  jobAlertOnFailureInput: document.getElementById('jobAlertOnFailureInput'),
  jobConfirmInput: document.getElementById('jobConfirmInput'),
  jobOnSuccessList: document.getElementById('jobOnSuccessList'),
  jobOnFailureList: document.getElementById('jobOnFailureList'),
  jobParamsList: document.getElementById('jobParamsList'),
  jobParamsAdd: document.getElementById('jobParamsAdd'),
  jobWebhookProvider: document.getElementById('jobWebhookProvider'),
  jobWebhookSecretRow: document.getElementById('jobWebhookSecretRow'),
  jobWebhookSecretInput: document.getElementById('jobWebhookSecretInput'),
  jobWebhookEventsRow: document.getElementById('jobWebhookEventsRow'),
  jobWebhookEventsInput: document.getElementById('jobWebhookEventsInput'),
  jobWebhookMappingSection: document.getElementById('jobWebhookMappingSection'),
  jobWebhookMappingList: document.getElementById('jobWebhookMappingList'),
  jobWebhookMappingAdd: document.getElementById('jobWebhookMappingAdd'),
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
  agentVisibility: document.getElementById('agentVisibility'),
  claudeModel: document.getElementById('claudeModel'),
  claudeEffort: document.getElementById('claudeEffort'),
  claudePermission: document.getElementById('claudePermission'),
  claudeVerbose: document.getElementById('claudeVerbose'),
  claudeDebug: document.getElementById('claudeDebug'),
  claudeDetached: document.getElementById('claudeDetached'),
  claudeResume: document.getElementById('claudeResume'),
  claudeFlagsPreview: document.getElementById('claudeFlagsPreview'),
  codexEffort: document.getElementById('codexEffort'),
  codexPermission: document.getElementById('codexPermission'),
  codexFlagsPreview: document.getElementById('codexFlagsPreview'),
  antigravitySkipPerms: document.getElementById('antigravitySkipPerms'),
  antigravitySandbox: document.getElementById('antigravitySandbox'),
  antigravityFlagsPreview: document.getElementById('antigravityFlagsPreview'),
  copilotModel: document.getElementById('copilotModel'),
  copilotSkipPerms: document.getElementById('copilotSkipPerms'),
  copilotFlagsPreview: document.getElementById('copilotFlagsPreview'),
  piModel: document.getElementById('piModel'),
  piEffort: document.getElementById('piEffort'),
  piTrust: document.getElementById('piTrust'),
  piFlagsPreview: document.getElementById('piFlagsPreview'),
  claudeList: document.getElementById('claudeList'),
  claudeEmpty: document.getElementById('claudeEmpty'),
  favFilterBtn: document.getElementById('favFilterBtn'),
  systemMapCard: document.getElementById('systemMapCard'),
  systemMapImage: document.getElementById('systemMapImage'),
  systemMapStatus: document.getElementById('systemMapStatus'),
  systemMapLightbox: document.getElementById('systemMapLightbox'),
  systemMapLightboxImage: document.getElementById('systemMapLightboxImage'),
  systemMapLightboxClose: document.getElementById('systemMapLightboxClose'),
  gitStatusBtn: document.getElementById('gitStatusBtn'),
  gitStatusSummary: document.getElementById('gitStatusSummary'),
  gitStatusLegend: document.getElementById('gitStatusLegend'),
  sessionsList: document.getElementById('sessionsList'),
  sessionsEmpty: document.getElementById('sessionsEmpty'),
  codingUsage: document.getElementById('codingUsage'),
  codingUsageSession: document.getElementById('codingUsageSession'),
  codingUsageWeekly: document.getElementById('codingUsageWeekly'),
  codingChiefStatus: document.getElementById('codingChiefStatus'),
  codingChiefStatusText: document.getElementById('codingChiefStatusText'),
  codingChiefStart: document.getElementById('codingChiefStart'),
  codingChiefResume: document.getElementById('codingChiefResume'),
  appsList: document.getElementById('appsList'),
  appsEmpty: document.getElementById('appsEmpty'),
  registeredTraysList: document.getElementById('registeredTraysList'),
  registeredTraysEmpty: document.getElementById('registeredTraysEmpty'),

  rescanBtn: document.getElementById('rescanBtn'),
  settingsPanel: document.getElementById('settingsPanel'),
  tabSettings: document.getElementById('tabSettings'),
  tokensList: document.getElementById('tokensList'),
  tokensEmpty: document.getElementById('tokensEmpty'),
  tokenLabelInput: document.getElementById('tokenLabelInput'),
  tokenJobSelect: document.getElementById('tokenJobSelect'),
  tokenMintBtn: document.getElementById('tokenMintBtn'),
  tokenMintResult: document.getElementById('tokenMintResult'),
  tokenMintValue: document.getElementById('tokenMintValue'),
  tokenMintUrl: document.getElementById('tokenMintUrl'),
  tokenCopyBtn: document.getElementById('tokenCopyBtn'),
  editMode: document.getElementById('editMode'),
  projectsDir: document.getElementById('projectsDir'),
  projectsIgnore: document.getElementById('projectsIgnore'),
  appsScanRoot: document.getElementById('appsScanRoot'),
  terminalHistoryLines: document.getElementById('terminalHistoryLines'),
  bootAutostartToggle: document.getElementById('bootAutostartToggle'),
  saveSettings: document.getElementById('saveSettings'),
  listenersList: document.getElementById('listenersList'),
  listenersEmpty: document.getElementById('listenersEmpty'),
  runningAppsList: document.getElementById('runningAppsList'),
  runningAppsEmpty: document.getElementById('runningAppsEmpty'),
  statusReadout: document.getElementById('statusReadout'),
  buildReadout: document.getElementById('buildReadout'),
  spikeVoiceLink: document.getElementById('spikeVoiceLink'),

  scanDialog: document.getElementById('scanDialog'),
  scanResults: document.getElementById('scanResults'),
  scanCancel: document.getElementById('scanCancel'),
  scanSave: document.getElementById('scanSave'),

  renameDialog: document.getElementById('renameDialog'),
  renameForm: document.getElementById('renameForm'),
  renameInput: document.getElementById('renameInput'),
  renameCancel: document.getElementById('renameCancel'),

  sessionRenameDialog: document.getElementById('sessionRenameDialog'),
  sessionRenameForm: document.getElementById('sessionRenameForm'),
  sessionRenameInput: document.getElementById('sessionRenameInput'),
  sessionRenameCancel: document.getElementById('sessionRenameCancel'),

  toast: document.getElementById('toast'),

  loginOverlay: document.getElementById('loginOverlay'),
  loginForm: document.getElementById('loginForm'),
  loginPassword: document.getElementById('loginPassword'),
  loginError: document.getElementById('loginError'),

  terminalOverlay: document.getElementById('terminalOverlay'),
  terminalBar: document.querySelector('.terminal-bar'),
  terminalBack: document.getElementById('terminalBack'),
  terminalKill: document.getElementById('terminalKill'),
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
  terminalRecord: document.getElementById('terminalRecord'),
  terminalSpeak: document.getElementById('terminalSpeak'),
  terminalSpeakPopover: document.getElementById('terminalSpeakPopover'),
  summaryModal: document.getElementById('summaryModal'),
  summaryModalText: document.getElementById('summaryModalText'),
  summaryModalClose: document.getElementById('summaryModalClose'),
  terminalScreenshot: document.getElementById('terminalScreenshot'),
  terminalScreenshotInput: document.getElementById('terminalScreenshotInput'),
  terminalComposeAttach: document.getElementById('terminalComposeAttach'),
  terminalOcrTray: document.getElementById('terminalOcrTray'),
  terminalOcrThumbs: document.getElementById('terminalOcrThumbs'),
  terminalOcrExtract: document.getElementById('terminalOcrExtract'),

};
