/* ---------------------------------------------------------------------------
   Property map (SDD 17.2).

   Drawn as inline SVG with a hand-rolled pan/zoom — no tile server, no mapping
   library, nothing that would need the internet (SDD 5.1). There is also no
   basemap on purpose: the property has not been surveyed, and a decorative
   satellite backdrop under unplaced assets would imply a precision that does
   not exist.

   When nothing has coordinates the screen does not show an empty rectangle; it
   shows the survey backlog, because that is the actionable information.
--------------------------------------------------------------------------- */

import { card, clear, fmtDateTime, h, resultProblem, statusChip, svgEl } from '../app.js';
import { api } from '../api.js';

const DOMAIN_STYLE = {
  site:        { colour: 'var(--st-designonly)', shape: 'diamond' },
  structure:   { colour: 'var(--text-dim)',      shape: 'square' },
  energy:      { colour: 'var(--st-ok)',         shape: 'circle' },
  water:       { colour: 'var(--sev-info)',      shape: 'triangle' },
  agriculture: { colour: 'var(--accent)',        shape: 'circle' },
  it:          { colour: 'var(--sev-info)',      shape: 'square' },
  security:    { colour: 'var(--sev-major)',     shape: 'triangle' },
  safety:      { colour: 'var(--sev-critical)',  shape: 'diamond' },
  storage:     { colour: 'var(--st-stale)',      shape: 'square' },
  spa:         { colour: 'var(--sev-info)',      shape: 'circle' },
  workshop:    { colour: 'var(--st-notdeployed)', shape: 'square' },
  fuel:        { colour: 'var(--sev-major)',     shape: 'diamond' },
};

function styleFor(domain) {
  return DOMAIN_STYLE[domain] || { colour: 'var(--text-faint)', shape: 'circle' };
}

/* ------------------------------------------------------------ projection -- */

/** Equirectangular projection scaled at the map's mean latitude. Good enough
 *  for a single property and honest about being a local plan, not a chart. */
function makeProjector(bbox) {
  const [minLon, minLat, maxLon, maxLat] = bbox;
  const midLat = (minLat + maxLat) / 2;
  const kx = Math.cos((midLat * Math.PI) / 180);
  return ([lon, lat]) => [(lon - minLon) * kx * 100000, (maxLat - lat) * 100000];
}

function coordsOf(geometry, project, out) {
  if (!geometry) return;
  if (geometry.type === 'Point') out.push(project(geometry.coordinates));
  else if (geometry.type === 'LineString' || geometry.type === 'MultiPoint') {
    geometry.coordinates.forEach((c) => out.push(project(c)));
  } else if (geometry.type === 'Polygon' || geometry.type === 'MultiLineString') {
    geometry.coordinates.forEach((ring) => ring.forEach((c) => out.push(project(c))));
  } else if (geometry.type === 'MultiPolygon') {
    geometry.coordinates.forEach((poly) => poly.forEach((ring) => ring.forEach((c) => out.push(project(c)))));
  }
}

function markerPath(shape, x, y, r) {
  if (shape === 'square') return `M${x - r} ${y - r}h${r * 2}v${r * 2}h${-r * 2}z`;
  if (shape === 'triangle') return `M${x} ${y - r * 1.2}L${x + r * 1.15} ${y + r * 0.85}L${x - r * 1.15} ${y + r * 0.85}z`;
  if (shape === 'diamond') return `M${x} ${y - r * 1.25}L${x + r * 1.25} ${y}L${x} ${y + r * 1.25}L${x - r * 1.25} ${y}z`;
  return `M${x} ${y}m${-r} 0a${r} ${r} 0 1 0 ${r * 2} 0a${r} ${r} 0 1 0 ${-r * 2} 0`;
}

/* ------------------------------------------------------------- empty view -- */

