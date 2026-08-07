/* Annunciator panel logic.
 *
 * Implements the conventional annunciator sequence (ISA-18.1 F3A, "ringback"):
 *
 *   normal      -> dark, silent
 *   abnormal    -> fast flash + horn          (unacknowledged)
 *   ACKNOWLEDGE -> steady lamp, horn silent   (still abnormal)
 *   returns     -> slow flash + ringback tone (waiting to be reset)
 *   RESET       -> dark
 *
 * Two rules are load-bearing and deliberately not configurable:
 *
 * 1. SILENCE HORN silences the horn and nothing else. It never changes a lamp
 *    and never acknowledges an alarm. Conflating the two is how a panel ends up
 *    quiet and dark with the condition still present.
 *
 * 2. ACKNOWLEDGE writes through to the alarm API with a named operator and an
 *    audit note. The panel does not keep a private idea of "acknowledged" that
 *    the rest of the platform cannot see -- with one exception: if the write
 *    fails, the lamp stays unacknowledged and the failure is shown, rather than
 *    the panel pretending the acknowledgement landed.
 */

import { identity, get, post, roleAtLeast, fmtDateTime, fmtAge } from './api.js';

const POLL_MS = 3000;
const STALE_MS = 12000;
const LAMP_TEST_MS = 4000;

const state = {
  data: null,
  hornSilenced: false,
  /** alarm_key -> true while a tile is mid-write, so it cannot be double-sent. */
  pending: new Set(),
  lastOk: 0,
  selected: null,
  audioAllowed: true,
  includePending: false,
  timer: null,
};

/* ------------------------------------------------------------------ audio -- */

/* The horn is synthesised rather than loaded from a file: the panel must work
   with no network, and shipping an audio asset for one tone is not worth the
   cache and CSP surface. */
const audio = {
  ctx: null,
  nodes: null,
  kind: null,

  ensure() {
    if (!this.ctx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return null;
      this.ctx = new Ctx();
    }
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
    return this.ctx;
  },

  play(kind) {
    if (this.kind === kind) return;
    this.stop();
    const ctx = this.ensure();
    if (!ctx) return;

    const gain = ctx.createGain();
    gain.gain.value = 0.0001;
    gain.connect(ctx.destination);

    const osc = ctx.createOscillator();
    const pulse = ctx.createOscillator();
    const pulseGain = ctx.createGain();

    if (kind === 'horn') {
      // Insistent, low, square: a sound you cannot ignore or mistake for a chime.
      osc.type = 'square';
      osc.frequency.value = 392;
      pulse.frequency.value = 3.1;
      gain.gain.value = 0.05;
    } else {
      // Ringback: higher, softer, slower. "Something returned to normal" is
      // information, not an emergency, and must not sound like one.
      osc.type = 'triangle';
      osc.frequency.value = 784;
      pulse.frequency.value = 0.9;
      gain.gain.value = 0.028;
    }

    pulseGain.gain.value = gain.gain.value;
    pulse.connect(pulseGain.gain);
    osc.connect(pulseGain);
    pulseGain.connect(gain);

    osc.start();
    pulse.start();
    this.nodes = { osc, pulse, gain, pulseGain };
    this.kind = kind;
  },

  stop() {
    if (!this.nodes) { this.kind = null; return; }
    const { osc, pulse } = this.nodes;
    try { osc.stop(); pulse.stop(); } catch { /* already stopped */ }
    this.nodes = null;
    this.kind = null;
  },
};

/* ------------------------------------------------------------------- dom -- */

const $ = (id) => document.getElementById(id);

function toast(message, kind = 'info') {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const node = document.createElement('div');
  node.className = 'toast';
  node.dataset.kind = kind;
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), kind === 'error' ? 6000 : 3000);
}

function tileNode(tile) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'tile';
  button.dataset.state = tile.state;
  button.dataset.sev = tile.severity;
  button.dataset.key = tile.alarm_key;

  const legend = document.createElement('span');
  legend.className = 'tile-legend';
  legend.textContent = tile.legend.join('\n');
  legend.style.whiteSpace = 'pre-line';
  button.appendChild(legend);

  const status = document.createElement('span');
  status.className = 'tile-status';
  status.textContent = statusWord(tile);
  button.appendChild(status);

  // The accessible name carries what colour and flash convey visually, so a
  // screen reader hears the same three facts: what, how bad, what state.
  button.setAttribute(
    'aria-label',
    `${tile.name}. ${tile.severity}. ${statusWord(tile) || 'normal'}.`
  );
  button.title = tile.service_note || tile.message || tile.name;
  button.addEventListener('click', () => select(tile.alarm_key));
  return button;
}

