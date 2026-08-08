"""Cross-implementation conformance: Python and .NET must answer identically.

This is the safety net for the whole ``docs/dotnet-migration.md`` sequence. Each
stage of that plan moves one ``/api/v1`` prefix from the Python platform to the
.NET gateway by flipping a route-ownership entry. The only thing that makes
those flips safe is evidence that the two implementations return the same thing,
gathered *before* the flip and re-gathered on every commit afterwards.

**How it works.** Both implementations are stood up over real HTTP against the
*same* SQLite database, seeded from the real v0.3 design package. The same
requests go to both. The responses are compared structurally.

**What "the same" means** is defined in :func:`compare_responses` and is
deliberately strict. The comparison is semantic rather than byte-for-byte --
JSON object key *order* is not part of the JSON data model and no client can
observe it -- but everything a client can observe is compared exactly:

* status code, exactly;
* response media type, exactly (``charset`` is excluded: RFC 8259 requires
  UTF-8 for ``application/json``, so a charset parameter carries no information);
* every object key set, exactly -- an extra or missing key is a failure, and a
  key present with value ``null`` is *not* the same as a key that is absent;
* every scalar, exactly, including its JSON type: ``2`` != ``2.0``,
  ``"true"`` != ``true``, ``""`` != ``null``;
* every array, element-wise **in order**. Order is load-bearing here: the panel
  is a fixed grid of engraved windows and an operator finds a tile by position.
  A comparison that sorted arrays first would pass a port that shuffled the wall.

The single exclusion is ``/generated_at``, which is a reading of each server's
own clock. Two processes cannot produce the same one. It is excluded from
equality and separately asserted to be a UTC instant within the window of the
request pair, so it cannot silently become null, a fixed constant, or a local
time -- see :func:`_assert_generated_at_is_honest`.

There are no other exclusions, no float tolerance, no case folding, no
null/missing equivalence, no whitespace normalisation. **Do not add any to make
a test pass.** A weakened conformance test is worse than none, because it will
be trusted. If the two sides disagree, the port is wrong or the difference is
real and belongs in the migration document.

:func:`test_the_comparator_detects_planted_differences` exists to prove the
comparison has teeth, so this file cannot rot into a vacuous pass.

**Adding a route.** Append a :class:`Case` to :data:`CASES`. Nothing else.

**When .NET is unavailable** -- no SDK, no network for the first NuGet restore,
a build failure in someone else's project -- every test here SKIPS with the
reason. The Linux pytest suite must never go red because of the .NET leg.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = REPO_ROOT / "windows"
CONFORMANCE_HOST_PROJECT = WINDOWS_ROOT / "tests" / "Chaos.Api.ConformanceHost"

#: Bounds on every wait in this module. A conformance run that hangs is a
#: conformance run nobody will keep, and the owner builds locally rather than
#: waiting on CI.
BUILD_TIMEOUT_S = 300.0
STARTUP_TIMEOUT_S = 60.0
REQUEST_TIMEOUT_S = 30.0
SHUTDOWN_TIMEOUT_S = 15.0

#: Printed by the conformance host once Kestrel has bound. The port is chosen by
#: the OS, so nothing here hard-codes one.
LISTENING_MARKER = "CHAOS_CONFORMANCE_LISTENING"


# ---------------------------------------------------------------------------
# Requests under test
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One request issued to both implementations.

    :param name: Test id.
    :param path: Path below the base URL, including the leading slash.
    :param query: Query parameters. A list value is sent as repeated keys.
    :param volatile: JSON pointers excluded from equality. Each exclusion is a
        claim that the field cannot be equal by construction, and needs its own
        justification -- not a way to silence a real difference.
    """

    name: str
    path: str
    query: dict[str, str | list[str]] = field(default_factory=dict)
    volatile: frozenset[str] = frozenset({"/generated_at"})


