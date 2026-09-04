"""The plugin system: contract, discovery, isolation and mirroring.

Two properties are worth more than everything else here and are tested hardest:

1. **A broken plugin cannot hurt the platform.** Every way a third party can
   fail -- an unimportable module, an invalid manifest, a ``setup()`` that
   raises, a ``routers()`` returning rubbish, a ``read()`` that throws -- is
   exercised with an assertion that the *other* plugin still works.
2. **A plugin cannot invent identity.** Readings resolve through registry rows
   or they are dropped. There is no path in these tests, or in the code, by
   which a vendor string becomes a point that nobody registered.

Nothing here sleeps or opens a socket. ``InMemoryBus`` delivers synchronously,
the mirror engine is driven by ``poll_once``, and every plugin under test has an
in-process transport.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import APIRouter

from chaos import topics
from chaos.config import Settings
from chaos.envelope import TelemetryEnvelope
from chaos.ingest.service import IngestService
from chaos.models.registry import (
    Asset,
    AssetClass,
    ExternalIdentifier,
    Point,
    PointBinding,
    PointDefinition,
)
from chaos.models.telemetry import CurrentState
from chaos.plugins.manager import (
    ORIGIN_BUILTIN,
    ORIGIN_ENTRY_POINT,
    Candidate,
    PluginManager,
    options_for,
)
from chaos.plugins.mirror import (
    DROP_NO_TOPIC,
    DROP_UNRESOLVED,
    IdentityMap,
    MirrorReading,
    MirrorService,
)
from chaos.plugins.spec import (
    HEALTH_DISABLED,
    HEALTH_FAILED,
    HEALTH_NOT_CONFIGURED,
    HEALTH_OK,
    PluginBase,
    PluginContext,
    PluginHealth,
    PluginManifest,
)

MONITOR = "it.environmental_monitor.rack_01.probe_01"
T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------


def build_point(
    session,
    asset_id: str,
    point_name: str,
    *,
    data_type: str = "float",
    unit: str | None = None,
    protocol: str | None = None,
    address: str | None = None,
) -> Point:
    """The smallest registry that resolves a point, independent of the loader."""
    parts = topics.split_asset_id(asset_id)
    if session.get(AssetClass, parts.asset_class) is None:
        session.add(AssetClass(name=parts.asset_class, allowed_domains=[parts.domain]))
    if session.get(Asset, asset_id) is None:
        session.add(
            Asset(
                asset_id=asset_id,
                domain=parts.domain,
                asset_class=parts.asset_class,
                name=asset_id,
                status="commissioned",
            )
        )
    if session.get(PointDefinition, point_name) is None:
        session.add(
            PointDefinition(
                name=point_name,
                default_class="AI",
                allowed_classes=["AI"],
                data_type=data_type,
                unit=unit,
            )
        )
    point = Point(
        point_id=topics.point_id(asset_id, point_name),
        asset_id=asset_id,
        point_name=point_name,
        point_class="AI",
        data_type=data_type,
        unit=unit,
    )
    session.add(point)
    session.add(
        PointBinding(
            point_id=point.point_id,
            asset_id=asset_id,
            point_name=point_name,
            binding_status="commissioned" if address else "tbd",
            source_protocol=protocol,
            source_address=address,
        )
    )
    session.flush()
    return point


class FakeSource:
    """A mirror source with a scripted answer, used to drive the engine."""

    plugin = "fake"
    protocol = "fake_protocol"
    id_type = "fake.device"
    poll_interval_s = 5.0

    def __init__(self, readings=(), *, name="sensors", available=True, raises=None):
        self.name = name
        self._readings = list(readings)
        self._available = available
        self._raises = raises
        self.reads = 0

    def available(self) -> bool:
        return self._available

    def read(self):
        self.reads += 1
        if self._raises is not None:
            raise self._raises
        return list(self._readings)


class GoodPlugin(PluginBase):
    manifest = PluginManifest(
        name="good",
        version="1.0.0",
        summary="A plugin that behaves",
        capabilities=("api", "mirror"),
    )

    def __init__(self, source=None):
        super().__init__()
        self._source = source or FakeSource()
        self._source.plugin = "good"

    def routers(self):
        router = APIRouter(tags=["good"])

        @router.get("/ping")
        def ping() -> dict:
            return {"pong": True}

        return (router,)

    def mirror_sources(self):
        return (self._source,)

    def health(self) -> PluginHealth:
        return PluginHealth.ok("in-process test double")


def manifest(name: str, **kwargs) -> PluginManifest:
    return PluginManifest(name=name, version="0.0.1", summary=f"{name} test plugin", **kwargs)


def candidate_for(plugin, name: str | None = None) -> Candidate:
    """A candidate that yields ``plugin`` without importing anything."""
    return Candidate(
        name=name or plugin.manifest.name,
        origin=ORIGIN_BUILTIN,
        module=f"tests.doubles.{name or plugin.manifest.name}",
        factory=None,
    )


@pytest.fixture()
def plugin_settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
        node_role="primary",
    )


def discover_with(settings, pairs, **kwargs) -> PluginManager:
    """Discover a fixed set of ``(candidate, plugin)`` pairs.

    ``_instantiate`` is monkeypatched at the call site rather than the module
    level so each test states exactly which plugin objects exist.
    """
    import chaos.plugins.manager as manager_module

    mapping = {candidate.name: plugin for candidate, plugin in pairs}
    original = manager_module._instantiate

    def fake(candidate):
        produced = mapping[candidate.name]
        if isinstance(produced, Exception):
            raise produced
        return produced

    manager_module._instantiate = fake
    try:
        return PluginManager.discover(
            settings,
            candidates=[candidate for candidate, _ in pairs],
            environ=kwargs.pop("environ", {}),
            **kwargs,
        )
    finally:
        manager_module._instantiate = original


# ---------------------------------------------------------------------------
# Manifest and health
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["NetBotz", "net-botz", "9lives", "x", "", "net botz", "a" * 41])
def test_a_plugin_name_must_be_a_slug(bad):
    """The name becomes a URL segment and an env-var fragment; both constrain it."""
    with pytest.raises(ValueError, match="Invalid plugin name"):
        manifest(bad)


def test_a_manifest_cannot_declare_an_unknown_capability():
    with pytest.raises(ValueError, match="unknown capabilities"):
        manifest("thing", capabilities=("api", "telepathy"))


def test_a_manifest_cannot_declare_an_unknown_node_role():
    with pytest.raises(ValueError, match="unknown node roles"):
        manifest("thing", node_roles=("primary", "tertiary"))


def test_a_manifest_with_no_node_roles_is_rejected():
    """It would load nowhere, which is a mistake rather than a configuration."""
    with pytest.raises(ValueError, match="no node roles"):
        manifest("thing", node_roles=())


def test_health_rejects_a_state_nobody_can_render():
    with pytest.raises(ValueError, match="Unknown plugin health state"):
        PluginHealth("probably_fine", "hmm")


def test_only_ok_counts_as_working():
    assert PluginHealth.ok("up").working
    for constructor in (PluginHealth.degraded, PluginHealth.not_configured, PluginHealth.failed):
        assert not constructor("down").working


def test_the_default_health_names_the_missing_options():
    class Needy(PluginBase):
        manifest = PluginManifest(
            name="needy", version="1", summary="needs things", required_options=("host", "token")
        )

    plugin = Needy()
    plugin.setup(PluginContext(settings=Settings(), options={"host": "10.0.0.1"}))
    health = plugin.health()
    assert health.state == HEALTH_NOT_CONFIGURED
    assert "token" in health.detail
    assert health.facts["missing_options"] == ["token"]


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def test_options_come_from_the_json_document():
    settings = Settings(plugin_options='{"netbotz": {"host": "10.10.10.40", "transport": "snmp"}}')
    assert options_for("netbotz", settings, environ={}) == {"host": "10.10.10.40", "transport": "snmp"}


def test_the_environment_overrides_the_document():
    """Credentials belong in the environment, so the environment has to win."""
    settings = Settings(plugin_options='{"netbotz": {"host": "old"}}')
    resolved = options_for(
        "netbotz",
        settings,
        environ={"CHAOS_PLUGIN_NETBOTZ_HOST": "new", "CHAOS_PLUGIN_NETBOTZ_PASSWORD": "s"},
    )
    assert resolved == {"host": "new", "password": "s"}


def test_options_for_another_plugin_are_not_visible():
    settings = Settings(plugin_options='{"other": {"host": "nope"}}')
    assert options_for("netbotz", settings, environ={"CHAOS_PLUGIN_OTHER_HOST": "nope"}) == {}


def test_invalid_options_json_is_ignored_rather_than_fatal():
    """A typo in one environment variable must not stop the platform booting."""
    settings = Settings(plugin_options="{not json")
    assert options_for("netbotz", settings, environ={}) == {}


def test_a_non_object_options_document_is_ignored():
    assert options_for("netbotz", Settings(plugin_options="[1, 2]"), environ={}) == {}


def test_context_option_coercion():
    context = PluginContext(
        settings=Settings(),
        options={"n": "12", "f": "1.5", "flag": "yes", "bad": "x", "empty": ""},
    )
    assert context.int_option("n", 0) == 12
    assert context.float_option("f", 0.0) == 1.5
    assert context.bool_option("flag") is True
    assert context.bool_option("missing", True) is True
    assert context.int_option("bad", 7) == 7, "an unparseable option falls back rather than raising"
    assert context.option("empty", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# Discovery and isolation
# ---------------------------------------------------------------------------


def test_the_netbotz_plugin_is_discovered_in_tree(plugin_settings):
    manager = PluginManager.discover(plugin_settings, environ={})
    record = manager.get("netbotz")
    assert record is not None and record.loaded
    assert record.origin == ORIGIN_BUILTIN


def test_framework_modules_are_not_mistaken_for_plugins(plugin_settings):
    """``spec``, ``mirror`` and ``manager`` sit beside the plugins and are not them."""
    manager = PluginManager.discover(plugin_settings, environ={})
    names = {record.name for record in manager.records}
    assert names.isdisjoint({"spec", "mirror", "manager"})


def test_a_plugin_that_cannot_be_instantiated_does_not_stop_the_others(plugin_settings):
    good = GoodPlugin()
    manager = discover_with(
        plugin_settings,
        [
            (candidate_for(good), good),
            (
                Candidate("broken", ORIGIN_BUILTIN, "tests.doubles.broken", None),
                ImportError("no such module"),
            ),
        ],
    )
    assert manager.get("good").loaded
    broken = manager.get("broken")
    assert not broken.loaded
    assert broken.health().state == HEALTH_FAILED
    assert "no such module" in broken.health().detail
    assert [record.name for record in manager.loaded] == ["good"]


def test_a_setup_that_raises_is_recorded_and_not_loaded(plugin_settings):
    class Exploding(PluginBase):
        manifest = manifest("exploding")

        def setup(self, context):
            raise RuntimeError("the vendor SDK is not installed")

    good = GoodPlugin()
    exploding = Exploding()
    manager = discover_with(
        plugin_settings, [(candidate_for(good), good), (candidate_for(exploding), exploding)]
    )
    record = manager.get("exploding")
    assert not record.loaded
    assert record.health().state == HEALTH_FAILED
    assert "the vendor SDK is not installed" in record.health().detail
    assert manager.get("good").loaded


def test_a_manifest_name_that_disagrees_with_discovery_is_refused(plugin_settings):
    """``CHAOS_PLUGIN_<NAME>_*`` would otherwise target a name nothing answers to."""
    plugin = GoodPlugin()
    manager = discover_with(plugin_settings, [(candidate_for(plugin, name="renamed"), plugin)])
    record = manager.get("renamed")
    assert not record.loaded
    assert "does not match" in record.error


def test_a_duplicate_plugin_name_loses_rather_than_replacing(plugin_settings):
    first, second = GoodPlugin(), GoodPlugin()
    manager = discover_with(
        plugin_settings,
        [
            (Candidate("good", ORIGIN_BUILTIN, "builtin.good", None), first),
            (Candidate("good", ORIGIN_ENTRY_POINT, "other_dist.good", None), second),
        ],
    )
    assert manager.get("good").module == "builtin.good"
    duplicate = next(r for r in manager.records if r.module == "other_dist.good")
    assert not duplicate.loaded
    assert "Duplicate plugin name" in duplicate.error


def test_a_plugin_can_be_disabled_by_configuration(plugin_settings):
    settings = plugin_settings.model_copy(update={"plugins_disabled": "good"})
    plugin = GoodPlugin()
    manager = discover_with(settings, [(candidate_for(plugin), plugin)])
    record = manager.get("good")
    assert not record.loaded
    assert record.health().state == HEALTH_DISABLED
    assert "CHAOS_PLUGINS_DISABLED" in record.health().detail


def test_the_disabled_list_beats_the_enabled_list(plugin_settings):
    settings = plugin_settings.model_copy(update={"plugins_enabled": "good", "plugins_disabled": "good"})
    plugin = GoodPlugin()
    manager = discover_with(settings, [(candidate_for(plugin), plugin)])
    assert not manager.get("good").loaded


def test_an_explicit_enabled_list_excludes_everything_else(plugin_settings):
    settings = plugin_settings.model_copy(update={"plugins_enabled": "other"})
    plugin = GoodPlugin()
    manager = discover_with(settings, [(candidate_for(plugin), plugin)])
    record = manager.get("good")
    assert not record.loaded
    assert "CHAOS_PLUGINS_ENABLED" in record.health().detail


def test_node_role_excludes_a_primary_only_plugin_on_the_secondary(plugin_settings):
    """SDD 16.1: an integration that commands equipment belongs on one node."""
    settings = plugin_settings.model_copy(update={"node_role": "secondary"})

    class PrimaryOnly(PluginBase):
        manifest = manifest("primary_only", node_roles=("primary",))

    plugin = PrimaryOnly()
    manager = discover_with(settings, [(candidate_for(plugin), plugin)])
    record = manager.get("primary_only")
    assert not record.loaded
    assert record.health().state == HEALTH_DISABLED
    assert "secondary node" in record.health().detail


def test_a_health_check_that_raises_becomes_a_failed_state(plugin_settings):
    class Moody(PluginBase):
        manifest = manifest("moody")

        def health(self):
            raise ZeroDivisionError("nope")

    plugin = Moody()
    manager = discover_with(plugin_settings, [(candidate_for(plugin), plugin)])
    health = manager.get("moody").health()
    assert health.state == HEALTH_FAILED
    assert "ZeroDivisionError" in health.detail


def test_a_health_check_returning_the_wrong_type_is_not_trusted(plugin_settings):
    class Liar(PluginBase):
        manifest = manifest("liar")

        def health(self):
            return "everything is fine"

    plugin = Liar()
    manager = discover_with(plugin_settings, [(candidate_for(plugin), plugin)])
    assert manager.get("liar").health().state == HEALTH_FAILED


def test_option_values_never_leave_the_plugin(plugin_settings):
    """The API renders this dict. It must carry key names and no secrets."""
    settings = plugin_settings.model_copy(update={"plugin_options": '{"good": {"password": "hunter2"}}'})
    plugin = GoodPlugin()
    manager = discover_with(settings, [(candidate_for(plugin), plugin)])
    payload = manager.get("good").as_dict()
    assert payload["configured_option_keys"] == ["password"]
    assert "hunter2" not in repr(payload)


# ---------------------------------------------------------------------------
# Contribution collection
# ---------------------------------------------------------------------------


def test_routers_are_namespaced_under_ext(plugin_settings):
    plugin = GoodPlugin()
    manager = discover_with(plugin_settings, [(candidate_for(plugin), plugin)])
    (record, router, prefix) = manager.routers()[0]
    assert record.name == "good"
    assert prefix == "/ext/good"
    assert isinstance(router, APIRouter)


def test_a_router_of_the_wrong_type_is_skipped_not_mounted(plugin_settings):
    class Confused(PluginBase):
        manifest = manifest("confused", capabilities=("api",))

        def routers(self):
            return ("/not/a/router",)

    good, confused = GoodPlugin(), Confused()
    manager = discover_with(
        plugin_settings, [(candidate_for(good), good), (candidate_for(confused), confused)]
    )
    assert [record.name for record, _, _ in manager.routers()] == ["good"]


def test_a_contribution_method_that_raises_costs_only_that_plugin(plugin_settings):
    class Hostile(PluginBase):
        manifest = manifest("hostile", capabilities=("api", "mirror"))

        def routers(self):
            raise RuntimeError("boom")

        def mirror_sources(self):
            raise RuntimeError("boom")

        def services(self):
            raise RuntimeError("boom")

    good, hostile = GoodPlugin(), Hostile()
    manager = discover_with(plugin_settings, [(candidate_for(good), good), (candidate_for(hostile), hostile)])
    assert [record.name for record, _, _ in manager.routers()] == ["good"]
    assert [source.plugin for source in manager.mirror_sources()] == ["good"]
    assert manager.services() == []


def test_a_plugin_cannot_contribute_a_source_under_another_plugins_name(plugin_settings):
    """Otherwise its counters and its failures would be filed against somebody else."""

    class Impostor(PluginBase):
        manifest = manifest("impostor", capabilities=("mirror",))

        def mirror_sources(self):
            source = FakeSource()
            source.plugin = "netbotz"
            return (source,)

    plugin = Impostor()
    manager = discover_with(plugin_settings, [(candidate_for(plugin), plugin)])
    assert manager.mirror_sources() == []


def test_a_service_without_a_lifecycle_is_refused(plugin_settings):
    class Sloppy(PluginBase):
        manifest = manifest("sloppy", capabilities=("service",))

        def services(self):
            return (object(),)

    plugin = Sloppy()
    manager = discover_with(plugin_settings, [(candidate_for(plugin), plugin)])
    assert manager.services() == []


@pytest.mark.parametrize("shape", ["instance", "class", "factory"])
def test_a_plugin_may_be_an_instance_a_class_or_a_factory(shape, monkeypatch):
    """A class carries its manifest as a class attribute; do not hand it back raw."""
    import types

    from chaos.plugins.manager import _instantiate

    module = types.ModuleType("tests.doubles.shapes")
    if shape == "instance":
        module.PLUGIN = GoodPlugin()
    elif shape == "class":
        module.PLUGIN = GoodPlugin
    else:
        module.get_plugin = GoodPlugin

    monkeypatch.setattr("importlib.import_module", lambda name: module)
    plugin = _instantiate(Candidate("good", ORIGIN_BUILTIN, "tests.doubles.shapes", None))
    assert isinstance(plugin, GoodPlugin), "a class must be instantiated, not returned"
    assert plugin.manifest.name == "good"


def test_a_package_that_is_not_a_plugin_says_so(monkeypatch):
    import types

    from chaos.plugins.manager import _instantiate

    monkeypatch.setattr("importlib.import_module", lambda name: types.ModuleType("empty"))
    with pytest.raises(AttributeError, match="exposes neither"):
        _instantiate(Candidate("empty", ORIGIN_BUILTIN, "chaos.plugins.empty", None))


def test_something_without_a_manifest_is_not_a_plugin(monkeypatch):
    import types

    from chaos.plugins.manager import _instantiate

    module = types.ModuleType("tests.doubles.nonsense")
    module.PLUGIN = object()
    monkeypatch.setattr("importlib.import_module", lambda name: module)
    with pytest.raises(TypeError, match="no PluginManifest"):
        _instantiate(Candidate("nonsense", ORIGIN_BUILTIN, "tests.doubles.nonsense", None))


def test_entry_point_discovery_survives_a_hostile_environment():
    """It reads other distributions' metadata, so it must not be able to raise."""
    from chaos.plugins.manager import iter_entry_point_candidates

    assert isinstance(list(iter_entry_point_candidates()), list)


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def test_a_binding_address_resolves_a_reading(db_session):
    build_point(
        db_session, MONITOR, "temperature_air_c", unit="°C", protocol="fake_protocol", address="pod/1"
    )
    db_session.commit()
    identity = IdentityMap()
    identity.refresh(db_session)

    resolution = identity.resolve(FakeSource(), MirrorReading(value=21.0, external_id="pod/1"))
    assert resolution.point_id == topics.point_id(MONITOR, "temperature_air_c")
    assert resolution.route == "binding"


