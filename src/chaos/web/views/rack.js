/* ---------------------------------------------------------------------------
   Rack elevation — the primary 42U rack, drawn to a whole-U grid.

   The visual language (U-grid geometry, muted device faceplates carrying a
   near-white label, the front-elevation framing) is adapted from Rackula, the
   drag-and-drop rack designer by Gareth Evans — MIT licensed. None of its code
   is used here: CHAOS has no build step, so this is vanilla ES modules and
   hand-written SVG. What is borrowed is the *model*: whole-U registration, the
   `y = (units - start - height + 1) * uHeight` mapping from bottom-to-top rack
   numbering to top-down screen coordinates, and a category palette muted far
   enough to carry white text at WCAG AA.

   The one thing this screen must never do is look like the rack that exists.
   `data/rack_layout.yaml` is a proposal: not ratified, no authority until owner
   review. So the status is carried in four independent places, three of which
   survive a screenshot and two of which survive an export:

     1. a permanent banner above the drawing;
     2. a title block inside the SVG;
     3. a diagonal watermark across the elevation itself;
     4. every faceplate outlined in a dashed stroke, because no position is
        fixed — a ratified placement would be drawn solid.

   Colour is never the only signal. Live status is a glyph plus a word on every
   faceplate, matching the console's shared vocabulary, so the drawing reads the
   same to a colour-blind operator and in a black-and-white print.
--------------------------------------------------------------------------- */

import {
  card, clear, fmtAge, fmtDateTime, h, kv, resultProblem, statusChip, svgEl, toast,
} from '../app.js';
import { get } from '../api.js';

/* ------------------------------------------------------------ stylesheet -- */

const UI_ROOT = window.__UI_ROOT__ || '/ui/';
if (!document.getElementById('rack-css')) {
  const link = document.createElement('link');
  link.id = 'rack-css';
  link.rel = 'stylesheet';
  link.href = `${UI_ROOT}rack.css`;
  document.head.appendChild(link);
}

/* --------------------------------------------------------------- palette -- */

/**
 * Device fills, in Rackula's spirit (muted, low-chroma, never a bright accent)
 * but chosen for this console's categories and verified against *our* label
 * colour rather than inherited on trust.
 *
 * Every fill below carries DEVICE_LABEL at 5.0:1 or better — WCAG AA for normal
 * text is 4.5:1, and these faceplate labels are 11.5px, which is normal text.
 * Measured ratios (label #F5F7F6 on fill):
 *
 *   power              #A2474B  5.51:1     network        #6E5F9E  5.14:1
 *   transfer           #8A5A2B  5.45:1     network_edge   #4F5F96  5.72:1
 *   power_distribution #8C4F3F  5.91:1     wireless       #2F6E68  5.50:1
 *   compute            #3F6F80  5.14:1     environment    #6B6B36  5.16:1
 *   storage            #37703F  5.49:1     security       #9C4F72  5.17:1
 *   safety             #A33C3C  5.97:1     other          #5A6A8A  5.05:1
 *
 * `verifyContrast()` below recomputes them at runtime, so a future edit that
 * drops one under AA is caught by the test suite and logged in the console.
 */
const CATEGORY_FILL = {
  power: '#A2474B',
  transfer: '#8A5A2B',
  power_distribution: '#8C4F3F',
  compute: '#3F6F80',
  storage: '#37703F',
  network: '#6E5F9E',
  network_edge: '#4F5F96',
  wireless: '#2F6E68',
  environment: '#6B6B36',
  security: '#9C4F72',
  safety: '#A33C3C',
  other: '#5A6A8A',
};

const DEVICE_LABEL = '#F5F7F6';

/**
 * The elevation always renders on its own fixed dark ground, in light theme as
 * well as dark. A rack elevation is a drawing, not a panel: pinning the ground
 * means the exported SVG/PNG is pixel-identical to what the operator saw, and
 * it keeps every faceplate contrast ratio above a single verified number
 * instead of two theme-dependent ones.
 */
const CHROME = {
  page: '#0B1210',
  frame: '#2B3A34',
  frameEdge: '#4A5D54',
  interior: '#101917',
  free: '#16211D',
  freeEdge: '#243330',
  uText: '#B9C7C0',       // 10.2:1 on the interior
  freeText: '#9FB3AA',    //  7.5:1 on the free fill
  title: '#E6EFE9',
  sub: '#A3B5AC',
  proposal: '#F2A6A6',    //  9.7:1 on the page ground
  pill: '#0D1513',
  spineLine: '#2A3A34',
  feedA: '#7FC8A0',
  feedB: '#D89A9A',
  select: '#7DD3FC',
  alarm: '#F87171',
};

/* --------------------------------------------------- U-grid geometry (px) -- */

/* One rack unit is 1.75 in; 26 px is Rackula's 22 px scaled up so an 11.5px
   label and a status pill both fit inside a single 1U faceplate. */
const U_PX = 26;
const RAIL = 18;
const RACK_W = 344;
const INTERIOR_W = RACK_W - RAIL * 2;
/* Wide enough for a two-digit U number plus the "≈" marker that flags a device
   whose height is a guess. The marker belongs in the gutter, not on the
   faceplate: the uncertainty is about which units it occupies. */
const GUTTER_L = 56;
const GUTTER_R = 176;
const PAD_TOP = 52;
const PAD_BOTTOM = 26;

const RACK_X = GUTTER_L;
const INTERIOR_X = RACK_X + RAIL;
const FEED_A_X = RACK_X + RACK_W + 24;
/* 34 px apart so the "FEED" and "NO SRC" captions under the two rails cannot
   touch even at the widest fallback font. */
const FEED_B_X = FEED_A_X + 34;
const OUTLET_X = FEED_B_X + 16;
const SVG_W = GUTTER_L + RACK_W + GUTTER_R;

const geometry = (unitCount) => {
  const interiorTop = PAD_TOP + RAIL;
  const totalH = unitCount * U_PX;
  return {
    unitCount,
    interiorTop,
    totalH,
    height: PAD_TOP + RAIL + totalH + RAIL + PAD_BOTTOM,
    /** Rackula's mapping, unchanged: bottom-to-top U numbering onto a
     *  top-down coordinate system. Unit 1 is the lowest unit. */
    deviceY: (start, h_) => interiorTop + (unitCount - start - h_ + 1) * U_PX,
    unitTop: (unit) => interiorTop + (unitCount - unit) * U_PX,
    unitMid: (unit) => interiorTop + (unitCount - unit) * U_PX + U_PX / 2,
  };
};

/* ------------------------------------------------------------- contrast --- */

function relativeLuminance(hex) {
  const value = hex.replace('#', '');
  const channels = [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16) / 255);
  const linear = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

