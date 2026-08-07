/* ---------------------------------------------------------------------------
   Operator console shell: router, refresh loop, chrome and shared UI kit.

   Vanilla ES modules, no framework, no build step, no network dependency other
   than this server (SDD 5.1 / FR-006). Views are loaded lazily with dynamic
   import so a phone opening the alarm screen never parses the map code.

   The shell owns three things every screen depends on:
     * one polled `/overview` request that drives the top bar and the nav badge;
     * the freshness / offline indicator, so nothing on screen can silently rot;
     * the operator identity and the mandatory-reason dialog for writes.
--------------------------------------------------------------------------- */

import { api, connection, fmtAge, identity, on, roleAtLeast } from './api.js';

export { fmtAge, fmtDateTime, fmtDuration, fmtNumber, fmtTime, identity, roleAtLeast } from './api.js';

const UI_ROOT = window.__UI_ROOT__ || '/ui/';

/* =========================================================== DOM helpers === */

/** Hyperscript. Children are appended as text nodes unless already nodes, so
 *  server-provided strings can never be interpreted as markup. */
export function h(tag, attrs = null, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    }
  }
  append(node, children);
  return node;
}

export function svgEl(tag, attrs = null, ...children) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else node.setAttribute(key, String(value));
    }
  }
  children.flat(4).forEach((child) => {
    if (child === null || child === undefined || child === false) return;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  });
  return node;
}

function append(node, children) {
  children.flat(4).forEach((child) => {
    if (child === null || child === undefined || child === false || child === '') return;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  });
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* ========================================================= status vocab === */

/** The availability vocabulary the API speaks, rendered so that colour is
 *  never the only signal (glyph + word + colour). */
export const STATUS_META = {
  ok:            { glyph: '●', label: 'Live' },
  stale:         { glyph: '◐', label: 'Stale' },
  no_data:       { glyph: '○', label: 'No data' },
  no_points:     { glyph: '○', label: 'No points' },
  design_only:   { glyph: '◇', label: 'Design only' },
  not_deployed:  { glyph: '⬚', label: 'Not yet deployed' },
  alarm:         { glyph: '▲', label: 'Alarm' },
  degraded:      { glyph: '▼', label: 'Degraded' },
  unknown:       { glyph: '?',      label: 'Unknown' },
};

export const SEVERITY_META = {
  emergency: { glyph: '✖', label: 'Emergency' },
  critical:  { glyph: '▲', label: 'Critical' },
  major:     { glyph: '◆', label: 'Major' },
  warning:   { glyph: '!', label: 'Warning' },
  info:      { glyph: 'ℹ', label: 'Info' },
};

export function statusChip(status, textOverride, big = false) {
  const meta = STATUS_META[status] || STATUS_META.unknown;
  const key = STATUS_META[status] ? status : 'unknown';
  return h('span', { class: `chip chip-${key}${big ? ' chip-lg' : ''}`, title: meta.label },
    h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: meta.glyph }),
    textOverride || meta.label);
}

export function severityChip(severity, textOverride, solid = false) {
  const key = SEVERITY_META[severity] ? severity : 'info';
  const meta = SEVERITY_META[key];
  return h('span', { class: `chip chip-${key}${solid ? ' chip-solid' : ''}` },
    h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: meta.glyph }),
    textOverride || meta.label);
}

/* ============================================================ UI blocks === */

export function card({ title, status, badge, note, children = [], statusText }) {
  const head = h('div', { class: 'card-head' },
    title ? h('h3', { text: title }) : null,
    badge || (status ? statusChip(status, statusText) : null));
  return h('section', { class: 'card', dataset: status ? { status } : {} },
    (title || badge || status) ? head : null,
    ...(Array.isArray(children) ? children : [children]),
    note ? h('p', { class: 'card-note', text: note }) : null);
}