#: Every request the two implementations are held to. Adding a ported route
#: means adding entries here.
CASES: tuple[Case, ...] = (
    Case("annunciator_default", "/api/v1/annunciator"),
    Case("annunciator_pending_true", "/api/v1/annunciator", {"include_pending": "true"}),
    Case("annunciator_pending_false", "/api/v1/annunciator", {"include_pending": "false"}),
    # The platform has always accepted pydantic's wider boolean vocabulary.
    # ASP.NET Core's own binder accepts none of these, so they are exactly where
    # a port silently narrows an API.
    Case("annunciator_pending_one", "/api/v1/annunciator", {"include_pending": "1"}),
    Case("annunciator_pending_zero", "/api/v1/annunciator", {"include_pending": "0"}),
    Case("annunciator_pending_yes_upper", "/api/v1/annunciator", {"include_pending": "YES"}),
    Case("annunciator_pending_off", "/api/v1/annunciator", {"include_pending": "off"}),
    Case("annunciator_pending_t", "/api/v1/annunciator", {"include_pending": "t"}),
    # Rejections have to match too: status code and body shape.
    Case("annunciator_pending_invalid", "/api/v1/annunciator", {"include_pending": "banana"}),
    Case("annunciator_pending_empty", "/api/v1/annunciator", {"include_pending": ""}),
    Case("annunciator_pending_untrimmed", "/api/v1/annunciator", {"include_pending": " true"}),
    # Starlette's multidict returns the LAST occurrence for a scalar parameter.
    Case(
        "annunciator_pending_repeated",
        "/api/v1/annunciator",
        {"include_pending": ["true", "false"]},
    ),
    # An unknown parameter is ignored, not rejected.
    Case("annunciator_unknown_param", "/api/v1/annunciator", {"nonsense": "1"}),
)


# ---------------------------------------------------------------------------
# Semantic comparison
# ---------------------------------------------------------------------------


def compare_json(
    expected: Any,
    actual: Any,
    *,
    volatile: frozenset[str] = frozenset(),
    pointer: str = "",
) -> list[str]:
    """Compare two parsed JSON documents. Returns one string per difference.

    ``expected`` is the Python platform -- it is the incumbent and it defines
    the contract. ``actual`` is the .NET port.
    """
    if pointer in volatile:
        return []

    if type(expected) is not type(actual):
        # bool before int: in Python ``True == 1`` and ``isinstance(True, int)``,
        # so a port returning 1 where the platform returns true would otherwise
        # slip through.
        if not _same_json_type(expected, actual):
            return [
                f"{pointer or '/'}: type differs -- python {_describe(expected)}, dotnet {_describe(actual)}"
            ]

    if isinstance(expected, dict):
        differences: list[str] = []
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        for key in missing:
            differences.append(
                f"{pointer}/{key}: missing from dotnet (python has {_describe(expected[key])})"
            )
        for key in extra:
            differences.append(f"{pointer}/{key}: extra in dotnet ({_describe(actual[key])})")
        for key in expected:
            if key in actual:
                differences.extend(
                    compare_json(
                        expected[key],
                        actual[key],
                        volatile=volatile,
                        pointer=f"{pointer}/{_escape(key)}",
                    )
                )
        return differences

    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{pointer or '/'}: length differs -- python {len(expected)}, dotnet {len(actual)}"]
        differences = []
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            differences.extend(compare_json(left, right, volatile=volatile, pointer=f"{pointer}/{index}"))
        return differences

    if expected != actual:
        return [f"{pointer or '/'}: python {expected!r} != dotnet {actual!r}"]

    return []


def _same_json_type(left: Any, right: Any) -> bool:
    """True when both values are the same JSON type."""
    for predicate in (
        lambda v: v is None,
        lambda v: isinstance(v, bool),
        lambda v: isinstance(v, int) and not isinstance(v, bool),
        lambda v: isinstance(v, float),
        lambda v: isinstance(v, str),
        lambda v: isinstance(v, list),
        lambda v: isinstance(v, dict),
    ):
        if predicate(left) or predicate(right):
            return predicate(left) and predicate(right)
    return False


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"bool {str(value).lower()}"
    if isinstance(value, dict):
        return f"object with {len(value)} keys"
    if isinstance(value, list):
        return f"array of {len(value)}"
    return f"{type(value).__name__} {value!r}"


def _escape(key: str) -> str:
    """RFC 6901 JSON-pointer escaping."""
    return key.replace("~", "~0").replace("/", "~1")