function surveyBacklog(payload) {
  const pending = payload.pending_placement || [];
  const meta = payload.metadata || {};
  const byDomain = meta.pending_by_domain || {};

  const root = h('div');
  root.appendChild(h('div', { class: 'empty' },
    h('h3', null, statusChip('not_deployed', 'Nothing has been surveyed yet')),
    h('p', { text: meta.note || 'No asset in the registry has recorded coordinates, so there is nothing to draw. The map deliberately shows no positions rather than approximate ones.' }),
    h('p', { text: `${pending.length} assets are waiting for a position. The list below is the survey backlog.` })));

  if (Object.keys(byDomain).length) {
    root.appendChild(h('div', { class: 'pill-row', style: 'margin:var(--gap) 0' },
      Object.entries(byDomain).sort().map(([domain, count]) => h('span', {
        class: 'chip chip-not_deployed',
      },
        h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '⬚' }), `${domain}: ${count}`))));
  }

  root.appendChild(h('div', { class: 'table-wrap' },
    h('table', null,
      h('caption', { text: 'Assets awaiting survey' }),
      h('thead', null, h('tr', null,
        h('th', { text: 'Asset' }), h('th', { text: 'Domain' }), h('th', { text: 'Status' }),
        h('th', { text: 'Why it is not placed' }), h('th', { text: 'Unresolved fields' }))),
      h('tbody', null, pending.slice(0, 400).map((item) => h('tr', null,
        h('td', null,
          h('a', { href: `#/assets/${encodeURIComponent(item.asset_id)}`, text: item.name || item.asset_id }),
          h('div', { class: 'alarm-key mono', text: item.asset_id })),
        h('td', { text: item.domain }),
        h('td', { text: item.status }),
        h('td', { text: item.reason === 'no_location_record'
          ? 'No location record'
          : 'Location record exists but has no coordinates' }),
        h('td', null, (item.open_fields || []).length
          ? (item.open_fields || []).map((field) => h('span', { class: 'tag', text: field }))
          : '—')))))));

  if (pending.length > 400) {
    root.appendChild(h('p', { class: 'card-note', text: `Showing the first 400 of ${pending.length}.` }));
  }
  return root;
}

/* --------------------------------------------------------------- drawing -- */