export function kv(pairs) {
  const list = h('dl', { class: 'kv' });
  pairs.forEach(([term, value]) => {
    if (value === undefined) return;
    list.appendChild(h('dt', { text: term }));
    const dd = h('dd');
    append(dd, [value === null || value === '' ? '—' : value]);
    list.appendChild(dd);
  });
  return list;
}

export function emptyState(title, body, items = []) {
  return h('div', { class: 'empty' },
    h('h3', { text: title }),
    body ? h('p', { text: body }) : null,
    items.length ? h('ul', null, items.map((item) => h('li', { text: item }))) : null);
}

/** Render a fetch result that failed, in operator language. */
export function resultProblem(result, what) {
  if (result.offline) {
    return h('div', { class: 'error-box' },
      h('h3', { text: 'API unreachable' }),
      h('p', { text: `${what} could not be loaded: ${result.error}. The last known values, if any, are shown elsewhere on this screen and are not live.` }));
  }
  if (result.missing) {
    return emptyState(
      'Endpoint not available on this node',
      `${what} needs an API route this server does not expose yet. This is a build-out gap, not a fault: the rest of the console keeps working.`,
      [result.error || `HTTP ${result.status}`]);
  }
  if (result.forbidden) {
    return h('div', { class: 'error-box' },
      h('h3', { text: 'Not permitted' }),
      h('p', { text: `${result.error} — set an operator identity with a sufficient role using the button in the top bar.` }));
  }
  return h('div', { class: 'error-box' },
    h('h3', { text: `Could not load ${what}` }),
    h('p', { text: `${result.error || 'Unknown error'} (HTTP ${result.status})` }));
}

/**
 * The core honesty widget: renders a metric object from the overview API.
 * An unavailable metric shows *why* it is unavailable and never shows a zero.
 */
export function metricReadout(metric, { digits = 1, unit } = {}) {
  if (!metric) {
    return h('div', { class: 'readout unavailable' }, h('span', { class: 'value', text: 'Unknown' }));
  }
  if (!metric.available || metric.value === null || metric.value === undefined) {
    const meta = STATUS_META[metric.status] || STATUS_META.unknown;
    return h('div', { class: 'readout unavailable' },
      h('span', { class: 'value', text: meta.label }));
  }
  const value = typeof metric.value === 'number' ? formatNum(metric.value, digits) : String(metric.value);
  return h('div', { class: 'readout' },
    h('span', { class: 'value num', text: value }),
    (unit || metric.unit) ? h('span', { class: 'unit', text: unit || metric.unit }) : null);
}

function formatNum(value, digits) {
  const abs = Math.abs(value);
  const places = abs >= 100 ? 0 : abs >= 10 ? digits : Math.max(digits, 1);
  return value.toLocaleString(undefined, { minimumFractionDigits: places, maximumFractionDigits: places });
}

/** SDD 38: measured values must be visibly separate from calculated ones. */
export function provenance(metric) {
  if (!metric) return null;
  const lines = [];
  if (!metric.available) {
    if (metric.note) lines.push(metric.note);
  } else {
    if (metric.source) lines.push(`Source: ${metric.source}`);
    if (metric.note) lines.push(metric.note);
    if (metric.as_of) lines.push(`Measured ${fmtAge(metric.as_of)}`);
    if (metric.quality && metric.quality !== 'good') lines.push(`Quality: ${metric.quality}`);
  }
  if (!lines.length) return null;
  const list = h('ul', { class: 'provenance' }, lines.map((line) => h('li', { text: line })));
  if (metric.contributors && metric.contributors.length) {
    const details = h('details', { class: 'raw' },
      h('summary', { text: `Contributing points (${metric.contributors.length})` }),
      h('ul', { class: 'provenance' }, metric.contributors.map((c) => h('li', {
        text: `${c.point_id} = ${c.value === null || c.value === undefined ? 'no value' : c.value}`
          + `${c.unit ? ' ' + c.unit : ''} · ${c.quality || 'no quality'} · ${c.ts ? fmtAge(c.ts) : 'never reported'}`,
      }))));
    return h('div', null, list, details);
  }
  return list;
}

