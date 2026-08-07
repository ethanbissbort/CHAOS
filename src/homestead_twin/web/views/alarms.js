/* ---------------------------------------------------------------------------
   Alarm screen (SDD 14).

   The SDD is explicit that a flood is a design failure: "A power-container
   outage should not create hundreds of separate notifications without a parent
   incident." So this screen groups by incident first and only then lists the
   member alarms. Alarms with no incident are collected under one honest
   "not correlated" group rather than being scattered.

   Severity is never carried by colour alone — every badge is glyph + word +
   colour, and the lifecycle state is spelled out.

   Every action collects a mandatory reason before it is sent, because the API
   audits the actor and the reason (SDD 15.2) and the operator should learn that
   from the dialog, not from a rejection.
--------------------------------------------------------------------------- */

import {
  askReason, clear, fmtAge, fmtDateTime, h, requireRole, resultProblem,
  severityChip, statusChip, toast,
} from '../app.js';
import { api } from '../api.js';

const SEVERITY_RANK = { emergency: 0, critical: 1, major: 2, warning: 3, info: 4 };

/** Lifecycle transitions an operator can drive (SDD 14.2). */
const ACTIONS = [
  { key: 'acknowledge', label: 'Acknowledge', from: ['detected', 'active'], role: 'operator',
    note: 'Acknowledging records that a named operator has seen this alarm. It does not clear the condition.' },
  { key: 'mitigate', label: 'Mitigate', from: ['detected', 'active', 'acknowledged'], role: 'operator',
    note: 'Mitigated means the immediate risk has been contained but the underlying condition may persist.' },
  { key: 'clear', label: 'Clear', from: ['detected', 'active', 'acknowledged', 'mitigated'], role: 'operator',
    danger: true,
    note: 'Clearing asserts the condition is gone. If the trigger is still true the alarm engine will raise it again.' },
];

function normalise(raw) {
  return {
    id: raw.id || raw.alarm_id,
    alarm_key: raw.alarm_key,
    severity: raw.severity || 'info',
    state: raw.state || 'active',
    asset_id: raw.asset_id || null,
    asset_name: raw.asset_name || null,
    domain: raw.domain || null,
    point_id: raw.point_id || null,
    message: raw.message || raw.name || raw.alarm_key,
    detected_at: raw.detected_at || raw.occurred_at || null,
    acknowledged_at: raw.acknowledged_at || null,
    acknowledged_by: raw.acknowledged_by || null,
    suppressed: Boolean(raw.suppressed),
    incident_id: raw.incident_id || null,
    incident_title: raw.incident_title || null,
  };
}

function groupByIncident(alarms) {
  const groups = new Map();
  alarms.forEach((alarm) => {
    const key = alarm.incident_id || '__uncorrelated__';
    if (!groups.has(key)) {
      groups.set(key, {
        id: key,
        title: alarm.incident_id
          ? (alarm.incident_title || `Incident ${alarm.incident_id.slice(0, 8)}`)
          : 'Not correlated to an incident',
        correlated: Boolean(alarm.incident_id),
        alarms: [],
      });
    }
    groups.get(key).alarms.push(alarm);
  });
  const list = Array.from(groups.values());
  list.forEach((group) => {
    group.alarms.sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9));
    group.severity = group.alarms[0] ? group.alarms[0].severity : 'info';
  });
  list.sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9));
  return list;
}

function alarmRow(alarm, onAction) {
  const actions = ACTIONS.filter((action) => action.from.includes(alarm.state));
  return h('div', { class: 'alarm-row alarm-sev-bar', style: `color: var(--sev-${alarm.severity})` },
    h('div', null,
      severityChip(alarm.severity, null, alarm.severity === 'critical' || alarm.severity === 'emergency'),
      h('div', { style: 'margin-top:.3rem;color:var(--text-dim)' },
        h('span', { class: 'tag', text: alarm.state }))),
    h('div', { class: 'alarm-main', style: 'color:var(--text)' },
      h('div', { class: 'alarm-msg', text: alarm.message }),
      h('div', { class: 'alarm-key', text: alarm.alarm_key }),
      h('div', { class: 'alarm-key' },
        alarm.asset_id
          ? h('a', { href: `#/control/${encodeURIComponent(alarm.asset_id)}`,
                     text: alarm.asset_name || alarm.asset_id })
          : 'site-wide',
        ` · detected ${fmtAge(alarm.detected_at)}`,
        alarm.acknowledged_by ? ` · acknowledged by ${alarm.acknowledged_by} ${fmtAge(alarm.acknowledged_at)}` : '',
        alarm.suppressed ? ' · SUPPRESSED' : ''),
      alarm.point_id ? h('div', { class: 'alarm-key mono', text: alarm.point_id }) : null),
    h('div', { class: 'alarm-actions', style: 'color:var(--text)' },
      actions.map((action) => h('button', {
        class: `btn btn-sm${action.danger ? ' btn-danger' : ''}`,
        onclick: () => onAction(alarm, action),
        text: action.label,
      })),
      alarm.asset_id
        ? h('a', { class: 'btn btn-sm', href: `#/control/${encodeURIComponent(alarm.asset_id)}`, text: 'Control' })
        : null));
}