function buildMap(payload, ctx) {
  const features = payload.features || [];
  const bbox = payload.bbox || [-0.0005, -0.0005, 0.0005, 0.0005];
  const project = makeProjector(bbox);

  const points = [];
  features.forEach((feature) => coordsOf(feature.geometry, project, points));
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const minX = Math.min(...xs, 0);
  const maxX = Math.max(...xs, 1);
  const minY = Math.min(...ys, 0);
  const maxY = Math.max(...ys, 1);
  const padX = Math.max((maxX - minX) * 0.15, 12);
  const padY = Math.max((maxY - minY) * 0.15, 12);

  const home = {
    x: minX - padX, y: minY - padY,
    w: (maxX - minX) + padX * 2, h: (maxY - minY) + padY * 2,
  };
  const view = { ...home };

  const svg = svgEl('svg', {
    viewBox: `${view.x} ${view.y} ${view.w} ${view.h}`,
    tabindex: '0',
    role: 'application',
    'aria-label': `Property map with ${features.length} placed assets. Use arrow keys to pan and plus or minus to zoom.`,
  });

  const layer = svgEl('g');
  svg.appendChild(layer);

  // A faint graticule so pan/zoom is legible without pretending to be a basemap.
  // It is drawn well outside the framed box: the SVG letterboxes its viewBox, so
  // the visible area is always larger than the data extent.
  const grid = svgEl('g', { opacity: '0.35' });
  const step = Math.max(home.w / 10, 1);
  const gx0 = home.x - home.w * 2;
  const gx1 = home.x + home.w * 3;
  const gy0 = home.y - home.h * 2;
  const gy1 = home.y + home.h * 3;
  for (let x = Math.ceil(gx0 / step) * step; x < gx1; x += step) {
    grid.appendChild(svgEl('line', {
      x1: x, y1: gy0, x2: x, y2: gy1, stroke: 'var(--line)', 'stroke-width': step / 90,
    }));
  }
  for (let y = Math.ceil(gy0 / step) * step; y < gy1; y += step) {
    grid.appendChild(svgEl('line', {
      x1: gx0, y1: y, x2: gx1, y2: y, stroke: 'var(--line)', 'stroke-width': step / 90,
    }));
  }
  layer.appendChild(grid);

  features.forEach((feature) => {
    const props = feature.properties || {};
    const style = styleFor(props.domain);
    const alarmed = props.active_alarms > 0;
    const stroke = alarmed ? 'var(--sev-critical)' : style.colour;
    const group = svgEl('g', {
      role: 'link', tabindex: '0',
      'aria-label': `${props.name || props.asset_id}, ${props.domain}, status ${props.status}`
        + (alarmed ? `, ${props.active_alarms} active alarms` : ''),
      style: 'cursor:pointer',
      onclick: () => ctx.navigate(`/assets/${encodeURIComponent(props.asset_id)}`),
      onkeydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          ctx.navigate(`/assets/${encodeURIComponent(props.asset_id)}`);
        }
      },
    });

    const geometry = feature.geometry;
    if (geometry.type === 'Point') {
      const [x, y] = project(geometry.coordinates);
      // Sizes are a fraction of the framed area, never an absolute floor: with a
      // single surveyed asset the frame is tiny and a floor would fill the screen.
      const r = home.w / 90;
      group.appendChild(svgEl('path', {
        d: markerPath(style.shape, x, y, r),
        fill: stroke, 'fill-opacity': 0.75, stroke, 'stroke-width': r * 0.28,
      }));
      if (alarmed) {
        group.appendChild(svgEl('circle', {
          cx: x, cy: y, r: r * 2.1, fill: 'none',
          stroke: 'var(--sev-critical)', 'stroke-width': r * 0.25, 'stroke-dasharray': `${r} ${r * 0.6}`,
        }));
      }
      if (features.length <= 60) {
        group.appendChild(svgEl('text', {
          x: x + r * 1.9, y: y + r * 0.7, 'font-size': home.w / 80, fill: 'var(--text-dim)',
        }, props.name || props.asset_id));
      }
    } else {
      const rings = geometry.type === 'Polygon' ? geometry.coordinates
        : geometry.type === 'MultiPolygon' ? geometry.coordinates.flat()
          : [geometry.coordinates];
      rings.forEach((ring) => {
        const d = ring.map((coordinate, index) => {
          const [x, y] = project(coordinate);
          return `${index ? 'L' : 'M'}${x} ${y}`;
        }).join(' ') + (geometry.type.includes('Polygon') ? ' Z' : '');
        group.appendChild(svgEl('path', {
          d, fill: geometry.type.includes('Polygon') ? stroke : 'none', 'fill-opacity': 0.1,
          stroke, 'stroke-width': Math.max(home.w / 300, 0.6),
        }));
      });
    }
    layer.appendChild(group);
  });

  function applyView() {
    svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
  }
  function zoom(factor, centreX, centreY) {
    const cx = centreX ?? view.x + view.w / 2;
    const cy = centreY ?? view.y + view.h / 2;
    const nw = Math.min(Math.max(view.w * factor, home.w / 60), home.w * 8);
    const nh = nw * (view.h / view.w);
    view.x = cx - ((cx - view.x) * nw) / view.w;
    view.y = cy - ((cy - view.y) * nh) / view.h;
    view.w = nw;
    view.h = nh;
    applyView();
  }
  function pan(dx, dy) { view.x += dx; view.y += dy; applyView(); }

  svg.addEventListener('wheel', (event) => {
    event.preventDefault();
    const rect = svg.getBoundingClientRect();
    const cx = view.x + ((event.clientX - rect.left) / rect.width) * view.w;
    const cy = view.y + ((event.clientY - rect.top) / rect.height) * view.h;
    zoom(event.deltaY > 0 ? 1.15 : 0.87, cx, cy);
  }, { passive: false });

  let dragging = null;
  svg.addEventListener('pointerdown', (event) => {
    if (event.target.closest('g[role="link"]')) return;
    dragging = { x: event.clientX, y: event.clientY };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    const rect = svg.getBoundingClientRect();
    pan(-((event.clientX - dragging.x) / rect.width) * view.w,
        -((event.clientY - dragging.y) / rect.height) * view.h);
    dragging = { x: event.clientX, y: event.clientY };
  });
  const endDrag = () => { dragging = null; };
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);

  svg.addEventListener('keydown', (event) => {
    const stepSize = view.w / 12;
    const moves = {
      ArrowLeft: [-stepSize, 0], ArrowRight: [stepSize, 0],
      ArrowUp: [0, -stepSize], ArrowDown: [0, stepSize],
    };
    if (moves[event.key]) { event.preventDefault(); pan(...moves[event.key]); }
    else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(0.8); }
    else if (event.key === '-') { event.preventDefault(); zoom(1.25); }
    else if (event.key === '0') { event.preventDefault(); Object.assign(view, home); applyView(); }
  });

  const tools = h('div', { class: 'map-tools' },
    h('button', { class: 'btn btn-sm', onclick: () => zoom(0.8), text: 'Zoom in' }),
    h('button', { class: 'btn btn-sm', onclick: () => zoom(1.25), text: 'Zoom out' }),
    h('button', { class: 'btn btn-sm', onclick: () => { Object.assign(view, home); applyView(); }, text: 'Fit' }),
    h('span', { class: 'card-note', style: 'margin:0 0 0 auto',
      text: 'Drag to pan · wheel to zoom · arrow keys and +/− when focused' }));

  const domains = Array.from(new Set(features.map((f) => (f.properties || {}).domain))).sort();
  const legend = h('div', { class: 'map-legend' },
    domains.map((domain) => h('span', null,
      h('span', { class: 'legend-swatch', style: `background:${styleFor(domain).colour};color:${styleFor(domain).colour}` }),
      domain)),
    h('span', null, h('span', { class: 'legend-swatch', style: 'background:transparent;color:var(--sev-critical)' }), 'ringed = active alarm'));

  return h('div', { class: 'map-frame' }, tools, svg, legend);
}