def test_a_binding_for_a_different_protocol_does_not_match(db_session):
    """Two SNMP integrations on one property must not claim each other's addresses."""
    build_point(db_session, MONITOR, "temperature_air_c", protocol="other_protocol", address="pod/1")
    db_session.commit()
    identity = IdentityMap()
    identity.refresh(db_session)
    assert identity.resolve(FakeSource(), MirrorReading(value=21.0, external_id="pod/1")).point_id is None


def test_an_external_identifier_plus_point_name_resolves_a_reading(db_session):
    build_point(db_session, MONITOR, "temperature_air_c", unit="°C")
    db_session.add(ExternalIdentifier(asset_id=MONITOR, id_type="fake.device", value="probe-01"))
    db_session.commit()
    identity = IdentityMap()
    identity.refresh(db_session)

    resolution = identity.resolve(
        FakeSource(), MirrorReading(value=21.0, device_id="probe-01", point_name="temperature_air_c")
    )
    assert resolution.point_id == topics.point_id(MONITOR, "temperature_air_c")
    assert resolution.route == "external_identifier"


def test_alternate_point_names_are_tried_in_order(db_session):
    """One vendor sensor means different canonical points on different classes."""
    build_point(db_session, MONITOR, "value")
    db_session.add(ExternalIdentifier(asset_id=MONITOR, id_type="fake.device", value="probe-01"))
    db_session.commit()
    identity = IdentityMap()
    identity.refresh(db_session)

    resolution = identity.resolve(
        FakeSource(),
        MirrorReading(
            value=21.0,
            device_id="probe-01",
            point_name="temperature_air_c",
            alternate_point_names=("temperature_c", "value"),
        ),
    )
    assert resolution.point_id == topics.point_id(MONITOR, "value")


