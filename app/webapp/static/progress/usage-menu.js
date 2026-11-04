/* The Usage card's ⋮ menu: Usage details (the sheet a tap on the card has
 * always opened) and History (the progress sheet). The menu is the vendored
 * row-menu, mounted on <body> while it is open. */

import { createRowMenu } from '../_vendored/row-menu/row-menu.js';
import { openUsageSheet } from '../usage-meter.js';
import { openProgressSheet } from './progress-sheet.js';

export function wireUsageMenu() {
  const kebab = document.getElementById('codingUsageKebab');
  if (!kebab) return;
  const menu = createRowMenu({ className: 'usage-menu' });
  menu.attach('usage', kebab, [
    {
      dataset: { item: 'usage-details-btn', track: 'coding.usage-details' },
      glyph: 'gauge', label: 'Usage details', onTap: openUsageSheet,
    },
    {
      dataset: { item: 'usage-history-btn', track: 'coding.usage-history' },
      glyph: 'clock', label: 'History', onTap: openProgressSheet,
    },
  ]);
}
