/* ---------------------------------------------------------------------------
   Asset registry browser and asset detail.

   The registry is the source of truth for identity (SDD 5.4), and it is also
   where the design's unfinished business lives. `open_fields` is therefore a
   first-class column here, not a footnote: an asset whose branch circuit or
   coordinates are still TBD must be visible as such, because that is exactly
   the information a commissioning engineer needs before touching it.
--------------------------------------------------------------------------- */

import {
  card, clear, fmtDateTime, h, kv, resultProblem, statusChip,
} from '../app.js';
import { api } from '../api.js';

const STATUS_TONE = {
  active: 'ok', commissioned: 'ok', installed: 'ok',
  degraded: 'degraded', maintenance: 'stale', failed: 'alarm',
  concept: 'design_only', planned: 'design_only', procured: 'design_only', reserve: 'design_only',
  retired: 'not_deployed',
};

const CRITICALITY_TONE = {
  life_safety: 'alarm', critical: 'degraded', important: 'stale', discretionary: 'no_data',
};

const FILTER_STATE = { domain: '', status: '', criticality: '', q: '', open_only: false, offset: 0 };
const PAGE_SIZE = 100;

/* ------------------------------------------------------------------ list -- */

function filterBar(onChange, facets) {
  const select = (name, label, options) => h('label', null,
    h('span', { text: label }),
    h('select', {
      class: 'select-sm',
      onchange: (event) => { FILTER_STATE[name] = event.target.value; FILTER_STATE.offset = 0; onChange(); },
    },
      h('option', { value: '', text: `All ${label.toLowerCase()}` }),
      options.map((value) => h('option', {
        value, text: value, selected: FILTER_STATE[name] === value,
      }))));

  return h('form', {
    class: 'filters',
    onsubmit: (event) => { event.preventDefault(); onChange(); },
  },
    h('label', null,
      h('span', { text: 'Search' }),
      h('input', {
        type: 'search', class: 'input', placeholder: 'asset id or name', value: FILTER_STATE.q,
        oninput: (event) => { FILTER_STATE.q = event.target.value; },
      })),
    select('domain', 'Domain', facets.domains),
    select('status', 'Status', facets.statuses),
    select('criticality', 'Criticality', facets.criticalities),
    h('label', { style: 'flex-direction:row;align-items:center;gap:.4rem' },
      h('input', {
        type: 'checkbox', id: 'open-only', checked: FILTER_STATE.open_only,
        onchange: (event) => { FILTER_STATE.open_only = event.target.checked; onChange(); },
      }),
      h('span', { text: 'Only assets with unresolved fields' })),
    h('button', { class: 'btn btn-sm', type: 'submit', text: 'Apply' }));
}

function assetTable(items, ctx) {
  return h('div', { class: 'table-wrap' },
    h('table', null,
      h('caption', { text: `${items.length} asset${items.length === 1 ? '' : 's'} shown` }),
      h('thead', null, h('tr', null,
        h('th', { text: 'Asset' }), h('th', { text: 'Domain' }), h('th', { text: 'Class' }),
        h('th', { text: 'Status' }), h('th', { text: 'Criticality' }),
        h('th', { text: 'Control' }), h('th', { text: 'Unresolved' }))),
      h('tbody', null, items.map((asset) => h('tr', null,
        h('td', null,
          h('a', { href: `#/assets/${encodeURIComponent(asset.asset_id)}`, text: asset.name }),
          h('div', { class: 'alarm-key mono', text: asset.asset_id })),
        h('td', { text: asset.domain }),
        h('td', { text: asset.asset_class }),
        h('td', null, statusChip(STATUS_TONE[asset.status] || 'unknown', asset.status)),
        h('td', null, statusChip(CRITICALITY_TONE[asset.criticality] || 'unknown', asset.criticality)),
        h('td', { text: asset.control_authority }),
        h('td', null, (asset.open_fields || []).length
          ? h('span', { class: 'chip chip-design_only' },
              h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '◇' }),
              `${asset.open_fields.length}`)
          : '—'))))));
}