function incidentBlock(group, onAction) {
  const counts = group.alarms.reduce((acc, alarm) => {
    acc[alarm.severity] = (acc[alarm.severity] || 0) + 1;
    return acc;
  }, {});
  return h('section', { class: 'incident', dataset: { severity: group.severity } },
    h('header', null,
      severityChip(group.severity, null, true),
      h('h3', { text: group.title }),
      h('div', { class: 'pill-row', style: 'margin-left:auto' },
        Object.entries(counts).map(([severity, count]) =>
          h('span', { class: `chip chip-${severity}` },
            h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '·' }), `${count} ${severity}`))),
      group.correlated
        ? null
        : h('span', { class: 'card-note', style: 'flex-basis:100%;margin:0',
                      text: 'These alarms have no parent incident. If several arrive together, the correlation rules may need work (SDD 14.2).' })),
    group.alarms.map((alarm) => alarmRow(alarm, onAction)));
}

async function loadAlarms() {
  // The dedicated alarms router is being built in parallel; the overview
  // aggregate always exists, so it is the dependable fallback.
  const direct = await api.activeAlarms();
  if (direct.ok) {
    const items = Array.isArray(direct.data) ? direct.data : (direct.data.items || direct.data.alarms || []);
    return { source: 'alarms API', alarms: items.map(normalise), result: direct, truncated: false };
  }
  const overview = await api.overview(100);
  if (!overview.ok) return { source: null, alarms: [], result: overview, truncated: false };
  const block = overview.data.alarms || {};
  return {
    source: 'overview aggregate',
    alarms: (block.top || []).map(normalise),
    summary: block,
    result: overview,
    truncated: (block.active_total || 0) > (block.top || []).length,
  };
}

export default {
  title: 'Alarms',
  async mount(root, ctx) {
    let state = { loading: true };

    async function onAction(alarm, action) {
      if (!requireRole(action.role)) return;
      const reason = await askReason({
        title: `${action.label} — ${alarm.message}`,
        note: `${action.note} Alarm ${alarm.alarm_key} on ${alarm.asset_name || alarm.asset_id || 'the site'}.`,
        confirmLabel: action.label,
        danger: Boolean(action.danger),
      });
      if (!reason) return;

      const response = await api.alarmAction(alarm.id, action.key, reason);
      if (response.ok) {
        toast('success', `${action.label}d`, `${alarm.alarm_key} — recorded with your reason.`);
        await draw();
        ctx.refreshOverview();
      } else if (response.missing) {
        toast('warn', 'Alarm actions not available yet',
          'This node does not expose an alarm lifecycle endpoint. Nothing was changed.');
      } else if (response.forbidden) {
        toast('error', 'Refused', `${response.error} — check your operator role.`);
      } else {
        toast('error', `${action.label} failed`, response.error || `HTTP ${response.status}`);
      }
    }

    async function draw() {
      const loaded = await loadAlarms();
      state = loaded;
      clear(root);

      root.appendChild(h('div', { class: 'page-head' },
        h('div', null,
          h('h2', { text: 'Alarms' }),
          h('p', { class: 'lede', text: 'Active alarms grouped by incident so one root cause reads as one event (SDD 14.2). Severity is shown as glyph, word and colour together.' })),
        h('div', { class: 'pill-row' },
          h('button', { class: 'btn btn-sm', onclick: draw, text: 'Reload' }))));

      if (!loaded.result.ok) {
        root.appendChild(resultProblem(loaded.result, 'active alarms'));
        return;
      }

      if (loaded.summary) {
        const s = loaded.summary;
        root.appendChild(h('div', { class: 'stat-row', style: 'margin-bottom:var(--gap)' },
          h('span', null, h('b', { text: String(s.active_total || 0) }), ' active'),
          h('span', null, h('b', { text: String((s.emergency_active || 0) + (s.critical_active || 0)) }), ' critical or emergency'),
          h('span', null, h('b', { text: String(s.major_active || 0) }), ' major'),
          h('span', null, h('b', { text: String(s.unacknowledged || 0) }), ' unacknowledged'),
          h('span', null, h('b', { text: String(s.open_incidents || 0) }), ' open incidents')));
      }

      if (!loaded.alarms.length) {
        root.appendChild(h('div', { class: 'empty' },
          h('h3', null, statusChip('ok', 'No active alarms')),
          h('p', { text: 'Nothing is detected, active, acknowledged or mitigated. Cleared and reviewed alarms are history, not active state.' })));
      } else {
        groupByIncident(loaded.alarms).forEach((group) => {
          root.appendChild(incidentBlock(group, onAction));
        });
      }

      if (loaded.truncated) {
        root.appendChild(h('p', { class: 'card-note',
          text: 'Only the highest-severity alarms are listed: this node has no dedicated alarms endpoint yet, so the overview aggregate is capping the list.' }));
      }

      root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
        text: `Source: ${loaded.source}. Lifecycle: detected → active → acknowledged → mitigated → cleared → reviewed.` }));
      root.appendChild(h('p', { class: 'card-note', text: `Loaded ${fmtDateTime(new Date().toISOString())}` }));
    }

    root.appendChild(h('p', { class: 'loading', text: 'Loading alarms…' }));
    await draw();

    return {
      async refresh() { if (!state.loading) await draw(); },
      destroy() {},
    };
  },
};