export function contrastRatio(a, b) {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Recomputed at load: a palette edit that fails AA should not ship quietly. */
export function verifyContrast(minimum = 4.5) {
  return Object.entries(CATEGORY_FILL)
    .map(([category, fill]) => ({ category, fill, ratio: contrastRatio(DEVICE_LABEL, fill) }))
    .filter((row) => row.ratio < minimum);
}

const CONTRAST_FAILURES = verifyContrast();
if (CONTRAST_FAILURES.length) {
  console.warn('Rack palette below WCAG AA against the device label:', CONTRAST_FAILURES);
}

/* ----------------------------------------------------------- status meta -- */

/** Glyph + word for the availability vocabulary the API speaks. Identical to
 *  the console's STATUS_META, restated here because the SVG needs the glyph and
 *  word as plain strings rather than as DOM chips. */
const SVG_STATUS = {
  ok: { glyph: '●', word: 'Live' },
  stale: { glyph: '◐', word: 'Stale' },
  no_data: { glyph: '○', word: 'No data' },
  no_points: { glyph: '○', word: 'No points' },
  design_only: { glyph: '◇', word: 'Design only' },
  not_deployed: { glyph: '⬚', word: 'Not in register' },
};
const svgStatus = (key) => SVG_STATUS[key] || { glyph: '?', word: 'Unknown' };

const FEED_LABEL = {
  A: 'A', B: 'B', A_and_B: 'A+B', not_assigned: '—',
};

/* -------------------------------------------------------------- helpers --- */

const truncate = (text, max) => {
  const value = String(text == null ? '' : text);
  return value.length <= max ? value : `${value.slice(0, Math.max(1, max - 1))}…`;
};

const uSpan = (device) => (device.rack_unit_height === 1
  ? `U${device.rack_unit_start}`
  : `U${device.rack_unit_start}–U${device.rack_unit_end}`);

/* =========================================================== the drawing == */

function drawElevation(data, state) {
  const unitCount = data.rack.rack_unit_count || 42;
  const g = geometry(unitCount);
  const doc = data.document || {};
  const ratified = Boolean(doc.ratified);

  // No xmlns attribute here: the element is already created in the SVG
  // namespace, and setting one by hand makes some serialisers emit it twice.
  // serialiseSvg() adds the declaration to the exported text if it is missing.
  const svg = svgEl('svg', {
    viewBox: `0 0 ${SVG_W} ${g.height}`,
    width: SVG_W,
    height: g.height,
    class: 'rack-svg',
    role: 'img',
    'aria-labelledby': 'rack-svg-title rack-svg-desc',
  });

  svg.appendChild(svgEl('title', { id: 'rack-svg-title' },
    `${ratified ? 'Rack elevation' : 'PROPOSED rack elevation, not ratified'} — `
    + `${unitCount}U, ${data.summary.occupied_units} units occupied, `
    + `${data.summary.free_units} free`));
  svg.appendChild(svgEl('desc', { id: 'rack-svg-desc' },
    `${doc.banner ? doc.banner.detail : ''} Numbering is bottom to top: unit 1 is the lowest unit. `
    + 'Every device is listed in the table below this drawing with the same information.'));

  // Fonts are declared inside the SVG so an exported file renders the same
  // when opened on its own. No @font-face, no network: system stack only.
  svg.appendChild(svgEl('style', null,
    'text{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}'
    + '.rk-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}'));

  const defs = svgEl('defs');
  defs.appendChild(svgEl('pattern', {
    id: 'rk-unratified', width: 8, height: 8, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)',
  }, svgEl('line', { x1: 0, y1: 0, x2: 0, y2: 8, stroke: '#ffffff', 'stroke-opacity': 0.035, 'stroke-width': 3 })));
  svg.appendChild(defs);

  // Opaque ground: an exported drawing must not rely on the page behind it.
  svg.appendChild(svgEl('rect', { x: 0, y: 0, width: SVG_W, height: g.height, fill: CHROME.page }));

  drawTitleBlock(svg, data, g);
  drawFrame(svg, g);
  drawUnits(svg, data, g);
  drawDevices(svg, data, g, state);
  drawPowerSpine(svg, data, g);
  if (!ratified) drawWatermark(svg, g);

  return svg;
}

function drawTitleBlock(svg, data, g) {
  const doc = data.document || {};
  const rackName = (data.rack.asset && data.rack.asset.name) || 'Primary rack';
  svg.appendChild(svgEl('text', {
    x: RACK_X, y: 20, fill: CHROME.title, 'font-size': 13.5, 'font-weight': 650,
  }, `${rackName} — FRONT ELEVATION`));
  svg.appendChild(svgEl('text', {
    x: RACK_X, y: 36, fill: doc.ratified ? CHROME.sub : CHROME.proposal,
    'font-size': 10.5, 'font-weight': 700, 'letter-spacing': 0.6, class: 'rk-mono',
  }, doc.ratified
    ? `RATIFIED · ${doc.approval_status}`
    : `PROPOSAL · NOT RATIFIED · authority: ${doc.authority || 'unknown'}`));
  svg.appendChild(svgEl('text', {
    x: SVG_W - 4, y: 20, fill: CHROME.sub, 'font-size': 10, 'text-anchor': 'end', class: 'rk-mono',
  }, `${g.unitCount}U · unit 1 lowest`));
  svg.appendChild(svgEl('text', {
    x: SVG_W - 4, y: 36, fill: CHROME.sub, 'font-size': 10, 'text-anchor': 'end', class: 'rk-mono',
  }, `${data.summary.occupied_units}U used · ${data.summary.free_units}U free`));
}

function drawFrame(svg, g) {
  const outerTop = PAD_TOP;
  const outerH = RAIL * 2 + g.totalH;
  svg.appendChild(svgEl('rect', {
    x: RACK_X, y: outerTop, width: RACK_W, height: outerH,
    fill: CHROME.frame, stroke: CHROME.frameEdge, 'stroke-width': 1, rx: 3,
  }));
  svg.appendChild(svgEl('rect', {
    x: INTERIOR_X, y: g.interiorTop, width: INTERIOR_W, height: g.totalH, fill: CHROME.interior,
  }));
  // Mounting holes: three per U on each rail, the EIA-310 cue that tells an
  // operator at a glance which way up the drawing is.
  for (let unit = 1; unit <= g.unitCount; unit += 1) {
    const top = g.unitTop(unit);
    [0.28, 0.5, 0.72].forEach((fraction) => {
      const cy = top + U_PX * fraction;
      [RACK_X + RAIL / 2, RACK_X + RACK_W - RAIL / 2].forEach((cx) => {
        svg.appendChild(svgEl('rect', {
          x: cx - 1.6, y: cy - 1.6, width: 3.2, height: 3.2, rx: 0.8, fill: '#0C1412', 'fill-opacity': 0.75,
        }));
      });
    });
  }
}