export function meterBar(pct, { hatched = false } = {}) {
  const width = pct === null || pct === undefined ? 0 : Math.max(0, Math.min(100, pct));
  return h('div', {
    class: `meter${hatched ? ' meter-hatched' : ''}`,
    role: 'img',
    'aria-label': pct === null || pct === undefined ? 'No value' : `${Math.round(width)} percent`,
  }, h('span', { style: `width:${hatched ? 100 : width}%` }));
}

/* =============================================================== toasts === */

export function toast(kind, title, body, ttl = 7000) {
  const host = document.getElementById('toasts');
  const node = h('div', { class: 'toast', dataset: { kind }, role: 'status' },
    h('strong', { text: title }),
    body ? h('span', { text: body }) : null);
  host.appendChild(node);
  window.setTimeout(() => node.remove(), ttl);
  return node;
}

/* ============================================== mandatory-reason dialog === */

/**
 * Every state-changing action in this console collects a reason first. The API
 * demands one for the audit log (SDD 15.2) and the operator should be told that
 * before the action, not by a 422 after it.
 */
export function askReason({ title, note, confirmLabel = 'Confirm', danger = false }) {
  const dialog = document.getElementById('reason-dialog');
  const form = document.getElementById('reason-form');
  const textarea = document.getElementById('reason-text');
  const confirm = document.getElementById('reason-confirm');

  document.getElementById('reason-title').textContent = title;
  document.getElementById('reason-note').textContent = note || '';
  confirm.textContent = confirmLabel;
  confirm.className = danger ? 'btn btn-danger' : 'btn btn-primary';
  textarea.value = '';

  return new Promise((resolve) => {
    const onClose = () => {
      form.removeEventListener('submit', onSubmit);
      dialog.removeEventListener('close', onClose);
      resolve(dialog.returnValue === 'confirm' && textarea.value.trim() ? textarea.value.trim() : null);
    };
    const onSubmit = (event) => {
      if (event.submitter && event.submitter.value === 'cancel') return;
      if (!textarea.value.trim()) {
        event.preventDefault();
        textarea.setCustomValidity('A reason is required — it is written to the audit log.');
        textarea.reportValidity();
      }
    };
    textarea.addEventListener('input', () => textarea.setCustomValidity(''));
    form.addEventListener('submit', onSubmit);
    dialog.addEventListener('close', onClose);
    dialog.showModal();
    textarea.focus();
  });
}

/** Guard a write action behind a named operator with a sufficient role. */
export function requireRole(minimum) {
  const who = identity.get();
  if (!who.signedIn) {
    toast('warn', 'Operator identity required',
      'Writes are audited against a named actor. Use the identity button in the top bar.');
    openOperatorDialog();
    return false;
  }
  if (!roleAtLeast(who.role, minimum)) {
    toast('warn', `Role '${minimum}' required`,
      `You are signed in as '${who.name}' with role '${who.role}'.`);
    return false;
  }
  return true;
}

/* ================================================== overview store/poll === */

const overviewStore = {
  data: null,
  result: null,
  at: null,
  subscribers: new Set(),
};

function publishOverview() {
  overviewStore.subscribers.forEach((fn) => {
    try { fn(overviewStore.data, overviewStore.result); } catch (err) { console.error(err); }
  });
}

export function subscribeOverview(fn) {
  overviewStore.subscribers.add(fn);
  if (overviewStore.result) fn(overviewStore.data, overviewStore.result);
  return () => overviewStore.subscribers.delete(fn);
}

export function latestOverview() {
  return { data: overviewStore.data, result: overviewStore.result, at: overviewStore.at };
}

export async function refreshOverview() {
  const result = await api.overview(10);
  overviewStore.result = result;
  if (result.ok) {
    overviewStore.data = result.data;
    overviewStore.at = new Date();
  }
  paintChrome();
  publishOverview();
  return result;
}

