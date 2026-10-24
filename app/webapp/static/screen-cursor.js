/* The PC's mouse cursor as the Screen view draws it (#1598).
 *
 * The server sends a shape name, never the cursor's own image (an app's
 * cursor can carry its content), so each name it can send has a drawing
 * here: a 24px SVG in the view's CSS px, and the hotspot (the point that
 * sits on the cursor's position). Any other name draws the arrow. The names
 * match src/desktop_cursor.py's STANDARD_SHAPES (tests/test_desktop_screen.py
 * fails when one is missing here).
 *
 * Colours are the overlay's two custom properties (styles/apps.css,
 * .screen-cursor): a light body with a dark edge, like Windows' own cursor,
 * so it reads over any desktop.
 */

const FILL = 'var(--screen-cursor-fill)';
const EDGE = 'var(--screen-cursor-edge)';

// `body`: a closed outline, filled. `lines`: strokes, edged by a wider
// stroke drawn under them.
export const CURSOR_SHAPES = {
  'arrow': { hot: [5, 3], body: 'M5 3v15.5l4-3.6 2.6 5.8 2.6-1.1-2.6-5.7H17z' },
  'text': { hot: [12, 12], lines: 'M9 4h6M12 4v16M9 20h6' },
  'hand': {
    hot: [10.5, 2],
    body: 'M9 3.5a1.5 1.5 0 0 1 3 0V10a1.5 1.5 0 0 1 3 0v1a1.5 1.5 0 0 1 3 0v1' +
      'a1.5 1.5 0 0 1 3 0v4a6 6 0 0 1-6 6h-2a6 6 0 0 1-4.5-2l-3.9-4.4' +
      'a1.5 1.5 0 0 1 2.2-2L9 15.5z',
  },
  'busy': { hot: [12, 12], lines: 'M12 5a7 7 0 1 0 7 7' },
  'resize-ns': { hot: [12, 12], lines: 'M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4' },
  'resize-ew': { hot: [12, 12], lines: 'M3 12h18M7 8l-4 4 4 4M17 8l4 4-4 4' },
  'resize-nwse': { hot: [12, 12], lines: 'M5 5l14 14M5 11V5h6M19 13v6h-6' },
  'resize-nesw': { hot: [12, 12], lines: 'M19 5L5 19M13 5h6v6M5 13v6h6' },
  'move': {
    hot: [12, 12],
    lines: 'M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3',
  },
  'not-allowed': { hot: [12, 12], lines: 'M12 5a7 7 0 1 0 0 14a7 7 0 1 0 0-14zM7 7l10 10' },
};

/** The shape to draw for a name from the server: itself, or the arrow. */
export function cursorShape(name) {
  return Object.prototype.hasOwnProperty.call(CURSOR_SHAPES, name) ? name : 'arrow';
}

/** The SVG markup for a shape, placed so its hotspot is at the anchor's 0,0. */
export function cursorSvg(name) {
  const shape = CURSOR_SHAPES[cursorShape(name)];
  const paths = shape.body
    ? '<path d="' + shape.body + '" fill="' + FILL + '" stroke="' + EDGE +
      '" stroke-width="1.5" stroke-linejoin="round"/>'
    : '<path d="' + shape.lines + '" stroke="' + EDGE + '" stroke-width="4"/>' +
      '<path d="' + shape.lines + '" stroke="' + FILL + '" stroke-width="2"/>';
  return '<svg viewBox="0 0 24 24" width="24" height="24" fill="none"' +
    ' stroke-linecap="round" stroke-linejoin="round" focusable="false"' +
    ' style="left:' + -shape.hot[0] + 'px;top:' + -shape.hot[1] + 'px">' + paths + '</svg>';
}