function drawUnits(svg, data, g) {
  const freeBlocks = data.free_blocks || [];
  (data.units || []).forEach((unit) => {
    const top = g.unitTop(unit.unit);
    if (unit.state === 'free') {
      svg.appendChild(svgEl('rect', {
        x: INTERIOR_X, y: top, width: INTERIOR_W, height: U_PX,
        fill: CHROME.free, stroke: CHROME.freeEdge, 'stroke-width': 0.5,
      }));
      svg.appendChild(svgEl('text', {
        x: INTERIOR_X + INTERIOR_W - 8, y: top + U_PX / 2 + 3.4,
        fill: CHROME.freeText, 'font-size': 9, 'text-anchor': 'end', class: 'rk-mono',
      }, 'free'));
    }
    // U numbers in the left gutter, on every unit. Numbering is bottom-to-top.
    svg.appendChild(svgEl('text', {
      x: RACK_X - 7, y: top + U_PX / 2 + 3.6,
      fill: CHROME.uText, 'font-size': 9.5, 'text-anchor': 'end', class: 'rk-mono',
      'font-weight': unit.unit % 5 === 0 || unit.unit === 1 ? 700 : 400,
    }, String(unit.unit)));
    svg.appendChild(svgEl('line', {
      x1: INTERIOR_X, y1: top, x2: INTERIOR_X + INTERIOR_W, y2: top,
      stroke: '#000000', 'stroke-opacity': 0.28, 'stroke-width': 0.5,
    }));
  });

  // Contiguous free runs get called out inside the interior. The layout keeps
  // one deliberate block for a future chassis; a drawing that shows 18 loose
  // empty rows loses that intent entirely.
  freeBlocks.filter((block) => block.size >= 2).forEach((block) => {
    const top = g.unitTop(block.end);
    const bottom = g.unitTop(block.start) + U_PX;
    const mid = (top + bottom) / 2;
    const x = INTERIOR_X + 16;
    svg.appendChild(svgEl('path', {
      d: `M ${x + 6} ${top + 4} L ${x} ${top + 4} L ${x} ${bottom - 4} L ${x + 6} ${bottom - 4}`,
      fill: 'none', stroke: CHROME.freeText, 'stroke-width': 1.1, 'stroke-opacity': 0.85,
    }));
    svg.appendChild(svgEl('text', {
      x: INTERIOR_X + INTERIOR_W / 2 + 8, y: mid - 4,
      fill: CHROME.freeText, 'font-size': 12, 'font-weight': 700, 'text-anchor': 'middle',
    }, `${block.size}U CONTIGUOUS FREE`));
    svg.appendChild(svgEl('text', {
      x: INTERIOR_X + INTERIOR_W / 2 + 8, y: mid + 12,
      fill: CHROME.freeText, 'font-size': 9.5, 'text-anchor': 'middle', class: 'rk-mono',
    }, `U${block.start}–U${block.end} · kept whole for a future chassis`));
  });
}

function drawDevices(svg, data, g, state) {
  (data.devices || []).forEach((device, index) => {
    const y = g.deviceY(device.rack_unit_start, device.rack_unit_height);
    const height = device.rack_unit_height * U_PX;
    const fill = CATEGORY_FILL[device.category] || CATEGORY_FILL.other;
    const selected = state.selected === device.asset_id;
    const status = svgStatus(device.health.status);
    const alarms = device.alarms.active_count;
    const preliminary = device.placement_status !== 'ratified';

    const group = svgEl('g', {
      class: `rk-device${selected ? ' is-selected' : ''}`,
      'data-asset': device.asset_id,
      tabindex: 0,
      role: 'button',
      'aria-label': `${device.name}, ${uSpan(device)}, ${device.rack_unit_height}U, `
        + `live status ${status.word}${alarms ? `, ${alarms} active alarms` : ''}. `
        + 'Proposed position, not ratified.',
      onclick: () => state.select(device.asset_id),
      onkeydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          state.select(device.asset_id);
        }
      },
    });
    group.appendChild(svgEl('title', null,
      `${device.name} · ${uSpan(device)} (${device.rack_unit_height}U) · ${status.word}`));

    group.appendChild(svgEl('rect', {
      x: INTERIOR_X + 1, y: y + 1, width: INTERIOR_W - 2, height: height - 2,
      fill, rx: 2.5,
    }));
    // Unratified positions are drawn dashed. Nothing in this document is fixed,
    // so nothing in this drawing gets a solid outline.
    group.appendChild(svgEl('rect', {
      x: INTERIOR_X + 1, y: y + 1, width: INTERIOR_W - 2, height: height - 2,
      fill: 'none', rx: 2.5,
      stroke: selected ? CHROME.select : 'rgba(255,255,255,.45)',
      'stroke-width': selected ? 2.2 : 1,
      'stroke-dasharray': preliminary ? '5 3' : null,
    }));
    // Category swatch: a colour cue that survives being read at a distance,
    // backed by the legend under the drawing.
    group.appendChild(svgEl('rect', {
      x: INTERIOR_X + 1, y: y + 1, width: 5, height: height - 2,
      fill: '#000000', 'fill-opacity': 0.28,
    }));

    const pillW = 94;
    const pillX = INTERIOR_X + INTERIOR_W - pillW - 7;
    // SVG has no cheap way to measure text before layout, so labels are cut to
    // a character budget from a conservative average advance width: 0.57em for
    // the 11.5px semibold name, 0.6em for the 9.5px monospace detail line.
    const textRoom = pillX - (INTERIOR_X + 14);
    const nameMax = Math.floor(textRoom / 7.2);
    const detailMax = Math.floor(textRoom / 6.2);
    const centre = y + height / 2;
    const nameY = device.rack_unit_height >= 2 ? centre - 5 : centre + 4;

    // Belt and braces. The character budget above gives a tidy ellipsis with
    // the fonts we expect; the clip guarantees that a wider fallback font can
    // never push a device name over its own status pill, which would be a
    // legibility failure in exactly the screenshot someone circulates.
    const clipId = `rk-clip-${index}`;
    group.appendChild(svgEl('clipPath', { id: clipId },
      svgEl('rect', { x: INTERIOR_X + 6, y, width: textRoom + 8, height })));

    group.appendChild(svgEl('text', {
      x: INTERIOR_X + 14, y: nameY, fill: DEVICE_LABEL, 'font-size': 11.5, 'font-weight': 600,
      'clip-path': `url(#${clipId})`,
    }, truncate(device.name, nameMax)));

    if (device.rack_unit_height >= 2) {
      const detail = [
        `${device.rack_unit_height}U`,
        uSpan(device),
        device.model || device.asset_class,
      ].filter(Boolean).join(' · ');
      group.appendChild(svgEl('text', {
        x: INTERIOR_X + 14, y: centre + 10, fill: DEVICE_LABEL, 'fill-opacity': 0.86,
        'font-size': 9.5, class: 'rk-mono', 'clip-path': `url(#${clipId})`,
      }, truncate(detail, detailMax)));
    }

    // Status pill on a fixed dark ground rather than on the category fill, so
    // one verified contrast ratio covers every category.
    const pillY = centre - 8;
    group.appendChild(svgEl('rect', {
      x: pillX, y: pillY, width: pillW, height: 16, rx: 8,
      fill: CHROME.pill, 'fill-opacity': 0.88, stroke: 'rgba(255,255,255,.22)', 'stroke-width': 0.7,
    }));
    group.appendChild(svgEl('text', {
      x: pillX + 9, y: pillY + 11.6, fill: DEVICE_LABEL, 'font-size': 9.5,
    }, status.glyph));
    group.appendChild(svgEl('text', {
      x: pillX + 21, y: pillY + 11.6, fill: DEVICE_LABEL, 'font-size': 9.5, class: 'rk-mono',
    }, status.word));

    if (alarms) {
      group.appendChild(svgEl('text', {
        x: pillX - 8, y: centre + 3.6, fill: CHROME.alarm, 'font-size': 11,
        'text-anchor': 'end', 'font-weight': 700,
      }, `▲${alarms}`));
    }
    if (device.rack_unit_height_is_estimated) {
      // In the gutter beside the U numbers this device claims, because that is
      // what is uncertain: if the guess is wrong, everything above it moves.
      const marker = svgEl('text', {
        x: 8, y: centre + 4, fill: CHROME.proposal, 'font-size': 12, 'font-weight': 700,
      }, '≈');
      marker.appendChild(svgEl('title', null,
        `${device.name}: rack_unit_height is an estimate (${device.rack_unit_height_basis}). `
        + 'The units it claims are provisional.'));
      group.appendChild(marker);
    }

    svg.appendChild(group);
  });
}