/* ================================================================ chrome === */

const SITE_STATE_META = {
  nominal:        { status: 'ok',           label: 'Nominal' },
  degraded:       { status: 'degraded',     label: 'Degraded' },
  alarm:          { status: 'alarm',        label: 'Critical alarm' },
  emergency:      { status: 'alarm',        label: 'EMERGENCY' },
  pre_deployment: { status: 'design_only',  label: 'Pre-deployment' },
  unknown:        { status: 'unknown',      label: 'Unknown' },
};

function paintChrome() {
  const data = overviewStore.data;
  const stateHost = document.getElementById('topbar-state');
  const siteLine = document.getElementById('site-line');
  const badge = document.getElementById('nav-alarm-badge');

  if (data) {
    const site = data.site || {};
    const opState = (site.operating_state || {}).state || 'unknown';
    const meta = SITE_STATE_META[opState] || SITE_STATE_META.unknown;
    const mode = site.operating_mode || {};
    clear(stateHost).appendChild(
      h('div', { class: 'pill-row' },
        statusChip(meta.status, meta.label, true),
        h('span', { class: 'chip chip-unknown chip-lg' },
          h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '⚙' }),
          `Mode: ${mode.mode || 'not set'}`)));

    const deployed = (site.operating_state || {}).assets_deployed;
    const total = (site.operating_state || {}).assets_total;
    siteLine.textContent = `${site.site_id || 'site'} · ${total || 0} assets registered`
      + (total ? ` · ${deployed || 0} installed` : '');

    const critical = (data.alarms || {}).critical_active || 0;
    const emergency = (data.alarms || {}).emergency_active || 0;
    const major = (data.alarms || {}).major_active || 0;
    const count = emergency + critical + major;
    badge.hidden = count === 0;
    badge.textContent = String(count);
    badge.title = `${emergency + critical} critical/emergency, ${major} major`;

    const annCount = document.getElementById('annunciator-count');
    if (annCount) {
      const unacked = (data.alarms || {}).unacknowledged_active;
      // Fall back to the lit-severity count when the overview does not report
      // an unacknowledged figure, rather than showing a confident zero.
      const lit = unacked === undefined || unacked === null ? count : unacked;
      annCount.hidden = lit === 0;
      annCount.textContent = String(lit);
    }
  } else if (overviewStore.result && !overviewStore.result.ok) {
    clear(stateHost).appendChild(statusChip('unknown', 'No overview', true));
    siteLine.textContent = overviewStore.result.offline ? 'API unreachable' : `API error ${overviewStore.result.status}`;
  }
  paintFreshness();
}

function paintFreshness() {
  const host = document.getElementById('freshness');
  const text = document.getElementById('freshness-text');
  const banner = document.getElementById('offline-banner');
  const detail = document.getElementById('offline-detail');

  const last = connection.lastSuccessAt;
  const interval = pollIntervalSeconds() || 30;
  const ageMs = last ? Date.now() - last.getTime() : Infinity;

  let state = 'unknown';
  if (connection.state === 'offline') state = 'offline';
  else if (!last) state = 'unknown';
  else if (ageMs > Math.max(interval * 3, 45) * 1000) state = 'stale';
  else state = 'live';

  host.dataset.state = state;
  if (state === 'offline') {
    text.textContent = last ? `Offline · last data ${fmtAge(last.toISOString())}` : 'Offline · never connected';
  } else if (state === 'unknown') {
    text.textContent = 'Never updated';
  } else if (state === 'stale') {
    text.textContent = `Stale · updated ${fmtAge(last.toISOString())}`;
  } else {
    text.textContent = `Updated ${fmtAge(last.toISOString())}`;
  }

  const showBanner = state === 'offline';
  banner.hidden = !showBanner;
  if (showBanner) {
    detail.textContent = connection.lastError
      ? `${connection.lastError}. Everything on screen is the last value received${last ? ` (${fmtAge(last.toISOString())})` : ''} — do not read it as live.`
      : 'Everything on screen is the last value received — do not read it as live.';
  }
}