async function renderList(root, ctx) {
  clear(root);
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Asset registry' }),
      h('p', { class: 'lede', text: 'Canonical asset identity (SDD 25). Unresolved design fields are shown, not hidden — an asset whose circuit or coordinates are still TBD is not ready to commission.' }))));

  const body = h('div');
  root.appendChild(body);

  const summaryResult = await api.registrySummary();
  const facets = {
    domains: [], statuses: [], criticalities: [],
  };
  if (summaryResult.ok) {
    const s = summaryResult.data;
    facets.domains = Object.keys(s.assets_by_domain || {}).sort();
    facets.statuses = Object.keys(s.assets_by_status || {}).sort();
    facets.criticalities = Object.keys(s.assets_by_criticality || {}).sort();
    root.insertBefore(h('div', { class: 'stat-row', style: 'margin-bottom:var(--gap)' },
      h('span', null, h('b', { text: String(s.assets) }), ' assets'),
      h('span', null, h('b', { text: String(s.points) }), ' points'),
      h('span', null, h('b', { text: String(s.relationships) }), ' relationships'),
      h('span', null, h('b', { text: String(s.bindings) }), ' bindings'),
      h('span', null, h('b', { text: String(s.assets_with_open_fields) }), ' assets with unresolved fields'),
      h('span', null, h('b', { text: String(s.points_control_capable) }), ' control-capable points')), body);
  }

  async function load() {
    clear(body);
    body.appendChild(filterBar(load, facets));
    const loading = h('p', { class: 'loading', text: 'Loading assets…' });
    body.appendChild(loading);

    const result = await api.assets({
      domain: FILTER_STATE.domain || undefined,
      status: FILTER_STATE.status || undefined,
      criticality: FILTER_STATE.criticality || undefined,
      q: FILTER_STATE.q || undefined,
      limit: PAGE_SIZE,
      offset: FILTER_STATE.offset,
    });
    loading.remove();

    if (!result.ok) {
      body.appendChild(resultProblem(result, 'the asset registry'));
      return;
    }
    let items = result.data.items || [];
    if (FILTER_STATE.open_only) items = items.filter((a) => (a.open_fields || []).length);

    if (!items.length) {
      body.appendChild(h('div', { class: 'empty' },
        h('h3', { text: 'No assets match' }),
        h('p', { text: result.data.total ? 'Adjust the filters above.' : 'The registry is empty. Load the machine-readable design package to populate it.' })));
      return;
    }
    body.appendChild(assetTable(items, ctx));

    const total = result.data.total || items.length;
    if (total > PAGE_SIZE) {
      body.appendChild(h('div', { class: 'pill-row', style: 'margin-top:.7rem' },
        h('button', {
          class: 'btn btn-sm', disabled: FILTER_STATE.offset === 0,
          onclick: () => { FILTER_STATE.offset = Math.max(0, FILTER_STATE.offset - PAGE_SIZE); load(); },
          text: '← Previous',
        }),
        h('span', { class: 'card-note', style: 'margin:0',
          text: `${FILTER_STATE.offset + 1}–${Math.min(FILTER_STATE.offset + PAGE_SIZE, total)} of ${total}` }),
        h('button', {
          class: 'btn btn-sm', disabled: FILTER_STATE.offset + PAGE_SIZE >= total,
          onclick: () => { FILTER_STATE.offset += PAGE_SIZE; load(); },
          text: 'Next →',
        })));
    }
  }

  await load();
  return { async refresh() {}, destroy() {} };
}

/* ---------------------------------------------------------------- detail -- */

function pointsTable(points) {
  if (!points.length) {
    return h('div', { class: 'empty' },
      h('h3', null, statusChip('no_points', 'No points registered')),
      h('p', { text: 'This asset carries no point instances yet. Points are materialised from the asset class and point profiles when the design package is loaded.' }));
  }
  return h('div', { class: 'table-wrap' },
    h('table', null,
      h('thead', null, h('tr', null,
        h('th', { text: 'Point' }), h('th', { text: 'Class' }), h('th', { text: 'Type' }),
        h('th', { text: 'Unit' }), h('th', { text: 'Control' }), h('th', { text: 'Auto' }))),
      h('tbody', null, points.map((point) => h('tr', null,
        h('td', null, h('span', { class: 'mono', text: point.point_name }),
          point.description ? h('div', { class: 'alarm-key', text: point.description }) : null),
        h('td', { text: point.point_class }),
        h('td', { text: point.data_type }),
        h('td', { text: point.unit || '—' }),
        h('td', null, point.control_capable ? statusChip('ok', 'commandable') : '—'),
        h('td', null, point.automatic_control_allowed
          ? statusChip('ok', 'allowed')
          : statusChip('design_only', 'not commissioned')))))));
}

