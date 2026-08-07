/* ---------------------------------------------------------------------------
   SDD section 38 energy dashboard.

   Two rules from the SDD drive the layout:

     * "The dashboard must show measured values separately from calculated and
       forecast values." Measured tiles carry their point provenance; derived
       tiles are grouped under a heading that says so.
     * "Data-quality indicators for every value used by the state machine."
       Every number is drawn with its availability status, so an EMS running on
       stale inputs is obvious rather than reassuring.

   Charts are hand-drawn inline SVG. No charting library, no CDN — the console
   has to render on an isolated network.
--------------------------------------------------------------------------- */

import {
  card, clear, fmtAge, fmtDateTime, fmtDuration, fmtNumber, h, meterBar,
  metricReadout, provenance, resultProblem, statusChip, svgEl,
} from '../app.js';
import { api } from '../api.js';

const ENERGY_STATE_TONE = {
  SURPLUS: 'ok', NORMAL: 'ok',
  CONSERVE: 'degraded', GENERATOR_SUPPORT: 'degraded', DEGRADED_SENSOR: 'stale',
  CRITICAL_RESERVE: 'alarm', EMERGENCY: 'alarm', BLACK_START: 'alarm',
  COMMISSIONING: 'design_only', MAINTENANCE: 'design_only',
};

/* ------------------------------------------------------------ one-line ---- */

/** SDD 38 bullet 1: a one-line diagram of PV, battery, generator, inverter and
 *  the panels, annotated with whatever is actually measured. */
function oneLine(energy) {
  const W = 720;
  const H = 210;
  const nodes = [
    { id: 'pv',   x: 40,  y: 30,  w: 120, h: 46, label: 'PV array',  metric: energy.pv_production_kw },
    { id: 'batt', x: 40,  y: 128, w: 120, h: 46, label: 'Battery',   metric: energy.reserve_pct, unit: '%' },
    { id: 'gen',  x: 40,  y: 79,  w: 120, h: 46, label: 'Generator', metric: (energy.generator || {}).output_kw },
    { id: 'inv',  x: 250, y: 79,  w: 130, h: 46, label: 'Inverter',  metric: null },
    { id: 'crit', x: 470, y: 30,  w: 200, h: 46, label: 'Critical panel', metric: energy.critical_load_kw },
    { id: 'norm', x: 470, y: 128, w: 200, h: 46, label: 'Site load',      metric: energy.site_load_kw },
  ];
  const edges = [
    ['pv', 'inv'], ['gen', 'inv'], ['batt', 'inv'], ['inv', 'crit'], ['inv', 'norm'],
  ];
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));

  const svg = svgEl('svg', {
    class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'One-line power flow from PV, generator and battery through the inverter to the critical and normal panels',
  });

  edges.forEach(([from, to]) => {
    const a = byId[from];
    const b = byId[to];
    const x1 = a.x + a.w;
    const y1 = a.y + a.h / 2;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    const mid = (x1 + x2) / 2;
    svg.appendChild(svgEl('path', {
      d: `M${x1} ${y1} H${mid} V${y2} H${x2}`,
      fill: 'none', stroke: 'var(--line-strong)', 'stroke-width': 2,
    }));
  });

  nodes.forEach((node) => {
    const available = node.metric && node.metric.available;
    svg.appendChild(svgEl('rect', {
      x: node.x, y: node.y, width: node.w, height: node.h, rx: 6,
      fill: 'var(--bg-raised)',
      stroke: available ? 'var(--st-ok)' : 'var(--st-notdeployed)',
      'stroke-width': 1.5,
      'stroke-dasharray': available ? '' : '4 3',
    }));
    svg.appendChild(svgEl('text', {
      x: node.x + 10, y: node.y + 18, fill: 'var(--text)', 'font-size': 11, 'font-weight': 600,
    }, node.label));
    const value = available
      ? `${fmtNumber(node.metric.value, 1)} ${node.unit || node.metric.unit || ''}`.trim()
      : (node.metric ? readableStatus(node.metric.status) : 'no measurement');
    svg.appendChild(svgEl('text', {
      x: node.x + 10, y: node.y + 35, 'font-size': 11,
      fill: available ? 'var(--text)' : 'var(--text-faint)',
    }, value));
  });

  return svg;
}

