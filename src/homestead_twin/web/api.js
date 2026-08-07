/* ---------------------------------------------------------------------------
   API client for the operator console.

   Everything here is local-first: same-origin fetch only, no third-party code,
   no telemetry off the property (SDD 5.1, FR-006).

   Two ideas shape this module:

   1. `request()` never throws for an HTTP or network problem. It returns a
      result object, because "the API is down" is an operational state the
      operator must see rendered on the page, not an exception that blanks it.
   2. Identity is explicit. The platform terminates authentication at the VPN or
      reverse proxy (SDD 15.3) but still records a named actor on every audited
      action (SDD 5.7), so the console attaches X-Operator / X-Operator-Role and
      surfaces a 403 as "you are a viewer", not as a generic failure.
--------------------------------------------------------------------------- */

const API_ROOT = window.__API_ROOT__ || '/api/v1';
const IDENTITY_KEY = 'homestead.operator';

/* ----------------------------------------------------------- identity ---- */

export const ROLES = ['viewer', 'operator', 'maintainer', 'administrator'];

/** Roles that the API will let write, in rank order (mirrors api/deps.py). */
export function roleAtLeast(role, minimum) {
  return ROLES.indexOf(role || 'viewer') >= ROLES.indexOf(minimum);
}

export const identity = {
  get() {
    try {
      const raw = window.localStorage.getItem(IDENTITY_KEY);
      if (!raw) return { name: '', role: 'viewer', signedIn: false };
      const parsed = JSON.parse(raw);
      return {
        name: parsed.name || '',
        role: ROLES.includes(parsed.role) ? parsed.role : 'viewer',
        signedIn: Boolean(parsed.name),
      };
    } catch {
      return { name: '', role: 'viewer', signedIn: false };
    }
  },
  set(name, role) {
    const value = { name: String(name || '').trim(), role: ROLES.includes(role) ? role : 'viewer' };
    window.localStorage.setItem(IDENTITY_KEY, JSON.stringify(value));
    emit('identity', identity.get());
    return identity.get();
  },
  clear() {
    window.localStorage.removeItem(IDENTITY_KEY);
    emit('identity', identity.get());
  },
  headers() {
    const who = identity.get();
    if (!who.signedIn) return {};
    return { 'X-Operator': who.name, 'X-Operator-Role': who.role };
  },
};

/* -------------------------------------------------------------- events ---- */

const listeners = new Map();

export function on(event, handler) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(handler);
  return () => listeners.get(event).delete(handler);
}

function emit(event, payload) {
  (listeners.get(event) || []).forEach((handler) => {
    try { handler(payload); } catch (err) { console.error('listener failed', err); }
  });
}

/* ---------------------------------------------------------- connection ---- */

export const connection = {
  state: 'unknown',      // unknown | online | offline
  lastSuccessAt: null,   // Date of the last 2xx from the API
  lastErrorAt: null,
  lastError: null,
};

function noteSuccess() {
  const wasOffline = connection.state === 'offline';
  connection.state = 'online';
  connection.lastSuccessAt = new Date();
  connection.lastError = null;
  if (wasOffline) emit('reconnected', connection);
  emit('connection', connection);
}

function noteFailure(message) {
  connection.state = 'offline';
  connection.lastErrorAt = new Date();
  connection.lastError = message;
  emit('connection', connection);
}

/* ------------------------------------------------------------- requests --- */

/**
 * @returns {Promise<{ok: boolean, status: number, data: any, error: string|null,
 *                    offline: boolean, missing: boolean, forbidden: boolean}>}
 */
export async function request(path, options = {}) {
  const { method = 'GET', body, signal, timeoutMs = 20000 } = options;
  const url = path.startsWith('/api/') || path.startsWith('http')
    ? path
    : API_ROOT + (path.startsWith('/') ? path : '/' + path);

  const headers = { Accept: 'application/json', ...identity.headers(), ...(options.headers || {}) };
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  if (signal) signal.addEventListener('abort', () => controller.abort(), { once: true });

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: 'no-store',
      credentials: 'same-origin',
    });
  } catch (err) {
    window.clearTimeout(timer);
    const message = err && err.name === 'AbortError'
      ? `Request timed out after ${Math.round(timeoutMs / 1000)} s`
      : 'Cannot reach the digital-twin API';
    noteFailure(message);
    return { ok: false, status: 0, data: null, error: message, offline: true, missing: false, forbidden: false };
  }
  window.clearTimeout(timer);

  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }

  // The server answered, so the network path is healthy even on a 4xx/5xx.
  noteSuccess();

  if (response.ok) {
    return { ok: true, status: response.status, data: payload, error: null, offline: false, missing: false, forbidden: false };
  }

  const detail = payload && typeof payload === 'object' && payload.detail
    ? (typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail))
    : (typeof payload === 'string' && payload ? payload : response.statusText);

  return {
    ok: false,
    status: response.status,
    data: payload,
    error: detail || `HTTP ${response.status}`,
    offline: false,
    missing: response.status === 404 || response.status === 405,
    forbidden: response.status === 401 || response.status === 403,
  };
}

