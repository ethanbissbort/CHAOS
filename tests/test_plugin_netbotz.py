"""The NetBotz reference plugin.

The transport this release ships is a stub, and these tests are careful to hold
that stub to the standard the rest of the platform is held to: it must return
nothing, say why, and never let ``not_configured`` be mistaken for ``ok``. The
mapping, identity routing, API and health reporting *are* complete, so those are
tested against the real v0.3 design package rather than against invented assets
-- including one end-to-end pass where a scripted appliance's readings become
current state on the real ``it.environmental_monitor.rack_01.nbrk0550_01``.

If somebody later implements SNMP, the only thing here that should need to
change is :class:`ScriptedTransport` gaining a sibling.
"""

from __future__ import annotations

import datetime as dt

import pytest

from chaos import topics
from chaos.config import Settings
from chaos.models.registry import ExternalIdentifier
from chaos.models.telemetry import CurrentState
from chaos.plugins.manager import PluginManager
from chaos.plugins.mirror import MirrorService
from chaos.plugins.netbotz import NetBotzPlugin
from chaos.plugins.netbotz import sensors as sensor_map
from chaos.plugins.netbotz.client import (
    TRANSPORT_HTTP,
    TRANSPORT_NONE,
    TRANSPORT_SNMP,
    NetBotzEnclosure,
    NetBotzSensorReading,
    UnconfiguredTransport,
    build_transport,
)
from chaos.plugins.netbotz.mirror import ID_TYPE, PROTOCOL, NetBotzMirrorSource
from chaos.plugins.spec import HEALTH_DEGRADED, HEALTH_NOT_CONFIGURED, HEALTH_OK, PluginContext

RACK_MONITOR = "it.environmental_monitor.rack_01.nbrk0550_01"
T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)


class ScriptedTransport:
    """A NetBotz appliance that exists only in this process."""

    kind = "scripted"
    configured = True

    def __init__(self, readings=(), enclosures=(), *, raises=None):
        self._readings = list(readings)
        self._enclosures = list(enclosures)
        self._raises = raises

    def describe(self) -> dict:
        return {"kind": self.kind, "configured": True, "host": "scripted"}

    def enclosures(self):
        if self._raises is not None:
            raise self._raises
        return list(self._enclosures)

    def read(self):
        return list(self._readings)


def reading(sensor_type: str, value, *, sensor_id="pod/1", enclosure="nbrk0550-01", unit=None):
    return NetBotzSensorReading(
        sensor_id=sensor_id,
        enclosure_id=enclosure,
        sensor_type=sensor_type,
        value=value,
        unit=unit,
        ts=T0,
    )


# ---------------------------------------------------------------------------
# The sensor catalogue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        ("temperature", "temperature"),
        ("Temp", "temperature"),
        ("TEMPERATURE_F", "temperature"),
        ("relative humidity", "humidity"),
        ("dewPoint", "dew_point"),
        ("door-switch", "door"),
        ("rope leak", "fluid"),
        ("Smoke Detector", "smoke"),
        ("comm status", "availability"),
    ],
)
def test_vendor_sensor_names_resolve_through_the_aliases(vendor, expected):
    resolved = sensor_map.sensor_type_for(vendor)
    assert resolved is not None and resolved.key == expected


def test_an_unknown_sensor_type_resolves_to_nothing():
    assert sensor_map.sensor_type_for("flux capacitor") is None
    assert sensor_map.sensor_type_for("") is None
    assert sensor_map.sensor_type_for(None) is None


def test_every_mapped_point_name_exists_in_the_point_dictionary():
    """The mapping may not name a point the design package does not define.

    This is the test that stops the catalogue drifting: a plugin that publishes
    to an undefined point produces a steady stream of dead letters, which is
    exactly the noise that masks real commissioning faults (see F-003 in
    docs/integration-findings.md).
    """
    import yaml

    from chaos.config import DATA_DIR

    document = yaml.safe_load((DATA_DIR / "point_dictionary.yaml").read_text(encoding="utf-8"))
    defined = set(document["points"])

    for sensor in sensor_map.SENSOR_TYPES:
        for name in sensor.point_names:
            assert name in defined, f"{sensor.key} maps to '{name}', which is not in the point dictionary"


def test_every_enum_value_the_plugin_publishes_is_in_the_dictionary():
    """Ingest rejects an enum violation, so an unmapped state would be dropped."""
    import yaml

    from chaos.config import DATA_DIR

    document = yaml.safe_load((DATA_DIR / "point_dictionary.yaml").read_text(encoding="utf-8"))

    for sensor in sensor_map.SENSOR_TYPES:
        if sensor.shape != "enum" or not sensor.mirrorable:
            continue
        allowed = set(document["points"][sensor.point_names[0]]["enum_values"])
        assert set(sensor.enum_values) <= allowed
        table = sensor_map.DOOR_STATES if sensor.key == "door" else sensor_map.AVAILABILITY_STATES
        assert set(table.values()) <= allowed
        assert sensor_map.UNKNOWN in allowed, "coerce() falls back to 'unknown'"


