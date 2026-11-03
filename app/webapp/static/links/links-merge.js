/* Folding an older window of a session's links into what the Links sheet
 * already shows.
 *
 * "Read more" asks the server for the next older slice of the transcript; its
 * links join the sheet without a second row for a URL both slices mention.
 * A plain function of two responses so tests/js/links_merge.test.mjs pins it
 * under Node.
 *
 * The rows already shown are newer than the older slice, so they keep their
 * place and the slice's new URLs follow them. A URL in both keeps the newer
 * slice's last mention and the older slice's first, adds the counts, and takes
 * the older slice's name when the newer one only had the short id (the
 * publish call sat in the older slice).
 */

const ORDER = ['artifacts', 'github', 'servers', 'other'];

function mergeGroup(base, older) {
  const links = base.links.map(function (l) { return Object.assign({}, l); });
  const byUrl = new Map(links.map(function (l) { return [l.url, l]; }));
  let overlap = 0;
  older.links.forEach(function (item) {
    const known = byUrl.get(item.url);
    if (!known) {
      const copy = Object.assign({}, item);
      byUrl.set(copy.url, copy);
      links.push(copy);
      return;
    }
    overlap += 1;
    known.count += item.count;
    known.first_seen = item.first_seen || known.first_seen;
    if (!known.title && item.title) {
      known.title = item.title;
      known.label = item.label;
    }
  });
  return Object.assign({}, base, { links: links, total: base.total + older.total - overlap });
}

// `base` is the merged response so far, `older` the response for the next
// older window; the result is shaped like a response.
export function mergeLinks(base, older) {
  const have = new Map(base.groups.map(function (g) { return [g.key, g]; }));
  const next = new Map(older.groups.map(function (g) { return [g.key, g]; }));
  const keys = ORDER.filter(function (k) { return have.has(k) || next.has(k); });
  const groups = keys.map(function (k) {
    if (!have.has(k)) return next.get(k);
    return next.has(k) ? mergeGroup(have.get(k), next.get(k)) : have.get(k);
  });
  return Object.assign({}, base, {
    groups: groups,
    total: groups.reduce(function (sum, g) { return sum + g.total; }, 0),
    partial: older.partial,
    offset: older.offset,
    read_from: older.read_from || base.read_from,
  });
}
