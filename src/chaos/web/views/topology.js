/* ---------------------------------------------------------------------------
   Topology and dependency canvas (SDD 16.1, 25.5).

   The design package is authoritative and hand-authored; health is a
   timestamped observation drawn on top of it. This screen keeps that
   separation visible: the shape of the graph comes from the register and never
   moves, and health is a pill on a card that can change between polls.

   Rendering — a deliberate hybrid, not a single-technology choice:

     * SVG for the grid, the group boundaries and the 191 edges. Edges need
       per-type dashes, weights and arrowheads, and SVG gives those declaratively
       with real markers rather than hand-rolled arrowhead trigonometry.
     * Absolutely-positioned HTML for the 137 node cards. Cards carry text,
       chips and links; in HTML they stay selectable, focusable, screen-reader
       addressable and stylable with the console's existing chip classes. In
       Canvas2D every one of those would have to be rebuilt, including hit
       testing, and the cards would stop being real UI.
     * Both layers live inside one wrapper whose `transform` is set once per
       frame. Pan and zoom are therefore a single composited transform and cost
       the same whether there are 137 nodes or 1370 — the DOM is built once and
       never re-laid-out while the camera moves.

   ~330 elements is a comfortable DOM. Canvas2D would only start to win in the
   low thousands, and it would cost every accessibility affordance above.

   Layout is deterministic by construction (see `computeLayout`). Operators
   build muscle memory from stable positions exactly as they do from the
   annunciator's fixed tiles, so a force simulation that settles differently on
   every load is not an option here.

   Acknowledgement: the visual register, the group-boundary idea and the
   HTML-cards-over-SVG-edges architecture are inspired by Reticle (MIT,
   Copyright (c) 2026 Matt Anderson / MA Software LLC); see ACKNOWLEDGEMENTS.
   No code was copied — the data model, the layout algorithm and the health
   semantics here are CHAOS's own. Deliberately no link: SDD 5.1 requires every
   web asset to be free of external references, including in comments.
--------------------------------------------------------------------------- */

import {
  clear, fmtAge, fmtDateTime, h, resultProblem, severityChip, statusChip, svgEl,
} from '../app.js';
import { get } from '../api.js';

/* ============================================================ constants === */

const POSITIONS_KEY = 'homestead.topology.positions';
const CAMERA_KEY = 'homestead.topology.camera';

const COL_PITCH = 262;      // world px between layout ranks
const ROW_PITCH = 82;       // world px between rows
const CARD_W = 206;
const CARD_H = 58;
const MARGIN = 130;
/** Padding around a group boundary, by nesting level. The steps are 24px and
 *  that is load-bearing twice over: a parent box always encloses its children
 *  with visible clearance, and each level's title tab clears the tab of the
 *  level above it (`GROUP_TITLE_H`), so nested titles can never stack on top of
 *  one another and become unclickable. */
const GROUP_PAD = [112, 88, 64, 40, 16];
const GROUP_TITLE_H = 22;
const BAND_GAP = 24;        // clear space between two adjacent boundary boxes
const MIN_ZOOM = 0.06;      // the whole 137-node graph must always fit on screen
const MAX_ZOOM = 2.4;
const BARYCENTRE_PASSES = 3;
/** Below these zooms a card is too small to read, so it sheds detail rather
 *  than rendering unreadable text. The card, its health colour and its position
 *  survive — which is what an overview is for. */
const ZOOM_MID = 0.66;
const ZOOM_FAR = 0.34;
/** Text on a 206px card stops being readable below about this zoom, so the
 *  canvas never *opens* below it. Zooming further out is still allowed — that
 *  is what the overview is for — but arrival should show a working view. */
const READABLE_ZOOM = 0.6;

/** Edge presentation, keyed by the register's relationship_type (SDD 25.5).
 *  Weight, dash and arrowhead carry the meaning; colour is a reinforcement,
 *  never the only signal. `feeds` is the heaviest line on the canvas because
 *  it is the one that carries power and water. */
const EDGE_STYLE = {
  feeds:           { w: 2.3, dash: '',            arrow: 'solid', tone: 'supply',  label: 'feeds' },
  powered_by:      { w: 2.3, dash: '',            arrow: 'solid', tone: 'supply',  label: 'powered by' },
  returns_to:      { w: 1.8, dash: '9 4',         arrow: 'solid', tone: 'supply',  label: 'returns to' },
  hosts:           { w: 1.9, dash: '',            arrow: 'solid', tone: 'host',    label: 'hosts' },
  serves:          { w: 1.7, dash: '',            arrow: 'open',  tone: 'host',    label: 'serves' },
  controls:        { w: 1.8, dash: '7 3',         arrow: 'solid', tone: 'control', label: 'controls' },
  protects:        { w: 1.7, dash: '2 2 7 2',     arrow: 'solid', tone: 'control', label: 'protects' },
  depends_on:      { w: 1.6, dash: '5 4',         arrow: 'open',  tone: 'depend',  label: 'depends on' },
  managed_by:      { w: 1.3, dash: '2 4',         arrow: 'open',  tone: 'depend',  label: 'managed by' },
  located_in:      { w: 1.2, dash: '1 4',         arrow: 'none',  tone: 'contain', label: 'located in' },
  contains:        { w: 1.2, dash: '1 4',         arrow: 'none',  tone: 'contain', label: 'contains' },
  part_of:         { w: 1.4, dash: '1 4',         arrow: 'none',  tone: 'contain', label: 'part of' },
  monitors:        { w: 1.2, dash: '1 3',         arrow: 'open',  tone: 'observe', label: 'monitors' },
  measures:        { w: 1.2, dash: '1 3',         arrow: 'open',  tone: 'observe', label: 'measures' },
  backs_up:        { w: 1.9, dash: '9 3 2 3',     arrow: 'solid', tone: 'backup',  label: 'backs up' },
  connected_to:    { w: 1.5, dash: '',            arrow: 'none',  tone: 'link',    label: 'connected to' },
  associated_with: { w: 1.1, dash: '2 6',         arrow: 'none',  tone: 'link',    label: 'associated with' },
  replaces:        { w: 1.4, dash: '6 3 1 3',     arrow: 'open',  tone: 'link',    label: 'replaces' },
};
const EDGE_FALLBACK = { w: 1.4, dash: '3 3', arrow: 'open', tone: 'link', label: 'other' };
const edgeStyle = (type) => EDGE_STYLE[type] || EDGE_FALLBACK;
const EDGE_TONES = ['supply', 'host', 'control', 'depend', 'contain', 'observe', 'backup', 'link'];

/** Kind glyphs. A 16px line drawing beats a coloured dot at distinguishing an
 *  inverter from a valve at a glance, and it survives a monochrome display. */
const ICONS = {
  battery:  'M3 7h13v10H3zM16 10h2v4h-2zM6 10v4M9 10v4',
  bolt:     'M12 2 5 13h5l-1 9 8-12h-5z',
  sun:      'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1',
  panel:    'M4 3h16v18H4zM4 9h16M4 15h16M8 6h.01M8 12h.01M8 18h.01',
  switchgear: 'M5 6h5l9 12M5 18h5M19 6h-4M15 6a2 2 0 1 0 4 0 2 2 0 0 0-4 0',
  server:   'M3 4h18v6H3zM3 14h18v6H3zM7 7h.01M7 17h.01',
  rack:     'M4 3h16v18H4zM4 8h16M4 13h16M4 18h16',
  network:  'M12 3v5M5 21v-4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v4M9 3h6v5H9z',
  wifi:     'M2 8a16 16 0 0 1 20 0M5.5 12a11 11 0 0 1 13 0M9 15.6a6 6 0 0 1 6 0M12 20h.01',
  storage:  'M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01',
  sensor:   'M12 3v4M12 17v4M3 12h4M17 12h4M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  camera:   'M3 7h11v10H3zM14 10l6-3v10l-6-3z',
  siren:    'M12 3a5 5 0 0 0-5 5v5H5v3h14v-3h-2V8a5 5 0 0 0-5-5zM10 19a2 2 0 0 0 4 0',
  pump:     'M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM12 8v4l3 2M20 6h-4',
  valve:    'M4 8l7 4-7 4zM20 8l-7 4 7 4zM12 4v16',
  tank:     'M5 7a7 3 0 0 1 14 0v10a7 3 0 0 1-14 0zM5 7a7 3 0 0 0 14 0',
  pipe:     'M2 9h8v6H2zM14 9h8v6h-8zM10 7h4v10h-4z',
  filter:   'M3 4h18l-7 8v8l-4-2v-6z',
  meter:    'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 12l4-3',
  well:     'M4 8h16l-2 13H6zM4 8 12 3l8 5',
  structure:'M3 11 12 4l9 7v10H3zM9 21v-6h6v6',
  room:     'M4 4h16v16H4zM4 12h7',
  zone:     'M4 6l8-3 8 3v9l-8 6-8-6z',
  site:     'M12 2 2 8v13h20V8zM9 21v-7h6v7',
  service:  'M4 6h16v12H4zM8 10h8M8 14h5',
  load:     'M6 3h12l-2 8h3l-9 10 2-8H6z',
  generator:'M4 7h16v10H4zM8 11h8M12 4v3M9 20l1.5-3M15 20l-1.5-3',
  fuel:     'M5 3h9v18H5zM14 8h3v9a1.5 1.5 0 0 1-3 0z',
  phone:    'M7 2h10v20H7zM10 5h4M11 19h2',
  monitorbox: 'M4 5h16v11H4zM9 20h6M12 16v4',
  generic:  'M5 5h14v14H5z',
};

