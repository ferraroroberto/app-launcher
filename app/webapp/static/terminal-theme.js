/* Terminal-screen theme resolution (issues #381, #383).
 *
 * xterm owns its rendered colours, so the app's CSS theme tokens have to be
 * resolved into an xterm ITheme and pushed again whenever html[data-theme]
 * changes. Machine-local terminal-themes.json overrides win last.
 */

import { els, state } from './state.js';

const LIGHT_ANSI = {
  cursor: '#1f2328',
  cursorAccent: '#ffffff',
  selectionBackground: '#0969da40',
  black: '#000000',
  red: '#cd3131',
  green: '#00bc00',
  yellow: '#949800',
  blue: '#0451a5',
  magenta: '#bc05bc',
  cyan: '#0598bc',
  white: '#555555',
  brightBlack: '#666666',
  brightRed: '#cd3131',
  brightGreen: '#14ce14',
  brightYellow: '#b5ba00',
  brightBlue: '#0451a5',
  brightMagenta: '#bc05bc',
  brightCyan: '#0598bc',
  brightWhite: '#a5a5a5',
};

let userTermThemes = {};

export function setUserTermThemes(themes) {
  userTermThemes = themes && typeof themes === 'object' ? themes : {};
}

function effectiveLight() {
  return document.documentElement.dataset.theme !== 'dark';
}

function userOverride() {
  return userTermThemes[effectiveLight() ? 'light' : 'dark'] || {};
}

export function termScreenTheme() {
  const rootStyle = getComputedStyle(document.documentElement);
  const theme = {
    background: rootStyle.getPropertyValue('--term-bg').trim() || '#0a0a0a',
    foreground: rootStyle.getPropertyValue('--term-fg').trim() || '#f3f3f3',
  };
  if (effectiveLight()) Object.assign(theme, LIGHT_ANSI);
  const user = userOverride();
  Object.keys(user).forEach(function (key) {
    if (key !== 'minimumContrastRatio') theme[key] = user[key];
  });
  return theme;
}

export function termContrastRatio() {
  const user = userOverride();
  if (typeof user.minimumContrastRatio === 'number' &&
      user.minimumContrastRatio >= 1) {
    return user.minimumContrastRatio;
  }
  return effectiveLight() ? 4.5 : 1;
}

export function applyTermTheme() {
  if (els.terminalOverlay) {
    els.terminalOverlay.style.background = userOverride().background || '';
  }
  const terminal = state.terminal;
  if (terminal && terminal.term) {
    try {
      terminal.term.options.theme = termScreenTheme();
      terminal.term.options.minimumContrastRatio = termContrastRatio();
    } catch (_) { /* renderer mid-teardown; next open resolves fresh */ }
  }
}