def test_an_unresolvable_reading_names_both_routes_it_tried(db_session):
    db_session.commit()
    identity = IdentityMap()
    identity.refresh(db_session)
    resolution = identity.resolve(FakeSource(), MirrorReading(value=1, external_id="pod/9", device_id="d"))
    assert resolution.point_id is None
    assert "point_bindings" in resolution.reason
    assert "external_identifiers" in resolution.reason


def test_placeholder_binding_addresses_are_not_treated_as_real(db_session):
    """The v0.3 register carries ``source_address: TBD`` on every SNMP binding."""
    build_point(db_session, MONITOR, "temperature_air_c", protocol="fake_protocol", address="TBD")
    build_point(db_session, MONITOR, "humidity_relative_pct", protocol="fake_protocol", address="tbd")
    db_session.commit()
    identity = IdentityMap()
    counts = identity.refresh(db_session)

    assert counts["bindings"] == 0
    assert counts["placeholder_addresses"] == 2
    assert identity.resolve(FakeSource(), MirrorReading(value=1, external_id="TBD")).point_id is None


def test_a_binding_wins_over_an_external_identifier(db_session):
    """Commissioning is more specific than a whole-appliance mapping."""
    build_point(db_session, MONITOR, "temperature_air_c", protocol="fake_protocol", address="pod/1")
    build_point(db_session, MONITOR, "temperature_c")
    db_session.add(ExternalIdentifier(asset_id=MONITOR, id_type="fake.device", value="probe-01"))
    db_session.commit()
    identity = IdentityMap()
    identity.refresh(db_session)

    resolution = identity.resolve(
        FakeSource(),
        MirrorReading(value=21.0, external_id="pod/1", device_id="probe-01", point_name="temperature_c"),
    )
    assert resolution.point_id == topics.point_id(MONITOR, "temperature_air_c")