def compare_responses(python_response, dotnet_response, case: Case) -> list[str]:
    """Compare a full HTTP response pair. Returns one string per difference."""
    differences: list[str] = []

    if python_response.status_code != dotnet_response.status_code:
        differences.append(
            f"status: python {python_response.status_code} != dotnet {dotnet_response.status_code}"
        )

    python_media = python_response.headers.get("content-type", "").split(";")[0].strip().lower()
    dotnet_media = dotnet_response.headers.get("content-type", "").split(";")[0].strip().lower()
    if python_media != dotnet_media:
        differences.append(f"content-type: python {python_media!r} != dotnet {dotnet_media!r}")

    try:
        python_body = python_response.json()
    except ValueError:
        return [*differences, f"python body is not JSON: {python_response.text[:200]!r}"]
    try:
        dotnet_body = dotnet_response.json()
    except ValueError:
        return [*differences, f"dotnet body is not JSON: {dotnet_response.text[:200]!r}"]

    return [*differences, *compare_json(python_body, dotnet_body, volatile=case.volatile)]


# ---------------------------------------------------------------------------
# Availability gate
# ---------------------------------------------------------------------------


def _dotnet_executable() -> str | None:
    """The dotnet CLI, if this machine has one."""
    for candidate in (shutil.which("dotnet"), "/usr/local/dotnet/dotnet"):
        if candidate and Path(candidate).exists():
            return candidate
    return None