function relationshipsList(relationships) {
  if (!relationships.length) {
    return h('p', { class: 'card-note', text: 'No typed relationships recorded.' });
  }
  const byType = new Map();
  relationships.forEach((rel) => {
    if (!byType.has(rel.relationship_type)) byType.set(rel.relationship_type, []);
    byType.get(rel.relationship_type).push(rel);
  });
  return h('div', null, Array.from(byType.entries()).map(([type, list]) =>
    h('div', { style: 'margin-bottom:.6rem' },
      h('div', { class: 'card-note', style: 'margin:0 0 .2rem', text: `${type} (${list.length})` }),
      h('ul', null, list.map((rel) => h('li', { class: 'provenance' },
        h('span', { text: rel.direction === 'outgoing' ? '→ ' : '← ' }),
        h('a', { href: `#/assets/${encodeURIComponent(rel.counterpart_id)}`,
                 text: rel.counterpart_name || rel.counterpart_id }),
        h('span', { text: ` · ${rel.counterpart_class || 'unknown class'} · ${rel.status}` })))))));
}

function jsonBlock(title, value) {
  const empty = value === null || value === undefined
    || (Array.isArray(value) && !value.length)
    || (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length);
  if (empty) return h('p', { class: 'card-note', text: `No ${title.toLowerCase()} recorded.` });
  return h('details', { class: 'raw' },
    h('summary', { text: title }),
    h('pre', { text: JSON.stringify(value, null, 2) }));
}