function statusWord(tile) {
  switch (tile.state) {
    case 'alarm': return 'ALARM';
    case 'acknowledged': return "ACK'D";
    case 'ringback': return 'RESET REQ';
    case 'inhibited': return 'INHIBITED';
    case 'out_of_service': return 'OUT OF SVC';
    default: return '';
  }
}

function render() {
  const data = state.data;
  if (!data) return;

  const panel = $('panel');
  panel.textContent = '';

  for (const bay of data.bays) {
    const section = document.createElement('section');
    section.className = 'bay';

    const heading = document.createElement('h2');
    heading.className = 'bay-title';
    const lit = bay.tiles.filter((t) => t.state === 'alarm' || t.state === 'acknowledged').length;
    heading.textContent = lit ? `${bay.title} — ${lit} LIT` : bay.title;
    section.appendChild(heading);

    const grid = document.createElement('div');
    grid.className = 'bay-grid';
    for (const tile of bay.tiles) grid.appendChild(tileNode(tile));
    section.appendChild(grid);
    panel.appendChild(section);
  }

  const s = data.summary;
  $('c-alarm').textContent = s.alarm;
  $('c-ack').textContent = s.acknowledged;
  $('c-ring').textContent = s.ringback;
  $('c-inh').textContent = s.inhibited;
  $('c-oos').textContent = s.out_of_service;
  $('counts').querySelector('.count-oos').dataset.nonzero = String(s.out_of_service > 0);

  const who = identity.get();
  const canAck = roleAtLeast(who.role, 'operator');
  $('btn-ack').disabled = !canAck || s.alarm === 0;
  $('btn-reset').disabled = !canAck || s.ringback === 0;
  $('btn-silence').disabled = audio.kind === null;

  const idNode = $('identity');
  idNode.textContent = who.signedIn ? `${who.name} · ${who.role}` : 'not signed in';
  idNode.dataset.role = who.role;

  updateAudio();
  if (state.selected) renderDetail(state.selected);
}

function updateAudio() {
  const s = state.data?.summary;
  if (!s || !state.audioAllowed) { audio.stop(); return; }
  if (s.horn && !state.hornSilenced) audio.play('horn');
  else if (s.ringback_tone && !state.hornSilenced) audio.play('ringback');
  else audio.stop();
  $('btn-silence').disabled = audio.kind === null;
}

function findTile(key) {
  for (const bay of state.data?.bays || []) {
    const found = bay.tiles.find((t) => t.alarm_key === key);
    if (found) return found;
  }
  return null;
}

function select(key) {
  state.selected = key;
  renderDetail(key);
}

