/* The History sheet's charts: inline SVG, no library.
 *
 * One function draws a chart into its host from a list of buckets. Bars and
 * lines share the axes; a stacked chart piles its series per bucket. A value
 * of null is a bucket that was not read: a bar is left out, a line breaks, and
 * the tooltip says "unknown" for it, so a gap never reads as a zero.
 *
 * Design contract (Charts): the canvas sizes to its card and never drives
 * horizontal overflow; the x-axis keeps a tick budget (4 labels on a phone,
 * 8 wide) with no rotation, recomputed on every resize; a series that colour
 * alone would separate also carries a second channel (a hatch, a dash with a
 * marker); the tooltip is the precise-value path.
 */

import { bucketLabel, bucketTitle, fmtN } from './progress-model.js';

const NS = 'http://www.w3.org/2000/svg';
const PHONE_WIDTH = 420;

function svgEl(name, attrs, parent) {
  const node = document.createElementNS(NS, name);
  Object.keys(attrs || {}).forEach(function (key) {
    const value = attrs[key];
    // A CSS variable in a paint attribute only resolves as a style.
    if ((key === 'fill' || key === 'stroke') && /var\(/.test(value)) node.style[key] = value;
    else node.setAttribute(key, value);
  });
  if (parent) parent.appendChild(node);
  return node;
}

// The next "round" number up: 1, 2, 5 times a power of ten.
function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

function marker(svg, kind, cx, cy, color) {
  if (kind === 'diamond') {
    svgEl('rect', { x: cx - 3, y: cy - 3, width: 6, height: 6, fill: color, transform: 'rotate(45 ' + cx + ' ' + cy + ')' }, svg);
  } else {
    svgEl('circle', { cx: cx, cy: cy, r: 3, fill: color }, svg);
  }
}

// The value range the series need: stacked charts sum a bucket's series,
// the rest take each series on its own (a negative one reaches below zero).
function valueRange(points, spec) {
  let hi = 0;
  let lo = 0;
  points.forEach(function (p) {
    if (spec.stacked) {
      const sum = spec.series.reduce(function (acc, s) { return acc + (s.get(p) || 0); }, 0);
      if (sum > hi) hi = sum;
      return;
    }
    spec.series.forEach(function (s) {
      const v = s.get(p);
      if (v == null) return;
      if (v > hi) hi = v;
      if (v < lo) lo = v;
    });
  });
  return { hi: niceMax(hi), lo: lo < 0 ? -niceMax(-lo) : 0 };
}

function drawAxes(svg, geometry, points, spec, range) {
  const { W, padL, padR, padT, padB, y, x } = geometry;
  const grid = svgEl('g', { class: 'pg-grid' }, svg);
  const axis = svgEl('g', { class: 'pg-axis' }, svg);
  const ticks = range.lo < 0
    ? [range.hi, range.hi / 2, 0, range.lo / 2, range.lo]
    : [range.hi, range.hi / 2, 0];
  ticks.forEach(function (v) {
    svgEl('line', { x1: padL, x2: W - padR, y1: y(v), y2: y(v), class: v === 0 ? 'pg-zero' : '' }, grid);
    const label = svgEl('text', { x: padL - 6, y: y(v) + 4, 'text-anchor': 'end' }, axis);
    label.textContent = fmtN(Math.abs(v));
  });
  const budget = W < PHONE_WIDTH ? 4 : 8;
  const step = Math.max(1, Math.ceil(points.length / budget));
  points.forEach(function (p, i) {
    if (i % step !== 0) return;
    const label = svgEl('text', { x: i === 0 ? padL : x(i), y: padT + geometry.ih + padB - 6, 'text-anchor': i === 0 ? 'start' : 'middle' }, axis);
    label.textContent = bucketLabel(p.k, spec.bucket);
  });
}

function drawBars(svg, geometry, points, spec) {
  const { padL, bw, y } = geometry;
  const gap = Math.min(3, bw * 0.25);
  const width = Math.max(1, bw - gap);
  if (spec.stacked) {
    points.forEach(function (p, i) {
      let base = 0;
      spec.series.forEach(function (s) {
        const v = s.get(p);
        if (!v) return;
        const top = y(base + v);
        const bottom = y(base);
        svgEl('rect', { x: padL + bw * i + gap / 2, y: top, width: width, height: Math.max(1, bottom - top - 1), fill: s.fill }, svg);
        base += v;
      });
    });
    return;
  }
  spec.series.filter(function (s) { return s.kind === 'bar'; }).forEach(function (s) {
    points.forEach(function (p, i) {
      const v = s.get(p);
      if (v == null) return;
      const y0 = y(0);
      const y1 = y(v);
      svgEl('rect', {
        x: padL + bw * i + gap / 2, y: Math.min(y0, y1), width: width, height: Math.max(v ? 1 : 0, Math.abs(y1 - y0)),
        rx: Math.min(3, width / 3), fill: s.fill,
      }, svg);
    });
  });
}

function drawLines(svg, geometry, points, spec) {
  const { x, y } = geometry;
  spec.series.filter(function (s) { return s.kind === 'line'; }).forEach(function (s) {
    // A bucket that was not read breaks the line rather than dropping it to 0.
    let path = '';
    let pen = false;
    points.forEach(function (p, i) {
      const v = s.get(p);
      if (v == null) { pen = false; return; }
      path += (pen ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1) + ' ';
      pen = true;
    });
    if (!path) return;
    svgEl('path', {
      d: path, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-dasharray': s.dash || '', 'stroke-linejoin': 'round',
    }, svg);
    if (points.length <= 40) {
      points.forEach(function (p, i) {
        const v = s.get(p);
        if (v != null) marker(svg, s.point, x(i), y(v), s.color);
      });
    }
  });
}

function tooltipRows(point, spec) {
  const listed = spec.stacked
    ? spec.series.filter(function (s) { return (s.get(point) || 0) > 0; }).reverse()
    : spec.series;
  const rows = listed.map(function (s) {
    const v = s.get(point);
    return [s.name, v == null ? 'unknown' : fmtN(Math.abs(v))];
  });
  if (spec.stacked && listed.length > 1) {
    rows.push(['Total', fmtN(listed.reduce(function (sum, s) { return sum + (s.get(point) || 0); }, 0)), true]);
  }
  if (!rows.length) rows.push(['', 'nothing measured']);
  return rows;
}

function wireTooltip(host, svg, geometry, points, spec) {
  const { W, padL, padT, bw, x } = geometry;
  const guide = svgEl('line', { y1: padT, y2: padT + geometry.ih, class: 'pg-guide', opacity: 0 }, svg);
  const tip = document.createElement('div');
  tip.className = 'pg-tip';
  tip.hidden = true;
  host.appendChild(tip);
  function show(ev) {
    const box = host.getBoundingClientRect();
    const i = Math.max(0, Math.min(points.length - 1, Math.floor((ev.clientX - box.left - padL) / bw)));
    const point = points[i];
    guide.setAttribute('x1', x(i));
    guide.setAttribute('x2', x(i));
    guide.setAttribute('opacity', 0.5);
    tip.replaceChildren();
    const head = document.createElement('b');
    head.textContent = bucketTitle(point.k, spec.bucket);
    tip.appendChild(head);
    tooltipRows(point, spec).forEach(function (r) {
      const row = document.createElement('div');
      row.className = 'pg-tip-row';
      if (r[2]) row.dataset.total = 'true';
      const name = document.createElement('span');
      name.textContent = r[0];
      const value = document.createElement('span');
      value.textContent = r[1];
      row.append(name, value);
      tip.appendChild(row);
    });
    tip.hidden = false;
    tip.style.left = Math.max(70, Math.min(W - 70, x(i))) + 'px';
    tip.style.top = padT + 'px';
  }
  function hide() {
    tip.hidden = true;
    guide.setAttribute('opacity', 0);
  }
  host.onpointermove = show;
  host.onpointerdown = show;
  host.onpointerleave = hide;
}

/**
 * Draw `points` (one per bucket, from progress-model.js) into `host`.
 *
 * spec: { label, bucket, stacked?, series: [{ name, kind: 'bar'|'line',
 *   fill | color, dash?, point?, get(point) -> number|null }] }
 */
export function drawChart(host, points, spec) {
  host.replaceChildren();
  const W = host.clientWidth;
  const H = host.clientHeight;
  if (!W || !H || !points.length) return;
  const padL = 40;
  const padR = 6;
  const padT = 8;
  const padB = 22;
  const iw = W - padL - padR;
  const ih = H - padT - padB;
  const range = valueRange(points, spec);
  const bw = iw / points.length;
  const geometry = {
    W: W, padL: padL, padR: padR, padT: padT, padB: padB, ih: ih, bw: bw,
    y: function (v) { return padT + (range.hi - v) / (range.hi - range.lo) * ih; },
    x: function (i) { return padL + bw * (i + 0.5); },
  };
  const svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': spec.label }, host);
  drawAxes(svg, geometry, points, spec, range);
  drawBars(svg, geometry, points, spec);
  drawLines(svg, geometry, points, spec);
  wireTooltip(host, svg, geometry, points, spec);
}