@pytest.fixture(scope="session")
def dotnet_host() -> Path:
    """Build the conformance host. Skips -- never fails -- when it cannot.

    The .NET leg is optional on Linux by design: contributors without the SDK,
    and CI legs that only run pytest, must still get a green suite.
    """
    if os.environ.get("CHAOS_SKIP_DOTNET_CONFORMANCE"):
        pytest.skip("CHAOS_SKIP_DOTNET_CONFORMANCE is set; skipping .NET conformance")

    executable = _dotnet_executable()
    if executable is None:
        pytest.skip("no dotnet CLI on PATH or at /usr/local/dotnet; skipping .NET conformance")
    if not CONFORMANCE_HOST_PROJECT.is_dir():
        pytest.skip(f"{CONFORMANCE_HOST_PROJECT} is absent; skipping .NET conformance")

    environment = {
        **os.environ,
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        # Never block on an interactive credential prompt for a private feed.
        "NUGET_CLI_LANGUAGE": "en-US",
    }

    try:
        completed = subprocess.run(  # noqa: S603
            [executable, "build", str(CONFORMANCE_HOST_PROJECT), "-c", "Debug", "--nologo"],
            cwd=WINDOWS_ROOT,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_S,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(
            f"dotnet build exceeded {BUILD_TIMEOUT_S:.0f}s (no NuGet cache and no network?); "
            "skipping .NET conformance"
        )
    except OSError as error:  # pragma: no cover - environment specific
        pytest.skip(f"could not run dotnet build ({error}); skipping .NET conformance")

    if completed.returncode != 0:
        tail = "\n".join((completed.stdout + completed.stderr).strip().splitlines()[-15:])
        pytest.skip(f"dotnet build failed; skipping .NET conformance.\n{tail}")

    return CONFORMANCE_HOST_PROJECT


# ---------------------------------------------------------------------------
# The shared database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def conformance_database(tmp_path_factory) -> Path:
    """One SQLite database, seeded from the real v0.3 design package.

    The panel's interesting properties are about how the design package and the
    platform disagree -- ten of the forty shipped alarms cannot fire
    (``docs/integration-findings.md`` F-007) -- so a convenient fixture would
    test the wrong thing. Synthetic rows are added on top only to reach the
    branches the shipped package does not exercise.
    """
    yaml = pytest.importorskip("yaml")  # noqa: F841  (loader dependency)
    from sqlalchemy.orm import sessionmaker

    from chaos.alarms.definitions import sync_definitions
    from chaos.config import Settings
    from chaos.db import build_engine
    from chaos.models.base import Base
    from chaos.registry.loader import load_package

    database_path = tmp_path_factory.mktemp("conformance") / "homestead.db"
    settings = Settings(
        database_url=f"sqlite:///{database_path}",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
    )

    engine = build_engine(settings)
    import chaos.models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(engine)

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        load_package(session)
        sync_definitions(session, settings=settings, strict=False)
        session.commit()
        _seed_edge_case_definitions(session)
        _seed_alarms(session)
        session.commit()
    finally:
        session.close()

    # Fold the write-ahead log back into the main file. Chaos.Api opens the
    # database with SQLite's Mode=ReadOnly, and a read-only connection cannot
    # create the -shm file a WAL database needs when no writer holds it open.
    # See docs/dotnet-migration.md, "SQLite WAL and read-only readers".
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()

    return database_path


def _seed_edge_case_definitions(session) -> None:
    """Definitions reaching serviceability branches the shipped package does not.

    Without these the conformance run never exercises a disabled definition, a
    definition with no trigger at all, a class-scoped definition whose class is
    not registered, an undeclared panel bay, or an unknown severity -- all of
    which are paths where two implementations can quietly disagree.
    """
    from chaos.models.alarms import AlarmDefinition

    rows = [
        AlarmDefinition(
            alarm_key="zz_conformance_disabled",
            name="Deliberately disabled definition",
            severity="major",
            domain="energy",
            point_name="state_of_charge_pct",
            asset_id="energy.battery_bank.power_container.01",
            enabled=False,
            notes=[{"meta": {"threshold_status": "not_applicable"}}],
        ),
        AlarmDefinition(
            alarm_key="zz_conformance_no_trigger",
            name="No trigger point and no expression",
            severity="warning",
            domain="energy",
            enabled=True,
            notes=[],
        ),
        AlarmDefinition(
            alarm_key="zz_conformance_unregistered_class",
            name="Class-scoped alarm on an unregistered class",
            severity="critical",
            domain="energy",
            point_name="tamper_active",
            asset_class="zz_not_a_real_class",
            enabled=True,
            notes=None,
        ),
        AlarmDefinition(
            alarm_key="zz_conformance_undeclared_bay",
            name="Alarm in a bay the wall order does not declare",
            severity="warning",
            domain="zz_undeclared",
            trigger_expression="1 == 1",
            enabled=True,
            notes=[{"meta": {}}],
        ),
        AlarmDefinition(
            alarm_key="zz_conformance_unknown_severity",
            name="Alarm carrying a severity the panel does not rank",
            severity="catastrophic",
            domain="zz_undeclared",
            trigger_expression="1 == 1",
            enabled=True,
            notes=[{"meta": {"threshold_status": None}}],
        ),
        AlarmDefinition(
            alarm_key="zz_conformance_no_hand_cut_legend",
            name="Some entirely new condition has failed and must be engraved",
            severity="info",
            domain="zz_undeclared",
            trigger_expression="1 == 1",
            enabled=True,
            notes=["a plain human note", {"meta": {"threshold_status": "commissioning_default"}}],
        ),
    ]
    for row in rows:
        session.add(row)


def _seed_alarms(session) -> None:
    """Alarms covering every tile state, plus duplicates and correlation.

    A panel with no alarms on it would compare 40 identical dark tiles and prove
    almost nothing, so every lifecycle state the endpoint distinguishes gets an
    occurrence. ``detected_at`` values are deliberately distinct: the platform
    orders open alarms by ``detected_at DESC`` alone, so equal timestamps leave
    "newest wins the tile" undefined and the two implementations would be
    entitled to disagree. That ambiguity is a real defect in the Python query
    and is recorded in docs/dotnet-migration.md rather than papered over here.
    """
    import datetime as dt

    from chaos.models.alarms import Alarm, Incident

    base = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)

    incident = Incident(
        id="11111111-1111-4111-8111-111111111111",
        title="Conformance incident",
        severity="major",
        opened_at=base,
        state="open",
    )
    session.add(incident)

    def alarm(
        key: str,
        state: str,
        offset_s: int,
        *,
        identifier: str,
        suppressed: bool = False,
        message: str | None = None,
        incident_id: str | None = None,
        activated: bool = True,
    ) -> Alarm:
        detected = base + dt.timedelta(seconds=offset_s)
        return Alarm(
            id=identifier,
            alarm_key=key,
            severity="major",
            state=state,
            message=message,
            detected_at=detected,
            # Microsecond 0 on some, non-zero on others: Python omits the
            # fractional part entirely when it is zero, and that formatting rule
            # is exactly the kind of thing a port gets wrong.
            activated_at=(detected + dt.timedelta(microseconds=123456)) if activated else None,
            suppressed=suppressed,
            suppression_reason="maintenance:conformance" if suppressed else None,
            incident_id=incident_id,
        )

    session.add_all(
        [
            # Serviceable definitions from the real package, in each state.
            alarm("battery_soc_low", "active", 10, identifier="a0000000-0000-4000-8000-000000000001"),
            alarm(
                "ups_on_battery",
                "acknowledged",
                20,
                identifier="a0000000-0000-4000-8000-000000000002",
                message="acknowledged by the conformance seed",
            ),
            alarm("generator_fuel_low", "cleared", 30, identifier="a0000000-0000-4000-8000-000000000003"),
            alarm("backup_overdue", "mitigated", 40, identifier="a0000000-0000-4000-8000-000000000004"),
            alarm(
                "core_switch_unreachable",
                "active",
                50,
                identifier="a0000000-0000-4000-8000-000000000005",
                suppressed=True,
                incident_id="11111111-1111-4111-8111-111111111111",
            ),
            # Only in scope when include_pending is set: the two runs must differ.
            alarm(
                "primary_server_unreachable",
                "detected",
                60,
                identifier="a0000000-0000-4000-8000-000000000006",
                activated=False,
            ),
            # Out of scope entirely, whatever include_pending says.
            alarm("camera_offline", "reviewed", 70, identifier="a0000000-0000-4000-8000-000000000007"),
            # Duplicates on one key: one lamp lights, the rest are counted.
            # Distinct timestamps, so "newest" is unambiguous.
            alarm(
                "pdu_overload",
                "active",
                80,
                identifier="a0000000-0000-4000-8000-000000000008",
                message="newest of three",
            ),
            alarm("pdu_overload", "active", 75, identifier="a0000000-0000-4000-8000-000000000009"),
            alarm("pdu_overload", "acknowledged", 70, identifier="a0000000-0000-4000-800a-00000000000a"),
            # An alarm on a definition whose tile can never light. The tile must
            # stay out_of_service on both sides: serviceability outranks the row.
            alarm(
                "rack_smoke_detected",
                "active",
                90,
                identifier="a0000000-0000-4000-800b-00000000000b",
                message="an alarm row against an unreachable trigger point",
            ),
            # Characters that JSON encoders disagree about escaping, and a
            # non-ASCII character, so the bodies are compared over real text.
            alarm(
                "server_zone_temperature_high",
                "active",
                100,
                identifier="a0000000-0000-4000-800c-00000000000c",
                message="quote ' ampersand & angle < slash / non-ascii é— done",
            ),
            # Zero-microsecond timestamp: Python drops the fraction entirely.
            Alarm(
                id="a0000000-0000-4000-800d-00000000000d",
                alarm_key="network_path_degraded",
                severity="warning",
                state="active",
                message="exact-second timestamp",
                detected_at=base + dt.timedelta(seconds=110),
                activated_at=base + dt.timedelta(seconds=110),
            ),
        ]
    )