/** asset_class -> icon, matched longest-prefix-first so a class the register
 *  gains tomorrow still lands on something sensible instead of a blank card. */
const CLASS_ICON = [
  ['battery_bank', 'battery'], ['bms', 'battery'], ['ups', 'battery'],
  ['pv_array', 'sun'], ['pv_row', 'sun'], ['combiner', 'switchgear'],
  ['inverter', 'bolt'], ['generator', 'generator'], ['ats', 'switchgear'],
  ['disconnect', 'switchgear'], ['panel', 'panel'], ['pdu', 'panel'],
  ['load', 'load'], ['fuel', 'fuel'],
  ['server', 'server'], ['rack', 'rack'], ['storage_array', 'storage'],
  ['switch', 'network'], ['router', 'network'], ['network_segment', 'network'],
  ['access_point', 'wifi'], ['wireless_controller', 'wifi'],
  ['application_service', 'service'], ['voice_endpoint', 'phone'],
  ['environmental_monitor', 'monitorbox'], ['access_controller', 'panel'],
  ['safety_sensor', 'sensor'], ['alarm_output', 'siren'], ['camera', 'camera'],
  ['sensor', 'sensor'], ['flow_meter', 'meter'], ['pressure_sensor', 'meter'],
  ['pump', 'pump'], ['valve', 'valve'], ['tank', 'tank'], ['cistern', 'tank'],
  ['filter', 'filter'], ['well', 'well'], ['controller', 'panel'],
  ['irrigation_zone', 'zone'], ['graywater_branch', 'pipe'], ['pipe', 'pipe'],
  ['manifold', 'pipe'], ['heater', 'bolt'], ['treatment', 'filter'],
  ['structure', 'structure'], ['room', 'room'], ['geographic_zone', 'zone'],
  ['site', 'site'],
];

function iconFor(assetClass) {
  const key = String(assetClass || '');
  for (const [prefix, icon] of CLASS_ICON) {
    if (key === prefix || key.startsWith(`${prefix}_`) || key.endsWith(`_${prefix}`)) return ICONS[icon];
  }
  for (const [prefix, icon] of CLASS_ICON) {
    if (key.includes(prefix)) return ICONS[icon];
  }
  return ICONS.generic;
}

/** Worst first. The impact endpoint already sorts by this; the chip row below
 *  the blast-radius headline follows the same order so the two agree. */
const CRITICALITY_ORDER = ['life_safety', 'critical', 'important', 'discretionary'];

/* ======================================================= module state ==== */

/* Kept at module scope so a hash change (deep-linking a selection) repaints
   instantly from cache instead of refetching and resetting the camera. The
   module object is cached by the dynamic import in app.js, so this survives
   the router destroying and remounting the view. */
const STATE = {
  payload: null,
  fetchedAt: 0,
  camera: null,
  filters: { domain: '', criticality: '', status: '', health: '', q: '', edgeTypes: null },
  selectedId: null,
  blast: null,          // { originId, data }
  blastOn: false,
};

/* ================================================== persisted positions == */

function loadJSON(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch { return fallback; }
}