function readableStatus(status) {
  return ({
    ok: 'live', stale: 'stale', no_data: 'no data', no_points: 'no points',
    design_only: 'design only', not_deployed: 'not deployed',
  })[status] || 'unknown';
}

/* --------------------------------------------------------------- charts --- */

/** Horizontal bar chart of estimated demand per load tier (SDD 31.2). */
function tierChart(tiers) {
  const rows = tiers.filter((t) => t.load_count > 0 || t.estimated_kw);
  if (!rows.length) return null;
  const max = Math.max(...rows.map((t) => t.estimated_kw || 0), 1);
  const rowH = 30;
  const W = 640;
  const H = rows.length * rowH + 14;
  const labelW = 190;
  const svg = svgEl('svg', {
    class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Estimated demand per load tier',
  });

  rows.forEach((tier, index) => {
    const y = index * rowH + 6;
    const width = tier.estimated_kw ? ((tier.estimated_kw / max) * (W - labelW - 70)) : 0;
    svg.appendChild(svgEl('text', { x: 0, y: y + 15, 'font-size': 11, fill: 'var(--text-dim)' }, tier.label));
    svg.appendChild(svgEl('rect', {
      x: labelW, y: y + 4, width: Math.max(width, 1.5), height: 14, rx: 3,
      fill: tier.shed_now ? 'var(--sev-major)' : 'var(--accent)',
      opacity: tier.estimated_kw ? 0.85 : 0.25,
    }));
    svg.appendChild(svgEl('text', {
      x: labelW + Math.max(width, 1.5) + 8, y: y + 16, 'font-size': 11, fill: 'var(--text)',
    }, tier.estimated_kw ? `${fmtNumber(tier.estimated_kw, 1)} kW est.` : 'no rated data'));
    svg.appendChild(svgEl('text', {
      x: labelW + Math.max(width, 1.5) + 8, y: y + 27, 'font-size': 9.5, fill: 'var(--text-faint)',
    }, `${tier.load_count} load${tier.load_count === 1 ? '' : 's'}`
      + (tier.shed_now ? ` · ${tier.shed_now} shed now` : '')));
  });
  return svg;
}

/** Balance bar: PV against load, or an explicit "cannot compute". */
function balanceChart(energy) {
  const pv = energy.pv_production_kw || {};
  const load = energy.site_load_kw || {};
  if (!pv.available || !load.available) return null;
  const max = Math.max(pv.value, load.value, 0.1);
  const W = 560;
  const H = 92;
  const barW = W - 120;
  const svg = svgEl('svg', {
    class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `PV production ${fmtNumber(pv.value, 1)} kilowatts against site load ${fmtNumber(load.value, 1)} kilowatts`,
  });
  [['PV', pv.value, 'var(--accent)', 12], ['Load', load.value, 'var(--sev-info)', 52]].forEach(([label, value, colour, y]) => {
    svg.appendChild(svgEl('text', { x: 0, y: y + 15, 'font-size': 11, fill: 'var(--text-dim)' }, label));
    svg.appendChild(svgEl('rect', { x: 46, y, width: barW, height: 20, rx: 4, fill: 'var(--bg-sunken)' }));
    svg.appendChild(svgEl('rect', {
      x: 46, y, width: Math.max((value / max) * barW, 2), height: 20, rx: 4, fill: colour,
    }));
    svg.appendChild(svgEl('text', {
      x: 46 + barW + 8, y: y + 15, 'font-size': 11, fill: 'var(--text)',
    }, `${fmtNumber(value, 1)} kW`));
  });
  return svg;
}