# ---------------------------------------------------------------------------
# The two servers
# ---------------------------------------------------------------------------


def _reserve_socket() -> socket.socket:
    """A socket bound to an OS-chosen free port on loopback."""
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reserved.bind(("127.0.0.1", 0))
    reserved.listen(64)
    return reserved


@pytest.fixture(scope="session")
def python_base_url(conformance_database: Path):
    """The Python platform, served by uvicorn over a real socket."""
    uvicorn = pytest.importorskip("uvicorn", reason="uvicorn is needed to serve the Python side")

    from chaos.api.app import create_app
    from chaos.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{conformance_database}",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
        allow_physical_control=False,
        node_role="primary",
    )
    app = create_app(settings, start_services=False, init_db=False)

    reserved = _reserve_socket()
    port = reserved.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [reserved]}, daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline:
            server.should_exit = True
            pytest.skip(f"the Python side did not start within {STARTUP_TIMEOUT_S:.0f}s")
        if not thread.is_alive():
            pytest.skip("the Python side exited during startup")
        time.sleep(0.05)

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT_S)
        reserved.close()


@pytest.fixture(scope="session")
def dotnet_base_url(dotnet_host: Path, conformance_database: Path, tmp_path_factory):
    """The .NET implementation, served by the Chaos.Api conformance host.

    Deliberately not Chaos.Host. While ``/api/v1/annunciator`` is still marked
    ``Python`` in the gateway's route-ownership manifest, the gateway would
    reverse-proxy the request straight back to the Python backend and this file
    would compare Python with Python and pass. A conformance suite that can pass
    without exercising the thing under test is worse than no suite at all.
    """
    executable = _dotnet_executable()
    assert executable is not None  # guaranteed by the dotnet_host fixture

    log_path = tmp_path_factory.mktemp("dotnet-host") / "host.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(  # noqa: S603
            [
                executable,
                "run",
                "--no-build",
                "--project",
                str(dotnet_host),
                "-c",
                "Debug",
                "--",
                "--database",
                str(conformance_database),
                # Port 0: the OS picks, the host announces, nothing is hard-coded.
                "--urls",
                "http://127.0.0.1:0",
            ],
            cwd=WINDOWS_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
        )

    base_url: str | None = None
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    try:
        while base_url is None:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no '{LISTENING_MARKER}' line within {STARTUP_TIMEOUT_S:.0f}s")
            if process.poll() is not None:
                raise RuntimeError(f"the host exited with code {process.returncode}")
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(LISTENING_MARKER):
                    base_url = line.split(maxsplit=1)[1].strip().rstrip("/")
                    break
            if base_url is None:
                time.sleep(0.05)
    except (TimeoutError, RuntimeError) as error:
        _terminate(process)
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
        pytest.skip(f"the .NET conformance host did not start: {error}\n{tail}")

    try:
        yield base_url
    finally:
        _terminate(process)