function drawPowerSpine(svg, data, g) {
  const feeds = (data.power && data.power.feeds) || [];
  const feedById = Object.fromEntries(feeds.map((feed) => [feed.feed_id, feed]));
  const top = g.interiorTop;
  const bottom = g.interiorTop + g.totalH;
  const pdus = (data.power && data.power.pdus) || [];
  /* Short tags so the spine stays readable. PDUs are numbered in document
     order; the ATS and UPS get their own tags because they are named as
     upstream sources by devices that are not on a PDU outlet at all. */
  const shortTag = {};
  pdus.forEach((pdu, index) => { shortTag[pdu.pdu_asset_id] = `PDU${index + 1}`; });
  (data.devices || []).forEach((device) => {
    if (device.asset_class === 'ats') shortTag[device.asset_id] = 'ATS';
    if (device.asset_class === 'ups') shortTag[device.asset_id] = 'UPS';
  });
  const tagFor = (assetId) => shortTag[assetId]
    || truncate(String(assetId).split('.').pop(), 11);

  const rail = (x, id, colour) => {
    const feed = feedById[id];
    const resolved = feed ? feed.resolved : false;
    svg.appendChild(svgEl('line', {
      x1: x, y1: top, x2: x, y2: bottom, stroke: colour, 'stroke-width': resolved ? 2.4 : 1.6,
      'stroke-dasharray': resolved ? null : '3 4', 'stroke-opacity': resolved ? 0.95 : 0.75,
    }));
    svg.appendChild(svgEl('text', {
      x, y: top - 20, fill: colour, 'font-size': 11, 'font-weight': 700, 'text-anchor': 'middle',
    }, id));
    svg.appendChild(svgEl('text', {
      x, y: top - 8, fill: resolved ? CHROME.sub : CHROME.proposal, 'font-size': 7.5,
      'text-anchor': 'middle', class: 'rk-mono',
    }, resolved ? 'FEED' : 'NO SRC'));
    if (!resolved) {
      svg.appendChild(svgEl('text', {
        x, y: bottom + 14, fill: CHROME.proposal, 'font-size': 7.5, 'text-anchor': 'middle', class: 'rk-mono',
      }, 'UNRESOLVED'));
    }
  };
  rail(FEED_A_X, 'A', CHROME.feedA);
  rail(FEED_B_X, 'B', CHROME.feedB);

  (data.devices || []).forEach((device) => {
    const mid = g.unitMid(device.rack_unit_start + Math.floor((device.rack_unit_height - 1) / 2));
    const feed = device.power.feed;
    const onA = feed === 'A' || feed === 'A_and_B';
    const onB = feed === 'A_and_B';
    const reach = onB ? FEED_B_X : (onA ? FEED_A_X : FEED_A_X - 12);

    svg.appendChild(svgEl('line', {
      x1: INTERIOR_X + INTERIOR_W + RAIL, y1: mid, x2: reach, y2: mid,
      stroke: CHROME.spineLine, 'stroke-width': 1,
    }));
    if (onA) {
      svg.appendChild(svgEl('circle', { cx: FEED_A_X, cy: mid, r: 3.2, fill: CHROME.feedA }));
    }
    if (onB) {
      // Hollow: the claim exists, the source does not.
      svg.appendChild(svgEl('circle', {
        cx: FEED_B_X, cy: mid, r: 3.2, fill: CHROME.page, stroke: CHROME.feedB, 'stroke-width': 1.4,
      }));
    }
    if (!onA && !onB) {
      svg.appendChild(svgEl('text', {
        x: FEED_A_X, y: mid + 3.4, fill: CHROME.sub, 'font-size': 9, 'text-anchor': 'middle', class: 'rk-mono',
      }, '—'));
    }

    // What this device is actually plugged into. An unknown outlet number is
    // drawn as "?" in the proposal colour rather than left blank: during a
    // power event, "we do not know which outlet" is the operative fact.
    const outlet = device.power.pdu_outlet;
    const source = device.power.power_source_asset_id;
    let label;
    let known = true;
    if (device.power.pdu_asset_id) {
      label = `${tagFor(device.power.pdu_asset_id)}·${outlet || '?'}`;
      known = Boolean(outlet);
    } else if (source) {
      label = `←${tagFor(source)}`;
    } else if (FEED_LABEL[feed] === '—') {
      label = 'no feed';
      known = false;
    } else {
      label = `feed ${FEED_LABEL[feed] || feed}`;
    }
    svg.appendChild(svgEl('text', {
      x: OUTLET_X, y: mid + 3.4, fill: known ? CHROME.sub : CHROME.proposal,
      'font-size': 9, class: 'rk-mono',
    }, label));
  });
}

