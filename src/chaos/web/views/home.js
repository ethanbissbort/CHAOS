/* ---------------------------------------------------------------------------
   SDD 17.1 home screen.

   One request (`GET /overview`) paints this whole page, because the wall
   display has to come up on a local network with no internet and no patience.

   The screen's job is to be honest at a glance. Three different kinds of
   "nothing" are drawn differently and never as a zero:

     Not yet deployed — no such asset exists on the property yet
     No data         — the asset is registered but has never reported
     Live            — a real measurement, with its age and quality attached
--------------------------------------------------------------------------- */

import {
  card, clear, fmtAge, fmtDateTime, fmtDuration, fmtNumber, h,
  meterBar, metricReadout, provenance, resultProblem, severityChip, statusChip,
} from '../app.js';

const SITE_STATE_COPY = {
  nominal:        { status: 'ok',          headline: 'Nominal' },
  degraded:       { status: 'degraded',    headline: 'Degraded' },
  alarm:          { status: 'alarm',       headline: 'Critical alarm active' },
  emergency:      { status: 'alarm',       headline: 'EMERGENCY' },
  pre_deployment: { status: 'design_only', headline: 'Pre-deployment' },
  unknown:        { status: 'unknown',     headline: 'Unknown' },
};

/** A metric tile: big number when live, the reason when not. */
function metricTile(title, metric, { digits = 1, extra = null, unit } = {}) {
  const status = metric ? metric.status : 'unknown';
  return card({
    title,
    status,
    children: [
      metricReadout(metric, { digits, unit }),
      extra,
      provenance(metric),
    ],
  });
}

function siteHero(data) {
  const site = data.site || {};
  const state = site.operating_state || {};
  const mode = site.operating_mode || {};
  const copy = SITE_STATE_COPY[state.state] || SITE_STATE_COPY.unknown;

  return h('section', { class: 'card', dataset: { status: copy.status } },
    h('div', { class: 'card-head' },
      h('h3', { text: 'Property operating state' }),
      statusChip(copy.status, state.label || copy.headline, true)),
    h('div', { class: 'stat-row', style: 'margin:.3rem 0 .6rem' },
      h('span', null, h('b', { text: String(state.assets_total ?? 0) }), ' assets registered'),
      h('span', null, h('b', { text: String(state.assets_deployed ?? 0) }), ' installed'),
      h('span', null, 'Lifecycle: ', h('b', { text: state.lifecycle_phase || 'unknown' }))),
    kvMode(mode),
    (state.basis || []).length
      ? h('ul', { class: 'provenance' }, (state.basis || []).map((line) => h('li', { text: line })))
      : null);
}

function kvMode(mode) {
  if (!mode || !mode.available) {
    return h('p', { class: 'card-note' },
      statusChip('no_data', 'Operating mode not set'), ' ',
      mode && mode.note ? mode.note : 'No site operating mode has been recorded.');
  }
  return h('p', { class: 'card-note' },
    h('span', { class: 'chip chip-ok' },
      h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '⚙' }), `Mode: ${mode.mode}`),
    ` set by ${mode.changed_by || 'unknown'} ${mode.changed_at ? fmtAge(mode.changed_at) : ''}`
    + (mode.reason ? ` — ${mode.reason}` : ''));
}