def _terminate(process: subprocess.Popen) -> None:
    """Stop a child process without ever blocking indefinitely."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()
        process.wait(timeout=SHUTDOWN_TIMEOUT_S)


@pytest.fixture(scope="session")
def http_client():
    """One HTTP client for both implementations."""
    httpx = pytest.importorskip("httpx")
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        yield client


# ---------------------------------------------------------------------------
# The conformance run
# ---------------------------------------------------------------------------


def _fetch(client, base_url: str, case: Case):
    """Issue one case's request, expanding list values into repeated keys."""
    params: list[tuple[str, str]] = []
    for key, value in case.query.items():
        if isinstance(value, list):
            params.extend((key, item) for item in value)
        else:
            params.append((key, value))
    return client.get(f"{base_url}{case.path}", params=params)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_python_and_dotnet_agree(case: Case, http_client, python_base_url: str, dotnet_base_url: str) -> None:
    """The same request must produce the same answer from both implementations."""
    started = time.time()
    python_response = _fetch(http_client, python_base_url, case)
    dotnet_response = _fetch(http_client, dotnet_base_url, case)
    finished = time.time()

    differences = compare_responses(python_response, dotnet_response, case)
    assert not differences, (
        f"{case.name}: the Python and .NET implementations disagree on "
        f"{case.path} {dict(case.query)}.\n"
        "Fix the port or record the difference in docs/dotnet-migration.md. "
        "Do NOT relax the comparison.\n  - " + "\n  - ".join(differences)
    )

    if python_response.status_code == 200:
        _assert_generated_at_is_honest(python_response.json(), dotnet_response.json(), started, finished)


def _assert_generated_at_is_honest(
    python_body: dict[str, Any],
    dotnet_body: dict[str, Any],
    started: float,
    finished: float,
) -> None:
    """``generated_at`` is excluded from equality, so check it means something.

    Without this the exclusion would let the .NET side return null, a fixed
    constant, or a local-time reading and still pass.
    """
    import datetime as dt

    window_start = dt.datetime.fromtimestamp(started - 5, dt.UTC)
    window_end = dt.datetime.fromtimestamp(finished + 5, dt.UTC)

    for implementation, body in (("python", python_body), ("dotnet", dotnet_body)):
        raw = body.get("generated_at")
        assert isinstance(raw, str), f"{implementation}: generated_at is {raw!r}, not a timestamp"
        assert raw.endswith("Z"), (
            f"{implementation}: generated_at {raw!r} carries no UTC designator; "
            "a client would parse it as local time"
        )
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        assert window_start <= parsed <= window_end, (
            f"{implementation}: generated_at {raw} is outside the request window "
            f"{window_start.isoformat()}..{window_end.isoformat()}"
        )