# ---------------------------------------------------------------------------
# The mirror engine
# ---------------------------------------------------------------------------


def make_mirror(session_factory, bus, settings, sources) -> MirrorService:
    service = MirrorService(session_factory, bus, settings, sources, auto_poll=False, clock=lambda: T0)
    service.refresh_identity()
    return service


def test_a_resolved_reading_is_published_as_a_canonical_envelope(
    db_session, session_factory, bus, plugin_settings
):
    build_point(
        db_session, MONITOR, "temperature_air_c", unit="°C", protocol="fake_protocol", address="pod/1"
    )
    db_session.commit()

    seen: list[tuple[str, bytes]] = []
    bus.subscribe("#", lambda message: seen.append((message.topic, message.payload)))

    source = FakeSource([MirrorReading(value=21.5, external_id="pod/1", unit="°C")])
    service = make_mirror(session_factory, bus, plugin_settings, [source])
    assert service.poll_once(force=True) == 1

    topic, payload = seen[0]
    assert topic == topics.telemetry_topic(MONITOR, "temperature_air_c", plugin_settings.mqtt_base_topic)
    envelope = TelemetryEnvelope.model_validate_json(payload)
    assert envelope.asset_id == MONITOR
    assert envelope.point == "temperature_air_c"
    assert envelope.value == 21.5
    assert envelope.source == "mirror.fake.sensors"