def test_unmirrorable_sensor_types_are_listed_rather_than_hidden():
    """A NetBotz capability with no canonical point is a documented gap."""
    unmirrorable = {sensor.key for sensor in sensor_map.SENSOR_TYPES if not sensor.mirrorable}
    assert {"airflow", "audio", "vibration", "camera_motion"} <= unmirrorable
    for key in unmirrorable:
        assert "no canonical point" in sensor_map.SENSOR_TYPES_BY_KEY[key].summary


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def test_fahrenheit_is_converted_because_the_point_is_named_in_celsius():
    """A mirrored Fahrenheit reading is large enough to defeat freeze protection."""
    temperature = sensor_map.SENSOR_TYPES_BY_KEY["temperature"]
    assert sensor_map.coerce(temperature, 68.0, unit="F") == pytest.approx(20.0)
    assert sensor_map.coerce(temperature, 68.0, unit="degF") == pytest.approx(20.0)
    assert sensor_map.coerce(temperature, 20.0, unit="C") == pytest.approx(20.0)
    assert sensor_map.coerce(temperature, 20.0, unit=None) == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, True), (0, False), ("Active", True), ("normal", False), ("WET", True), ("dry", False), (True, True)],
)
def test_boolean_sensors_read_vendor_spellings(raw, expected):
    fluid = sensor_map.SENSOR_TYPES_BY_KEY["fluid"]
    assert sensor_map.coerce(fluid, raw) is expected


def test_an_unreadable_boolean_is_an_error_not_a_reassuring_default():
    """A leak sensor that cannot be read is not a leak sensor reading "dry"."""
    fluid = sensor_map.SENSOR_TYPES_BY_KEY["fluid"]
    with pytest.raises(sensor_map.CoercionError):
        sensor_map.coerce(fluid, "banana")


def test_a_missing_value_is_an_error():
    temperature = sensor_map.SENSOR_TYPES_BY_KEY["temperature"]
    with pytest.raises(sensor_map.CoercionError, match="no value"):
        sensor_map.coerce(temperature, None)


def test_a_non_numeric_value_for_a_numeric_point_is_an_error():
    temperature = sensor_map.SENSOR_TYPES_BY_KEY["temperature"]
    with pytest.raises(sensor_map.CoercionError, match="not numeric"):
        sensor_map.coerce(temperature, "warm")


def test_an_unrecognised_enum_state_becomes_unknown_rather_than_a_guess():
    door = sensor_map.SENSOR_TYPES_BY_KEY["door"]
    assert sensor_map.coerce(door, "opened") == "open"
    assert sensor_map.coerce(door, "who knows") == sensor_map.UNKNOWN


# ---------------------------------------------------------------------------
# The transport, and its honesty
# ---------------------------------------------------------------------------


def test_no_transport_is_selected_by_default():
    transport = build_transport(None, None)
    assert transport.kind == TRANSPORT_NONE
    assert transport.configured is False
    assert transport.read() == ()
    assert transport.enclosures() == ()


def test_a_selected_transport_still_reports_that_it_is_unimplemented():
    """This release has no SNMP. Saying otherwise would be the dangerous bug."""
    transport = build_transport(TRANSPORT_SNMP, "10.10.10.40")
    assert transport.configured is False
    assert "no implementation in this release" in transport.reason
    assert "SNMP client library" in transport.requirement
    assert transport.read() == ()


def test_a_transport_with_no_host_says_so():
    transport = build_transport(TRANSPORT_HTTP, None)
    assert "no host was given" in transport.reason


def test_an_unknown_transport_name_is_named_back(caplog):
    transport = build_transport("carrier_pigeon", "10.10.10.40")
    assert transport.kind == TRANSPORT_NONE
    assert "carrier_pigeon" in transport.reason