def test_the_conformance_run_is_not_vacuous(http_client, python_base_url: str, dotnet_base_url: str) -> None:
    """The panel under test must actually contain the properties that matter.

    A conformance suite that agreed on an empty document would pass forever.
    """
    body = _fetch(http_client, dotnet_base_url, CASES[0]).json()
    tiles = {tile["alarm_key"]: tile for bay in body["bays"] for tile in bay["tiles"]}

    assert body["summary"]["total"] >= 40, "the real v0.3 alarm set was not loaded"
    assert body["summary"]["out_of_service"] >= 10, (
        "F-007 says ten shipped alarms cannot fire; the panel is not reporting them"
    )
    assert body["summary"]["alarm"] > 0, "no lit tiles: the alarm seed did not land"
    assert body["summary"]["ringback"] > 0, "no ringback tiles: the cleared-alarm seed did not land"
    assert body["summary"]["inhibited"] > 0, "no inhibited tiles: the suppressed-alarm seed did not land"
    assert body["summary"]["acknowledged"] > 0, "no acknowledged tiles"

    # The property this whole port exists to preserve.
    assert tiles["battery_cell_imbalance"]["state"] == "out_of_service"
    assert tiles["rack_smoke_detected"]["state"] == "out_of_service", (
        "a tile with an open alarm row but an unreachable trigger point must stay out of service"
    )
    for tile in tiles.values():
        if not tile["serviceable"]:
            assert tile["state"] == "out_of_service", (
                f"{tile['alarm_key']}: unserviceable but reported {tile['state']}"
            )
            assert tile["state"] != "normal"


def test_include_pending_actually_changes_the_answer(
    http_client, python_base_url: str, dotnet_base_url: str
) -> None:
    """Guard against the two include_pending cases being the same request twice."""
    default = _fetch(http_client, python_base_url, CASES[0]).json()
    pending = _fetch(http_client, python_base_url, CASES[1]).json()

    assert default["summary"] != pending["summary"], (
        "include_pending made no difference, so the parameter cases prove nothing"
    )


def test_the_comparator_detects_planted_differences() -> None:
    """Prove the comparison has teeth. This is the test that guards the tests."""
    baseline = {
        "summary": {"total": 40, "out_of_service": 10, "horn": True},
        "bays": [{"domain": "energy", "tiles": [{"alarm_key": "a"}, {"alarm_key": "b"}]}],
        "note": None,
    }

    def mutated(**overrides: Any) -> dict[str, Any]:
        return {**json.loads(json.dumps(baseline)), **overrides}

    # A changed scalar.
    assert compare_json(baseline, mutated(summary={"total": 39, "out_of_service": 10, "horn": True}))

    # bool vs int: True == 1 in Python, so a naive comparison would miss this.
    assert compare_json(baseline, mutated(summary={"total": 40, "out_of_service": 10, "horn": 1}))

    # int vs float.
    assert compare_json(baseline, mutated(summary={"total": 40.0, "out_of_service": 10, "horn": True}))

    # A missing key, and an extra key.
    assert compare_json(baseline, mutated(summary={"total": 40, "horn": True}))
    assert compare_json(baseline, mutated(extra="surprise"))

    # null is not the same as absent, and not the same as "".
    assert compare_json(baseline, {k: v for k, v in baseline.items() if k != "note"})
    assert compare_json(baseline, mutated(note=""))

    # Reordered array elements: tile positions are part of the contract.
    assert compare_json(
        baseline,
        mutated(bays=[{"domain": "energy", "tiles": [{"alarm_key": "b"}, {"alarm_key": "a"}]}]),
    )

    # A dropped array element.
    assert compare_json(
        baseline,
        mutated(bays=[{"domain": "energy", "tiles": [{"alarm_key": "a"}]}]),
    )

    # And the control: an identical document reports nothing.
    assert compare_json(baseline, json.loads(json.dumps(baseline))) == []

    # The volatile exclusion is scoped to the pointer it names, not the key name.
    assert compare_json(
        {"generated_at": "a", "bays": [{"generated_at": "x"}]},
        {"generated_at": "b", "bays": [{"generated_at": "y"}]},
        volatile=frozenset({"/generated_at"}),
    ) == ["/bays/0/generated_at: python 'x' != dotnet 'y'"]