/* ========================================================= operator IDs === */

/** Reference to the annunciator window, so repeat clicks focus it rather than
 *  opening a second copy -- two panels disagreeing about the horn would be
 *  worse than none. */
let annunciatorWindow = null;

function openAnnunciator() {
  if (annunciatorWindow && !annunciatorWindow.closed) {
    annunciatorWindow.focus();
    return;
  }
  const url = `${window.__UI_ROOT__}annunciator.html`;
  annunciatorWindow = window.open(
    url,
    'chaos-annunciator',
    'width=1440,height=920,menubar=no,toolbar=no,location=no,status=no'
  );
  if (!annunciatorWindow) {
    // Popup blocked: give the operator the link rather than failing silently.
    toast(
      'warning',
      'Popup blocked',
      `Allow popups for this site, or open the panel directly at ${url}`
    );
  }
}

function openOperatorDialog() {
  const dialog = document.getElementById('operator-dialog');
  const who = identity.get();
  document.getElementById('operator-name').value = who.name;
  document.getElementById('operator-role').value = who.role;
  dialog.showModal();
}

function paintOperator() {
  const who = identity.get();
  const button = document.getElementById('operator-button');
  const label = document.getElementById('operator-label');
  label.textContent = who.signedIn ? `${who.name} · ${who.role}` : 'Not signed in';
  button.dataset.signed = who.signedIn ? 'yes' : 'no';
  button.title = who.signedIn
    ? `Requests are sent as ${who.name} with role ${who.role}`
    : 'No operator identity set — the API treats you as a viewer and will refuse writes';
}

/* ================================================================ router === */

const ROUTES = [
  { pattern: /^\/?$|^\/home$/,        view: 'home',    nav: 'home' },
  { pattern: /^\/energy$/,            view: 'energy',  nav: 'energy' },
  { pattern: /^\/alarms$/,            view: 'alarms',  nav: 'alarms' },
  { pattern: /^\/map$/,               view: 'map',     nav: 'map' },
  { pattern: /^\/rack$/,              view: 'rack',    nav: 'rack' },
  { pattern: /^\/rack\/(.+)$/,        view: 'rack',    nav: 'rack', param: 'assetId' },
  { pattern: /^\/topology$/,          view: 'topology', nav: 'topology' },
  { pattern: /^\/topology\/(.+)$/,    view: 'topology', nav: 'topology', param: 'assetId' },
  { pattern: /^\/assets$/,            view: 'assets',  nav: 'assets' },
  { pattern: /^\/assets\/(.+)$/,      view: 'assets',  nav: 'assets', param: 'assetId' },
  { pattern: /^\/control$/,           view: 'control', nav: 'control' },
  { pattern: /^\/control\/(.+)$/,     view: 'control', nav: 'control', param: 'assetId' },
];

let activeView = null;
let activeRouteKey = null;

export function navigate(hash) {
  window.location.hash = hash.startsWith('#') ? hash : `#${hash}`;
}

