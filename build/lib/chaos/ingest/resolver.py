"""Topic -> identity resolution, backed by the registry (SDD 26.2).

    "The canonical asset registry maps the MQTT topic to ``asset_id`` and
    ``point_id``; parsing a topic is not the sole identity mechanism."

The resolver therefore builds an in-memory index from the database and never
infers identity from topic text. Two sources feed the index, in priority order:

1. **Explicit bindings** -- ``point_bindings.mqtt_topic``. A vendor gateway may
   publish on any topic it likes; the binding row is what makes that topic mean
   a canonical point. Explicit bindings always win.
2. **Derived topics** -- for every registered ``Point`` the deterministic
   projection ``topics.telemetry_topic(asset_id, point_name)`` is registered as
   a fallback. Most of the v0.3 register is still ``binding_status: tbd`` with no
   explicit topic, so without this fallback the platform could not be exercised
   before field commissioning.

A third index maps the asset prefix (``<base>/<domain>/<location>/<asset>``) to
an ``asset_id``, which is how availability, event and alarm topics -- none of
which name a point directly -- are attributed to a registered asset.

Unknown topics resolve to ``None``; the caller dead-letters them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos import topics as topic_utils
from chaos.models.registry import Asset, Point, PointBinding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolverStats:
    """What the last :meth:`TopicResolver.refresh` produced."""

    points: int = 0
    assets: int = 0
    explicit_bindings: int = 0
    derived_topics: int = 0
    unprojectable_points: int = 0
    unprojectable_assets: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "points": self.points,
            "assets": self.assets,
            "explicit_bindings": self.explicit_bindings,
            "derived_topics": self.derived_topics,
            "unprojectable_points": self.unprojectable_points,
            "unprojectable_assets": self.unprojectable_assets,
        }


def normalise_topic(topic: str) -> str:
    """Trim whitespace and stray separators so cache keys compare equal."""
    return topic.strip().strip("/")


class TopicResolver:
    """Resolves inbound topics to registry identities.

    The cache is rebuilt wholesale by :meth:`refresh`; the registry loader is a
    batch operation, so incremental invalidation would add risk for no gain.
    """

    def __init__(self, base: str = topic_utils.DEFAULT_BASE) -> None:
        self.base = base
        self._topic_to_point: dict[str, str] = {}
        self._point_to_topic: dict[str, str] = {}
        self._prefix_to_asset: dict[str, str] = {}
        self._known_points: set[str] = set()
        self._stats = ResolverStats()

    # -- construction ----------------------------------------------------

    def refresh(self, session: Session) -> ResolverStats:
        """Rebuild the cache from the registry. Safe to call at any time."""
        topic_to_point: dict[str, str] = {}
        point_to_topic: dict[str, str] = {}
        prefix_to_asset: dict[str, str] = {}
        known_points: set[str] = set()
        unprojectable_points = 0
        unprojectable_assets = 0

        asset_ids = session.execute(select(Asset.asset_id)).scalars().all()
        for asset_id in asset_ids:
            try:
                prefix = topic_utils.asset_prefix(asset_id, self.base)
            except topic_utils.TopicError:
                # An asset ID that does not match the canonical four-part form
                # cannot be projected onto MQTT. It stays addressable through
                # the API; only its topic routing is unavailable.
                unprojectable_assets += 1
                logger.debug("Asset %s has no MQTT projection", asset_id)
                continue
            prefix_to_asset[normalise_topic(prefix)] = asset_id

        point_rows = session.execute(select(Point.point_id, Point.asset_id, Point.point_name)).all()
        for point_id, asset_id, point_name in point_rows:
            known_points.add(point_id)
            try:
                derived = normalise_topic(topic_utils.telemetry_topic(asset_id, point_name, self.base))
            except topic_utils.TopicError:
                unprojectable_points += 1
                logger.debug("Point %s has no MQTT projection", point_id)
                continue
            point_to_topic[point_id] = derived
            # First registration wins so a duplicate projection cannot silently
            # steal another point's topic; explicit bindings override below.
            topic_to_point.setdefault(derived, point_id)
        derived_topics = len(topic_to_point)

        binding_rows = session.execute(
            select(PointBinding.point_id, PointBinding.mqtt_topic).where(PointBinding.mqtt_topic.is_not(None))
        ).all()
        explicit = 0
        for point_id, mqtt_topic in binding_rows:
            if point_id not in known_points:
                logger.warning("Binding for unknown point %s ignored by resolver", point_id)
                continue
            topic = normalise_topic(mqtt_topic)
            if not topic:
                continue
            topic_to_point[topic] = point_id
            point_to_topic[point_id] = topic
            explicit += 1

        self._topic_to_point = topic_to_point
        self._point_to_topic = point_to_topic
        self._prefix_to_asset = prefix_to_asset
        self._known_points = known_points
        self._stats = ResolverStats(
            points=len(known_points),
            assets=len(prefix_to_asset),
            explicit_bindings=explicit,
            derived_topics=derived_topics,
            unprojectable_points=unprojectable_points,
            unprojectable_assets=unprojectable_assets,
        )
        logger.info(
            "Topic resolver refreshed: %d points (%d explicit bindings), %d assets",
            self._stats.points,
            self._stats.explicit_bindings,
            self._stats.assets,
        )
        return self._stats

    # -- lookups ---------------------------------------------------------

    def resolve(self, topic: str) -> str | None:
        """Return the ``point_id`` for a telemetry topic, or ``None``."""
        return self._topic_to_point.get(normalise_topic(topic))

    def resolve_availability(self, topic: str) -> str | None:
        """Return the ``asset_id`` behind ``.../availability``, or ``None``."""
        normalised = normalise_topic(topic)
        suffix = f"/{topic_utils.KIND_AVAILABILITY}"
        if not normalised.endswith(suffix):
            return None
        return self._prefix_to_asset.get(normalised[: -len(suffix)])

    def resolve_asset(self, topic: str) -> str | None:
        """Return the ``asset_id`` owning any topic under a known asset prefix.

        Used for event, alarm and availability topics, which identify an asset
        but not a point.
        """
        segments = normalise_topic(topic).split("/")
        if len(segments) < 4:
            return None
        return self._prefix_to_asset.get("/".join(segments[:4]))

    def topic_for_point(self, point_id: str) -> str | None:
        """Reverse lookup: the topic this platform expects a point to arrive on."""
        return self._point_to_topic.get(point_id)

    def topics(self) -> tuple[str, ...]:
        """Every topic the registry can currently resolve."""
        return tuple(self._topic_to_point)

    def knows_point(self, point_id: str) -> bool:
        return point_id in self._known_points

    def knows_asset(self, asset_id: str) -> bool:
        return asset_id in set(self._prefix_to_asset.values())

    # -- introspection ---------------------------------------------------

    @property
    def stats(self) -> ResolverStats:
        return self._stats

    def __len__(self) -> int:
        return len(self._topic_to_point)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<TopicResolver base={self.base!r} topics={len(self._topic_to_point)}>"