/** Recent EMS state transitions drawn as a labelled strip (most recent left). */
function transitionStrip(transitions) {
  if (!transitions.length) return null;
  const W = 720;
  const rowH = 26;
  const H = transitions.length * rowH + 8;
  const svg = svgEl('svg', {
    class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Recent energy state transitions',
  });
  transitions.forEach((entry, index) => {
    const y = index * rowH + 4;
    const tone = ENERGY_STATE_TONE[entry.to_state] || 'unknown';
    const colour = ({ ok: 'var(--st-ok)', degraded: 'var(--st-degraded)', alarm: 'var(--st-alarm)',
      stale: 'var(--st-stale)', design_only: 'var(--st-designonly)' })[tone] || 'var(--text-faint)';
    svg.appendChild(svgEl('rect', { x: 0, y, width: 4, height: rowH - 6, rx: 2, fill: colour }));
    svg.appendChild(svgEl('text', { x: 14, y: y + 13, 'font-size': 11.5, fill: 'var(--text)', 'font-weight': 600 },
      `${entry.from_state || '—'} → ${entry.to_state}`));
    svg.appendChild(svgEl('text', { x: 210, y: y + 13, 'font-size': 11, fill: 'var(--text-dim)' },
      entry.trigger || ''));
    svg.appendChild(svgEl('text', { x: 420, y: y + 13, 'font-size': 11, fill: 'var(--text-faint)' },
      (entry.reason || '').slice(0, 46)));
    svg.appendChild(svgEl('text', { x: W - 4, y: y + 13, 'font-size': 10.5, fill: 'var(--text-faint)', 'text-anchor': 'end' },
      fmtAge(entry.occurred_at)));
  });
  return svg;
}

/* --------------------------------------------------------------- render --- */

function derivedRow(label, block, unit, formatter = (v) => fmtNumber(v, 1)) {
  const available = block && block.available;
  return [
    h('dt', { text: label }),
    h('dd', null, available
      ? `${formatter(block.value)}${unit ? ' ' + unit : ''}`
      : statusChip((block && block.status) || 'no_data', 'Not published')),
  ];
}