function renderDetail(key) {
  const tile = findTile(key);
  const box = $('detail');
  const body = $('detail-body');
  if (!tile) { box.hidden = true; state.selected = null; return; }

  body.textContent = '';
  box.hidden = false;

  const legend = document.createElement('p');
  legend.className = 'detail-legend';
  legend.textContent = tile.legend.join(' ');
  body.appendChild(legend);

  const name = document.createElement('p');
  name.className = 'detail-name';
  name.textContent = tile.name;
  body.appendChild(name);

  const rows = [
    ['Status', statusWord(tile) || 'NORMAL'],
    ['Severity', tile.severity.toUpperCase()],
    ['Domain', tile.domain],
    ['Key', tile.alarm_key],
  ];
  if (tile.since) rows.push(['Since', `${fmtDateTime(tile.since)} (${fmtAge(tile.since)})`]);
  if (tile.incident_id) rows.push(['Incident', `#${tile.incident_id}`]);
  if (tile.duplicate_open_count) rows.push(['Also open', `${tile.duplicate_open_count} more`]);
  if (tile.threshold_status) rows.push(['Threshold', tile.threshold_status.replace(/_/g, ' ')]);
  if (tile.requires_manual_reset) rows.push(['Reset', 'manual required']);

  const list = document.createElement('dl');
  for (const [label, value] of rows) {
    const row = document.createElement('div');
    row.className = 'detail-row';
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    row.append(dt, dd);
    list.appendChild(row);
  }
  body.appendChild(list);

  if (tile.message) {
    const message = document.createElement('p');
    message.className = 'detail-note';
    message.dataset.kind = 'message';
    message.textContent = tile.message;
    body.appendChild(message);
  }

  if (tile.state === 'out_of_service' && tile.service_note) {
    const note = document.createElement('p');
    note.className = 'detail-note';
    note.textContent = `OUT OF SERVICE — ${tile.service_note} This window cannot light, so its dark state is not evidence that the condition is normal.`;
    body.appendChild(note);
  }

  if (tile.state === 'inhibited' && tile.suppression_reason) {
    const note = document.createElement('p');
    note.className = 'detail-note';
    note.dataset.kind = 'inhibited';
    note.textContent = `INHIBITED — ${tile.suppression_reason}. The alarm is recorded but deliberately not annunciated.`;
    body.appendChild(note);
  }

  const actions = document.createElement('div');
  actions.className = 'detail-actions';
  const who = identity.get();
  if (tile.alarm_id && roleAtLeast(who.role, 'operator')) {
    if (tile.state === 'alarm') {
      actions.appendChild(actionButton('Acknowledge', () => acknowledge([tile])));
    }
    if (tile.state === 'ringback') {
      actions.appendChild(actionButton('Reset', () => reset([tile])));
    }
  }
  if (actions.children.length) body.appendChild(actions);
}

function actionButton(label, handler) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'ctl';
  button.style.minWidth = '120px';
  const key = document.createElement('span');
  key.className = 'ctl-key';
  key.textContent = label.toUpperCase();
  button.appendChild(key);
  button.addEventListener('click', handler);
  return button;
}

/* --------------------------------------------------------------- actions -- */

function litTiles(states) {
  const out = [];
  for (const bay of state.data?.bays || []) {
    for (const tile of bay.tiles) {
      if (states.includes(tile.state) && tile.alarm_id) out.push(tile);
    }
  }
  return out;
}

async function acknowledge(tiles) {
  const targets = tiles.filter((t) => !state.pending.has(t.alarm_key));
  if (!targets.length) return;

  // The horn goes quiet the moment ACKNOWLEDGE is pressed, as on a real panel:
  // the operator has taken the alarm. The lamps only change once the write
  // lands, so a failed acknowledgement leaves the tile flashing.
  state.hornSilenced = true;
  updateAudio();

  const note = `Acknowledged at annunciator panel (${targets.length} window${targets.length > 1 ? 's' : ''})`;
  let ok = 0;
  const failures = [];
  for (const tile of targets) {
    state.pending.add(tile.alarm_key);
    const result = await post(`/alarms/${tile.alarm_id}/acknowledge`, { note });
    if (result.ok) ok += 1;
    else failures.push(`${tile.alarm_key}: ${result.error}`);
    state.pending.delete(tile.alarm_key);
  }

  if (failures.length) {
    toast(`${ok} acknowledged, ${failures.length} failed — ${failures[0]}`, 'error');
  } else {
    toast(`${ok} window${ok > 1 ? 's' : ''} acknowledged`);
  }
  await poll();
}

async function reset(tiles) {
  const targets = tiles.filter((t) => !state.pending.has(t.alarm_key));
  if (!targets.length) return;
  const note = 'Reset at annunciator panel after return to normal';
  let ok = 0;
  const failures = [];
  for (const tile of targets) {
    state.pending.add(tile.alarm_key);
    const result = await post(`/alarms/${tile.alarm_id}/review`, { note });
    if (result.ok) ok += 1;
    else failures.push(`${tile.alarm_key}: ${result.error}`);
    state.pending.delete(tile.alarm_key);
  }
  if (failures.length) toast(`${ok} reset, ${failures.length} failed — ${failures[0]}`, 'error');
  else toast(`${ok} window${ok > 1 ? 's' : ''} reset`);
  await poll();
}

function lampTest() {
  document.body.dataset.lamptest = 'on';
  const wasSilenced = state.hornSilenced;
  state.hornSilenced = false;
  if (state.audioAllowed) audio.play('horn');
  setTimeout(() => {
    delete document.body.dataset.lamptest;
    state.hornSilenced = wasSilenced;
    updateAudio();
  }, LAMP_TEST_MS);
}