function alarmTile(data, ctx) {
  const alarms = data.alarms || {};
  const urgent = (alarms.top || []).filter((a) => ['emergency', 'critical', 'major'].includes(a.severity));
  const total = (alarms.emergency_active || 0) + (alarms.critical_active || 0) + (alarms.major_active || 0);
  const status = total ? 'alarm' : (alarms.active_total ? 'degraded' : 'ok');

  const body = [];
  body.push(h('div', { class: 'stat-row', style: 'margin-bottom:.5rem' },
    h('span', null, h('b', { text: String(alarms.critical_active + alarms.emergency_active || 0) }), ' critical'),
    h('span', null, h('b', { text: String(alarms.major_active || 0) }), ' major'),
    h('span', null, h('b', { text: String(alarms.unacknowledged || 0) }), ' unacknowledged'),
    h('span', null, h('b', { text: String(alarms.open_incidents || 0) }), ' open incidents')));

  if (!urgent.length) {
    body.push(h('p', { class: 'card-note', text: alarms.active_total
      ? 'No critical or major alarms. Lower-severity alarms are on the alarms screen.'
      : 'No active alarms.' }));
  } else {
    body.push(h('ul', null, urgent.slice(0, 5).map((alarm) => h('li', {
      class: 'alarm-row', style: 'grid-template-columns:auto 1fr; padding:.35rem 0; border-bottom:none',
    },
      severityChip(alarm.severity, null, alarm.severity === 'critical' || alarm.severity === 'emergency'),
      h('div', { class: 'alarm-main' },
        h('div', { class: 'alarm-msg', text: alarm.message || alarm.alarm_key }),
        h('div', { class: 'alarm-key', text: `${alarm.asset_name || alarm.asset_id || 'site'} · ${alarm.state} · ${fmtAge(alarm.detected_at)}` }))))));
  }
  body.push(h('button', {
    class: 'linkish', style: 'margin-top:.5rem',
    onclick: () => ctx.navigate('/alarms'),
    text: 'Open the alarm screen →',
  }));
  return card({ title: 'Active alarms', status, children: body });
}

function energyTiles(data) {
  const energy = data.energy || {};
  const tiles = [];

  const reserve = energy.reserve_pct || {};
  const autonomy = energy.autonomy_current_h || {};
  const autonomyCritical = energy.autonomy_critical_h || {};
  tiles.push(card({
    title: 'Battery reserve',
    status: reserve.status || 'unknown',
    children: [
      metricReadout(reserve, { digits: 1, unit: '%' }),
      meterBar(reserve.available ? reserve.value : null, { hatched: !reserve.available }),
      h('dl', { class: 'kv', style: 'margin-top:.6rem' },
        h('dt', { text: 'Autonomy now' }),
        h('dd', null, autonomy.available ? fmtDuration(autonomy.value) : statusChip(autonomy.status || 'no_data')),
        h('dt', { text: 'Critical loads only' }),
        h('dd', null, autonomyCritical.available ? fmtDuration(autonomyCritical.value) : statusChip(autonomyCritical.status || 'no_data')),
        h('dt', { text: 'Energy state' }),
        h('dd', null, energy.state
          ? h('span', { class: 'tag', text: energy.state })
          : statusChip('no_data', 'EMS not reporting'))),
      provenance(reserve),
    ],
  }));

  tiles.push(metricTile('PV production', energy.pv_production_kw, { unit: 'kW' }));
  tiles.push(metricTile('Total site load', energy.site_load_kw, { unit: 'kW' }));

  const net = energy.net_power_kw || {};
  tiles.push(card({
    title: 'Net power (calculated)',
    status: net.status || 'no_data',
    children: [
      net.available
        ? h('div', { class: 'readout' },
            h('span', { class: 'value num', text: `${net.value > 0 ? '+' : ''}${fmtNumber(net.value, 1)}` }),
            h('span', { class: 'unit', text: 'kW' }))
        : metricReadout(net),
      h('p', { class: 'card-note', text: net.note || 'Requires both PV production and total load.' }),
    ],
  }));

  return tiles;
}

function waterTile(data) {
  const water = data.water || {};
  return card({
    title: 'Water reserves',
    status: water.status || 'unknown',
    children: [
      metricReadout(water.reserve_pct, { unit: '%' }),
      water.reserve_pct && water.reserve_pct.available
        ? meterBar(water.reserve_pct.value)
        : meterBar(null, { hatched: true }),
      h('p', { class: 'card-note', text: water.note
        || (water.status === 'not_deployed'
          ? 'No water assets are registered yet. This is not a reading of zero.'
          : 'Water instrumentation has not reported.') }),
    ],
  });
}