def test_an_unresolved_reading_is_dropped_counted_and_explained(
    db_session, session_factory, bus, plugin_settings
):
    db_session.commit()
    source = FakeSource([MirrorReading(value=1, external_id="pod/unknown")])
    service = make_mirror(session_factory, bus, plugin_settings, [source])

    assert service.poll_once(force=True) == 0
    state = service.states["fake.sensors"]
    assert state.drops[DROP_UNRESOLVED] == 1
    assert "pod/unknown" in state.unresolved
    assert service.counters[f"dropped_{DROP_UNRESOLVED}"] == 1


def test_a_point_with_no_mqtt_projection_is_dropped_distinctly(
    db_session, session_factory, bus, plugin_settings
):
    """An asset ID outside the canonical four-part form cannot be published to."""
    session = db_session
    session.add(AssetClass(name="oddity", allowed_domains=["it"]))
    session.add(Asset(asset_id="not-canonical", domain="it", asset_class="oddity", name="odd"))
    session.add(
        PointDefinition(
            name="temperature_air_c", default_class="AI", allowed_classes=["AI"], data_type="float"
        )
    )
    session.add(
        Point(
            point_id="not-canonical/temperature_air_c",
            asset_id="not-canonical",
            point_name="temperature_air_c",
            point_class="AI",
            data_type="float",
        )
    )
    session.add(
        PointBinding(
            point_id="not-canonical/temperature_air_c",
            asset_id="not-canonical",
            point_name="temperature_air_c",
            source_protocol="fake_protocol",
            source_address="pod/1",
        )
    )
    session.commit()

    source = FakeSource([MirrorReading(value=1.0, external_id="pod/1")])
    service = make_mirror(session_factory, bus, plugin_settings, [source])
    assert service.poll_once(force=True) == 0
    assert service.states["fake.sensors"].drops[DROP_NO_TOPIC] == 1