export const get = (path, options) => request(path, { ...options, method: 'GET' });
export const post = (path, body, options) => request(path, { ...options, method: 'POST', body });

/**
 * Try several endpoints in order and return the first that is not
 * "route does not exist". The alarm/command routers are being written in
 * parallel with this console, so the exact verb path is not settled; falling
 * through candidates is honest about that instead of hard-failing.
 */
export async function postFirst(paths, body) {
  let last = null;
  for (const path of paths) {
    const result = await post(path, body);
    if (!result.missing) return result;
    last = result;
  }
  return {
    ...(last || { ok: false, status: 404, data: null, offline: false, forbidden: false }),
    missing: true,
    error: `No endpoint available. Tried: ${paths.join(', ')}`,
  };
}

export async function getFirst(paths) {
  let last = null;
  for (const path of paths) {
    const result = await get(path);
    if (!result.missing) return result;
    last = result;
  }
  return { ...(last || { ok: false, status: 404, data: null }), missing: true };
}

/* ------------------------------------------------------- endpoint calls --- */

export const api = {
  overview: (alarmLimit = 8) => get(`/overview?alarm_limit=${encodeURIComponent(alarmLimit)}`),
  subsystems: () => get('/overview/subsystems'),
  map: () => get('/overview/map'),
  control: (assetId) => get(`/overview/control/${encodeURI(assetId)}`),
  health: () => get('/health'),

  // Built in parallel by other agents; the console degrades when absent.
  assets: (params = {}) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') query.set(key, value);
    });
    const suffix = query.toString();
    return get('/assets' + (suffix ? `?${suffix}` : ''));
  },
  asset: (assetId) => get(`/assets/${encodeURI(assetId)}`),
  assetPoints: (assetId) => get(`/assets/${encodeURI(assetId)}/points`),
  assetRelationships: (assetId) => get(`/assets/${encodeURI(assetId)}/relationships`),
  assetDependencies: (assetId) => get(`/assets/${encodeURI(assetId)}/dependencies`),
  registrySummary: () => get('/registry/summary'),
  activeAlarms: () => getFirst(['/alarms/active', '/alarms?state=active']),
  incidents: (state = 'open') => get(`/incidents?state=${encodeURIComponent(state)}`),
  energyState: () => get('/energy/state'),
  loadBudgets: () => getFirst(['/energy/load-budgets', '/energy/loads']),

  // The alarms router takes {note, force}; `note` is the mandatory audit reason.
  alarmAction: (alarmId, action, reason) =>
    postFirst(
      [`/alarms/${encodeURIComponent(alarmId)}/${action}`, `/alarms/${encodeURIComponent(alarmId)}/transition`],
      { note: reason },
    ),

  /** POST /commands rejects unknown fields, so this mirrors CommandCreate exactly. */
  issueCommand: ({ assetId, pointName, command, value, reason, dryRun = false }) => {
    const body = { asset_id: assetId, command, reason, dry_run: Boolean(dryRun), requires_ack: true };
    if (pointName) body.point_name = pointName;
    if (value !== undefined) body.value = value;
    return post('/commands', body);
  },
};

/* ------------------------------------------------------------ formatting -- */

export function fmtNumber(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  if (typeof value !== 'number') return String(value);
  const abs = Math.abs(value);
  const places = abs >= 100 ? 0 : abs >= 10 ? digits : Math.max(digits, 2);
  return value.toLocaleString(undefined, { minimumFractionDigits: places, maximumFractionDigits: places });
}

export function fmtTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function fmtDateTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

export function fmtAge(iso, now = Date.now()) {
  if (!iso) return 'never';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return String(iso);
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export function fmtDuration(hours) {
  if (hours === null || hours === undefined || Number.isNaN(hours)) return '—';
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${fmtNumber(hours, 1)} h`;
  return `${fmtNumber(hours / 24, 1)} d`;
}

export { API_ROOT };