function drawWatermark(svg, g) {
  const layer = svgEl('g', { 'aria-hidden': 'true', 'pointer-events': 'none' });
  layer.appendChild(svgEl('rect', {
    x: INTERIOR_X, y: g.interiorTop, width: INTERIOR_W, height: g.totalH, fill: 'url(#rk-unratified)',
  }));
  const step = Math.max(220, Math.round(g.totalH / 4));
  for (let y = g.interiorTop + 130; y < g.interiorTop + g.totalH; y += step) {
    layer.appendChild(svgEl('text', {
      x: INTERIOR_X + INTERIOR_W / 2, y,
      fill: CHROME.proposal, 'fill-opacity': 0.17, 'font-size': 24, 'font-weight': 800,
      'text-anchor': 'middle', 'letter-spacing': 2,
      transform: `rotate(-28 ${INTERIOR_X + INTERIOR_W / 2} ${y})`,
    }, 'PROPOSAL — NOT RATIFIED'));
  }
  svg.appendChild(layer);
}

/* ============================================================== export ==== */

/** Serialise the live SVG. Self-contained by construction: no <use>, no CSS
 *  variables, no external font, an opaque background rect and inline colours. */
function serialiseSvg(svg) {
  const clone = svg.cloneNode(true);
  clone.removeAttribute('class');
  clone.querySelectorAll('[tabindex]').forEach((node) => node.removeAttribute('tabindex'));
  const markup = new XMLSerializer().serializeToString(clone);
  const namespaced = markup.includes('xmlns="http://www.w3.org/2000/svg"')
    ? markup
    : markup.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"');
  return `<?xml version="1.0" encoding="UTF-8"?>\n${namespaced}`;
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = h('a', { href: url, download: filename });
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function exportName(data, extension) {
  const stamp = new Date().toISOString().slice(0, 10);
  const tag = data.document && data.document.ratified ? 'ratified' : 'PROPOSAL-NOT-RATIFIED';
  return `rack-elevation-${tag}-${stamp}.${extension}`;
}

function exportSvg(svg, data) {
  download(new Blob([serialiseSvg(svg)], { type: 'image/svg+xml;charset=utf-8' }), exportName(data, 'svg'));
}

function exportPng(svg, data) {
  const source = serialiseSvg(svg);
  const blob = new Blob([source], { type: 'image/svg+xml;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const image = new Image();
  const scale = 2;
  image.onload = () => {
    const canvas = document.createElement('canvas');
    canvas.width = SVG_W * scale;
    canvas.height = svg.viewBox.baseVal.height * scale;
    const context = canvas.getContext('2d');
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.drawImage(image, 0, 0);
    URL.revokeObjectURL(url);
    canvas.toBlob((out) => {
      if (out) download(out, exportName(data, 'png'));
      else toast('warn', 'PNG export failed', 'The browser refused to rasterise the drawing. The SVG export still works.');
    }, 'image/png');
  };
  image.onerror = () => {
    URL.revokeObjectURL(url);
    toast('warn', 'PNG export failed', 'Use the SVG export instead; it carries the same drawing.');
  };
  image.src = url;
}

/* ========================================================== page blocks === */

function proposalBanner(data) {
  const doc = data.document || {};
  const banner = doc.banner || {};
  if (doc.ratified && doc.usable_as_built) {
    return h('div', { class: 'rack-banner rack-banner-ok', role: 'note' },
      h('strong', { text: banner.headline || 'Ratified layout' }),
      h('p', { text: banner.detail || '' }));
  }
  return h('div', { class: 'rack-banner', role: 'note', 'aria-label': 'Document status' },
    h('div', { class: 'rack-banner-head' },
      h('span', { class: 'rack-banner-glyph', 'aria-hidden': 'true', text: '◇' }),
      h('strong', { text: banner.headline || 'PROPOSAL — NOT RATIFIED' })),
    h('p', { text: banner.detail || '' }),
    h('dl', { class: 'rack-banner-meta' },
      h('div', null, h('dt', { text: 'document_status' }), h('dd', { class: 'mono', text: doc.document_status || '—' })),
      h('div', null, h('dt', { text: 'approval_status' }), h('dd', { class: 'mono', text: doc.approval_status || '—' })),
      h('div', null, h('dt', { text: 'authority' }), h('dd', { class: 'mono', text: doc.authority || '—' })),
      h('div', null, h('dt', { text: 'review required' }),
        h('dd', { class: 'mono', text: doc.review_required_before_use ? 'yes — before any use' : 'no' }))),
    (banner.open_fields || []).length
      ? h('div', { class: 'rack-banner-fields' },
          h('p', { class: 'rack-banner-fields-title',
            text: `${banner.open_field_count} document-level fields must be resolved before this layout can be built:` }),
          h('ul', null, (banner.open_fields || []).map((field) => h('li', { class: 'mono', text: field }))))
      : null);
}

const FINDING_TONE = { blocking: 'alarm', warning: 'degraded', info: 'no_data' };

function findingsCard(data) {
  const findings = data.findings || [];
  if (!findings.length) {
    return card({
      title: 'Consistency checks',
      children: h('p', { class: 'card-note', text: 'The layout re-derives cleanly and no conflicts were found.' }),
    });
  }
  return card({
    title: `Consistency checks (${findings.length})`,
    note: 'Re-derived from the placements on every request rather than taken from the document\'s own summary.',
    children: h('ul', { class: 'rack-findings' }, findings.map((finding) => h('li', {
      class: `rack-finding rack-finding-${finding.severity}`,
    },
      h('div', { class: 'rack-finding-head' },
        statusChip(FINDING_TONE[finding.severity] || 'unknown', finding.severity),
        h('strong', { text: finding.title }),
        h('span', { class: 'alarm-key mono', text: finding.code })),
      h('p', { text: finding.detail }),
      (finding.assets || []).length
        ? h('p', { class: 'provenance mono', text: (finding.assets || []).join(', ') })
        : null,
      (finding.units || []).length
        ? h('p', { class: 'provenance mono', text: `Units: ${(finding.units || []).join(', ')}` })
        : null,
      (finding.open_fields || []).length
        ? h('p', { class: 'provenance mono', text: `Open: ${(finding.open_fields || []).join(', ')}` })
        : null))),
  });
}

function feedCard(data) {
  const feeds = (data.power && data.power.feeds) || [];
  return card({
    title: 'Power feeds',
    note: 'A feed with no upstream asset cannot carry load. Dual-fed devices below are single-fed until it exists.',
    children: h('div', { class: 'rack-feeds' }, feeds.map((feed) => h('div', {
      class: `rack-feed${feed.resolved ? '' : ' rack-feed-unresolved'}`,
    },
      h('div', { class: 'rack-feed-head' },
        h('span', { class: 'rack-feed-id', text: `FEED ${feed.feed_id}` }),
        statusChip(feed.resolved ? 'ok' : 'no_data', feed.resolved ? 'source identified' : 'no source')),
      h('p', { class: 'card-note', text: feed.description || '' }),
      kv([
        ['Upstream', feed.upstream_asset_id
          ? h('a', { class: 'mono', href: `#/assets/${encodeURIComponent(feed.upstream_asset_id)}`,
                     text: feed.upstream_asset_id })
          : 'none — this feed resolves to nothing'],
        ['Data status', feed.data_status],
        ['Open fields', (feed.open_fields || []).join(', ') || '—'],
      ])))),
  });
}

function pduCard(data) {
  const pdus = (data.power && data.power.pdus) || [];
  return card({
    title: `PDUs and outlet assignments (${pdus.length})`,
    note: 'Every outlet is shown, including spares. An outlet held for a redundant cord is not a free outlet.',
    children: pdus.map((pdu, index) => h('div', { class: 'rack-pdu' },
      h('div', { class: 'rack-pdu-head' },
        h('span', { class: 'rack-pdu-tag mono', text: `PDU${index + 1}` }),
        h('a', { class: 'mono', href: `#/assets/${encodeURIComponent(pdu.pdu_asset_id)}`, text: pdu.pdu_asset_id }),
        statusChip(pdu.health.status),
        h('span', { class: 'chip chip-unknown' },
          h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '⚡' }),
          `feed ${pdu.power_feed}`)),
      h('p', { class: 'card-note',
        text: `${pdu.used_outlets} of ${pdu.outlets_modelled} outlets assigned · ${pdu.spare_outlets} spare · `
          + `${pdu.nominal_voltage_v ?? '?'} V nominal` }),
      h('div', { class: 'table-wrap' },
        h('table', null,
          h('thead', null, h('tr', null,
            h('th', { text: 'Outlet' }), h('th', { text: 'Assigned to' }),
            h('th', { text: 'Purpose' }), h('th', { text: 'Status' }))),
          h('tbody', null, (pdu.outlets || []).map((outlet) => h('tr', null,
            h('td', { class: 'mono', text: String(outlet.outlet) }),
            h('td', null, outlet.assigned_asset_id
              ? h('a', { href: `#/rack/${encodeURIComponent(outlet.assigned_asset_id)}`,
                         text: outlet.assigned_name || outlet.assigned_asset_id })
              : h('span', { class: 'card-note', text: '— unassigned' })),
            h('td', { text: outlet.purpose || '—' }),
            h('td', { class: 'mono', text: outlet.status || '—' })))))),
      (pdu.notes || []).length
        ? h('ul', { class: 'provenance' }, (pdu.notes || []).map((note) => h('li', { text: note })))
        : null)),
  });
}

function zeroUCard(data) {
  const items = data.zero_u_devices || [];
  return card({
    title: `Zero-U and door-mounted devices (${items.length})`,
    note: 'These consume no rack units, so they do not appear in the elevation. They are still in the rack.',
    children: items.length
      ? h('div', { class: 'table-wrap' },
          h('table', null,
            h('thead', null, h('tr', null,
              h('th', { text: 'Device' }), h('th', { text: 'Mount' }), h('th', { text: 'Power' }),
              h('th', { text: 'Switch port' }), h('th', { text: 'Live status' }))),
            h('tbody', null, items.map((device) => h('tr', null,
              h('td', null,
                h('a', { href: `#/rack/${encodeURIComponent(device.asset_id)}`, text: device.name }),
                h('div', { class: 'alarm-key mono', text: device.asset_id })),
              h('td', { class: 'mono', text: device.mount_style }),
              h('td', { class: 'mono',
                text: device.power.pdu_outlet
                  ? `feed ${device.power.feed} · outlet ${device.power.pdu_outlet}`
                  : `feed ${device.power.feed}` }),
              h('td', { class: 'mono', text: device.network.switch_port ?? '—' }),
              h('td', null, statusChip(device.health.status)))))))
      : h('p', { class: 'card-note', text: 'None recorded.' }),
  });
}

function switchCard(data) {
  const plan = data.switch_port_plan || {};
  return card({
    title: 'Switch-port plan',
    note: 'Port numbers are positional indexes, not interface names. Do not build a patching schedule from them.',
    children: [
      kv([
        ['Numbering basis', plan.numbering_basis],
        ['Interface naming', plan.interface_naming_status],
        ['Open fields', (plan.open_fields || []).join(', ') || '—'],
      ]),
      ...(plan.switches || []).map((entry) => h('div', { class: 'rack-switch' },
        h('div', { class: 'rack-switch-head' },
          h('a', { class: 'mono', href: `#/rack/${encodeURIComponent(entry.switch_asset_id)}`,
                   text: entry.switch_asset_id }),
          h('span', { class: 'tag', text: entry.role }),
          h('span', { class: 'card-note', style: 'margin:0', text: `${entry.assigned_count} ports proposed` })),
        h('div', { class: 'table-wrap' },
          h('table', null,
            h('thead', null, h('tr', null,
              h('th', { text: 'Port' }), h('th', { text: 'Device' }),
              h('th', { text: 'VLANs' }), h('th', { text: 'Purpose' }))),
            h('tbody', null, (entry.assignments || []).map((row) => h('tr', null,
              h('td', { class: 'mono', text: String(row.port ?? '—') }),
              h('td', null, h('a', { href: `#/rack/${encodeURIComponent(row.asset_id)}`, text: row.name })),
              h('td', { class: 'mono', text: (row.vlan_ids || []).join(', ') || '—' }),
              h('td', { text: row.purpose })))))))),
      (plan.notes || []).length
        ? h('ul', { class: 'provenance' }, (plan.notes || []).map((note) => h('li', { text: note })))
        : null,
    ],
  });
}

function thermalCard(data) {
  const thermal = data.thermal || {};
  return card({
    title: 'Thermal and load',
    status: thermal.available ? 'ok' : 'no_data',
    statusText: thermal.available ? 'estimated' : 'not calculated',
    children: [
      h('p', { class: 'card-note', style: 'margin-top:0', text: thermal.basis || '' }),
      kv([
        ['Total estimated power', thermal.total_estimated_power_w === null || thermal.total_estimated_power_w === undefined
          ? 'not estimated — no wattage exists for any device' : `${thermal.total_estimated_power_w} W`],
        ['Power data', thermal.power_data_status],
        ['Heat load', thermal.heat_load_status],
      ]),
      (thermal.required_before_use || []).length
        ? h('div', null,
            h('p', { class: 'card-note', text: 'Required before the rack is populated:' }),
            h('ul', { class: 'provenance' },
              (thermal.required_before_use || []).map((item) => h('li', { text: item }))))
        : null,
    ],
  });
}

function excludedCard(data) {
  const items = data.excluded_assets || [];
  return card({
    title: `Deliberately excluded assets (${items.length})`,
    note: 'Recorded so their absence from the elevation is a decision on the record, not an oversight.',
    children: items.length
      ? h('ul', { class: 'rack-excluded' }, items.map((entry) => h('li', null,
          h('a', { class: 'mono', href: `#/assets/${encodeURIComponent(entry.asset_id)}`, text: entry.asset_id }),
          h('p', { class: 'card-note', text: entry.reason || '' }))))
      : h('p', { class: 'card-note', text: 'None.' }),
  });
}

function legend(data) {
  const labels = data.category_labels || {};
  const used = new Set((data.devices || []).concat(data.zero_u_devices || []).map((d) => d.category));
  return h('div', { class: 'rack-legend' },
    h('div', { class: 'rack-legend-group' },
      h('h4', { text: 'Category' }),
      h('ul', null, Array.from(used).sort().map((category) => h('li', null,
        h('span', { class: 'rack-swatch', style: `background:${CATEGORY_FILL[category] || CATEGORY_FILL.other}`,
                    'aria-hidden': 'true' }),
        labels[category] || category)))),
    h('div', { class: 'rack-legend-group' },
      h('h4', { text: 'Live status' }),
      h('ul', null, Object.entries(SVG_STATUS).map(([key, meta]) => h('li', null,
        h('span', { class: 'rack-legend-glyph', 'aria-hidden': 'true', text: meta.glyph }),
        `${meta.word} — ${(data.status_vocabulary || {})[key] || ''}`)))),
    h('div', { class: 'rack-legend-group' },
      h('h4', { text: 'Drawing conventions' }),
      h('ul', null,
        h('li', null, h('span', { class: 'rack-legend-glyph', 'aria-hidden': 'true', text: '⌐' }),
          'Dashed faceplate outline — position proposed, not ratified. Nothing here is fixed.'),
        h('li', null, h('span', { class: 'rack-legend-glyph', 'aria-hidden': 'true', text: '●' }),
          'Filled dot on a feed rail — cord assigned to a feed with an identified source.'),
        h('li', null, h('span', { class: 'rack-legend-glyph', 'aria-hidden': 'true', text: '○' }),
          'Hollow dot — cord assigned to a feed that resolves to no source.'),
        h('li', null, h('span', { class: 'rack-legend-glyph', 'aria-hidden': 'true', text: '≈' }),
          '≈ beside the U numbers — that device\'s height is an estimate, so the units it claims '
          + 'are provisional and everything above it moves if the guess is wrong.'),
        h('li', null, h('span', { class: 'rack-legend-glyph', 'aria-hidden': 'true', text: '▲' }),
          'Alarm triangle with a count — active unresolved alarms on that asset.'))));
}

/* ------------------------------------------------------- detail panel ----- */

function detailPanel(data, assetId, state) {
  const all = (data.devices || []).concat(data.zero_u_devices || []);
  const device = all.find((entry) => entry.asset_id === assetId);
  if (!device) {
    return card({
      title: 'Device detail',
      children: h('p', { class: 'card-note', text: assetId
        ? `${assetId} is not placed in this rack layout. It may be a zero-U accessory, excluded, or not in the document at all.`
        : 'Select a device in the elevation to see its proposed position, power path and live status.' }),
    });
  }

  const health = device.health;
  const power = device.power;
  const network = device.network;
  const pduTag = power.pdu_asset_id
    ? `${power.pdu_asset_id}${power.pdu_outlet ? ` · outlet ${power.pdu_outlet}` : ' · outlet unassigned'}`
    : 'not on a PDU';

  return card({
    title: device.name,
    badge: h('div', { class: 'pill-row' },
      statusChip(health.status),
      device.alarms.active_count
        ? h('span', { class: 'chip chip-alarm' },
            h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '▲' }),
            `${device.alarms.active_count} active`)
        : null,
      h('button', {
        class: 'btn btn-sm', type: 'button', text: 'Clear', onclick: () => state.select(null),
      })),
    children: [
      h('p', { class: 'lede mono', style: 'margin:.2rem 0 .6rem', text: device.asset_id }),
      h('div', { class: 'rack-detail-flags' },
        h('span', { class: 'chip chip-design_only' },
          h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '◇' }),
          `placement: ${device.placement_status}`),
        h('span', { class: 'chip chip-design_only' },
          h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '◇' }),
          `data: ${device.data_status}`)),
      kv([
        ['Model', device.model || '— not recorded'],
        ['Manufacturer', device.manufacturer || '—'],
        ['Asset class', device.asset_class],
        ['Register status', device.register_status],
        ['Lifecycle status', health.lifecycle_status || 'not in register'],
        ['Criticality', health.criticality || '—'],
        ['Position', device.consumes_rack_units
          ? `${uSpan(device)} · ${device.rack_unit_height}U`
          : `${device.mount_style} · consumes no rack units`],
        ['Height basis', device.rack_unit_height_basis
          + (device.rack_unit_height_is_estimated ? ' (estimated — the elevation above it moves if wrong)' : '')],
        ['Depth', device.depth_class],
      ]),
      h('h4', { class: 'rack-detail-h', text: 'Power' }),
      kv([
        ['Feed', `${power.feed}${power.feed_is_dual_claim ? ' (dual claim)' : ''}`],
        ['Feed basis', power.feed_basis],
        ['PDU / outlet', pduTag],
        ['PDU modelled', power.pdu_is_modelled
          ? 'yes'
          : 'no — the recorded source is not in the document\'s PDU list, so no outlet can be assigned'],
        ['Outlet basis', power.pdu_outlet_basis],
        ['Upstream source', power.power_source_asset_id
          ? h('a', { class: 'mono', href: `#/assets/${encodeURIComponent(power.power_source_asset_id)}`,
                     text: power.power_source_asset_id })
          : '—'],
        ['Power figure', power.estimated_power_w === null || power.estimated_power_w === undefined
          ? `none — ${power.power_data_status}` : `${power.estimated_power_w} W`],
        ['Heat load', power.heat_load_status],
      ]),
      h('h4', { class: 'rack-detail-h', text: 'Network' }),
      kv([
        ['Switch', network.switch_asset_id
          ? h('a', { class: 'mono', href: `#/rack/${encodeURIComponent(network.switch_asset_id)}`,
                     text: network.switch_asset_id })
          : '— no switch recorded'],
        ['Port', network.switch_port === null || network.switch_port === undefined
          ? '—' : `${network.switch_port} (positional index, not an interface name)`],
        ['Port basis', network.switch_port_basis],
        ['VLANs', (network.vlan_ids || []).join(', ') || '— none recorded'],
        ['VLAN basis', network.vlan_basis],
        ['Additional links', (network.additional_switch_connections || []).length
          ? h('ul', { class: 'provenance' }, (network.additional_switch_connections || []).map((link) =>
              h('li', { text: `${link.switch_asset_id} port ${link.switch_port} · ${link.purpose} · VLAN `
                + `${(link.vlan_ids || []).join(', ') || '—'} · ${link.status}` })))
          : '—'],
      ]),
      h('h4', { class: 'rack-detail-h', text: 'Live status' }),
      h('p', { class: 'card-note', text: health.reason }),
      kv([
        ['Availability', statusChip(health.status)],
        ['Expected to report', health.expected_to_report
          ? 'yes — recorded as installed'
          : 'no — still a design record, so silence is expected but is not a healthy reading'],
        ['Points', `${health.reporting_point_count} reporting of ${health.point_count} registered`
          + (health.stale_point_count ? ` · ${health.stale_point_count} stale` : '')],
        ['Last value', health.last_reported_at ? fmtAge(health.last_reported_at) : 'never'],
        ['Can report going silent', health.availability_point
          ? 'yes — availability_state is registered'
          : 'no — no availability_state point, so a silent device looks the same as a healthy one'],
      ]),
      device.alarms.active_count
        ? h('div', null,
            h('h4', { class: 'rack-detail-h', text: `Active alarms (${device.alarms.active_count})` }),
            h('ul', { class: 'rack-alarms' }, (device.alarms.items || []).map((alarm) => h('li', null,
              h('strong', { text: alarm.severity }),
              h('span', { text: ` · ${alarm.message || alarm.alarm_key}` }),
              h('div', { class: 'alarm-key mono', text: `${alarm.alarm_key} · ${fmtAge(alarm.detected_at)}` })))))
        : null,
      (device.open_fields || []).length
        ? h('div', { class: 'rack-open' },
            h('h4', { class: 'rack-detail-h',
              text: `Open fields (${device.open_fields.length}) — deliberately not invented` }),
            h('ul', null, (device.open_fields || []).map((field) => h('li', { class: 'mono', text: field }))))
        : null,
      (device.notes || []).length
        ? h('ul', { class: 'provenance' }, (device.notes || []).map((note) => h('li', { text: note })))
        : null,
      h('div', { class: 'pill-row', style: 'margin-top:.7rem' },
        h('a', { class: 'btn btn-sm', href: `#/assets/${encodeURIComponent(device.asset_id)}`,
                 text: 'Asset record →' }),
        h('a', { class: 'btn btn-sm', href: `#/control/${encodeURIComponent(device.asset_id)}`,
                 text: 'Control panel →' })),
    ],
  });
}