def test_an_invalid_quality_code_drops_only_that_reading(db_session, session_factory, bus, plugin_settings):
    build_point(db_session, MONITOR, "temperature_air_c", protocol="fake_protocol", address="pod/1")
    build_point(db_session, MONITOR, "humidity_relative_pct", protocol="fake_protocol", address="pod/2")
    db_session.commit()

    source = FakeSource(
        [
            MirrorReading(value=1.0, external_id="pod/1", quality="probably_fine"),
            MirrorReading(value=50.0, external_id="pod/2"),
        ]
    )
    service = make_mirror(session_factory, bus, plugin_settings, [source])
    assert service.poll_once(force=True) == 1


def test_a_source_that_raises_does_not_stop_the_others(db_session, session_factory, bus, plugin_settings):
    build_point(db_session, MONITOR, "temperature_air_c", protocol="fake_protocol", address="pod/1")
    db_session.commit()

    angry = FakeSource(raises=ConnectionError("appliance refused the connection"), name="angry")
    calm = FakeSource([MirrorReading(value=21.0, external_id="pod/1")], name="calm")
    service = make_mirror(session_factory, bus, plugin_settings, [angry, calm])

    assert service.poll_once(force=True) == 1
    assert "ConnectionError" in service.states["fake.angry"].last_error
    assert service.counters["source_errors"] == 1