def test_the_transport_description_carries_no_credentials():
    described = UnconfiguredTransport(TRANSPORT_SNMP, host="10.10.10.40").describe()
    assert set(described) == {"kind", "configured", "host", "reason", "requirement"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def setup_plugin(transport=None, options=None) -> NetBotzPlugin:
    plugin = NetBotzPlugin(transport_factory=(lambda context: transport)) if transport else NetBotzPlugin()
    plugin.setup(PluginContext(settings=Settings(), options=options or {}))
    return plugin


def test_an_unconfigured_plugin_reports_not_configured_not_ok():
    health = setup_plugin().health()
    assert health.state == HEALTH_NOT_CONFIGURED
    assert "transport" in health.detail.lower()


def test_a_host_alone_does_not_make_the_plugin_healthy():
    """The base implementation would say ``ok`` here. A host is not a connection."""
    health = setup_plugin(options={"host": "10.10.10.40", "transport": "snmp"}).health()
    assert health.state == HEALTH_NOT_CONFIGURED


def test_a_reachable_appliance_reports_ok():
    transport = ScriptedTransport(enclosures=[NetBotzEnclosure("nbrk0550-01", reachable=True)])
    health = setup_plugin(transport).health()
    assert health.state == HEALTH_OK
    assert health.facts["enclosures"] == 1


def test_an_unreachable_appliance_reports_degraded():
    """Configured and not answering is a different problem from never set up."""
    transport = ScriptedTransport(enclosures=[NetBotzEnclosure("nbrk0550-01", reachable=False)])
    health = setup_plugin(transport).health()
    assert health.state == HEALTH_DEGRADED
    assert "nbrk0550-01" in health.detail


def test_a_transport_that_raises_reports_degraded():
    transport = ScriptedTransport(raises=TimeoutError("no route to host"))
    health = setup_plugin(transport).health()
    assert health.state == HEALTH_DEGRADED
    assert "TimeoutError" in health.detail


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def test_a_reading_is_offered_by_both_identity_routes():
    """Binding address and appliance identity, so it works before and after commissioning."""
    source = NetBotzMirrorSource(ScriptedTransport())
    translated = source.translate(reading("temperature", 21.0, sensor_id="nbrk0550-01/temp/1"))

    assert translated.external_id == "nbrk0550-01/temp/1"
    assert translated.device_id == "nbrk0550-01"
    assert translated.point_name == "temperature_air_c"
    assert translated.alternate_point_names == ("temperature_c", "value")
    assert translated.unit == "°C"


def test_an_unknown_sensor_type_is_counted_as_a_mapping_gap():
    source = NetBotzMirrorSource(ScriptedTransport())
    assert source.translate(reading("flux_capacitor", 1)) is None
    assert source.counters["unknown_sensor_type"] == 1
    assert source.unmapped_types["flux_capacitor"] == 1


def test_a_known_but_unmirrorable_type_is_counted_separately():
    """ "We have no point for airflow" and "we have never heard of it" differ."""
    source = NetBotzMirrorSource(ScriptedTransport())
    assert source.translate(reading("airflow", 2.0)) is None
    assert source.counters["unmirrorable_sensor_type"] == 1
    assert source.counters["unknown_sensor_type"] == 0


def test_an_uncoercible_value_is_counted_and_dropped():
    source = NetBotzMirrorSource(ScriptedTransport())
    assert source.translate(reading("fluid", "banana")) is None
    assert source.counters["uncoercible_value"] == 1


def test_a_source_with_no_transport_is_unavailable():
    source = NetBotzMirrorSource(build_transport(TRANSPORT_NONE, None))
    assert source.available() is False
    assert list(source.read()) == []


def test_reading_translates_the_whole_batch():
    transport = ScriptedTransport(
        [
            reading("temperature", 68.0, sensor_id="a", unit="F"),
            reading("humidity", 41.0, sensor_id="b"),
            reading("airflow", 2.0, sensor_id="c"),
        ]
    )
    produced = list(NetBotzMirrorSource(transport).read())
    assert [item.point_name for item in produced] == ["temperature_air_c", "humidity_relative_pct"]
    assert produced[0].value == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# End to end, against the real design package
# ---------------------------------------------------------------------------


def test_appliance_readings_become_current_state_on_the_real_rack_monitor(
    db_session, session_factory, bus, loaded_registry
):
    """The whole point, exercised on the shipped v0.3 register.

    One ``external_identifiers`` row is the entire commissioning step: no
    binding per sensor, no topic configuration, no change to ingest.
    """
    db_session.add(ExternalIdentifier(asset_id=RACK_MONITOR, id_type=ID_TYPE, value="nbrk0550-01"))
    db_session.commit()

    settings = Settings(database_url="sqlite://", mqtt_enabled=False)
    transport = ScriptedTransport(
        [
            reading("temperature", 24.5, sensor_id="nbrk0550-01/temp/1"),
            reading("humidity", 38.0, sensor_id="nbrk0550-01/humi/1"),
        ]
    )

    from chaos.ingest.service import IngestService

    ingest = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    ingest.start()

    service = MirrorService(
        session_factory,
        bus,
        settings,
        [NetBotzMirrorSource(transport)],
        auto_poll=False,
        clock=lambda: T0,
    )
    service.refresh_identity()
    published = service.poll_once(force=True)
    ingest.stop()

    assert published == 2
    temperature = db_session.get(CurrentState, topics.point_id(RACK_MONITOR, "temperature_air_c"))
    humidity = db_session.get(CurrentState, topics.point_id(RACK_MONITOR, "humidity_relative_pct"))
    assert temperature is not None and temperature.value_numeric == pytest.approx(24.5)
    assert humidity is not None and humidity.value_numeric == pytest.approx(38.0)
    assert temperature.quality == "good"


def test_nothing_is_mirrored_until_the_registry_says_what_the_appliance_is(
    db_session, session_factory, bus, loaded_registry
):
    """No external identifier, no binding: the readings are dropped, not guessed."""
    settings = Settings(database_url="sqlite://", mqtt_enabled=False)
    transport = ScriptedTransport([reading("temperature", 24.5, sensor_id="nbrk0550-01/temp/1")])

    service = MirrorService(
        session_factory,
        bus,
        settings,
        [NetBotzMirrorSource(transport)],
        auto_poll=False,
        clock=lambda: T0,
    )
    service.refresh_identity()

    assert service.poll_once(force=True) == 0
    state = service.states["netbotz.sensors"]
    assert state.drops["unresolved_identity"] == 1
    assert "nbrk0550-01/temp/1" in state.unresolved


# ---------------------------------------------------------------------------
# The plugin's API
# ---------------------------------------------------------------------------


def test_the_plugin_declares_the_capabilities_it_provides():
    plugin = setup_plugin()
    assert set(plugin.manifest.capabilities) == {"api", "mirror"}
    assert len(plugin.routers()) == 1
    assert len(plugin.mirror_sources()) == 1
    assert plugin.services() == ()


def test_the_status_endpoint_reports_the_transport(client):
    payload = client.get("/api/v1/ext/netbotz").json()
    assert payload["manifest"]["name"] == "netbotz"
    assert payload["transport"]["configured"] is False
    assert payload["health"]["state"] == HEALTH_NOT_CONFIGURED


def test_the_sensor_type_endpoint_documents_the_mapping(client):
    payload = client.get("/api/v1/ext/netbotz/sensor-types").json()
    assert payload["protocol"] == PROTOCOL
    assert payload["id_type"] == ID_TYPE
    assert payload["mirrorable"] > 0
    assert payload["unmirrorable"] > 0
    temperature = next(entry for entry in payload["sensor_types"] if entry["key"] == "temperature")
    assert temperature["point_names"][0] == "temperature_air_c"


def test_the_readings_endpoint_returns_nothing_when_nothing_is_connected(client):
    payload = client.get("/api/v1/ext/netbotz/readings").json()
    assert payload["count"] == 0
    assert payload["transport"]["configured"] is False


def test_the_bindings_endpoint_says_nothing_refers_to_netbotz_yet(client):
    payload = client.get("/api/v1/ext/netbotz/bindings").json()
    assert payload["summary"] == {
        "enclosures_mapped": 0,
        "bindings_resolvable": 0,
        "bindings_uncommissioned": 0,
    }
    assert "Nothing in the registry refers to NetBotz" in payload["detail"]


def test_the_bindings_endpoint_distinguishes_unbound_from_unreachable(client, db_session):
    """The two failures look identical on a dashboard and have opposite fixes."""
    db_session.add(ExternalIdentifier(asset_id=RACK_MONITOR, id_type=ID_TYPE, value="nbrk0550-01"))
    from chaos.models.registry import Asset, AssetClass

    if db_session.get(AssetClass, "environmental_monitor") is None:
        db_session.add(AssetClass(name="environmental_monitor", allowed_domains=["it"]))
    if db_session.get(Asset, RACK_MONITOR) is None:
        db_session.add(
            Asset(
                asset_id=RACK_MONITOR,
                domain="it",
                asset_class="environmental_monitor",
                name="NetBotz Rack Monitor 550",
            )
        )
    db_session.commit()

    payload = client.get("/api/v1/ext/netbotz/bindings").json()
    assert payload["summary"]["enclosures_mapped"] == 1
    assert payload["external_identifiers"][0]["enclosure_id"] == "nbrk0550-01"
    assert "mapped to assets by external identifier" in payload["detail"]


def test_the_plugin_loads_on_the_secondary_node():
    """The NetBotz 500 is in the container the secondary node exists to outlive."""
    settings = Settings(database_url="sqlite://", mqtt_enabled=False, node_role="secondary")
    record = PluginManager.discover(settings, environ={}).get("netbotz")
    assert record is not None and record.loaded