/* ---------------------------------------------------------------- render -- */

async function draw(root, ctx) {
  const result = await api.map();
  clear(root);

  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Property map' }),
      h('p', { class: 'lede', text: 'Structures, utility assets and sensor locations (MVP acceptance criterion 8). Only assets with real recorded coordinates are drawn — nothing is placed by guesswork.' })),
    h('button', { class: 'btn btn-sm', onclick: () => draw(root, ctx), text: 'Reload' })));

  if (!result.ok) {
    root.appendChild(resultProblem(result, 'the property map'));
    return;
  }

  const payload = result.data;
  const meta = payload.metadata || {};

  root.appendChild(h('div', { class: 'stat-row', style: 'margin-bottom:var(--gap)' },
    h('span', null, h('b', { text: String(meta.placed_count ?? 0) }), ' placed'),
    h('span', null, h('b', { text: String(meta.pending_count ?? 0) }), ' awaiting survey'),
    h('span', null, 'Survey status: ', statusChip(
      meta.survey_status === 'complete' ? 'ok' : meta.survey_status === 'partial' ? 'stale' : 'not_deployed',
      meta.survey_status || 'unknown')),
    h('span', { class: 'mono', text: meta.crs || '' })));

  if ((payload.features || []).length) {
    root.appendChild(buildMap(payload, ctx));
    if ((payload.pending_placement || []).length) {
      root.appendChild(h('div', { style: 'margin-top:var(--gap)' },
        card({
          title: `Still awaiting survey (${payload.pending_placement.length})`,
          status: 'not_deployed',
          children: [surveyBacklog(payload)],
        })));
    }
  } else {
    root.appendChild(surveyBacklog(payload));
  }

  root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
    text: `Map generated ${fmtDateTime(meta.generated_at)}` }));
}

export default {
  title: 'Map',
  async mount(root, ctx) {
    root.appendChild(h('p', { class: 'loading', text: 'Loading the property map…' }));
    await draw(root, ctx);
    return {
      async refresh() { await draw(root, ctx); },
      destroy() {},
    };
  },
};