/* ================================================================ mount === */

export default {
  title: 'Rack',

  async mount(root, ctx) {
    const state = {
      selected: ctx.params.assetId || null,
      data: null,
      select: null,
    };

    const render = () => {
      const scroll = window.scrollY;
      clear(root);
      const data = state.data;

      root.appendChild(h('div', { class: 'page-head' },
        h('div', null,
          h('h2', { text: 'Rack elevation' }),
          h('p', { class: 'lede', text: 'Proposed 42U elevation for the primary rack, joined to live asset '
            + 'state. Numbering is bottom-to-top: unit 1 is the lowest unit.' }))));

      if (!data) return;

      root.appendChild(proposalBanner(data));

      const svg = drawElevation(data, state);
      const tools = h('div', { class: 'rack-tools' },
        h('span', { class: 'card-note', style: 'margin:0',
          text: 'Export carries the proposal watermark and the not-ratified title block.' }),
        h('button', { class: 'btn btn-sm', type: 'button', text: 'Export SVG',
                      onclick: () => exportSvg(svg, data) }),
        h('button', { class: 'btn btn-sm', type: 'button', text: 'Export PNG',
                      onclick: () => exportPng(svg, data) }));

      const layout = h('div', { class: 'rack-layout' },
        h('div', { class: 'rack-plate' },
          tools,
          h('div', { class: 'rack-svg-wrap' }, svg),
          legend(data),
          h('p', { class: 'card-note',
            text: 'The elevation renders on a fixed dark ground in both themes so the exported drawing '
              + 'matches the screen and every faceplate keeps one verified contrast ratio.' })),
        h('div', { class: 'rack-side' },
          detailPanel(data, state.selected, state),
          findingsCard(data),
          feedCard(data)));

      root.appendChild(layout);
      root.appendChild(h('div', { class: 'grid grid-wide', style: 'margin-top:var(--gap)' },
        zeroUCard(data), thermalCard(data), excludedCard(data)));
      root.appendChild(h('div', { style: 'margin-top:var(--gap)' }, pduCard(data)));
      root.appendChild(h('div', { style: 'margin-top:var(--gap)' }, switchCard(data)));
      root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
        text: `Layout read from ${(data.data_sources || {}).layout_document || 'the design package'} · `
          + `generated ${fmtDateTime(data.generated_at)}` }));

      window.scrollTo({ top: scroll });
    };

    state.select = (assetId) => {
      state.selected = assetId;
      // replaceState keeps the URL deep-linkable without firing hashchange,
      // so selecting a device does not tear down and rebuild the whole view.
      try {
        const hash = assetId ? `#/rack/${encodeURIComponent(assetId)}` : '#/rack';
        window.history.replaceState(null, '', hash);
      } catch {
        /* Older browsers or a file:// origin: the panel still works. */
      }
      render();
      if (assetId) {
        const panel = root.querySelector('.rack-side .card');
        if (panel) panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    };

    const load = async () => {
      const result = await get('/rack');
      if (!result.ok) {
        clear(root);
        root.appendChild(h('div', { class: 'page-head' },
          h('div', null, h('h2', { text: 'Rack elevation' }))));
        root.appendChild(resultProblem(result, 'the rack elevation'));
        return false;
      }
      state.data = result.data;
      render();
      return true;
    };

    clear(root).appendChild(h('p', { class: 'loading', text: 'Loading the rack elevation…' }));
    await load();

    return {
      async refresh() { await load(); },
      destroy() {},
    };
  },
};