function parseHash() {
  const raw = window.location.hash.replace(/^#/, '') || '/home';
  for (const route of ROUTES) {
    const match = raw.match(route.pattern);
    if (match) {
      const params = {};
      if (route.param && match[1]) params[route.param] = decodeURIComponent(match[1]);
      return { ...route, params, raw };
    }
  }
  return { view: 'home', nav: 'home', params: {}, raw };
}

async function renderRoute() {
  const route = parseHash();
  const main = document.getElementById('view');

  document.querySelectorAll('.sidenav a').forEach((link) => {
    if (link.dataset.nav === route.nav) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });

  const key = `${route.view}:${JSON.stringify(route.params)}`;
  if (key === activeRouteKey && activeView) return;
  activeRouteKey = key;

  if (activeView && typeof activeView.destroy === 'function') {
    try { activeView.destroy(); } catch (err) { console.error(err); }
  }
  activeView = null;

  clear(main).appendChild(h('p', { class: 'loading', text: 'Loading…' }));

  let module;
  try {
    module = await import(`${UI_ROOT}views/${route.view}.js`);
  } catch (err) {
    console.error(err);
    clear(main).appendChild(h('div', { class: 'error-box' },
      h('h3', { text: 'View failed to load' }),
      h('p', { text: String(err && err.message ? err.message : err) })));
    return;
  }

  const ctx = { params: route.params, navigate, subscribeOverview, latestOverview, refreshOverview };
  try {
    clear(main);
    activeView = (await module.default.mount(main, ctx)) || {};
  } catch (err) {
    console.error(err);
    clear(main).appendChild(h('div', { class: 'error-box' },
      h('h3', { text: 'View crashed while rendering' }),
      h('p', { text: String(err && err.message ? err.message : err) })));
    return;
  }
  document.title = `${module.default.title || 'Console'} — Homestead Digital Twin`;
  main.focus({ preventScroll: true });
}

/* =============================================================== polling === */

let pollTimer = null;

function pollIntervalSeconds() {
  const select = document.getElementById('refresh-interval');
  return select ? Number(select.value) : 15;
}

async function tick() {
  await refreshOverview();
  if (activeView && typeof activeView.refresh === 'function') {
    try { await activeView.refresh(); } catch (err) { console.error('view refresh failed', err); }
  }
}

function restartPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  const seconds = pollIntervalSeconds();
  window.localStorage.setItem('homestead.refresh', String(seconds));
  if (seconds > 0) pollTimer = window.setInterval(tick, seconds * 1000);
}

/* ================================================================= boot === */

function boot() {
  // Restore persisted preferences.
  const savedRefresh = window.localStorage.getItem('homestead.refresh');
  const select = document.getElementById('refresh-interval');
  if (savedRefresh !== null && select) {
    const option = Array.from(select.options).find((o) => o.value === savedRefresh);
    if (option) select.value = savedRefresh;
  }
  if (window.localStorage.getItem('homestead.wall') === '1') setWallMode(true);

  select.addEventListener('change', restartPolling);
  document.getElementById('refresh-now').addEventListener('click', () => { tick(); });
  document.getElementById('toggle-wall').addEventListener('click', () => {
    setWallMode(document.documentElement.dataset.density !== 'wall');
  });
  document.getElementById('operator-button').addEventListener('click', openOperatorDialog);
  document.getElementById('open-annunciator').addEventListener('click', openAnnunciator);

  document.getElementById('operator-form').addEventListener('submit', (event) => {
    const action = event.submitter && event.submitter.value;
    if (action === 'signout') {
      identity.clear();
      toast('info', 'Signed out', 'The API now treats this console as an anonymous viewer.');
      return;
    }
    const name = document.getElementById('operator-name').value.trim();
    const role = document.getElementById('operator-role').value;
    if (!name) { event.preventDefault(); return; }
    identity.set(name, role);
    toast('success', 'Identity set', `Requests are now sent as ${name} (${role}).`);
  });

  on('identity', paintOperator);
  on('connection', paintFreshness);
  paintOperator();

  window.addEventListener('hashchange', renderRoute);
  if (!window.location.hash) window.location.hash = '#/home';

  renderRoute();
  tick();
  restartPolling();
  window.setInterval(paintFreshness, 1000);

  // A tab returning to the foreground should not show minutes-old numbers.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') tick();
  });
}

function setWallMode(enabled) {
  document.documentElement.dataset.density = enabled ? 'wall' : 'normal';
  document.getElementById('toggle-wall').setAttribute('aria-pressed', enabled ? 'true' : 'false');
  window.localStorage.setItem('homestead.wall', enabled ? '1' : '0');
}

boot();