function weatherTile(data) {
  const weather = data.weather || {};
  const temp = weather.outside_temperature_c || {};
  const risk = weather.risk || {};
  const riskLabel = risk.available
    ? { freeze: 'Freeze risk', heat: 'Heat risk', none: 'No freeze or heat risk' }[risk.risk] || 'Unknown'
    : null;
  return card({
    title: 'Weather and freeze risk',
    status: temp.status || 'unknown',
    children: [
      metricReadout(temp, { unit: '°C' }),
      riskLabel
        ? h('p', { style: 'margin-top:.4rem' },
            statusChip(risk.risk === 'none' ? 'ok' : 'alarm', riskLabel))
        : h('p', { class: 'card-note', text: risk.note || 'No weather station is registered yet.' }),
    ],
  });
}

function agricultureTile(data) {
  const ag = data.agriculture || {};
  const rows = [['Greenhouse', ag.greenhouse || {}], ['Orchard / food forest', ag.orchard || {}]];
  return card({
    title: 'Greenhouse and orchard',
    status: ag.status || 'unknown',
    children: [
      h('dl', { class: 'kv' }, rows.flatMap(([label, block]) => [
        h('dt', { text: label }),
        h('dd', null, statusChip(block.status || 'not_deployed'),
          block.reporting ? ` · ${block.reporting} points reporting` : ''),
      ])),
      h('p', { class: 'card-note', text: ag.note || '' }),
    ],
  });
}

function commsTile(data) {
  const comms = data.communications || {};
  const platform = comms.platform || {};
  const values = Object.entries(comms.by_value || {});
  return card({
    title: 'Communications and server',
    status: comms.status || 'unknown',
    children: [
      values.length
        ? h('div', { class: 'pill-row', style: 'margin-bottom:.5rem' },
            values.map(([value, count]) => h('span', {
              class: `chip chip-${value === 'online' ? 'ok' : value === 'offline' ? 'alarm' : 'stale'}`,
            },
              h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: value === 'online' ? '●' : '○' }),
              `${value}: ${count}`)))
        : h('p', { class: 'card-note', text: comms.note || 'No communications assets are reporting.' }),
      h('dl', { class: 'kv' },
        h('dt', { text: 'This node' }),
        h('dd', { text: `${platform.node_role || '?'} · v${platform.version || '?'}` }),
        h('dt', { text: 'Broker' }),
        h('dd', null, platform.mqtt_enabled
          ? `${platform.mqtt_host || 'configured'}`
          : statusChip('not_deployed', 'MQTT disabled')),
        h('dt', { text: 'Historian' }),
        h('dd', { text: platform.historian_backend || '—' }),
        h('dt', { text: 'Physical control' }),
        h('dd', null, platform.physical_control_enabled
          ? statusChip('ok', 'Enabled')
          : statusChip('design_only', 'Disabled until commissioning'))),
    ],
  });
}

function securityTile(data) {
  const security = data.security || {};
  const safety = data.safety || {};
  return card({
    title: 'Security and safety',
    status: security.status === 'ok' && safety.status === 'ok' ? 'ok' : (security.status || 'unknown'),
    children: [
      h('dl', { class: 'kv' },
        h('dt', { text: 'Security' }),
        h('dd', null, statusChip(security.status || 'not_deployed'),
          security.asset_count ? ` · ${security.asset_count} assets` : ''),
        h('dt', { text: 'Safety' }),
        h('dd', null, statusChip(safety.status || 'not_deployed'),
          safety.asset_count ? ` · ${safety.asset_count} assets` : '')),
      Object.keys(security.by_value || {}).length
        ? h('div', { class: 'pill-row', style: 'margin-top:.5rem' },
            Object.entries(security.by_value).map(([value, count]) =>
              h('span', { class: 'tag', text: `${value}: ${count}` })))
        : h('p', { class: 'card-note', text: security.note || '' }),
    ],
  });
}

