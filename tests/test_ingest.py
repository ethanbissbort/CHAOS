"""Telemetry ingest tests.

Nothing here sleeps. ``InMemoryBus`` delivers synchronously on the publishing
thread, and every time-dependent code path takes ``now`` as an argument, so
staleness, out-of-order rejection and retention are all exercised by moving a
timestamp rather than by waiting.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json

import pytest

from homestead_twin import topics
from homestead_twin.envelope import AvailabilityEnvelope, EventEnvelope, TelemetryEnvelope
from homestead_twin.ingest.resolver import TopicResolver
from homestead_twin.ingest.retention import (
    DOWNSAMPLE_SOURCE_PREFIX,
    apply_retention,
    bucket_start,
    downsample,
    downsample_source,
    interval_seconds,
)
from homestead_twin.ingest.service import IngestService
from homestead_twin.ingest.writer import (
    ENUM_VIOLATION,
    OUT_OF_ORDER,
    OUT_OF_PHYSICAL_RANGE,
    SEQUENCE_GAP,
    UNIT_MISMATCH,
    UNKNOWN_POINT,
    VALUE_TYPE_MISMATCH,
    TelemetryWriter,
    as_utc,
)
from homestead_twin.models.registry import (
    Asset,
    AssetClass,
    Point,
    PointBinding,
    PointDefinition,
    PointSampleIndex,
)
from homestead_twin.models.telemetry import CurrentState, IngestDeadLetter, TelemetrySample

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)

BATTERY = "energy.battery_bank.power_container.01"
CISTERN = "water.cistern.orchard.01"
GATE = "security.gate.perimeter.01"


def seconds(n: float) -> dt.timedelta:
    return dt.timedelta(seconds=n)


# ---------------------------------------------------------------------------
# Minimal registry builder -- independent of the register loader
# ---------------------------------------------------------------------------


class RegistryBuilder:
    """Creates just enough registry rows to exercise ingest."""

    def __init__(self, session):
        self.session = session

    def asset(self, asset_id: str) -> Asset:
        existing = self.session.get(Asset, asset_id)
        if existing is not None:
            return existing
        parts = topics.split_asset_id(asset_id)
        if self.session.get(AssetClass, parts.asset_class) is None:
            self.session.add(AssetClass(name=parts.asset_class, allowed_domains=[parts.domain]))
        asset = Asset(
            asset_id=asset_id,
            domain=parts.domain,
            asset_class=parts.asset_class,
            name=asset_id,
            status="commissioned",
        )
        self.session.add(asset)
        self.session.flush()
        return asset

    def point(
        self,
        asset_id: str,
        point_name: str,
        *,
        data_type: str = "float",
        point_class: str = "AI",
        unit: str | None = None,
        enum_values: list[str] | None = None,
        limits: dict | None = None,
        historian_policy: str | None = None,
        stale_after_s: int | None = None,
        mqtt_topic: str | None = None,
        quality_policy: str | None = "reject_invalid",
        binding: bool = True,
    ) -> Point:
        self.asset(asset_id)
        if self.session.get(PointDefinition, point_name) is None:
            self.session.add(
                PointDefinition(
                    name=point_name,
                    default_class=point_class,
                    allowed_classes=[point_class],
                    data_type=data_type,
                    unit=unit,
                    enum_values=enum_values,
                )
            )
        point_id = topics.point_id(asset_id, point_name)
        point = Point(
            point_id=point_id,
            asset_id=asset_id,
            point_name=point_name,
            point_class=point_class,
            data_type=data_type,
            unit=unit,
            enum_values=enum_values,
            limits=limits or {},
            historian_policy=historian_policy,
            stale_after_s=stale_after_s,
        )
        self.session.add(point)
        if binding:
            self.session.add(
                PointBinding(
                    point_id=point_id,
                    asset_id=asset_id,
                    point_name=point_name,
                    binding_status="commissioned" if mqtt_topic else "tbd",
                    mqtt_topic=mqtt_topic,
                    historian_policy=historian_policy,
                    quality_policy=quality_policy,
                )
            )
        self.session.commit()
        return point


@pytest.fixture()
def registry(db_session) -> RegistryBuilder:
    return RegistryBuilder(db_session)


@pytest.fixture()
def soc_point(registry) -> Point:
    return registry.point(BATTERY, "soc_pct", data_type="float", unit="%", stale_after_s=20)


@pytest.fixture()
def writer(settings, session_factory) -> TelemetryWriter:
    return TelemetryWriter(settings, session_factory=session_factory)


def envelope(point: Point | str, value, **kwargs) -> TelemetryEnvelope:
    point_id = point if isinstance(point, str) else point.point_id
    asset_id, point_name = topics.split_point_id(point_id)
    kwargs.setdefault("ts", T0)
    kwargs.setdefault("source", "bms.master")
    return TelemetryEnvelope(asset_id=asset_id, point=point_name, value=value, **kwargs)


def refresh_view(session) -> None:
    """Drop this session's read snapshot so another session's commit is visible."""
    session.rollback()
    session.expire_all()


def samples_for(session, point_id: str) -> list[TelemetrySample]:
    return (
        session.query(TelemetrySample)
        .filter(TelemetrySample.point_id == point_id)
        .order_by(TelemetrySample.ts)
        .all()
    )


# ---------------------------------------------------------------------------
# Resolver -- registry-first identity (SDD 26.2)
# ---------------------------------------------------------------------------


def test_resolver_derives_topic_for_point_without_explicit_binding(db_session, soc_point):
    resolver = TopicResolver()
    stats = resolver.refresh(db_session)

    derived = topics.telemetry_topic(BATTERY, "soc_pct")
    assert derived == "homestead/energy/power_container/battery_bank_01/soc_pct"
    assert resolver.resolve(derived) == soc_point.point_id
    assert stats.derived_topics == 1
    assert stats.explicit_bindings == 0
    assert resolver.topic_for_point(soc_point.point_id) == derived


def test_resolver_prefers_explicit_binding_topic(db_session, registry):
    point = registry.point(
        CISTERN, "level_pct", unit="%", mqtt_topic="vendor/gateway7/tank/level"
    )
    resolver = TopicResolver()
    stats = resolver.refresh(db_session)

    assert resolver.resolve("vendor/gateway7/tank/level") == point.point_id
    assert stats.explicit_bindings == 1
    # The conventional projection is still resolvable for the same point.
    assert resolver.resolve(topics.telemetry_topic(CISTERN, "level_pct")) == point.point_id


def test_resolver_returns_none_for_unknown_topic(db_session, soc_point):
    resolver = TopicResolver()
    resolver.refresh(db_session)

    assert resolver.resolve("homestead/energy/power_container/battery_bank_09/soc_pct") is None
    assert resolver.resolve("totally/unrelated/topic") is None


def test_resolver_maps_availability_and_asset_prefix(db_session, soc_point):
    resolver = TopicResolver()
    resolver.refresh(db_session)

    availability = topics.availability_topic(BATTERY)
    assert resolver.resolve_availability(availability) == BATTERY
    assert resolver.resolve_asset(topics.event_topic(BATTERY, "opened")) == BATTERY
    assert resolver.resolve_availability("homestead/x/y/z/availability") is None


def test_resolver_refresh_picks_up_new_points(db_session, registry, soc_point):
    resolver = TopicResolver()
    resolver.refresh(db_session)
    assert len(resolver) == 1

    registry.point(CISTERN, "level_pct", unit="%")
    resolver.refresh(db_session)

    assert len(resolver) == 2
    assert resolver.resolve(topics.telemetry_topic(CISTERN, "level_pct")) is not None


# ---------------------------------------------------------------------------
# Writer -- current state + historian
# ---------------------------------------------------------------------------


def test_happy_path_writes_current_state_and_historian(db_session, writer, soc_point):
    result = writer.apply(db_session, envelope(soc_point, 73.4, sequence=1), now=T0)
    db_session.commit()

    assert result.ok and result.current_state_updated and result.historised
    state = db_session.get(CurrentState, soc_point.point_id)
    assert state.value_numeric == pytest.approx(73.4)
    assert state.value_bool is None and state.value_text is None
    assert (state.unit, state.quality, state.source) == ("%", "good", "bms.master")
    assert state.stale_after_s == 20  # from the point, not the platform default
    assert state.received_at is not None

    stored = samples_for(db_session, soc_point.point_id)
    assert len(stored) == 1
    assert stored[0].value_numeric == pytest.approx(73.4)

    index = db_session.get(PointSampleIndex, soc_point.point_id)
    assert index.sample_count == 1
    assert index.series_key == soc_point.point_id


def test_stale_after_falls_back_to_settings_default(db_session, writer, registry, settings):
    point = registry.point(CISTERN, "level_pct", unit="%")
    writer.apply(db_session, envelope(point, 40.0), now=T0)
    db_session.commit()

    state = db_session.get(CurrentState, point.point_id)
    assert state.stale_after_s == settings.default_stale_after_s


def test_out_of_order_sample_is_historised_but_does_not_rewind_current_state(
    db_session, writer, soc_point
):
    writer.apply(db_session, envelope(soc_point, 73.4, ts=T0, sequence=1), now=T0)
    late = writer.apply(
        db_session, envelope(soc_point, 10.0, ts=T0 - seconds(30), sequence=2), now=T0
    )
    db_session.commit()

    assert late.out_of_order and late.historised
    assert not late.current_state_updated
    assert late.has(OUT_OF_ORDER)
    # Ordering warnings are counters, not dead letters.
    assert late.dead_letter_reasons == []
    assert writer.counters["out_of_order"] == 1

    state = db_session.get(CurrentState, soc_point.point_id)
    assert state.value_numeric == pytest.approx(73.4)
    assert len(samples_for(db_session, soc_point.point_id)) == 2


def test_sequence_gap_is_counted_as_a_warning(db_session, writer, soc_point):
    writer.apply(db_session, envelope(soc_point, 73.4, ts=T0, sequence=100), now=T0)
    result = writer.apply(
        db_session, envelope(soc_point, 73.9, ts=T0 + seconds(5), sequence=104), now=T0
    )
    db_session.commit()

    assert result.sequence_gap == 3
    assert result.has(SEQUENCE_GAP)
    assert result.dead_letter_reasons == []
    assert writer.counters["sequence_gaps"] == 1
    assert db_session.get(CurrentState, soc_point.point_id).sequence == 104


def test_unit_mismatch_is_a_data_integrity_fault(db_session, writer, soc_point):
    result = writer.apply(db_session, envelope(soc_point, 73.4, unit="kWh"), now=T0)
    db_session.commit()

    assert result.quality == "bad"
    assert result.has(UNIT_MISMATCH)
    assert any("kWh" in reason for reason in result.dead_letter_reasons)
    state = db_session.get(CurrentState, soc_point.point_id)
    assert state.quality == "bad"
    assert state.unit == "%"  # the dictionary unit is canonical
    # ``reject_invalid`` keeps the faulted number out of current state.
    assert state.value_numeric is None
    assert writer.counters["unit_mismatches"] == 1


def test_matching_unit_is_accepted(db_session, writer, soc_point):
    result = writer.apply(db_session, envelope(soc_point, 73.4, unit="%"), now=T0)
    assert result.ok and result.quality == "good"


def test_non_numeric_value_on_analog_point_is_rejected(db_session, writer, soc_point):
    writer.apply(db_session, envelope(soc_point, 73.4), now=T0)
    result = writer.apply(
        db_session, envelope(soc_point, "seventy three", ts=T0 + seconds(5)), now=T0
    )
    db_session.commit()

    assert result.quality == "bad"
    assert result.has(VALUE_TYPE_MISMATCH)
    assert result.dead_letter_reasons
    state = db_session.get(CurrentState, soc_point.point_id)
    # Last good value retained, quality tells the operator not to trust it.
    assert state.value_numeric == pytest.approx(73.4)
    assert state.quality == "bad"
    assert state.value_text is None
    assert writer.counters["type_mismatches"] == 1


def test_boolean_is_not_silently_coerced_into_a_numeric_point(db_session, writer, soc_point):
    result = writer.apply(db_session, envelope(soc_point, True), now=T0)
    assert result.has(VALUE_TYPE_MISMATCH)
    assert db_session.get(CurrentState, soc_point.point_id).value_numeric is None


def test_enum_violation_marks_quality_bad(db_session, writer, registry):
    point = registry.point(
        BATTERY,
        "mode_actual",
        data_type="enum",
        point_class="DI",
        enum_values=["off", "manual", "automatic"],
    )
    good = writer.apply(db_session, envelope(point, "automatic"), now=T0)
    bad = writer.apply(db_session, envelope(point, "turbo", ts=T0 + seconds(5)), now=T0)
    db_session.commit()

    assert good.ok
    assert bad.quality == "bad" and bad.has(ENUM_VIOLATION)
    assert any("turbo" in reason for reason in bad.dead_letter_reasons)
    state = db_session.get(CurrentState, point.point_id)
    assert state.value_text == "automatic"
    assert state.quality == "bad"
    assert writer.counters["enum_violations"] == 1


def test_values_route_to_the_column_their_data_type_names(db_session, writer, registry):
    flag = registry.point(BATTERY, "fault_active", data_type="boolean", point_class="DI")
    doc = registry.point(GATE, "motion_event", data_type="object", point_class="EVENT")
    text = registry.point(BATTERY, "fault_code", data_type="string", point_class="TEXT")

    writer.apply(db_session, envelope(flag, False), now=T0)
    writer.apply(db_session, envelope(doc, {"camera": "cam_01", "score": 0.92}), now=T0)
    writer.apply(db_session, envelope(text, "E-0412"), now=T0)
    db_session.commit()

    assert db_session.get(CurrentState, flag.point_id).value_bool is False
    assert db_session.get(CurrentState, flag.point_id).value_numeric is None
    assert db_session.get(CurrentState, doc.point_id).value_json == {
        "camera": "cam_01",
        "score": 0.92,
    }
    assert db_session.get(CurrentState, text.point_id).value_text == "E-0412"
    # The relational historian has no JSON column: the document is kept as text.
    assert json.loads(samples_for(db_session, doc.point_id)[0].value_text)["camera"] == "cam_01"


def test_value_outside_physical_range_is_bad(db_session, writer, registry):
    point = registry.point(
        CISTERN, "level_pct", unit="%", limits={"minimum_physical": 0, "maximum_physical": 100}
    )
    result = writer.apply(db_session, envelope(point, 137.5), now=T0)
    db_session.commit()

    assert result.quality == "bad" and result.has(OUT_OF_PHYSICAL_RANGE)
    # ``reject_invalid`` (SDD 29) keeps the impossible reading out of current state.
    assert db_session.get(CurrentState, point.point_id).value_numeric is None
    assert writer.counters["range_violations"] == 1


def test_unknown_point_is_reported_not_written(db_session, writer):
    result = writer.apply(db_session, envelope("energy.inverter.power_container.09/power_kw", 5.0))

    assert not result.known_point
    assert result.has(UNKNOWN_POINT)
    assert result.dead_letter_reasons
    assert writer.counters["unknown_points"] == 1


def test_historian_policy_none_keeps_current_state_only(db_session, writer, registry):
    point = registry.point(BATTERY, "firmware_version", data_type="string", historian_policy="none")
    result = writer.apply(db_session, envelope(point, "1.4.2"), now=T0)
    db_session.commit()

    assert result.current_state_updated and not result.historised
    assert samples_for(db_session, point.point_id) == []
    assert db_session.get(PointSampleIndex, point.point_id) is None


def test_event_on_change_policy_skips_unchanged_values(db_session, writer, registry):
    point = registry.point(
        BATTERY, "fault_active", data_type="boolean", historian_policy="event_on_change"
    )
    first = writer.apply(db_session, envelope(point, False, ts=T0), now=T0)
    same = writer.apply(db_session, envelope(point, False, ts=T0 + seconds(5)), now=T0)
    changed = writer.apply(db_session, envelope(point, True, ts=T0 + seconds(10)), now=T0)
    db_session.commit()

    assert first.historised and changed.historised
    assert not same.historised
    assert len(samples_for(db_session, point.point_id)) == 2


def test_sample_index_tracks_first_last_and_count(db_session, writer, soc_point):
    writer.apply(db_session, envelope(soc_point, 70.0, ts=T0 + seconds(60)), now=T0)
    writer.apply(db_session, envelope(soc_point, 71.0, ts=T0), now=T0)  # out of order
    db_session.commit()

    index = db_session.get(PointSampleIndex, soc_point.point_id)
    assert index.sample_count == 2
    assert as_utc(index.first_sample_at) == T0
    assert as_utc(index.last_sample_at) == T0 + seconds(60)


def test_mark_stale_demotes_only_timed_out_good_points(db_session, writer, registry, soc_point):
    fresh = registry.point(CISTERN, "level_pct", unit="%", stale_after_s=600)
    writer.apply(db_session, envelope(soc_point, 73.4, ts=T0), now=T0)
    writer.apply(db_session, envelope(fresh, 40.0, ts=T0), now=T0)
    db_session.commit()

    assert writer.mark_stale(db_session, T0 + seconds(10)) == 0

    marked = writer.mark_stale(db_session, T0 + seconds(120))
    db_session.commit()

    assert marked == 1
    assert db_session.get(CurrentState, soc_point.point_id).quality == "stale"
    assert db_session.get(CurrentState, fresh.point_id).quality == "good"
    # An already-stale point is never re-counted; the 600 s point holds out.
    assert writer.mark_stale(db_session, T0 + seconds(180)) == 0
    assert writer.mark_stale(db_session, T0 + seconds(1200)) == 1
    assert db_session.get(CurrentState, fresh.point_id).quality == "stale"


def test_mark_stale_ignores_non_good_qualities(db_session, writer, soc_point):
    writer.apply(db_session, envelope(soc_point, 73.4, quality="maintenance", ts=T0), now=T0)
    db_session.commit()

    assert writer.mark_stale(db_session, T0 + seconds(3600)) == 0
    assert db_session.get(CurrentState, soc_point.point_id).quality == "maintenance"


def test_availability_offline_marks_asset_points_stale(db_session, writer, registry, soc_point):
    availability = registry.point(
        BATTERY,
        "availability_state",
        data_type="enum",
        point_class="DI",
        enum_values=["online", "offline", "degraded", "unknown"],
    )
    other_asset_point = registry.point(CISTERN, "level_pct", unit="%")
    writer.apply(db_session, envelope(soc_point, 73.4), now=T0)
    writer.apply(db_session, envelope(other_asset_point, 40.0), now=T0)
    db_session.commit()

    result = writer.apply_availability(
        db_session,
        AvailabilityEnvelope(asset_id=BATTERY, state="offline", ts=T0 + seconds(5)),
        now=T0 + seconds(5),
    )
    db_session.commit()

    assert result.points_marked_stale == 1
    assert result.availability_point_updated
    assert db_session.get(CurrentState, soc_point.point_id).quality == "stale"
    assert db_session.get(CurrentState, availability.point_id).value_text == "offline"
    assert db_session.get(CurrentState, availability.point_id).quality == "good"
    # Another asset's points are untouched.
    assert db_session.get(CurrentState, other_asset_point.point_id).quality == "good"


def test_availability_online_does_not_mark_anything_stale(db_session, writer, registry, soc_point):
    registry.point(BATTERY, "availability_state", data_type="enum", point_class="DI")
    writer.apply(db_session, envelope(soc_point, 73.4), now=T0)
    result = writer.apply_availability(
        db_session, AvailabilityEnvelope(asset_id=BATTERY, state="online", ts=T0), now=T0
    )
    db_session.commit()

    assert result.points_marked_stale == 0
    assert db_session.get(CurrentState, soc_point.point_id).quality == "good"


def test_write_convenience_manages_and_commits_its_own_session(
    session_factory, writer, soc_point, db_session
):
    result = writer.write(envelope(soc_point, 55.5), now=T0)
    assert result.ok

    refresh_view(db_session)
    assert db_session.get(CurrentState, soc_point.point_id).value_numeric == pytest.approx(55.5)


def test_apply_many_batches_into_one_transaction(db_session, writer, soc_point):
    results = writer.apply_many(
        db_session,
        [envelope(soc_point, 70.0 + i, ts=T0 + seconds(i)) for i in range(5)],
        now=T0,
    )
    db_session.commit()

    assert all(r.ok for r in results)
    assert len(samples_for(db_session, soc_point.point_id)) == 5
    assert db_session.get(CurrentState, soc_point.point_id).value_numeric == pytest.approx(74.0)


def test_invalidate_picks_up_registry_changes(db_session, writer, registry, soc_point):
    writer.apply(db_session, envelope(soc_point, 73.4), now=T0)

    point = db_session.get(Point, soc_point.point_id)
    point.unit = "ratio"
    db_session.commit()

    cached = writer.apply(db_session, envelope(soc_point, 0.73, unit="%", ts=T0 + seconds(1)), now=T0)
    assert cached.ok  # still using the cached "%" unit

    writer.invalidate()
    fresh = writer.apply(db_session, envelope(soc_point, 0.73, unit="%", ts=T0 + seconds(2)), now=T0)
    assert fresh.has(UNIT_MISMATCH)


def test_simulated_source_overrides_the_envelope(db_session, writer, soc_point):
    writer.apply(db_session, envelope(soc_point, 73.4), now=T0, source="api.simulate")
    db_session.commit()

    assert db_session.get(CurrentState, soc_point.point_id).source == "api.simulate"


# ---------------------------------------------------------------------------
# Service -- bus subscription, classification, dead-lettering
# ---------------------------------------------------------------------------


@pytest.fixture()
def service(session_factory, bus, settings):
    svc = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    yield svc
    svc.stop()


def started(service: IngestService) -> IngestService:
    service.start()
    return service


def test_service_matches_the_runtime_contract(service):
    from homestead_twin.runtime import BackgroundService

    assert service.name == "ingest"
    assert isinstance(service, BackgroundService)


def test_service_ingests_published_telemetry(db_session, bus, service, soc_point):
    started(service)
    bus.publish(
        topics.telemetry_topic(BATTERY, "soc_pct"),
        envelope(soc_point, 73.4, sequence=7).to_payload(),
    )

    refresh_view(db_session)
    state = db_session.get(CurrentState, soc_point.point_id)
    assert state.value_numeric == pytest.approx(73.4)
    assert state.sequence == 7
    assert len(samples_for(db_session, soc_point.point_id)) == 1
    assert service.counters["messages_received"] == 1
    assert service.counters["messages_written"] == 1
    assert service.counters["dead_lettered"] == 0


def test_service_subscribes_to_explicit_binding_topics_outside_the_base(
    db_session, bus, service, registry
):
    point = registry.point(CISTERN, "level_pct", unit="%", mqtt_topic="vendor/gw7/tank/level")
    started(service)

    bus.publish("vendor/gw7/tank/level", envelope(point, 41.5).to_payload())

    refresh_view(db_session)
    assert db_session.get(CurrentState, point.point_id).value_numeric == pytest.approx(41.5)


def test_unknown_topic_is_dead_lettered(db_session, bus, service, soc_point):
    started(service)
    bus.publish(
        "homestead/energy/power_container/battery_bank_99/soc_pct",
        envelope("energy.battery_bank.power_container.99/soc_pct", 12.0).to_payload(),
    )

    refresh_view(db_session)
    letters = db_session.query(IngestDeadLetter).all()
    assert len(letters) == 1
    assert letters[0].reason.startswith("unresolved_topic")
    assert "battery_bank_99" in letters[0].topic
    assert service.counters["dead_lettered"] == 1
    assert service.counters["unresolved_topics"] == 1


def test_malformed_json_is_dead_lettered(db_session, bus, service, soc_point):
    started(service)
    bus.publish(topics.telemetry_topic(BATTERY, "soc_pct"), b"{not json at all")

    refresh_view(db_session)
    letter = db_session.query(IngestDeadLetter).one()
    assert letter.reason.startswith("malformed_json")
    assert letter.payload == "{not json at all"
    assert db_session.get(CurrentState, soc_point.point_id) is None
    assert service.counters["invalid_payloads"] == 1


def test_envelope_failing_validation_is_dead_lettered(db_session, bus, service, soc_point):
    started(service)
    bus.publish(
        topics.telemetry_topic(BATTERY, "soc_pct"),
        json.dumps({"asset_id": BATTERY, "point": "soc_pct", "value": 1, "quality": "excellent"}),
    )

    refresh_view(db_session)
    letter = db_session.query(IngestDeadLetter).one()
    assert letter.reason.startswith("invalid_telemetry_envelope")
    assert db_session.get(CurrentState, soc_point.point_id) is None


def test_writer_faults_become_dead_letters(db_session, bus, service, soc_point):
    started(service)
    bus.publish(
        topics.telemetry_topic(BATTERY, "soc_pct"),
        envelope(soc_point, 73.4, unit="kWh").to_payload(),
    )

    refresh_view(db_session)
    letter = db_session.query(IngestDeadLetter).one()
    assert letter.reason.startswith("unit_mismatch")
    assert db_session.get(CurrentState, soc_point.point_id).quality == "bad"


def test_command_setpoint_and_ack_topics_are_ignored(db_session, bus, service, registry):
    registry.point(BATTERY, "mode_requested", data_type="enum", point_class="DO")
    started(service)

    payload = json.dumps({"anything": True})
    bus.publish(topics.command_topic(BATTERY, "set_mode"), payload)
    bus.publish(topics.command_ack_topic(BATTERY, "set_mode"), payload)
    bus.publish(topics.setpoint_topic(BATTERY, "power_kw"), payload)

    refresh_view(db_session)
    assert service.counters["ignored_control"] == 3
    assert service.counters["dead_lettered"] == 0
    assert db_session.query(CurrentState).count() == 0


def test_empty_retained_payload_is_ignored(db_session, bus, service, soc_point):
    started(service)
    bus.publish(topics.telemetry_topic(BATTERY, "soc_pct"), b"", retain=True)

    refresh_view(db_session)
    assert service.counters["ignored_empty"] == 1
    assert db_session.query(IngestDeadLetter).count() == 0


def test_availability_offline_over_the_bus_marks_points_stale(
    db_session, bus, service, registry, soc_point
):
    registry.point(
        BATTERY,
        "availability_state",
        data_type="enum",
        point_class="DI",
        enum_values=["online", "offline", "degraded", "unknown"],
    )
    started(service)
    bus.publish(
        topics.telemetry_topic(BATTERY, "soc_pct"), envelope(soc_point, 73.4).to_payload()
    )
    bus.publish(
        topics.availability_topic(BATTERY),
        AvailabilityEnvelope(asset_id=BATTERY, state="offline", ts=T0).to_payload(),
        retain=True,
    )

    refresh_view(db_session)
    assert db_session.get(CurrentState, soc_point.point_id).quality == "stale"
    assert service.counters["points_marked_offline"] == 1
    assert service.counters["availability_received"] == 1


def test_event_topic_materialises_the_event_point(db_session, bus, service, registry):
    point = registry.point(GATE, "motion_event", data_type="object", point_class="EVENT")
    started(service)

    bus.publish(
        topics.event_topic(GATE, "motion_event"),
        EventEnvelope(
            asset_id=GATE, event="motion_event", detail={"zone": "north"}, ts=T0
        ).to_payload(),
    )

    refresh_view(db_session)
    state = db_session.get(CurrentState, point.point_id)
    assert state.value_json == {"zone": "north"}
    assert state.source == "mqtt.event"
    assert service.counters["events_received"] == 1
    assert service.counters["dead_lettered"] == 0


def test_event_without_a_registered_point_is_dead_lettered(db_session, bus, service, soc_point):
    started(service)
    bus.publish(
        topics.event_topic(BATTERY, "opened"),
        EventEnvelope(asset_id=BATTERY, event="opened", ts=T0).to_payload(),
    )

    refresh_view(db_session)
    letter = db_session.query(IngestDeadLetter).one()
    assert letter.reason.startswith("unresolved_event_point")


def test_topic_and_envelope_disagreement_resolves_via_the_registry(
    db_session, bus, service, registry, soc_point
):
    registry.point(CISTERN, "level_pct", unit="%")
    started(service)

    # Published on the battery's topic but claiming to be the cistern.
    bus.publish(
        topics.telemetry_topic(BATTERY, "soc_pct"),
        envelope(f"{CISTERN}/level_pct", 12.5).to_payload(),
    )

    refresh_view(db_session)
    assert db_session.get(CurrentState, soc_point.point_id).value_numeric == pytest.approx(12.5)
    assert db_session.get(CurrentState, f"{CISTERN}/level_pct") is None
    letter = db_session.query(IngestDeadLetter).one()
    assert letter.reason.startswith("envelope_topic_mismatch")


def test_handler_errors_are_dead_lettered_not_raised(db_session, bus, service, soc_point):
    started(service)

    def explode(*_args, **_kwargs):
        raise RuntimeError("historian offline")

    service.writer.apply = explode
    bus.publish(topics.telemetry_topic(BATTERY, "soc_pct"), envelope(soc_point, 1.0).to_payload())

    refresh_view(db_session)
    letter = db_session.query(IngestDeadLetter).one()
    assert letter.reason.startswith("handler_error")
    assert service.counters["handler_errors"] == 1
    assert service.last_error is not None


def test_messages_after_stop_are_ignored(db_session, bus, service, soc_point):
    started(service)
    service.stop()
    bus.publish(topics.telemetry_topic(BATTERY, "soc_pct"), envelope(soc_point, 73.4).to_payload())

    refresh_view(db_session)
    assert service.counters["messages_received"] == 0
    assert db_session.get(CurrentState, soc_point.point_id) is None


def test_service_sweep_marks_stale_points(db_session, bus, service, soc_point):
    started(service)
    bus.publish(topics.telemetry_topic(BATTERY, "soc_pct"), envelope(soc_point, 73.4).to_payload())

    assert service.sweep_stale(T0 + seconds(5)) == 0
    assert service.sweep_stale(T0 + seconds(600)) == 1

    refresh_view(db_session)
    assert db_session.get(CurrentState, soc_point.point_id).quality == "stale"
    assert service.counters["stale_marked"] == 1
    assert service.counters["sweeps"] == 2


def test_sweep_thread_starts_and_stops_cleanly(session_factory, bus, settings):
    svc = IngestService(session_factory, bus, settings, sweep_interval_s=0.01, clock=lambda: T0)
    svc.start()
    thread = svc._thread
    assert thread is not None and thread.is_alive()

    svc.stop()

    assert not svc.running
    assert not thread.is_alive()  # joined, not merely signalled


def test_sweep_interval_defaults_to_half_the_point_timeout(session_factory, bus, settings):
    svc = IngestService(session_factory, bus, settings, auto_sweep=False)
    assert svc.sweep_interval_s == settings.default_stale_after_s / 2


def test_stats_expose_the_documented_counters(service, soc_point):
    started(service)
    stats = service.stats()

    assert stats["name"] == "ingest" and stats["running"] is True
    for counter in (
        "messages_received",
        "messages_written",
        "dead_lettered",
        "out_of_order",
        "stale_marked",
    ):
        assert counter in stats["counters"]
    assert stats["resolver"]["points"] == 1
    assert "writer" in stats


def test_refresh_registry_makes_new_points_resolvable(db_session, bus, service, registry, soc_point):
    started(service)
    point = registry.point(CISTERN, "level_pct", unit="%")
    service.refresh_registry()

    bus.publish(topics.telemetry_topic(CISTERN, "level_pct"), envelope(point, 44.0).to_payload())

    refresh_view(db_session)
    assert db_session.get(CurrentState, point.point_id).value_numeric == pytest.approx(44.0)


# ---------------------------------------------------------------------------
# Retention (SDD 16.4)
# ---------------------------------------------------------------------------


def test_interval_parsing_and_bucketing():
    assert interval_seconds("1min") == 60
    assert interval_seconds("5m") == 300
    assert interval_seconds("1h") == 3600
    with pytest.raises(ValueError):
        interval_seconds("fortnightly")

    assert bucket_start(T0 + seconds(97), 60) == T0 + seconds(60)


def test_downsample_averages_numeric_samples_per_minute(db_session, writer, soc_point):
    for offset in (0, 20, 40, 61, 80):
        writer.apply(db_session, envelope(soc_point, 70.0 + offset, ts=T0 + seconds(offset)), now=T0)
    db_session.commit()

    summary = downsample(db_session, T0, T0 + seconds(120))
    db_session.commit()

    assert summary["raw_samples"] == 5
    assert summary["aggregates_written"] == 2
    aggregates = [
        s for s in samples_for(db_session, soc_point.point_id) if s.source == downsample_source("1min")
    ]
    assert [as_utc(a.ts) for a in aggregates] == [T0, T0 + seconds(60)]
    assert aggregates[0].value_numeric == pytest.approx((70 + 90 + 110) / 3)
    assert aggregates[1].value_numeric == pytest.approx((131 + 150) / 2)
    assert aggregates[0].unit == "%"


def test_downsample_keeps_the_worst_quality_in_the_bucket(db_session, writer, soc_point):
    writer.apply(db_session, envelope(soc_point, 70.0, ts=T0), now=T0)
    writer.apply(db_session, envelope(soc_point, 71.0, ts=T0 + seconds(10), quality="bad"), now=T0)
    db_session.commit()

    downsample(db_session, T0, T0 + seconds(60))
    db_session.commit()

    aggregate = next(
        s for s in samples_for(db_session, soc_point.point_id) if s.source.startswith(
            DOWNSAMPLE_SOURCE_PREFIX
        )
    )
    assert aggregate.quality == "bad"


def test_downsample_is_repeatable_over_the_same_window(db_session, writer, soc_point):
    writer.apply(db_session, envelope(soc_point, 70.0, ts=T0), now=T0)
    db_session.commit()

    downsample(db_session, T0, T0 + seconds(60))
    downsample(db_session, T0, T0 + seconds(60))
    db_session.commit()

    aggregates = [
        s for s in samples_for(db_session, soc_point.point_id) if s.source.startswith(
            DOWNSAMPLE_SOURCE_PREFIX
        )
    ]
    assert len(aggregates) == 1


def test_apply_retention_downsamples_then_deletes_raw(db_session, writer, settings, soc_point):
    now = T0
    old = now - dt.timedelta(days=settings.historian_raw_retention_days + 1)
    for offset in (0, 30, 90):
        writer.apply(db_session, envelope(soc_point, 60.0 + offset, ts=old + seconds(offset)), now=now)
    writer.apply(db_session, envelope(soc_point, 73.4, ts=now), now=now)
    db_session.commit()

    summary = apply_retention(db_session, now, settings)
    db_session.commit()

    assert summary["raw_deleted"] == 3
    assert summary["aggregates_written"] == 2
    assert summary["retention_days"] == settings.historian_raw_retention_days

    rows = samples_for(db_session, soc_point.point_id)
    kept_raw = [r for r in rows if not (r.source or "").startswith(DOWNSAMPLE_SOURCE_PREFIX)]
    aggregates = [r for r in rows if (r.source or "").startswith(DOWNSAMPLE_SOURCE_PREFIX)]
    assert len(kept_raw) == 1  # the recent sample survives
    assert len(aggregates) == 2


def test_apply_retention_does_not_destroy_earlier_aggregates(db_session, writer, settings, soc_point):
    now = T0
    old = now - dt.timedelta(days=settings.historian_raw_retention_days + 2)
    writer.apply(db_session, envelope(soc_point, 60.0, ts=old), now=now)
    db_session.commit()
    apply_retention(db_session, now, settings)
    db_session.commit()

    # A day later a new raw sample has aged past the cutoff.
    later = now + dt.timedelta(days=1)
    newly_old = later - dt.timedelta(days=settings.historian_raw_retention_days) - seconds(60)
    writer.apply(db_session, envelope(soc_point, 65.0, ts=newly_old), now=later)
    db_session.commit()

    summary = apply_retention(db_session, later, settings)
    db_session.commit()

    aggregates = [
        r
        for r in samples_for(db_session, soc_point.point_id)
        if (r.source or "").startswith(DOWNSAMPLE_SOURCE_PREFIX)
    ]
    assert summary["raw_deleted"] == 1
    assert len(aggregates) == 2  # the first run's aggregate is still there


def test_apply_retention_with_nothing_to_do(db_session, writer, settings, soc_point):
    writer.apply(db_session, envelope(soc_point, 73.4, ts=T0), now=T0)
    db_session.commit()

    summary = apply_retention(db_session, T0, settings)
    assert summary["raw_deleted"] == 0 and summary["aggregates_written"] == 0
    assert len(samples_for(db_session, soc_point.point_id)) == 1


def test_retention_deletes_raw_rows_that_never_named_a_source(db_session, writer, settings, soc_point):
    old = T0 - dt.timedelta(days=settings.historian_raw_retention_days + 1)
    writer.apply(db_session, envelope(soc_point, 60.0, ts=old, source=None), now=T0)
    db_session.commit()
    assert samples_for(db_session, soc_point.point_id)[0].source is None

    summary = apply_retention(db_session, T0, settings)
    db_session.commit()

    assert summary["raw_deleted"] == 1


# ---------------------------------------------------------------------------
# End-to-end against the real v0.3 register
# ---------------------------------------------------------------------------


def _registry_loader_available() -> bool:
    try:
        return importlib.util.find_spec("homestead_twin.registry.loader") is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.skipif(
    not _registry_loader_available(), reason="registry loader not present in this working tree"
)
def test_end_to_end_against_the_loaded_register(
    loaded_registry, db_session, bus, session_factory, settings
):
    """The 90-asset register must be ingestable with no per-point wiring.

    None of the v0.3 bindings carry an ``mqtt_topic`` yet, so this exercises the
    derived-topic fallback across the whole register.
    """
    service = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    service.start()
    try:
        assert service.resolver.stats.points > 100

        point_id = f"{BATTERY}/soc_pct"
        assert db_session.get(Point, point_id) is not None
        topic = topics.telemetry_topic(BATTERY, "soc_pct")
        assert service.resolver.resolve(topic) == point_id

        bus.publish(
            topic,
            TelemetryEnvelope(
                ts=T0, asset_id=BATTERY, point="soc_pct", value=64.2, unit="%", source="bms.master"
            ).to_payload(),
        )

        refresh_view(db_session)
        state = db_session.get(CurrentState, point_id)
        assert state.value_numeric == pytest.approx(64.2)
        assert state.quality == "good"
        assert state.unit == "%"
        assert state.stale_after_s == 20  # binding value from the register
        assert len(samples_for(db_session, point_id)) == 1
        assert db_session.query(IngestDeadLetter).count() == 0

        assert service.sweep_stale(T0 + seconds(3600)) == 1
        refresh_view(db_session)
        assert db_session.get(CurrentState, point_id).quality == "stale"
    finally:
        service.stop()