function saveJSON(key, value) {
  try { window.localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
}

const loadOverrides = () => loadJSON(POSITIONS_KEY, {}) || {};
const saveOverrides = (value) => saveJSON(POSITIONS_KEY, value);

/* ================================================================ style == */

function ensureStylesheet() {
  const id = 'topology-css';
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = `${window.__UI_ROOT__ || '/ui/'}topology.css`;
  document.head.appendChild(link);
}

/* =============================================================== layout == */

/**
 * Deterministic layered layout. Every input is either the design package or a
 * value derived from it by a total order, and nothing consults a clock, a
 * random source or the previous frame. The same package therefore produces the
 * same picture on every machine and every reload — which is the whole point:
 * an operator who learns that the battery bank sits third from the left in the
 * energy band must find it there tomorrow.
 *
 * 1. **Layout graph.** Directed edges are the register's propagating
 *    relationships in the direction the backend reports (`propagates`, which is
 *    the alarm correlator's own table), plus parent → child containment. The
 *    picture therefore flows the way failure flows: sources on the left, the
 *    things that die with them on the right.
 * 2. **Cycle breaking.** The register genuinely contains cycles (a valve that
 *    is part of an irrigation zone that controls the valve). An iterative DFS
 *    over ids in lexical order drops any edge that closes back onto the current
 *    stack. Lexical order makes the choice of which edge to drop reproducible.
 * 3. **Ranking.** Longest path from the sources over the remaining DAG, by
 *    Kahn topological order. Rank becomes the x column.
 * 4. **Bands.** Each node belongs to the outermost group that contains it,
 *    excluding the site root (that is the whole canvas). Ungrouped nodes fall
 *    into a per-domain band. Bands are ordered by (lowest rank of any member,
 *    band key) and occupy disjoint horizontal strips, which is what keeps a
 *    group's boundary box a tidy rectangle instead of a shape wrapped around
 *    half the diagram.
 * 5. **Row ordering.** Inside one (band, rank) bucket, nodes start in
 *    (domain, asset_id) order and are then refined by three fixed barycentre
 *    passes — down, up, down — sorted by the mean row of their neighbours with
 *    asset_id breaking every tie. Fixed pass count and total tie-breaking mean
 *    the result is a pure function of the input, unlike the iterate-to-
 *    equilibrium relaxation a force layout performs.
 */
export function computeLayout(payload) {
  const nodes = payload.nodes.slice().sort((a, b) => (a.asset_id < b.asset_id ? -1 : 1));
  const groupIds = new Set(payload.groups.map((g) => g.group_id));
  const byId = new Map(nodes.map((n) => [n.asset_id, n]));

  // Groups are drawn as boundaries, not cards, so they are not placed in rows.
  const placed = nodes.filter((n) => !groupIds.has(n.asset_id));

  /* -- 1. layout graph ---------------------------------------------------- */
  const out = new Map();
  const inn = new Map();
  const addEdge = (from, to) => {
    if (from === to || !byId.has(from) || !byId.has(to)) return;
    if (!out.has(from)) out.set(from, new Set());
    if (!inn.has(to)) inn.set(to, new Set());
    out.get(from).add(to);
    inn.get(to).add(from);
  };
  payload.edges.forEach((edge) => {
    if (edge.propagates === 'forward') addEdge(edge.from_asset_id, edge.to_asset_id);
    else if (edge.propagates === 'reverse') addEdge(edge.to_asset_id, edge.from_asset_id);
  });
  nodes.forEach((n) => { if (n.parent_id) addEdge(n.parent_id, n.asset_id); });

  /* -- 2. cycle breaking (iterative DFS, lexical order) ------------------- */
  const WHITE = 0; const GREY = 1; const BLACK = 2;
  const colour = new Map(nodes.map((n) => [n.asset_id, WHITE]));
  const dropped = new Set();
  const sortedIds = nodes.map((n) => n.asset_id);
  for (const root of sortedIds) {
    if (colour.get(root) !== WHITE) continue;
    const stack = [{ id: root, kids: Array.from(out.get(root) || []).sort(), i: 0 }];
    colour.set(root, GREY);
    while (stack.length) {
      const frame = stack[stack.length - 1];
      if (frame.i >= frame.kids.length) {
        colour.set(frame.id, BLACK);
        stack.pop();
        continue;
      }
      const kid = frame.kids[frame.i++];
      const state = colour.get(kid);
      if (state === GREY) { dropped.add(`${frame.id}|${kid}`); continue; }
      if (state === BLACK) continue;
      colour.set(kid, GREY);
      stack.push({ id: kid, kids: Array.from(out.get(kid) || []).sort(), i: 0 });
    }
  }
  const forward = (id) => Array.from(out.get(id) || []).sort()
    .filter((to) => !dropped.has(`${id}|${to}`));
  const backward = (id) => Array.from(inn.get(id) || []).sort()
    .filter((from) => !dropped.has(`${from}|${id}`));

  /* -- 3. longest-path ranking ------------------------------------------- */
  const indegree = new Map(sortedIds.map((id) => [id, backward(id).length]));
  const rank = new Map(sortedIds.map((id) => [id, 0]));
  const queue = sortedIds.filter((id) => indegree.get(id) === 0);
  let cursor = 0;
  while (cursor < queue.length) {
    const id = queue[cursor++];
    for (const to of forward(id)) {
      rank.set(to, Math.max(rank.get(to), rank.get(id) + 1));
      indegree.set(to, indegree.get(to) - 1);
      if (indegree.get(to) === 0) queue.push(to);
    }
  }

  /* -- 4. bands ----------------------------------------------------------- */
  /* A node's band is its *innermost* containing group, and bands are ordered
     depth-first over the group tree. That is what makes every boundary a clean
     rectangle: an inner group occupies one contiguous strip, and its parent
     occupies the union of its children's strips, which is contiguous too.
     Banding by the outermost group instead would let three rooms interleave
     row by row and their boundaries would cross each other. */
  const groupById = new Map(payload.groups.map((g) => [g.group_id, g]));
  const rootSet = new Set(payload.groups.filter((g) => g.level === 0).map((g) => g.group_id));

  const ancestry = (groupId) => {
    const chain = [];
    let cursor = groupId;
    let guard = 0;
    while (cursor && guard++ < 32) {
      if (!rootSet.has(cursor)) chain.unshift(cursor);   // the site is the canvas itself
      cursor = (groupById.get(cursor) || {}).parent_group_id;
    }
    return chain;
  };

  const innermost = new Map();       // asset -> band-defining group id
  payload.groups.forEach((group) => {
    if (rootSet.has(group.group_id)) return;
    group.members.forEach((member) => {
      const held = innermost.get(member);
      if (!held || (groupById.get(group.group_id).level > groupById.get(held).level)) {
        innermost.set(member, group.group_id);
      }
    });
  });

  const bandOf = (node) => innermost.get(node.asset_id) || `domain:${node.domain}`;
  const bands = new Map();
  placed.forEach((node) => {
    const key = bandOf(node);
    if (!bands.has(key)) {
      // "~" sorts after every asset id, so ungrouped domain strips gather below
      // the grouped ones instead of splitting a boundary in half.
      const path = key.startsWith('domain:') ? ['~', key] : ancestry(key);
      bands.set(key, { key, path, members: [] });
    }
    bands.get(key).members.push(node.asset_id);
  });
  const bandOrder = Array.from(bands.values()).sort((a, b) => {
    const depth = Math.max(a.path.length, b.path.length);
    for (let i = 0; i < depth; i += 1) {
      const left = a.path[i] || '';
      const right = b.path[i] || '';
      if (left !== right) return left < right ? -1 : 1;
    }
    return 0;
  });

  /** How much clear space two adjacent strips need: enough for the boundaries
   *  that end between them, which are the ones at their first differing level. */
  const gapBetween = (a, b) => {
    let shared = 0;
    while (shared < a.path.length && shared < b.path.length && a.path[shared] === b.path[shared]) {
      shared += 1;
    }
    const level = Math.min(shared + 1, GROUP_PAD.length - 1);
    return GROUP_PAD[level] * 2 + BAND_GAP;
  };

  /* -- 5. rows ------------------------------------------------------------ */
  const bandIndex = new Map();
  bandOrder.forEach((band, index) => bandIndex.set(band.key, index));

  const buckets = new Map();         // `${bandKey}|${rank}` -> [assetId]
  placed.forEach((node) => {
    const key = `${bandOf(node)}|${rank.get(node.asset_id)}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(node.asset_id);
  });
  buckets.forEach((list) => list.sort((a, b) => {
    const na = byId.get(a); const nb = byId.get(b);
    if (na.domain !== nb.domain) return na.domain < nb.domain ? -1 : 1;
    return a < b ? -1 : 1;
  }));

  // Ranks are normalised so the leftmost drawn column is column zero. The site
  // root is a boundary rather than a card, so without this the canvas would
  // open on an empty column.
  const minPlacedRank = Math.min(...placed.map((n) => rank.get(n.asset_id)), 0);

  const rowOf = new Map();
  const applyRows = () => {
    buckets.forEach((list) => list.forEach((id, index) => rowOf.set(id, index)));
  };
  applyRows();

  const meanRow = (ids) => {
    const known = ids.map((id) => rowOf.get(id)).filter((v) => v !== undefined);
    if (!known.length) return null;
    return known.reduce((a, b) => a + b, 0) / known.length;
  };
  const maxRank = Math.max(0, ...placed.map((n) => rank.get(n.asset_id)));
  for (let pass = 0; pass < BARYCENTRE_PASSES; pass += 1) {
    const descending = pass % 2 === 1;
    for (let r = 0; r <= maxRank; r += 1) {
      const level = descending ? maxRank - r : r;
      bandOrder.forEach((band) => {
        const list = buckets.get(`${band.key}|${level}`);
        if (!list || list.length < 2) return;
        const scored = list.map((id, index) => ({
          id,
          index,
          bc: meanRow(descending ? forward(id) : backward(id)),
        }));
        scored.sort((a, b) => {
          const av = a.bc === null ? a.index : a.bc;
          const bv = b.bc === null ? b.index : b.bc;
          if (av !== bv) return av - bv;
          return a.id < b.id ? -1 : 1;
        });
        buckets.set(`${band.key}|${level}`, scored.map((s) => s.id));
      });
      applyRows();
    }
  }

  /* -- 6. coordinates ----------------------------------------------------- */
  const bandRows = new Map();
  bandOrder.forEach((band) => {
    let tallest = 1;
    for (let r = 0; r <= maxRank; r += 1) {
      const list = buckets.get(`${band.key}|${r}`);
      if (list) tallest = Math.max(tallest, list.length);
    }
    bandRows.set(band.key, tallest);
  });
  const bandTop = new Map();
  let running = MARGIN;
  bandOrder.forEach((band, index) => {
    if (index > 0) running += gapBetween(bandOrder[index - 1], band);
    bandTop.set(band.key, running);
    running += (bandRows.get(band.key) - 1) * ROW_PITCH + CARD_H;
  });

  const overrides = loadOverrides();
  const positions = new Map();
  buckets.forEach((list, key) => {
    const [bandKey, rankText] = key.split('|');
    const r = Number(rankText);
    const height = bandRows.get(bandKey);
    const offset = (height - list.length) / 2;
    list.forEach((id, index) => {
      positions.set(id, {
        x: MARGIN + (r - minPlacedRank) * COL_PITCH,
        y: bandTop.get(bandKey) + (offset + index) * ROW_PITCH,
        rank: r,
        band: bandKey,
        bandIndex: bandIndex.get(bandKey),
        w: CARD_W,
        h: CARD_H,
        moved: false,
      });
    });
  });
  Object.entries(overrides).forEach(([id, xy]) => {
    const at = positions.get(id);
    if (at && Number.isFinite(xy.x) && Number.isFinite(xy.y)) {
      at.x = xy.x; at.y = xy.y; at.moved = true;
    }
  });

  /* -- 7. group boxes (bounding box of the members actually drawn) -------- */
  const boxes = [];
  payload.groups.forEach((group) => {
    const members = group.members.filter((id) => positions.has(id));
    if (!members.length) return;
    let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
    members.forEach((id) => {
      const at = positions.get(id);
      minX = Math.min(minX, at.x); minY = Math.min(minY, at.y);
      maxX = Math.max(maxX, at.x + at.w); maxY = Math.max(maxY, at.y + at.h);
    });
    const pad = GROUP_PAD[Math.min(group.level, GROUP_PAD.length - 1)];
    boxes.push({
      group,
      x: minX - pad,
      y: minY - pad - GROUP_TITLE_H,
      w: (maxX - minX) + pad * 2,
      h: (maxY - minY) + pad * 2 + GROUP_TITLE_H,
    });
  });
  boxes.sort((a, b) => (a.group.level - b.group.level) || (b.w * b.h - a.w * a.h));

  let extentX = 0; let extentY = 0;
  positions.forEach((at) => {
    extentX = Math.max(extentX, at.x + at.w); extentY = Math.max(extentY, at.y + at.h);
  });
  boxes.forEach((box) => {
    extentX = Math.max(extentX, box.x + box.w); extentY = Math.max(extentY, box.y + box.h);
  });

  return {
    positions, boxes, ranks: rank, maxRank, dropped: dropped.size,
    extent: { w: extentX + MARGIN, h: extentY + MARGIN },
    bands: bandOrder.length,
  };
}

/* =============================================================== camera == */

function makeCamera(host) {
  const saved = STATE.camera || loadJSON(CAMERA_KEY, null);
  const camera = {
    x: saved && Number.isFinite(saved.x) ? saved.x : 0,
    y: saved && Number.isFinite(saved.y) ? saved.y : 0,
    zoom: saved && Number.isFinite(saved.zoom) ? saved.zoom : 0.55,
    host,
  };
  return camera;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function zoomAt(camera, sx, sy, factor) {
  const next = clamp(camera.zoom * factor, MIN_ZOOM, MAX_ZOOM);
  if (next === camera.zoom) return;
  const wx = camera.x + sx / camera.zoom;
  const wy = camera.y + sy / camera.zoom;
  camera.zoom = next;
  camera.x = wx - sx / next;
  camera.y = wy - sy / next;
}

/* ============================================================== helpers == */

function anchor(rect, side) {
  if (side === 'right') return { x: rect.x + rect.w, y: rect.y + rect.h / 2 };
  if (side === 'left') return { x: rect.x, y: rect.y + rect.h / 2 };
  return { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 };
}

function edgePath(from, to) {
  const forwardFlow = to.x + to.w / 2 >= from.x + from.w / 2;
  const a = anchor(from, forwardFlow ? 'right' : 'left');
  const b = anchor(to, forwardFlow ? 'left' : 'right');
  const span = Math.abs(b.x - a.x);
  const bend = Math.max(38, Math.min(150, span * 0.45));
  const dir = forwardFlow ? 1 : -1;
  return `M${a.x.toFixed(1)} ${a.y.toFixed(1)} C${(a.x + bend * dir).toFixed(1)} ${a.y.toFixed(1)} `
       + `${(b.x - bend * dir).toFixed(1)} ${b.y.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
}

const shortName = (name, limit = 44) => (name && name.length > limit ? `${name.slice(0, limit - 1)}…` : name || '');

/* ================================================================= view == */

async function mount(root, ctx) {
  ensureStylesheet();
  clear(root);

  const shell = h('div', { class: 'topo' });
  root.appendChild(shell);

  const toolbar = h('div', { class: 'topo-toolbar' });
  const stage = h('div', { class: 'topo-stage' });
  const side = h('aside', { class: 'topo-side' });
  const statusbar = h('div', { class: 'topo-statusbar' });
  shell.append(toolbar, h('div', { class: 'topo-body' }, stage, side), statusbar);

  const fresh = Date.now() - STATE.fetchedAt < 60000;
  if (!STATE.payload || !fresh) {
    stage.appendChild(h('p', { class: 'loading', text: 'Loading topology…' }));
    const result = await get('/topology');
    clear(stage);
    if (!result.ok) {
      clear(shell);
      shell.appendChild(h('div', { class: 'page-head' },
        h('div', null,
          h('h2', { text: 'Topology' }),
          h('p', { class: 'lede', text: 'Asset topology, typed relationships and blast-radius analysis (SDD 16.1, 25.5).' }))));
      shell.appendChild(resultProblem(result, 'the topology graph'));
      return { async refresh() {}, destroy() {} };
    }
    STATE.payload = result.data;
    STATE.fetchedAt = Date.now();
  }

  const payload = STATE.payload;
  if (!payload.nodes.length) {
    clear(shell);
    shell.appendChild(h('div', { class: 'empty' },
      h('h3', null, statusChip('not_deployed', 'The registry is empty')),
      h('p', { text: 'No assets are loaded, so there is no topology to draw. Load the machine-readable design package to populate it.' })));
    return { async refresh() {}, destroy() {} };
  }

  const view = buildCanvas({ payload, stage, side, toolbar, statusbar });
  const deepLink = ctx.params && ctx.params.assetId;
  if (deepLink && payload.nodes.some((n) => n.asset_id === deepLink)) {
    view.select(deepLink, { centre: true });
  } else if (STATE.selectedId && payload.nodes.some((n) => n.asset_id === STATE.selectedId)) {
    view.select(STATE.selectedId, { centre: false });
  } else {
    view.select(null);
  }

  return {
    async refresh() {
      const result = await get('/topology');
      if (!result.ok) { view.setStale(result); return; }
      STATE.payload = result.data;
      STATE.fetchedAt = Date.now();
      view.applyHealth(result.data);
    },
    destroy() { view.destroy(); },
  };
}

/* ------------------------------------------------------------ the canvas -- */

function buildCanvas({ payload, stage, side, toolbar, statusbar }) {
  const layout = computeLayout(payload);
  const byId = new Map(payload.nodes.map((n) => [n.asset_id, n]));
  const groupIds = new Set(payload.groups.map((g) => g.group_id));
  const camera = makeCamera(stage);

  const edgesIn = new Map();
  const edgesOut = new Map();
  payload.edges.forEach((edge) => {
    if (!edgesOut.has(edge.from_asset_id)) edgesOut.set(edge.from_asset_id, []);
    if (!edgesIn.has(edge.to_asset_id)) edgesIn.set(edge.to_asset_id, []);
    edgesOut.get(edge.from_asset_id).push(edge);
    edgesIn.get(edge.to_asset_id).push(edge);
  });

  const allEdgeTypes = Object.keys(payload.summary.by_relationship_type).sort();
  if (!STATE.filters.edgeTypes) STATE.filters.edgeTypes = new Set(allEdgeTypes);

  /* ---- DOM skeleton ---- */
  const world = h('div', { class: 'topo-world' });
  const svg = svgEl('svg', { class: 'topo-svg', xmlns: 'http://www.w3.org/2000/svg' });
  const defs = svgEl('defs');
  const gridRect = svgEl('rect', { class: 'topo-grid', x: 0, y: 0, width: '100%', height: '100%', fill: 'url(#topo-grid-pattern)' });
  const gGroups = svgEl('g', { class: 'topo-groups' });
  const gEdges = svgEl('g', { class: 'topo-edges' });
  const cards = h('div', { class: 'topo-cards' });
  const gridPattern = svgEl('pattern', { id: 'topo-grid-pattern', patternUnits: 'userSpaceOnUse', width: 40, height: 40 });
  const gridDot = svgEl('circle', { cx: 20, cy: 20, r: 1, class: 'topo-grid-dot' });
  gridPattern.appendChild(gridDot);
  defs.appendChild(gridPattern);

  EDGE_TONES.forEach((tone) => {
    ['solid', 'open'].forEach((kind) => {
      const marker = svgEl('marker', {
        id: `topo-arrow-${tone}-${kind}`, viewBox: '0 0 10 10', refX: 9, refY: 5,
        markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse',
        markerUnits: 'userSpaceOnUse',
      });
      marker.appendChild(kind === 'solid'
        ? svgEl('path', { d: 'M0 1 L9 5 L0 9 z', class: `topo-arrow tone-${tone}` })
        : svgEl('path', { d: 'M1 1 L9 5 L1 9', class: `topo-arrow-open tone-${tone}` }));
      defs.appendChild(marker);
    });
  });
  svg.append(defs, gridRect, gGroups, gEdges);
  world.append(svg, cards);
  stage.appendChild(world);

  const overlay = h('div', { class: 'topo-overlay' });
  stage.appendChild(overlay);

  /* ---- group boundaries ---- */
  // Reticle's idea, kept: the boundary itself is inert so a drag across a group
  // interior still pans the canvas; only the title tab is a handle.
  const boxNodes = new Map();
  layout.boxes.forEach((box) => {
    const g = svgEl('g', { class: 'topo-group', 'data-id': box.group.group_id, 'data-level': box.group.level });
    const rect = svgEl('rect', {
      x: box.x, y: box.y, width: box.w, height: box.h, rx: 14,
      class: `topo-group-rect health-${box.group.health}`,
    });
    const caption = `${box.group.name.toUpperCase()}  ·  ${box.group.member_count} inside`;
    const tab = svgEl('rect', {
      x: box.x, y: box.y, width: Math.min(box.w, 20 + caption.length * 6.1), height: GROUP_TITLE_H, rx: 8,
      class: 'topo-group-tab', role: 'button', tabindex: '0',
      'aria-label': `${box.group.name}, boundary containing ${box.group.member_count} assets`,
    });
    const label = svgEl('text', { x: box.x + 14, y: box.y + GROUP_TITLE_H - 6, class: 'topo-group-label' }, caption);
    g.append(rect, tab, label);
    tab.addEventListener('pointerdown', (event) => { event.stopPropagation(); });
    tab.addEventListener('click', () => api.select(box.group.group_id, { centre: false }));
    tab.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      api.select(box.group.group_id, { centre: false });
    });
    gGroups.appendChild(g);
    boxNodes.set(box.group.group_id, g);
  });

  /* ---- edges ---- */
  const rectOf = (id) => {
    const at = layout.positions.get(id);
    if (at) return at;
    const box = layout.boxes.find((b) => b.group.group_id === id);
    return box ? { x: box.x, y: box.y, w: box.w, h: box.h } : null;
  };
  const edgeNodes = [];
  payload.edges.forEach((edge) => {
    const from = rectOf(edge.from_asset_id);
    const to = rectOf(edge.to_asset_id);
    if (!from || !to) return;
    const style = edgeStyle(edge.relationship_type);
    const path = svgEl('path', {
      class: `topo-edge tone-${style.tone}`,
      d: edgePath(from, to),
      'stroke-width': style.w,
      'stroke-dasharray': style.dash || null,
      'data-type': edge.relationship_type,
      'marker-end': style.arrow === 'none' ? null : `url(#topo-arrow-${style.tone}-${style.arrow})`,
    });
    path.appendChild(svgEl('title', null,
      `${byId.get(edge.from_asset_id)?.name || edge.from_asset_id} — ${style.label} → `
      + `${byId.get(edge.to_asset_id)?.name || edge.to_asset_id}`));
    gEdges.appendChild(path);
    edgeNodes.push({ edge, path, from: edge.from_asset_id, to: edge.to_asset_id });
  });

  /* ---- node cards ---- */
  const cardNodes = new Map();
  payload.nodes.forEach((node) => {
    if (groupIds.has(node.asset_id)) return;
    const at = layout.positions.get(node.asset_id);
    if (!at) return;
    const card = h('article', {
      class: 'topo-card',
      tabindex: '0',
      role: 'button',
      'data-id': node.asset_id,
      'data-health': node.health,
      'data-domain': node.domain,
      'data-criticality': node.criticality,
      'aria-label': `${node.name}, ${node.asset_class}, ${node.domain}, health ${node.health}`,
      style: `left:${at.x}px;top:${at.y}px;width:${at.w}px;height:${at.h}px`,
    },
      h('div', { class: 'topo-card-top' },
        svgEl('svg', { class: 'topo-card-icon', viewBox: '0 0 24 24', 'aria-hidden': 'true' },
          svgEl('path', { d: iconFor(node.asset_class) })),
        h('div', { class: 'topo-card-titles' },
          h('span', { class: 'topo-card-name', text: shortName(node.name) }),
          h('span', { class: 'topo-card-class', text: `${node.domain} · ${node.asset_class}` }))),
      h('div', { class: 'topo-card-foot' },
        statusChip(node.health),
        node.criticality === 'critical' || node.criticality === 'life_safety'
          ? h('span', { class: 'topo-crit', title: `Criticality: ${node.criticality}`, text: node.criticality === 'life_safety' ? 'LIFE SAFETY' : 'CRITICAL' })
          : null,
        node.open_field_count
          ? h('span', { class: 'topo-open', title: `${node.open_field_count} unresolved design fields`, text: `◇${node.open_field_count}` })
          : null,
        node.alarms.active
          ? h('span', { class: 'topo-alarmdot', title: `${node.alarms.active} active alarm(s)`, text: '▲' })
          : null));
    cards.appendChild(card);
    cardNodes.set(node.asset_id, card);
  });

  /* ---- painting ---- */
  let frame = null;
  function paint() {
    if (frame) return;
    frame = window.requestAnimationFrame(() => {
      frame = null;
      world.style.transform = `translate(${(-camera.x * camera.zoom).toFixed(2)}px,${(-camera.y * camera.zoom).toFixed(2)}px) scale(${camera.zoom.toFixed(4)})`;
      // The dot lattice is drawn in world units inside the scaled layer, so it
      // only needs its radius compensating to stay a 1px dot at any zoom.
      gridDot.setAttribute('r', (1 / camera.zoom).toFixed(3));
      stage.classList.toggle('is-mid', camera.zoom < ZOOM_MID && camera.zoom >= ZOOM_FAR);
      stage.classList.toggle('is-far', camera.zoom < ZOOM_FAR);
      STATE.camera = { x: camera.x, y: camera.y, zoom: camera.zoom };
      saveJSON(CAMERA_KEY, STATE.camera);
      zoomLabel.textContent = `${Math.round(camera.zoom * 100)}%`;
    });
  }

  function sizeWorld() {
    world.style.width = `${layout.extent.w}px`;
    world.style.height = `${layout.extent.h}px`;
    svg.setAttribute('width', layout.extent.w);
    svg.setAttribute('height', layout.extent.h);
    svg.setAttribute('viewBox', `0 0 ${layout.extent.w} ${layout.extent.h}`);
  }
  sizeWorld();

  function frameOn(box, { pad = 40, minZoom = MIN_ZOOM, maxZoom = 1 } = {}) {
    const rect = stage.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const zoom = clamp(Math.min(
      (rect.width - pad * 2) / box.w,
      (rect.height - pad * 2) / box.h,
    ), minZoom, maxZoom);
    camera.zoom = zoom;
    camera.x = box.x + box.w / 2 - rect.width / (2 * zoom);
    camera.y = box.y + box.h / 2 - rect.height / (2 * zoom);
    paint();
  }

  const fit = () => frameOn({ x: 0, y: 0, w: layout.extent.w, h: layout.extent.h });

  /** Opening frame. Fitting all 137 nodes lands around 10% zoom, where nothing
   *  is readable and the screen says nothing on arrival. So the canvas opens on
   *  the largest boundary — on this package that is the power and server
   *  container, which is exactly what SDD 16.1 is about. Fit is one key away. */
  function openingFrame() {
    const candidates = layout.boxes.filter((box) => box.group.level > 0);
    const biggest = candidates.sort((a, b) =>
      (b.group.member_count - a.group.member_count) || (a.group.group_id < b.group.group_id ? -1 : 1))[0];
    if (!biggest) { fit(); return; }
    frameOn(biggest, { pad: 40, minZoom: READABLE_ZOOM, maxZoom: 0.85 });
  }

  function centreOn(assetId) {
    const at = rectOf(assetId);
    if (!at) return;
    const rect = stage.getBoundingClientRect();
    camera.x = at.x + at.w / 2 - rect.width / (2 * camera.zoom);
    camera.y = at.y + at.h / 2 - rect.height / (2 * camera.zoom);
    paint();
  }

  /* ---- interaction: pan, zoom, node drag ---- */
  let panning = null;
  let dragging = null;

  stage.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 && event.button !== 1) return;
    const card = event.target.closest && event.target.closest('.topo-card');
    if (card && event.button === 0) {
      dragging = {
        id: card.dataset.id, card,
        startX: event.clientX, startY: event.clientY,
        originX: parseFloat(card.style.left), originY: parseFloat(card.style.top),
        moved: false, pointerId: event.pointerId,
      };
      card.setPointerCapture(event.pointerId);
      event.preventDefault();
      return;
    }
    panning = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    stage.setPointerCapture(event.pointerId);
    stage.classList.add('is-panning');
  });

  stage.addEventListener('pointermove', (event) => {
    if (dragging) {
      const dx = (event.clientX - dragging.startX) / camera.zoom;
      const dy = (event.clientY - dragging.startY) / camera.zoom;
      if (!dragging.moved && Math.hypot(dx, dy) * camera.zoom < 3) return;
      dragging.moved = true;
      const at = layout.positions.get(dragging.id);
      at.x = Math.round(dragging.originX + dx);
      at.y = Math.round(dragging.originY + dy);
      dragging.card.style.left = `${at.x}px`;
      dragging.card.style.top = `${at.y}px`;
      redrawEdgesFor(dragging.id);
      return;
    }
    if (!panning) return;
    camera.x -= (event.clientX - panning.x) / camera.zoom;
    camera.y -= (event.clientY - panning.y) / camera.zoom;
    panning.x = event.clientX; panning.y = event.clientY;
    paint();
  });

  function endPointer(event) {
    if (dragging) {
      const wasMoved = dragging.moved;
      const id = dragging.id;
      try { dragging.card.releasePointerCapture(dragging.pointerId); } catch { /* already gone */ }
      dragging = null;
      if (wasMoved) {
        const at = layout.positions.get(id);
        at.moved = true;
        const overrides = loadOverrides();
        overrides[id] = { x: at.x, y: at.y };
        saveOverrides(overrides);
        recomputeBoxes();
        paintMovedChip();
      } else {
        api.select(id, { centre: false });
      }
      return;
    }
    if (panning) {
      try { stage.releasePointerCapture(panning.pointerId); } catch { /* already gone */ }
      panning = null;
      stage.classList.remove('is-panning');
      if (event && event.target === stage) api.select(null);
    }
  }
  stage.addEventListener('pointerup', endPointer);
  stage.addEventListener('pointercancel', endPointer);

  stage.addEventListener('wheel', (event) => {
    event.preventDefault();
    const rect = stage.getBoundingClientRect();
    zoomAt(camera, event.clientX - rect.left, event.clientY - rect.top,
      Math.exp(-event.deltaY * (event.deltaMode === 1 ? 0.05 : 0.0016)));
    paint();
  }, { passive: false });

  cards.addEventListener('keydown', (event) => {
    const card = event.target.closest && event.target.closest('.topo-card');
    if (!card) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      api.select(card.dataset.id, { centre: false });
    }
  });

  function redrawEdgesFor(assetId) {
    edgeNodes.forEach((item) => {
      if (item.from !== assetId && item.to !== assetId) return;
      const from = rectOf(item.from); const to = rectOf(item.to);
      if (from && to) item.path.setAttribute('d', edgePath(from, to));
    });
  }

  function recomputeBoxes() {
    layout.boxes.forEach((box) => {
      const members = box.group.members.filter((id) => layout.positions.has(id));
      if (!members.length) return;
      let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
      members.forEach((id) => {
        const at = layout.positions.get(id);
        minX = Math.min(minX, at.x); minY = Math.min(minY, at.y);
        maxX = Math.max(maxX, at.x + at.w); maxY = Math.max(maxY, at.y + at.h);
      });
      const pad = GROUP_PAD[Math.min(box.group.level, GROUP_PAD.length - 1)];
      box.x = minX - pad; box.y = minY - pad - GROUP_TITLE_H;
      box.w = (maxX - minX) + pad * 2; box.h = (maxY - minY) + pad * 2 + GROUP_TITLE_H;
      const g = boxNodes.get(box.group.group_id);
      const rect = g && g.querySelector('.topo-group-rect');
      const tab = g && g.querySelector('.topo-group-tab');
      const label = g && g.querySelector('text');
      if (rect) {
        rect.setAttribute('x', box.x); rect.setAttribute('y', box.y);
        rect.setAttribute('width', box.w); rect.setAttribute('height', box.h);
      }
      if (label) { label.setAttribute('x', box.x + 14); label.setAttribute('y', box.y + GROUP_TITLE_H - 6); }
      if (tab) { tab.setAttribute('x', box.x); tab.setAttribute('y', box.y); }
    });
  }

  /* ---- filters and highlighting ---- */
  function matches(node) {
    const f = STATE.filters;
    if (f.domain && node.domain !== f.domain) return false;
    if (f.criticality && node.criticality !== f.criticality) return false;
    if (f.status && node.status !== f.status) return false;
    if (f.health && node.health !== f.health) return false;
    if (f.q) {
      const needle = f.q.toLowerCase();
      if (!node.asset_id.toLowerCase().includes(needle)
        && !(node.name || '').toLowerCase().includes(needle)
        && !(node.asset_class || '').toLowerCase().includes(needle)) return false;
    }
    return true;
  }

  function applyVisualState() {
    const blastSet = STATE.blastOn && STATE.blast
      ? new Set([STATE.blast.data.origin.asset_id, ...STATE.blast.data.affected.map((a) => a.asset_id)])
      : null;
    let shown = 0;
    cardNodes.forEach((card, id) => {
      const node = byId.get(id);
      const ok = matches(node);
      if (ok) shown += 1;
      const inBlast = !blastSet || blastSet.has(id);
      card.classList.toggle('is-filtered', !ok);
      card.classList.toggle('is-outside-blast', !inBlast);
      card.classList.toggle('is-selected', id === STATE.selectedId);
      card.classList.toggle('is-origin', Boolean(blastSet) && id === STATE.blast.data.origin.asset_id);
    });
    boxNodes.forEach((g, id) => {
      const inBlast = !blastSet || blastSet.has(id);
      g.classList.toggle('is-outside-blast', !inBlast);
      g.classList.toggle('is-selected', id === STATE.selectedId);
    });
    const neighbours = new Set();
    if (STATE.selectedId) {
      (edgesIn.get(STATE.selectedId) || []).forEach((e) => neighbours.add(e.from_asset_id));
      (edgesOut.get(STATE.selectedId) || []).forEach((e) => neighbours.add(e.to_asset_id));
    }
    edgeNodes.forEach((item) => {
      const typeOn = STATE.filters.edgeTypes.has(item.edge.relationship_type);
      const inBlast = !blastSet || (blastSet.has(item.from) && blastSet.has(item.to));
      const touching = STATE.selectedId
        && (item.from === STATE.selectedId || item.to === STATE.selectedId);
      item.path.classList.toggle('is-hidden', !typeOn);
      item.path.classList.toggle('is-outside-blast', !inBlast);
      item.path.classList.toggle('is-touching', Boolean(touching));
    });
    cardNodes.forEach((card, id) => {
      card.classList.toggle('is-neighbour', neighbours.has(id));
    });
    countLabel.textContent = shown === cardNodes.size
      ? `${cardNodes.size} nodes · ${edgeNodes.length} relationships`
      : `${shown} of ${cardNodes.size} nodes match · ${edgeNodes.length} relationships`;
  }

  /* ---- inspector ---- */
  function relationshipList(assetId) {
    const rows = [];
    (edgesOut.get(assetId) || []).forEach((e) => rows.push({ e, dir: 'out', other: e.to_asset_id }));
    (edgesIn.get(assetId) || []).forEach((e) => rows.push({ e, dir: 'in', other: e.from_asset_id }));
    if (!rows.length) return h('p', { class: 'card-note', text: 'No typed relationships recorded for this asset.' });
    rows.sort((a, b) => (a.e.relationship_type < b.e.relationship_type ? -1
      : a.e.relationship_type > b.e.relationship_type ? 1 : (a.other < b.other ? -1 : 1)));
    return h('ul', { class: 'topo-rel' }, rows.map(({ e, dir, other }) => {
      const style = edgeStyle(e.relationship_type);
      const node = byId.get(other);
      return h('li', { class: `topo-rel-item tone-${style.tone}` },
        h('span', { class: 'topo-rel-dir', 'aria-hidden': 'true', text: dir === 'out' ? '→' : '←' }),
        h('span', { class: 'topo-rel-type', text: style.label }),
        h('button', {
          class: 'topo-link', type: 'button', text: node ? node.name : other,
          onclick: () => api.select(other, { centre: true }),
        }),
        e.propagates
          ? h('span', { class: 'topo-rel-flag', title: 'Failure propagates along this edge', text: '⚡' })
          : h('span', { class: 'topo-rel-flag muted', title: e.semantic || 'Does not propagate failure', text: '·' }));
    }));
  }

  function renderInspector() {
    clear(side);
    if (!STATE.selectedId) {
      side.appendChild(h('div', { class: 'topo-panel' },
        h('h3', { text: 'Nothing selected' }),
        h('p', { class: 'card-note', text: 'Select an asset to see its identity, its relationships in and out, its unresolved design fields, and what a failure of it would take with it.' })));
      side.appendChild(legendPanel());
      side.appendChild(packagePanel());
      return;
    }
    const node = byId.get(STATE.selectedId);
    if (!node) return;
    const group = payload.groups.find((g) => g.group_id === node.asset_id);

    const head = h('div', { class: 'topo-panel topo-panel-head' },
      h('div', { class: 'topo-panel-title' },
        svgEl('svg', { class: 'topo-card-icon', viewBox: '0 0 24 24', 'aria-hidden': 'true' },
          svgEl('path', { d: iconFor(node.asset_class) })),
        h('h3', { text: node.name })),
      h('p', { class: 'topo-mono', text: node.asset_id }),
      h('div', { class: 'topo-chiprow' },
        statusChip(node.health),
        h('span', { class: 'chip chip-unknown', text: node.status }),
        h('span', { class: 'chip chip-unknown', text: node.criticality })),
      h('p', { class: 'card-note', text: node.health_note }),
      h('div', { class: 'topo-actions' },
        h('button', {
          class: 'btn btn-sm btn-primary', type: 'button', text: 'Blast radius',
          onclick: () => api.runBlast(node.asset_id),
        }),
        h('a', { class: 'btn btn-sm', href: `#/assets/${encodeURIComponent(node.asset_id)}`, text: 'Asset record' }),
        h('a', { class: 'btn btn-sm', href: `#/control/${encodeURIComponent(node.asset_id)}`, text: 'Control' })));

    const facts = h('div', { class: 'topo-panel' },
      h('h4', { text: 'Identity' }),
      h('dl', { class: 'kv' },
        ...[
          ['Domain', node.domain],
          ['Class', node.asset_class],
          ['Control authority', node.control_authority || '—'],
          ['Parent', node.parent_id || '—'],
          ['Points', `${node.points.total} total · ${node.points.bound} bound · ${node.points.reporting} reporting`],
          ['Last value', node.last_seen ? fmtAge(node.last_seen) : 'never'],
          ['Source', node.source],
          group ? ['Contains', `${group.member_count} assets`] : null,
        ].filter(Boolean).flatMap(([term, value]) => [h('dt', { text: term }), h('dd', { text: String(value) })])));

    const alarms = h('div', { class: 'topo-panel' },
      h('h4', { text: `Active alarms (${node.alarms.active})` }),
      node.alarms.active
        ? h('ul', { class: 'topo-alarms' }, node.alarms.items.map((alarm) => h('li', null,
            severityChip(alarm.severity),
            h('span', { text: ` ${alarm.message || alarm.alarm_key}` }),
            h('div', { class: 'card-note', text: `${alarm.state}${alarm.suppressed ? ' · suppressed' : ''} · ${alarm.since ? fmtAge(alarm.since) : 'unknown age'}` }))))
        : h('p', { class: 'card-note', text: node.health === 'not_deployed'
            ? 'None — and that is not a statement of health. This asset does not exist yet, so it cannot raise one.'
            : 'None active.' }));

    const openFields = h('div', { class: 'topo-panel' },
      h('h4', { text: `Unresolved design fields (${node.open_field_count})` }),
      node.open_field_count
        ? h('ul', { class: 'topo-fields' }, node.open_fields.map((field) => h('li', { text: field })))
        : h('p', { class: 'card-note', text: 'Nothing outstanding on this asset.' }));

    const rels = h('div', { class: 'topo-panel' },
      h('h4', { text: `Relationships (${(edgesIn.get(node.asset_id) || []).length + (edgesOut.get(node.asset_id) || []).length})` }),
      relationshipList(node.asset_id));

    side.append(head, blastPanel(), facts, alarms, rels, openFields, legendPanel());
  }

  function blastPanel() {
    const panel = h('div', { class: 'topo-panel topo-blast' });
    if (!STATE.blast || STATE.blast.originId !== STATE.selectedId) {
      panel.appendChild(h('h4', { text: 'Blast radius' }));
      panel.appendChild(h('p', { class: 'card-note', text: 'Press “Blast radius” to compute what a total loss of this asset takes with it (SDD 16.1).' }));
      return panel;
    }
    const data = STATE.blast.data;
    const s = data.summary;
    panel.appendChild(h('div', { class: 'topo-panel-title' },
      h('h4', { text: 'Blast radius' }),
      h('button', {
        class: `btn btn-sm${STATE.blastOn ? ' btn-primary' : ''}`, type: 'button',
        text: STATE.blastOn ? 'Dimming on' : 'Dim canvas',
        onclick: () => { STATE.blastOn = !STATE.blastOn; applyVisualState(); renderInspector(); },
      })));
    panel.appendChild(h('p', { class: 'topo-blast-head' },
      h('b', { text: String(s.affected_total) }),
      h('span', { text: ` of ${s.graph_total} assets lost · ` }),
      h('b', { text: String(s.unaffected_total) }),
      h('span', { text: ' survive' })));
    panel.appendChild(h('div', { class: 'topo-chiprow' },
      ...CRITICALITY_ORDER.filter((c) => s.by_criticality[c])
        .map((c) => h('span', { class: `chip chip-${c === 'life_safety' || c === 'critical' ? 'alarm' : c === 'important' ? 'degraded' : 'no_data'}`, text: `${s.by_criticality[c]} ${c}` }))));

    const list = h('ol', { class: 'topo-impact' }, data.affected.slice(0, 400).map((item) => h('li', {
      class: `crit-${item.criticality}`,
    },
      h('button', {
        class: 'topo-link', type: 'button', text: item.name,
        onclick: () => api.select(item.asset_id, { centre: true }),
      }),
      h('div', { class: 'topo-impact-meta' },
        h('span', { class: 'topo-impact-crit', text: item.criticality }),
        h('span', { text: ` · hop ${item.depth} · via ${(item.reached_by || []).join(', ')}` })),
      h('div', { class: 'topo-impact-path', title: item.path.map((p) => p.asset_id).join(' → ') },
        item.path.map((p) => (byId.get(p.asset_id) || {}).name || p.asset_id).join(' → ')))));
    panel.appendChild(h('h5', { class: 'topo-subhead', text: 'Impacted, worst criticality first' }));
    panel.appendChild(list);

    if (data.observability_lost.length) {
      panel.appendChild(h('h5', { class: 'topo-subhead', text: 'Survives, but goes blind' }));
      panel.appendChild(h('ul', { class: 'topo-fields' }, data.observability_lost.map((item) =>
        h('li', { text: `${item.name} — loses ${item.monitored_by.length} monitor(s)` }))));
    }
    if (data.redundancy_lost.length) {
      panel.appendChild(h('h5', { class: 'topo-subhead', text: 'Survives, but loses its backup' }));
      panel.appendChild(h('ul', { class: 'topo-fields' }, data.redundancy_lost.map((item) =>
        h('li', { text: `${item.name} — backed up by ${item.backed_up_by.join(', ')}` }))));
    }
    panel.appendChild(h('details', { class: 'raw' },
      h('summary', { text: 'How this was computed' }),
      h('p', { class: 'card-note', text: `Propagating forwards along ${data.rules.forward.join(', ')}; backwards along ${data.rules.reverse.join(', ')}. ${data.rules.hierarchy}` }),
      h('ul', { class: 'topo-fields' }, data.caveats.map((line) => h('li', { text: line })))));
    return panel;
  }

  function legendPanel() {
    const panel = h('div', { class: 'topo-panel topo-legend' });
    panel.appendChild(h('h4', { text: 'Legend' }));
    panel.appendChild(h('h5', { class: 'topo-subhead', text: 'Relationship types — click to hide' }));
    const types = h('ul', { class: 'topo-legend-edges' }, allEdgeTypes.map((type) => {
      const style = edgeStyle(type);
      const on = STATE.filters.edgeTypes.has(type);
      const sample = svgEl('svg', { class: 'topo-legend-sample', viewBox: '0 0 60 12', 'aria-hidden': 'true' },
        svgEl('path', {
          class: `topo-edge tone-${style.tone}`, d: 'M2 6 L48 6',
          'stroke-width': style.w, 'stroke-dasharray': style.dash || null,
          'marker-end': style.arrow === 'none' ? null : `url(#topo-arrow-${style.tone}-${style.arrow})`,
        }));
      return h('li', null, h('button', {
        class: `topo-legend-row${on ? '' : ' is-off'}`, type: 'button',
        'aria-pressed': on ? 'true' : 'false',
        onclick: () => {
          if (STATE.filters.edgeTypes.has(type)) STATE.filters.edgeTypes.delete(type);
          else STATE.filters.edgeTypes.add(type);
          applyVisualState();
          renderInspector();
        },
      },
        sample,
        h('span', { class: 'topo-legend-label', text: style.label }),
        h('span', { class: 'topo-legend-count', text: String(payload.summary.by_relationship_type[type]) })));
    }));
    panel.appendChild(types);
    panel.appendChild(h('p', { class: 'card-note', text: 'A dashed monitor line never carries failure — it carries sight. Solid heavy lines are supply.' }));

    panel.appendChild(h('h5', { class: 'topo-subhead', text: 'Health states' }));
    panel.appendChild(h('ul', { class: 'topo-legend-health' },
      Object.entries(payload.health_vocabulary).map(([key, text]) => h('li', null,
        statusChip(key),
        h('span', { class: 'card-note', text })))));
    return panel;
  }

  function packagePanel() {
    const pkg = payload.package;
    const panel = h('div', { class: 'topo-panel' });
    panel.appendChild(h('h4', { text: 'Where this graph comes from' }));
    panel.appendChild(h('p', { class: 'card-note', text: `${pkg.registry_nodes} assets from the registry database and ${pkg.extension_nodes} from design-package extensions that the loader does not merge yet. The register is authoritative; health is an observation drawn on top of it.` }));
    (pkg.extensions || []).forEach((ext) => {
      panel.appendChild(h('p', { class: 'card-note' },
        statusChip(ext.available ? 'design_only' : 'no_data'),
        h('span', { text: ` ${ext.file}: ${ext.error || `${ext.assets_applied} assets, ${ext.relationships_applied} relationships (${ext.document_status || 'status unrecorded'})`}` })));
    });
    return panel;
  }

  /* ---- toolbar ---- */
  const countLabel = h('span', { class: 'topo-count' });
  const zoomLabel = h('span', { class: 'topo-zoom', text: '100%' });
  const movedChip = h('span', { class: 'topo-moved', hidden: true });

  function paintMovedChip() {
    const overrides = loadOverrides();
    const count = Object.keys(overrides).length;
    movedChip.hidden = count === 0;
    clear(movedChip);
    if (count) {
      movedChip.append(
        h('span', { text: `${count} node${count === 1 ? '' : 's'} moved by hand` }),
        h('button', {
          class: 'btn btn-sm', type: 'button', text: 'Reset layout',
          onclick: () => {
            saveOverrides({});
            // Recompute once, with the overrides cleared, and re-seat every
            // card: that is the same pure function that placed them originally,
            // so "reset" always lands back on the canonical picture.
            const pristine = computeLayout(payload).positions;
            layout.positions.forEach((at, id) => {
              const fresh = pristine.get(id);
              if (!fresh) return;
              at.x = fresh.x; at.y = fresh.y; at.moved = false;
              const card = cardNodes.get(id);
              if (card) { card.style.left = `${at.x}px`; card.style.top = `${at.y}px`; }
            });
            edgeNodes.forEach((item) => {
              const from = rectOf(item.from); const to = rectOf(item.to);
              if (from && to) item.path.setAttribute('d', edgePath(from, to));
            });
            recomputeBoxes();
            paintMovedChip();
          },
        }));
    }
  }

  const facet = (name, label, values) => h('label', { class: 'topo-facet' },
    h('span', { class: 'visually-hidden', text: label }),
    h('select', {
      class: 'select-sm',
      title: label,
      onchange: (event) => { STATE.filters[name] = event.target.value; applyVisualState(); },
    },
      h('option', { value: '', text: label }),
      values.map((value) => h('option', { value, text: value, selected: STATE.filters[name] === value }))));

  const search = h('input', {
    type: 'search', class: 'input topo-search', placeholder: 'Search name, id or class',
    value: STATE.filters.q,
    oninput: (event) => { STATE.filters.q = event.target.value; applyVisualState(); },
    onkeydown: (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const hit = payload.nodes.find((n) => matches(n) && !groupIds.has(n.asset_id));
      if (hit) api.select(hit.asset_id, { centre: true });
    },
  });

  toolbar.append(
    h('div', { class: 'topo-toolbar-main' },
      search,
      facet('domain', 'Domain', Object.keys(payload.summary.by_domain).sort()),
      facet('criticality', 'Criticality', Object.keys(payload.summary.by_criticality).sort()),
      facet('status', 'Lifecycle', Object.keys(payload.summary.by_status).sort()),
      facet('health', 'Health', Object.keys(payload.summary.by_health).filter((k) => payload.summary.by_health[k]).sort()),
      h('button', { class: 'btn btn-sm', type: 'button', text: 'Clear', onclick: () => {
        STATE.filters.domain = ''; STATE.filters.criticality = ''; STATE.filters.status = '';
        STATE.filters.health = ''; STATE.filters.q = '';
        STATE.filters.edgeTypes = new Set(allEdgeTypes);
        toolbar.querySelectorAll('select').forEach((select) => { select.value = ''; });
        search.value = '';
        applyVisualState(); renderInspector();
      } })),
    h('div', { class: 'topo-toolbar-right' },
      movedChip,
      h('button', { class: 'btn btn-sm', type: 'button', text: '−', title: 'Zoom out', onclick: () => { const r = stage.getBoundingClientRect(); zoomAt(camera, r.width / 2, r.height / 2, 1 / 1.25); paint(); } }),
      zoomLabel,
      h('button', { class: 'btn btn-sm', type: 'button', text: '+', title: 'Zoom in', onclick: () => { const r = stage.getBoundingClientRect(); zoomAt(camera, r.width / 2, r.height / 2, 1.25); paint(); } }),
      h('button', { class: 'btn btn-sm', type: 'button', text: 'Fit', onclick: fit })));

  statusbar.append(
    countLabel,
    h('span', { class: 'topo-sep', text: '·' }),
    h('span', { text: `${payload.groups.length} boundaries · ${layout.bands} bands · ${layout.maxRank + 1} layers` }),
    h('span', { class: 'topo-sep', text: '·' }),
    h('span', {
      class: 'topo-det',
      title: 'The layout is a pure function of the design package: same package, same picture, every load.',
      text: `deterministic layout${layout.dropped ? ` · ${layout.dropped} cycle edge${layout.dropped === 1 ? '' : 's'} broken` : ''}`,
    }),
    h('span', { class: 'topo-sep', text: '·' }),
    h('span', { class: 'topo-generated', text: `generated ${fmtDateTime(payload.generated_at)}` }));

  /* ---- public surface ---- */
  const blastCache = new Map();
  const api = {
    select(assetId, options = {}) {
      STATE.selectedId = assetId;
      if (assetId && STATE.blast && STATE.blast.originId !== assetId) STATE.blastOn = false;
      applyVisualState();
      renderInspector();
      if (assetId && options.centre) centreOn(assetId);
      // replaceState rather than assigning location.hash: the shell's router
      // keys on the hash and would tear this view down and rebuild it on every
      // click. The URL still deep-links on reload and copies correctly.
      const target = assetId ? `#/topology/${encodeURIComponent(assetId)}` : '#/topology';
      if (window.location.hash !== target) {
        window.history.replaceState(null, '', target);
      }
      if (assetId) {
        const card = cardNodes.get(assetId);
        if (card) card.focus({ preventScroll: true });
      }
    },
    async runBlast(assetId) {
      if (!blastCache.has(assetId)) {
        const result = await get(`/topology/impact/${encodeURIComponent(assetId)}`);
        if (!result.ok) {
          clear(side);
          side.appendChild(resultProblem(result, `the blast radius of ${assetId}`));
          return;
        }
        blastCache.set(assetId, result.data);
      }
      STATE.blast = { originId: assetId, data: blastCache.get(assetId) };
      STATE.blastOn = true;
      applyVisualState();
      renderInspector();
    },
    setStale(result) {
      statusbar.classList.add('is-stale');
      statusbar.title = result.error || 'The topology could not be refreshed.';
    },
    applyHealth(next) {
      // Health only. Positions never move on a poll: the graph's shape comes
      // from the versioned package, not from the telemetry.
      statusbar.classList.remove('is-stale');
      next.nodes.forEach((fresh) => {
        const previous = byId.get(fresh.asset_id);
        if (!previous) return;
        Object.assign(previous, fresh);
        const card = cardNodes.get(fresh.asset_id);
        if (!card) return;
        card.dataset.health = fresh.health;
        const chip = card.querySelector('.topo-card-foot .chip');
        if (chip) chip.replaceWith(statusChip(fresh.health));
      });
      if (STATE.selectedId) renderInspector();
    },
    destroy() {
      if (frame) window.cancelAnimationFrame(frame);
      window.removeEventListener('keydown', onKey);
    },
  };

  function onKey(event) {
    if (event.target && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) {
      if (event.key === 'Escape') event.target.blur();
      return;
    }
    if (event.key === 'Escape') { STATE.blastOn = false; api.select(null); }
    else if (event.key === 'f') fit();
    else if (event.key === '/') { event.preventDefault(); search.focus(); }
  }
  window.addEventListener('keydown', onKey);

  paintMovedChip();
  applyVisualState();
  if (!STATE.camera) openingFrame(); else paint();
  overlay.appendChild(h('p', { class: 'topo-hint', text: 'Drag to pan · wheel to zoom · drag a card to reposition it · f to fit · / to search' }));
  window.setTimeout(() => overlay.classList.add('is-faded'), 6000);

  return api;
}

export default {
  title: 'Topology',
  mount,
};