function subsystemGrid(data, ctx) {
  const rows = data.subsystems || [];
  const grid = h('div', { class: 'grid grid-auto' });
  rows.forEach((entry) => {
    const assets = entry.assets || {};
    const alarms = entry.alarms || {};
    const points = entry.points || {};
    grid.appendChild(card({
      title: entry.label,
      status: entry.health,
      children: [
        h('div', { class: 'stat-row', style: 'font-size:.8rem' },
          h('span', null, h('b', { text: String(assets.total || 0) }), ' assets'),
          assets.deployed ? h('span', null, h('b', { text: String(assets.deployed) }), ' installed') : null,
          alarms.active_total ? h('span', null, h('b', { text: String(alarms.active_total) }), ' alarms') : null,
          points.with_data ? h('span', null, h('b', { text: String(points.with_data) }), ' live points') : null),
        entry.open_fields && entry.open_fields.unresolved_count
          ? h('p', { class: 'card-note', text: `${entry.open_fields.unresolved_count} unresolved design fields` })
          : null,
      ],
    }));
  });
  return h('section', null,
    h('div', { class: 'page-head', style: 'margin:1.4rem 0 .6rem' },
      h('div', null,
        h('h2', { style: 'font-size:1.05rem', text: 'Subsystem health' }),
        h('p', { class: 'lede', text: 'Every SDD 25.4 domain, including the ones the property does not have yet.' })),
      h('button', { class: 'linkish', onclick: () => ctx.navigate('/assets'), text: 'Browse the registry →' })),
    grid);
}

function render(root, data, ctx) {
  clear(root);
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Property overview' }),
      h('p', { class: 'lede', text: 'FR-001: subsystem health, active alarms, energy reserve, water reserve, communications and operating mode — from one request, so this screen works with no internet.' })),
    h('p', { class: 'card-note', text: `Generated ${fmtDateTime(data.generated_at)}` })));

  const sources = data.data_sources || {};
  if (sources.degraded) {
    root.appendChild(h('div', { class: 'error-box', style: 'margin-bottom:var(--gap)' },
      h('h3', { text: 'Incomplete data — some tables could not be read' }),
      h('p', { text: sources.note || 'Parts of the database are unavailable, so this screen is incomplete. Treat missing subsystems as unknown, not as healthy.' }),
      h('ul', { class: 'provenance' }, (sources.unavailable || []).map((problem) =>
        h('li', { text: `${problem.source}: ${problem.error} — ${problem.detail}` })))));
  }

  const grid = h('div', { class: 'grid grid-wide' });
  grid.appendChild(siteHero(data, ctx));
  grid.appendChild(alarmTile(data, ctx));
  energyTiles(data).forEach((tile) => grid.appendChild(tile));
  grid.appendChild(waterTile(data));
  grid.appendChild(weatherTile(data));
  grid.appendChild(agricultureTile(data));
  grid.appendChild(commsTile(data));
  grid.appendChild(securityTile(data));
  root.appendChild(grid);

  root.appendChild(subsystemGrid(data, ctx));

  root.appendChild(h('details', { class: 'raw' },
    h('summary', { text: 'What the status words mean' }),
    h('dl', { class: 'kv', style: 'margin-top:.5rem' },
      Object.entries(data.status_vocabulary || {}).flatMap(([key, text]) => [
        h('dt', null, statusChip(key)),
        h('dd', { text }),
      ]))));
}

export default {
  title: 'Home',
  async mount(root, ctx) {
    const unsubscribe = ctx.subscribeOverview((data, result) => {
      if (data) render(root, data, ctx);
      else if (result && !result.ok) {
        clear(root).appendChild(resultProblem(result, 'the property overview'));
      }
    });

    const current = ctx.latestOverview();
    if (!current.result) {
      root.appendChild(h('p', { class: 'loading', text: 'Loading the property overview…' }));
      await ctx.refreshOverview();
    } else if (!current.data && current.result && !current.result.ok) {
      clear(root).appendChild(resultProblem(current.result, 'the property overview'));
    }

    return {
      // The shell already refreshes /overview; the subscription repaints us.
      async refresh() {},
      destroy() { unsubscribe(); },
    };
  },
};