/* ------------------------------------------------------------------ poll -- */

async function poll() {
  const query = state.includePending ? '?include_pending=true' : '';
  // request() reports failure in the result rather than throwing, so a failed
  // poll is handled here and never mistaken for a rendering fault below.
  const result = await get(`/annunciator${query}`);
  if (!result.ok) {
    const kind = result.offline && Date.now() - state.lastOk > STALE_MS ? 'down' : 'stale';
    setLink(kind, { message: result.error });
    return;
  }

  const data = result.data;
  const previous = state.data;
  state.data = data;
  state.lastOk = Date.now();

  // A newly lit window re-arms the horn even if it was silenced for an
  // earlier alarm. Silencing must never mask the *next* thing that happens.
  if (previous && newAlarmAppeared(previous, data)) state.hornSilenced = false;

  // A rendering fault is a different failure from a comms fault and must not
  // be reported as "no data" -- that would send an operator to check the
  // network while the panel quietly fails to draw the alarm they need.
  try {
    setLink('ok');
    render();
  } catch (error) {
    setLink('render', error);
    // eslint-disable-next-line no-console
    console.error('Annunciator render failed', error);
  }
}

function newAlarmAppeared(previous, next) {
  const before = new Set();
  for (const bay of previous.bays) {
    for (const tile of bay.tiles) if (tile.state === 'alarm') before.add(tile.alarm_key);
  }
  for (const bay of next.bays) {
    for (const tile of bay.tiles) {
      if (tile.state === 'alarm' && !before.has(tile.alarm_key)) return true;
    }
  }
  return false;
}

function setLink(kind, error) {
  const node = $('link-state');
  node.dataset.state = kind;
  if (kind === 'ok') {
    node.textContent = 'linked';
    return;
  }
  if (kind === 'render') {
    node.textContent = `PANEL FAULT · ${error?.message || 'render failed'}`;
    return;
  }
  const age = Math.round((Date.now() - state.lastOk) / 1000);
  // A panel that silently freezes is worse than one that says it is blind.
  node.textContent = state.lastOk
    ? `NO DATA · last ${age}s ago`
    : `NO DATA · ${error?.message || 'unreachable'}`;
}

function tick() {
  const now = new Date();
  $('clock').textContent = now.toTimeString().slice(0, 8);
  if (state.lastOk && Date.now() - state.lastOk > STALE_MS) setLink('down');
}

/* ------------------------------------------------------------------ boot -- */

export function start() {
  $('btn-ack').addEventListener('click', () => acknowledge(litTiles(['alarm'])));
  $('btn-reset').addEventListener('click', () => reset(litTiles(['ringback'])));
  $('btn-test').addEventListener('click', lampTest);
  $('btn-silence').addEventListener('click', () => {
    state.hornSilenced = true;
    updateAudio();
    toast('Horn silenced — lamps unchanged');
  });
  $('detail-close').addEventListener('click', () => {
    state.selected = null;
    $('detail').hidden = true;
  });
  $('opt-audio').addEventListener('change', (event) => {
    state.audioAllowed = event.target.checked;
    updateAudio();
  });
  $('opt-pending').addEventListener('change', (event) => {
    state.includePending = event.target.checked;
    poll();
  });

  document.addEventListener('keydown', (event) => {
    if (event.target.tagName === 'INPUT') return;
    const key = event.key.toLowerCase();
    if (key === 'a') { event.preventDefault(); $('btn-ack').click(); }
    if (key === 's') { event.preventDefault(); $('btn-silence').click(); }
    if (key === 'r') { event.preventDefault(); $('btn-reset').click(); }
    if (key === 't') { event.preventDefault(); lampTest(); }
    if (key === 'escape') { state.selected = null; $('detail').hidden = true; }
  });

  // Browsers block audio until the user interacts; the first click anywhere
  // unlocks the horn. Until then the panel is visually complete but silent,
  // and says so rather than appearing to have a working horn.
  document.addEventListener('click', () => audio.ensure(), { once: true });

  poll();
  state.timer = setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
  tick();
}

export { engraveTest, state as __state };

/* Exposed for the test suite: keeps the legend-wrapping contract checkable
   from the browser test without exporting the whole module surface. */
function engraveTest(tile) {
  return tileNode(tile);
}
