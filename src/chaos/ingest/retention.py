"""Historian retention and downsampling (SDD 16.4).

Policy implemented here::

    raw high-frequency telemetry   90 days   (settings.historian_raw_retention_days)
    downsampled 1-minute rows      2 years
    hourly / daily rollups         indefinite

**Where aggregates live.** Aggregated rows stay in ``telemetry_samples``
alongside raw ones, marked by their ``source`` column::

    source = "downsample:1min"

The alternative -- a separate table per resolution -- was rejected because every
reader (API history endpoint, EMS trend inputs, Grafana) would then need to know
which table to query for a given time range, and that knowledge would drift.
Keeping one series with a resolution marker means a history query spanning the
retention boundary returns one continuous series, and callers that care can
filter on ``source``. The ``downsample:`` prefix is reserved: ingest never
writes it (field sources are device/gateway names, and simulated data uses
``api.simulate``), so raw and derived rows can always be told apart.

Aggregation rules per (point, bucket):

* numeric  -> arithmetic mean of the bucket (``value_numeric``)
* boolean  -> last value in the bucket (a mean of booleans is meaningless)
* text/json-> last value in the bucket
* quality  -> the *worst* quality in the bucket, so downsampling can never
  launder a bad reading into a good one
* unit     -> carried through unchanged (units are canonical per point)

Nothing here is scheduled. The caller decides when to run it, which keeps
retention an explicit, auditable operation rather than a background surprise.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from chaos.config import Settings, get_settings
from chaos.ingest.writer import as_utc
from chaos.models.telemetry import TelemetrySample

logger = logging.getLogger(__name__)

#: Marker written into ``telemetry_samples.source`` for aggregated rows.
DOWNSAMPLE_SOURCE_PREFIX = "downsample:"

#: Quality codes ordered best -> worst (SDD 26.6). Used to pick the worst
#: quality in a bucket; unknown codes sort worst so they can never be ignored.
_QUALITY_RANK = {
    "good": 0,
    "calculated": 1,
    "substituted": 2,
    "uncertain": 3,
    "maintenance": 4,
    "stale": 5,
    "bad": 6,
}

_INTERVAL_RE = re.compile(
    r"^\s*(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hour|hours|d|day|days)\s*$"
)
_INTERVAL_UNITS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}


def interval_seconds(interval: str) -> int:
    """Parse ``"1min"``, ``"5m"``, ``"1h"``, ``"1d"`` into seconds."""
    match = _INTERVAL_RE.match(interval.lower())
    if not match:
        raise ValueError(f"Unsupported downsample interval: {interval!r}")
    return int(match.group(1)) * _INTERVAL_UNITS[match.group(2)]


def downsample_source(interval: str) -> str:
    return f"{DOWNSAMPLE_SOURCE_PREFIX}{interval}"


def bucket_start(ts: dt.datetime, seconds: int) -> dt.datetime:
    """Floor ``ts`` to the start of its bucket, in UTC."""
    aware = as_utc(ts)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    offset = int((aware - epoch).total_seconds())
    return epoch + dt.timedelta(seconds=offset - (offset % seconds))


def downsample(
    session: Session,
    since: dt.datetime,
    until: dt.datetime,
    interval: str = "1min",
) -> dict[str, Any]:
    """Aggregate raw samples in ``[since, until)`` into ``interval`` buckets.

    Raw rows are left in place; deleting them is :func:`apply_retention`'s job.
    Re-running over the same window replaces the aggregates it produced before,
    so a partially completed run is safe to repeat. Does not commit.
    """
    seconds = interval_seconds(interval)
    source = downsample_source(interval)
    since_utc, until_utc = as_utc(since), as_utc(until)

    rows = (
        session.execute(
            select(TelemetrySample)
            .where(
                TelemetrySample.ts >= since_utc,
                TelemetrySample.ts < until_utc,
                _raw_clause(),
            )
            .order_by(TelemetrySample.ts)
        )
        .scalars()
        .all()
    )

    buckets: dict[tuple[str, dt.datetime], list[TelemetrySample]] = defaultdict(list)
    for row in rows:
        buckets[(row.point_id, bucket_start(row.ts, seconds))].append(row)

    if buckets:
        # Replace only the aggregates this run regenerates. A blanket delete of
        # the window would destroy aggregates whose raw rows have already been
        # retention-deleted, which is the one thing this module must never do.
        existing = session.execute(
            select(TelemetrySample.id, TelemetrySample.point_id, TelemetrySample.ts).where(
                TelemetrySample.source == source,
                TelemetrySample.ts >= since_utc,
                TelemetrySample.ts < until_utc,
            )
        ).all()
        superseded = [row_id for row_id, point_id, ts in existing if (point_id, as_utc(ts)) in buckets]
        if superseded:
            session.execute(delete(TelemetrySample).where(TelemetrySample.id.in_(superseded)))

    written = 0
    for (point_id, bucket), samples in sorted(buckets.items(), key=lambda item: (item[0][1], item[0][0])):
        session.add(_aggregate(point_id, bucket, samples, source))
        written += 1

    session.flush()
    summary = {
        "interval": interval,
        "since": since_utc.isoformat(),
        "until": until_utc.isoformat(),
        "source": source,
        "raw_samples": len(rows),
        "aggregates_written": written,
        "points": len({point_id for point_id, _ in buckets}),
    }
    logger.info("Downsampled %d raw sample(s) into %d %s row(s)", len(rows), written, interval)
    return summary


def apply_retention(
    session: Session,
    now: dt.datetime,
    settings: Settings | None = None,
    *,
    interval: str = "1min",
) -> dict[str, Any]:
    """Enforce raw retention: downsample first, then delete (SDD 16.4).

    Raw rows are only ever deleted *after* the aggregates covering them exist,
    so an interrupted run loses resolution at worst, never history. Does not
    commit -- the caller owns the transaction boundary.
    """
    settings = settings or get_settings()
    now_utc = as_utc(now)
    cutoff = now_utc - dt.timedelta(days=settings.historian_raw_retention_days)

    oldest = session.execute(
        select(TelemetrySample.ts)
        .where(TelemetrySample.ts < cutoff, _raw_clause())
        .order_by(TelemetrySample.ts)
        .limit(1)
    ).scalar_one_or_none()

    if oldest is None:
        return {
            "cutoff": cutoff.isoformat(),
            "retention_days": settings.historian_raw_retention_days,
            "interval": interval,
            "raw_samples": 0,
            "aggregates_written": 0,
            "points": 0,
            "raw_deleted": 0,
        }

    summary = downsample(session, as_utc(oldest), cutoff, interval=interval)

    deleted = session.execute(
        delete(TelemetrySample).where(TelemetrySample.ts < cutoff, _raw_clause())
    ).rowcount
    session.flush()

    summary.update(
        {
            "cutoff": cutoff.isoformat(),
            "retention_days": settings.historian_raw_retention_days,
            "raw_deleted": int(deleted or 0),
        }
    )
    logger.info(
        "Retention: %d raw sample(s) older than %s deleted, %d aggregate(s) kept",
        summary["raw_deleted"],
        cutoff.isoformat(),
        summary["aggregates_written"],
    )
    return summary


def _raw_clause():
    """Match rows this module treats as raw telemetry.

    ``source`` is nullable -- a device may publish an envelope without naming
    itself -- so a plain ``NOT LIKE`` would silently exempt those rows from
    retention and let the historian grow forever.
    """
    return or_(
        TelemetrySample.source.is_(None),
        TelemetrySample.source.not_like(f"{DOWNSAMPLE_SOURCE_PREFIX}%"),
    )


def _aggregate(
    point_id: str, bucket: dt.datetime, samples: list[TelemetrySample], source: str
) -> TelemetrySample:
    ordered = sorted(samples, key=lambda s: as_utc(s.ts))
    last = ordered[-1]
    numerics = [s.value_numeric for s in ordered if s.value_numeric is not None]
    worst = max(ordered, key=lambda s: _QUALITY_RANK.get(s.quality, len(_QUALITY_RANK)))
    return TelemetrySample(
        point_id=point_id,
        asset_id=last.asset_id,
        point_name=last.point_name,
        ts=bucket,
        value_numeric=(sum(numerics) / len(numerics)) if numerics else None,
        value_text=last.value_text,
        value_bool=last.value_bool,
        unit=last.unit,
        quality=worst.quality,
        source=source,
    )