def test_an_unavailable_source_is_not_an_error(db_session, session_factory, bus, plugin_settings):
    """A transport nobody has configured is a normal state, not a failure."""
    db_session.commit()
    source = FakeSource([MirrorReading(value=1, external_id="pod/1")], available=False)
    service = make_mirror(session_factory, bus, plugin_settings, [source])

    assert service.poll_once(force=True) == 0
    assert source.reads == 0
    assert service.states["fake.sensors"].last_error is None
    assert service.counters["source_errors"] == 0


def test_a_source_is_not_polled_before_it_is_due(db_session, session_factory, bus, plugin_settings):
    build_point(db_session, MONITOR, "temperature_air_c", protocol="fake_protocol", address="pod/1")
    db_session.commit()

    clock = {"now": 1000.0}
    source = FakeSource([MirrorReading(value=1.0, external_id="pod/1")])
    service = MirrorService(
        session_factory,
        bus,
        plugin_settings,
        [source],
        auto_poll=False,
        clock=lambda: T0,
        monotonic=lambda: clock["now"],
    )
    service.refresh_identity()

    service.poll_once()
    assert source.reads == 1
    service.poll_once()
    assert source.reads == 1, "polled again before its 5s interval elapsed"
    clock["now"] += 5.1
    service.poll_once()
    assert source.reads == 2


def test_duplicate_source_names_are_refused(session_factory, bus, plugin_settings):
    with pytest.raises(ValueError, match="Duplicate mirror source"):
        MirrorService(session_factory, bus, plugin_settings, [FakeSource(), FakeSource()], auto_poll=False)


def test_mirrored_telemetry_survives_the_round_trip_through_ingest(
    db_session, session_factory, bus, plugin_settings
):
    """The point of the whole design: vendor data becomes ordinary current state.

    Nothing between the mirror and the historian knows a plugin was involved --
    the reading goes onto the bus as a standard envelope and ingest applies its
    normal rules to it, which is what makes a vendor integration no more trusted
    than a field device.
    """
    build_point(
        db_session, MONITOR, "temperature_air_c", unit="°C", protocol="fake_protocol", address="pod/1"
    )
    db_session.commit()

    ingest = IngestService(session_factory, bus, plugin_settings, auto_sweep=False, clock=lambda: T0)
    ingest.start()

    source = FakeSource([MirrorReading(value=23.75, external_id="pod/1", unit="°C", ts=T0)])
    make_mirror(session_factory, bus, plugin_settings, [source]).poll_once(force=True)
    ingest.stop()

    state = db_session.get(CurrentState, topics.point_id(MONITOR, "temperature_air_c"))
    assert state is not None
    assert state.value_numeric == pytest.approx(23.75)
    assert state.quality == "good"
    assert state.source == "mirror.fake.sensors"


