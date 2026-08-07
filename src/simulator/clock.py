"""Virtual time.

Nothing inside a component may call :func:`datetime.now` or :func:`time.time`.
Every model is a pure function of the ``(now, dt_s, context)`` it is handed, so
a 24-hour day can be simulated in milliseconds and replayed exactly.

Two concerns are deliberately separated:

* :class:`SimClock` -- *what time it is in the simulation*. Advancing it is
  arithmetic; it never sleeps.
* :class:`Pacer` -- *how fast wall-clock time is allowed to pass*. Only the
  pacer sleeps, and its sleep function is injectable so tests never block.

``speed`` is simulated seconds per wall-clock second: ``speed=1`` is real time,
``speed=3600`` runs an hour per second, ``speed=inf`` runs flat out.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

UTC = dt.timezone.utc

#: Default simulated start instant: a summer morning, midnight local.
DEFAULT_START = dt.datetime(2026, 6, 21, 5, 0, 0, tzinfo=UTC)

#: The site is in America/Toronto. A fixed offset is used rather than a tz
#: database lookup so the simulator has no data dependency and no DST
#: discontinuity mid-run; the solar model derives true solar time from
#: longitude independently, so this offset only shapes human load profiles.
DEFAULT_UTC_OFFSET_H = -5.0


@dataclass
class SimClock:
    """Monotonic virtual clock.

    ``advance`` is the only way time moves, which makes every run replayable
    from ``(start, dt_s, step count)``.
    """

    start: dt.datetime = DEFAULT_START
    utc_offset_h: float = DEFAULT_UTC_OFFSET_H
    elapsed_s: float = 0.0
    steps: int = 0

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=UTC)
        else:
            self.start = self.start.astimezone(UTC)

    # -- time -----------------------------------------------------------
    def now(self) -> dt.datetime:
        """Current simulated instant (timezone-aware, UTC)."""
        return self.start + dt.timedelta(seconds=self.elapsed_s)

    def advance(self, dt_s: float) -> dt.datetime:
        """Move the simulation forward. Never sleeps, never goes backwards."""
        if dt_s < 0:
            raise ValueError(f"dt_s must be non-negative, got {dt_s}")
        self.elapsed_s += dt_s
        self.steps += 1
        return self.now()

    def reset(self) -> None:
        self.elapsed_s = 0.0
        self.steps = 0

    # -- calendar helpers used by the physical models --------------------
    def local_time(self) -> dt.datetime:
        """Naive local civil time, used only for human/diurnal load profiles."""
        return (self.now() + dt.timedelta(hours=self.utc_offset_h)).replace(tzinfo=None)

    def hour_of_day(self) -> float:
        """Local hour as a float in ``[0, 24)``."""
        local = self.local_time()
        return local.hour + local.minute / 60.0 + local.second / 3600.0

    def day_of_year(self) -> int:
        return self.local_time().timetuple().tm_yday

    def local_day_index(self) -> int:
        """Ordinal local day, used to reset daily energy counters at midnight."""
        return self.local_time().toordinal()


def hour_of_day(now: dt.datetime, utc_offset_h: float = DEFAULT_UTC_OFFSET_H) -> float:
    """Local hour for an arbitrary instant (components receive ``now``, not the clock)."""
    local = now.astimezone(UTC) + dt.timedelta(hours=utc_offset_h)
    return local.hour + local.minute / 60.0 + local.second / 3600.0


def day_of_year(now: dt.datetime, utc_offset_h: float = DEFAULT_UTC_OFFSET_H) -> int:
    local = now.astimezone(UTC) + dt.timedelta(hours=utc_offset_h)
    return local.timetuple().tm_yday


def local_day_index(now: dt.datetime, utc_offset_h: float = DEFAULT_UTC_OFFSET_H) -> int:
    local = now.astimezone(UTC) + dt.timedelta(hours=utc_offset_h)
    return local.toordinal()


# --- pacing ---------------------------------------------------------------


class Pacer(Protocol):
    """Controls how much wall-clock time a simulated step is allowed to take."""

    def pace(self, dt_s: float) -> None: ...

    def reset(self) -> None: ...


@dataclass
class SteppedPacer:
    """No pacing at all: used by tests, EMS prototyping and offline runs."""

    def pace(self, dt_s: float) -> None:  # noqa: D102 - protocol impl
        return None

    def reset(self) -> None:  # noqa: D102 - protocol impl
        return None


@dataclass
class RealTimePacer:
    """Sleeps so that ``dt_s`` of simulated time takes ``dt_s / speed`` of real time.

    Both the sleep and the monotonic source are injectable, so a test can drive
    the CLI's run loop without ever blocking. Sleep debt is not accumulated:
    if a step overruns its budget the pacer simply does not sleep, which keeps
    a slow host from spiralling.
    """

    speed: float = 1.0
    sleeper: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _last: float | None = field(default=None, repr=False)
    slept_s: float = 0.0

    def pace(self, dt_s: float) -> None:  # noqa: D102 - protocol impl
        if self.speed <= 0 or math.isinf(self.speed):
            return
        budget = dt_s / self.speed
        now = self.monotonic()
        if self._last is None:
            spent = 0.0
        else:
            spent = now - self._last
        remaining = budget - spent
        if remaining > 0:
            self.sleeper(remaining)
            self.slept_s += remaining
            self._last = self.monotonic()
        else:
            self._last = now

    def reset(self) -> None:  # noqa: D102 - protocol impl
        self._last = None
        self.slept_s = 0.0


def build_pacer(
    speed: float | None,
    sleeper: Callable[[float], None] | None = None,
) -> Pacer:
    """Return a pacer for ``speed`` (``None``/``<=0``/``inf`` means "flat out")."""
    if speed is None or speed <= 0 or math.isinf(speed):
        return SteppedPacer()
    pacer = RealTimePacer(speed=speed)
    if sleeper is not None:
        pacer.sleeper = sleeper
    return pacer


# --- duration parsing -----------------------------------------------------

_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h|d)?", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, None: 1.0}


def parse_duration(value: str | float | int) -> float:
    """Parse ``"24h"``, ``"90m"``, ``"1d12h"`` or a bare number of seconds.

    Used by the CLI and by scenario definitions so durations read the way an
    operator would write them in a commissioning test record.
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty duration")
    total = 0.0
    position = 0
    matched = False
    while position < len(text):
        match = _DURATION_RE.match(text, position)
        if not match or match.end() == position:
            raise ValueError(f"cannot parse duration {value!r}")
        total += float(match.group("value")) * _UNIT_SECONDS[match.group("unit")]
        position = match.end()
        matched = True
    if not matched:
        raise ValueError(f"cannot parse duration {value!r}")
    return total


def format_duration(seconds: float) -> str:
    """Human-readable duration for CLI summaries."""
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if sec and not days:
        parts.append(f"{sec}s")
    return "".join(parts) or "0s"