function render(root, data, extras) {
  const energy = data.energy || {};
  const budget = energy.load_budget || {};
  clear(root);

  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Energy' }),
      h('p', { class: 'lede', text: 'SDD 38: one-line flow, site energy state, reserve and autonomy, tier budgets, shed groups, generator and recent state transitions. Measured values are shown separately from calculated ones.' })),
    h('p', { class: 'card-note', text: `Generated ${fmtDateTime(data.generated_at)}` })));

  /* --- state ------------------------------------------------------- */
  const tone = ENERGY_STATE_TONE[energy.state] || 'unknown';
  const stateCard = card({
    title: 'Site energy state',
    status: energy.state ? tone : 'no_data',
    statusText: energy.state || 'EMS not reporting',
    children: [
      h('div', { class: 'readout' },
        h('span', { class: 'value', text: energy.state || 'Unknown' })),
      h('dl', { class: 'kv', style: 'margin-top:.5rem' },
        h('dt', { text: 'Entered' }),
        h('dd', { text: energy.state_entered_at ? `${fmtDateTime(energy.state_entered_at)} (${fmtAge(energy.state_entered_at)})` : '—' }),
        h('dt', { text: 'Candidate' }),
        h('dd', { text: energy.candidate_state ? `${energy.candidate_state} since ${fmtAge(energy.candidate_since)}` : 'none' }),
        h('dt', { text: 'Data quality' }),
        h('dd', null, h('span', {
          class: `chip chip-${energy.data_quality === 'good' ? 'ok' : energy.data_quality === 'unknown' ? 'no_data' : 'stale'}`,
        }, h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '◍' }), energy.data_quality || 'unknown')),
        h('dt', { text: 'Last evaluated' }),
        h('dd', { text: energy.last_evaluated_at ? fmtAge(energy.last_evaluated_at) : 'never' }),
        h('dt', { text: 'Generator request' }),
        h('dd', { text: energy.generator_request || 'none' }),
        h('dt', { text: 'Frozen until' }),
        h('dd', { text: energy.frozen_until ? `${fmtDateTime(energy.frozen_until)} by ${energy.frozen_by || 'unknown'}` : 'not frozen' })),
      energy.note ? h('p', { class: 'card-note', text: energy.note }) : null,
    ],
  });

  const reserve = energy.reserve_pct || {};
  const reserveCard = card({
    title: 'Reserve and autonomy',
    status: reserve.status || 'unknown',
    children: [
      metricReadout(reserve, { unit: '%' }),
      meterBar(reserve.available ? reserve.value : null, { hatched: !reserve.available }),
      h('h4', { class: 'card-note', style: 'margin-top:.7rem;text-transform:uppercase;letter-spacing:.06em', text: 'Calculated by the EMS' }),
      h('dl', { class: 'kv' },
        ...derivedRow('Usable above reserve', energy.usable_energy_kwh, 'kWh'),
        ...derivedRow('Autonomy at current load', energy.autonomy_current_h, '', (v) => fmtDuration(v)),
        ...derivedRow('Autonomy, critical only', energy.autonomy_critical_h, '', (v) => fmtDuration(v)),
        ...derivedRow('Forecast energy margin', energy.forecast_energy_margin_kwh, 'kWh')),
      provenance(reserve),
    ],
  });

  root.appendChild(h('div', { class: 'grid grid-2' }, stateCard, reserveCard));

  /* --- one-line ---------------------------------------------------- */
  root.appendChild(h('div', { style: 'margin-top:var(--gap)' },
    card({
      title: 'One-line power flow',
      children: [
        oneLine(energy),
        h('p', { class: 'card-note', text: 'Solid outline: a live measurement exists. Dashed outline: no measured value — the equipment is not installed, not instrumented, or has never reported.' }),
      ],
    })));

  /* --- measured ---------------------------------------------------- */
  const measured = h('div', { class: 'grid grid-auto', style: 'margin-top:var(--gap)' });
  [['PV production', energy.pv_production_kw], ['Total site load', energy.site_load_kw],
    ['Critical load', energy.critical_load_kw], ['Generator output', (energy.generator || {}).output_kw],
    ['Generator fuel', (energy.generator || {}).fuel_pct]].forEach(([label, metric]) => {
    measured.appendChild(card({
      title: label,
      status: (metric && metric.status) || 'unknown',
      children: [metricReadout(metric), provenance(metric)],
    }));
  });
  root.appendChild(h('h3', { style: 'margin-top:1.3rem;font-size:.95rem', text: 'Measured values' }));
  root.appendChild(measured);

  const balance = balanceChart(energy);
  if (balance) {
    root.appendChild(h('div', { style: 'margin-top:var(--gap)' },
      card({ title: 'Generation against demand', children: [balance] })));
  }

  /* --- tiers and shed groups --------------------------------------- */
  const shed = energy.shed_groups_active || [];
  const tierCard = card({
    title: 'Load tiers and budgets',
    status: budget.status || 'no_data',
    children: [
      budget.profile_count
        ? [
            tierChart(budget.tiers || []),
            h('div', { class: 'table-wrap', style: 'margin-top:.7rem' },
              h('table', null,
                h('thead', null, h('tr', null,
                  h('th', { text: 'Tier' }), h('th', { class: 'num', text: 'Loads' }),
                  h('th', { class: 'num', text: 'Estimated kW' }), h('th', { class: 'num', text: 'Measured kW' }),
                  h('th', { text: 'Shed groups' }), h('th', { text: 'Data status' }))),
                h('tbody', null, (budget.tiers || []).map((tier) => h('tr', null,
                  h('td', { text: tier.label }),
                  h('td', { class: 'num', text: String(tier.load_count) }),
                  h('td', { class: 'num', text: tier.estimated_kw === null ? '—' : fmtNumber(tier.estimated_kw, 1) }),
                  h('td', { class: 'num' }, tier.measured_kw === null
                    ? statusChip('no_data', 'none') : fmtNumber(tier.measured_kw, 1)),
                  h('td', null, tier.shed_groups.length
                    ? tier.shed_groups.map((group) => h('span', {
                        class: `tag${shed.includes(group) ? ' chip chip-degraded' : ''}`, text: group,
                      }))
                    : '—'),
                  h('td', { text: Object.entries(tier.data_status || {}).map(([key, count]) => `${key}: ${count}`).join(', ') || '—' })))))),
          ]
        : h('p', { class: 'card-note', text: budget.note || 'No load schedule recorded.' }),
    ],
  });

  const shedCard = card({
    title: 'Shed groups active',
    status: shed.length ? 'degraded' : (energy.state ? 'ok' : 'no_data'),
    children: [
      shed.length
        ? h('div', { class: 'pill-row' }, shed.map((group) => h('span', { class: 'chip chip-degraded' },
            h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '▼' }), group)))
        : h('p', { class: 'card-note', text: energy.state ? 'No shed groups are active.' : 'The EMS has not published a state, so shedding status is unknown.' }),
      h('h4', { class: 'card-note', style: 'margin-top:.8rem;text-transform:uppercase;letter-spacing:.06em', text: 'Power-budget leases' }),
      (budget.active_leases || []).length
        ? h('ul', null, budget.active_leases.map((lease) => h('li', { class: 'provenance' },
            h('span', { text: `${lease.granted_kw} kW → ${lease.asset_id} · priority ${lease.priority} · expires ${fmtAge(lease.expires_at)} · ${lease.reason}` }))))
        : h('p', { class: 'card-note', text: 'No active leases.' }),
    ],
  });

  root.appendChild(h('div', { class: 'grid grid-2', style: 'margin-top:var(--gap)' }, tierCard, shedCard));

  /* --- generator --------------------------------------------------- */
  const generator = energy.generator || {};
  root.appendChild(h('div', { class: 'grid grid-2', style: 'margin-top:var(--gap)' },
    card({
      title: 'Generator',
      status: (generator.output_kw && generator.output_kw.status) || 'unknown',
      children: [
        h('dl', { class: 'kv' },
          h('dt', { text: 'Output' }),
          h('dd', null, generator.output_kw && generator.output_kw.available
            ? `${fmtNumber(generator.output_kw.value, 1)} kW` : statusChip((generator.output_kw || {}).status || 'no_data')),
          h('dt', { text: 'Fuel' }),
          h('dd', null, generator.fuel_pct && generator.fuel_pct.available
            ? `${fmtNumber(generator.fuel_pct.value, 0)} %` : statusChip((generator.fuel_pct || {}).status || 'no_data')),
          h('dt', { text: 'EMS request' }),
          h('dd', { text: generator.request || 'none' })),
        h('p', { class: 'card-note', text: 'Start/stop is executed through the generator-native controller (SDD 34.4); the EMS only requests.' }),
      ],
    }),
    card({
      title: 'Recent state transitions',
      status: (energy.recent_transitions || []).length ? 'ok' : 'no_data',
      children: [
        transitionStrip(energy.recent_transitions || [])
          || h('p', { class: 'card-note', text: 'The EMS has not recorded any state transitions yet.' }),
      ],
    })));

  /* --- EMS inputs -------------------------------------------------- */
  const inputs = energy.inputs || {};
  const derived = energy.derived || {};
  if (Object.keys(inputs).length || Object.keys(derived).length) {
    root.appendChild(h('div', { style: 'margin-top:var(--gap)' },
      card({
        title: 'EMS inputs and derived values',
        children: [
          h('p', { class: 'card-note', text: 'Exactly what the state machine used, so a state selection can be explained (SDD 30.6).' }),
          h('details', { class: 'raw', open: true },
            h('summary', { text: 'Snapshot' }),
            h('pre', { text: JSON.stringify({ inputs, derived }, null, 2) })),
        ],
      })));
  }

  /* --- detailed energy API ----------------------------------------- */
  root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem' },
    'Detailed energy API: ',
    extras.energyApi === 'available'
      ? statusChip('ok', '/energy/state available')
      : statusChip('not_deployed', '/energy/state not present on this node'),
    ' — this dashboard is built from the overview aggregate and does not depend on it.'));
}

export default {
  title: 'Energy',
  async mount(root, ctx) {
    const extras = { energyApi: 'unknown' };
    const unsubscribe = ctx.subscribeOverview((data, result) => {
      if (data) render(root, data, extras);
      else if (result && !result.ok) clear(root).appendChild(resultProblem(result, 'the energy dashboard'));
    });

    const probe = await api.energyState();
    extras.energyApi = probe.missing ? 'missing' : 'available';

    const current = ctx.latestOverview();
    if (!current.result) {
      root.appendChild(h('p', { class: 'loading', text: 'Loading energy state…' }));
      await ctx.refreshOverview();
    } else if (current.data) {
      render(root, current.data, extras);
    }

    return {
      async refresh() {},
      destroy() { unsubscribe(); },
    };
  },
};