def test_stats_report_every_drop_reason(db_session, session_factory, bus, plugin_settings):
    db_session.commit()
    service = make_mirror(session_factory, bus, plugin_settings, [FakeSource()])
    stats = service.stats()
    assert stats["name"] == "plugin-mirror"
    assert set(stats["sources"][0]["drops"]) == {DROP_UNRESOLVED, DROP_NO_TOPIC, "invalid_reading"}


# ---------------------------------------------------------------------------
# Runtime wiring
# ---------------------------------------------------------------------------


def test_the_mirror_engine_is_registered_with_the_other_services(session_factory, bus, plugin_settings):
    from chaos.runtime import build_services

    plugin = GoodPlugin()
    manager = discover_with(plugin_settings, [(candidate_for(plugin), plugin)])
    services = build_services(plugin_settings, session_factory, bus, plugins=manager)
    assert "plugin-mirror" in {service.name for service in services.services}


def test_mirroring_can_be_switched_off(session_factory, bus, plugin_settings):
    from chaos.runtime import build_services

    settings = plugin_settings.model_copy(update={"plugin_mirror_enabled": False})
    plugin = GoodPlugin()
    manager = discover_with(settings, [(candidate_for(plugin), plugin)])
    services = build_services(settings, session_factory, bus, plugins=manager)
    assert "plugin-mirror" not in {service.name for service in services.services}


def test_no_sources_means_no_mirror_service(session_factory, bus, plugin_settings):
    """An engine with nothing to poll is a thread doing nothing."""
    from chaos.runtime import build_services

    class Quiet(PluginBase):
        manifest = manifest("quiet")

    plugin = Quiet()
    manager = discover_with(plugin_settings, [(candidate_for(plugin), plugin)])
    services = build_services(plugin_settings, session_factory, bus, plugins=manager)
    assert "plugin-mirror" not in {service.name for service in services.services}


def test_build_services_without_plugins_is_unchanged(session_factory, bus, plugin_settings):
    from chaos.runtime import build_services

    services = build_services(plugin_settings, session_factory, bus)
    assert "plugin-mirror" not in {service.name for service in services.services}


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_the_plugins_endpoint_lists_every_plugin(client):
    payload = client.get("/api/v1/plugins").json()
    assert payload["discovered"] >= 1
    names = {entry["name"] for entry in payload["plugins"]}
    assert "netbotz" in names
    assert payload["mount_prefix"] == "/api/v1/ext/<plugin>"


def test_the_plugins_endpoint_says_which_are_not_working(client):
    payload = client.get("/api/v1/plugins").json()
    # The shipped NetBotz transport is an honest stub, so it must appear here.
    assert "netbotz" in payload["not_working"]


def test_one_plugin_can_be_fetched_by_name(client):
    payload = client.get("/api/v1/plugins/netbotz").json()
    assert payload["name"] == "netbotz"
    assert payload["mount_prefix"] == "/api/v1/ext/netbotz"
    assert payload["health"]["state"] in {HEALTH_OK, HEALTH_NOT_CONFIGURED}


def test_an_unknown_plugin_is_a_404(client):
    assert client.get("/api/v1/plugins/nonesuch").status_code == 404


def test_the_mirror_endpoint_is_not_shadowed_by_the_name_route(client):
    """``/plugins/mirror`` must not be read as a plugin called "mirror"."""
    payload = client.get("/api/v1/plugins/mirror").json()
    assert "sources" in payload
    assert "name" not in payload or payload.get("name") == "plugin-mirror"


def test_reloading_plugins_requires_an_administrator(client, operator_headers):
    response = client.post("/api/v1/plugins/reload", json={"reason": "testing"}, headers=operator_headers)
    assert response.status_code == 403


def test_reloading_plugins_says_that_routes_did_not_move(client, admin_headers):
    response = client.post(
        "/api/v1/plugins/reload", json={"reason": "credentials updated"}, headers=admin_headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["routes_remounted"] is False
    assert payload["reloaded_by"] == "test.admin"
    assert "restart" in payload["detail"].lower()


def test_reloading_requires_a_reason(client, admin_headers):
    assert client.post("/api/v1/plugins/reload", json={}, headers=admin_headers).status_code == 422


def test_plugin_routes_are_mounted_under_ext(client):
    assert client.get("/api/v1/ext/netbotz").status_code == 200


def test_no_plugin_route_can_sit_outside_ext(app):
    """The one structural guarantee: a plugin cannot shadow a core route."""
    plugin_names = {record.name for record in app.state.plugins.loaded}
    for route in app.routes:
        path = getattr(route, "path", "")
        for name in plugin_names:
            if name in path:
                assert path.startswith(f"/api/v1/ext/{name}"), path