async function renderDetail(root, ctx, assetId) {
  clear(root);
  root.appendChild(h('p', { class: 'loading', text: `Loading ${assetId}…` }));

  const [assetResult, pointsResult, relResult, depResult, controlResult] = await Promise.all([
    api.asset(assetId), api.assetPoints(assetId), api.assetRelationships(assetId),
    api.assetDependencies(assetId), api.control(assetId),
  ]);

  clear(root);
  if (!assetResult.ok) {
    root.appendChild(h('div', { class: 'page-head' },
      h('div', null,
        h('h2', { text: assetId }),
        h('p', { class: 'lede' }, h('a', { href: '#/assets', text: '← Back to the registry' })))));
    root.appendChild(resultProblem(assetResult, `asset ${assetId}`));
    return { async refresh() {}, destroy() {} };
  }

  const asset = assetResult.data;
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('p', { class: 'card-note', style: 'margin:0' },
        h('a', { href: '#/assets', text: '← Registry' })),
      h('h2', { text: asset.name }),
      h('p', { class: 'lede mono', text: asset.asset_id })),
    h('div', { class: 'pill-row' },
      statusChip(STATUS_TONE[asset.status] || 'unknown', asset.status),
      statusChip(CRITICALITY_TONE[asset.criticality] || 'unknown', asset.criticality),
      h('a', { class: 'btn btn-sm', href: `#/control/${encodeURIComponent(asset.asset_id)}`, text: 'Control panel →' }))));

  const openFields = asset.open_fields || [];
  if (openFields.length) {
    root.appendChild(h('div', { class: 'empty', style: 'margin-bottom:var(--gap);border-color:var(--st-designonly)' },
      h('h3', null, statusChip('design_only', `${openFields.length} unresolved design fields`)),
      h('p', { text: 'These values are deliberately not invented. They must be surveyed, measured or decided before this asset can be commissioned.' }),
      h('ul', null, openFields.map((field) => h('li', { text: field })))));
  }

  const identity = card({
    title: 'Identity',
    children: kv([
      ['Domain', asset.domain],
      ['Asset class', asset.asset_class],
      ['Status', asset.status],
      ['Criticality', asset.criticality],
      ['Control authority', asset.control_authority],
      ['Functional position', asset.functional_position ? 'yes' : 'no'],
      ['Parent', asset.parent_id
        ? h('a', { href: `#/assets/${encodeURIComponent(asset.parent_id)}`, text: asset.parent_id })
        : '—'],
      ['Children', String(asset.child_count ?? 0)],
      ['Points', String(asset.point_count ?? 0)],
      ['Tags', (asset.tags || []).length ? (asset.tags || []).map((t) => h('span', { class: 'tag', text: t })) : '—'],
    ]),
  });

  const classDef = asset.asset_class_definition;
  const classCard = card({
    title: 'Asset class definition',
    children: classDef
      ? [
          h('p', { class: 'card-note', style: 'margin-top:0', text: classDef.purpose || '' }),
          kv([
            ['Allowed domains', (classDef.allowed_domains || []).join(', ') || '—'],
            ['Required properties', (classDef.required_properties || []).join(', ') || '—'],
            ['Default points', (classDef.default_points || []).join(', ') || '—'],
            ['Dictionary status', classDef.dictionary_status || '—'],
            ['SDD section', classDef.source_section || '—'],
          ]),
        ]
      : h('p', { class: 'card-note', text: 'The class dictionary entry was not found.' }),
  });

  const liveCard = card({
    title: 'Live control summary',
    status: controlResult.ok ? undefined : 'no_data',
    children: controlResult.ok
      ? [
          kv([
            ['Effective mode', (controlResult.data.operating_mode || {}).mode || 'not set'],
            ['Authority', (controlResult.data.authority || {}).local_or_remote || 'unknown'],
            ['Manual override', controlResult.data.manual_override.active === null
              ? statusChip('no_data', 'unknown')
              : (controlResult.data.manual_override.active ? statusChip('degraded', 'ACTIVE') : statusChip('ok', 'not active'))],
            ['Blocking interlocks', String((controlResult.data.interlocks || {}).blocking_count ?? 0)],
            ['Commandable', controlResult.data.commandable.allowed
              ? statusChip('ok', 'yes') : statusChip('design_only', 'no')],
          ]),
          h('a', { class: 'btn btn-sm', style: 'margin-top:.6rem',
                   href: `#/control/${encodeURIComponent(asset.asset_id)}`, text: 'Open the control panel' }),
        ]
      : h('p', { class: 'card-note', text: 'Control presentation is unavailable for this asset.' }),
  });

  root.appendChild(h('div', { class: 'grid grid-wide' }, identity, classCard, liveCard));

  root.appendChild(h('h3', { style: 'margin-top:1.3rem;font-size:.95rem', text: `Points (${(pointsResult.data || []).length || 0})` }));
  root.appendChild(pointsResult.ok ? pointsTable(pointsResult.data || []) : resultProblem(pointsResult, 'points'));

  const relCard = card({
    title: 'Relationships',
    children: relResult.ok ? relationshipsList(relResult.data || []) : resultProblem(relResult, 'relationships'),
  });

  const depCard = card({
    title: 'Dependencies',
    children: depResult.ok
      ? (Array.isArray(depResult.data) && depResult.data.length
          ? h('ul', null, depResult.data.map((dep) => h('li', { class: 'provenance' },
              typeof dep === 'string'
                ? h('span', { text: dep })
                : h('a', { href: `#/assets/${encodeURIComponent(dep.asset_id || dep.counterpart_id || '')}`,
                           text: dep.name || dep.asset_id || JSON.stringify(dep) }))))
          : [
              (asset.dependencies || []).length
                ? h('ul', null, (asset.dependencies || []).map((dep) => h('li', {
                    class: 'provenance', text: typeof dep === 'string' ? dep : JSON.stringify(dep) })))
                : h('p', { class: 'card-note', text: 'No operational dependencies recorded.' }),
            ])
      : h('p', { class: 'card-note', text: 'The dependency endpoint is unavailable on this node.' }),
  });

  const docCard = card({
    title: 'Documentation',
    children: (asset.documentation || []).length
      ? h('ul', null, (asset.documentation || []).map((doc) => h('li', { class: 'provenance' },
          typeof doc === 'string'
            ? h('span', { text: doc })
            : h('span', { text: `${doc.title || doc.doc_type || 'document'}${doc.uri ? ' — ' + doc.uri : ''}` }))))
      : h('p', { class: 'card-note', text: 'No manuals, drawings or commissioning records are linked yet.' }),
  });

  root.appendChild(h('div', { class: 'grid grid-wide', style: 'margin-top:var(--gap)' }, relCard, depCard, docCard));

  root.appendChild(h('div', { style: 'margin-top:var(--gap)' }, card({
    title: 'Recorded detail',
    children: [
      jsonBlock('Properties', asset.properties),
      jsonBlock('Location record', asset.location),
      jsonBlock('Network', asset.network),
      jsonBlock('Power', asset.power),
      jsonBlock('Manual override', asset.manual_override),
      jsonBlock('Maintenance plan', asset.maintenance_plan),
      jsonBlock('Source references', asset.source_refs),
      jsonBlock('Notes', asset.notes),
    ],
  })));

  root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
    text: `Loaded ${fmtDateTime(new Date().toISOString())}` }));

  return { async refresh() {}, destroy() {} };
}

export default {
  title: 'Assets',
  async mount(root, ctx) {
    if (ctx.params.assetId) return renderDetail(root, ctx, ctx.params.assetId);
    return renderList(root, ctx);
  },
};
