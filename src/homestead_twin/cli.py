"""Operator command line for the Homestead Digital Twin platform.

The CLI is the local-first operator surface: it works over SSH on the primary
node in the power container and on the physically separate secondary control
node, with no browser, no cloud and no orchestration layer required
(SDD sections 5.1, 16.1).

Design rules for this module:

1. **Subsystem imports are lazy.** ``homestead-twin --help``, ``init-db``,
   ``status``, ``backup`` and ``export`` must keep working even when the
   registry loader, EMS, alarm engine, ingest or simulator packages are absent
   or broken. A missing subsystem is reported with an actionable message and a
   dedicated exit code, never a traceback.
2. **Every failure exits non-zero.** Exit codes: ``0`` success, ``1`` runtime
   failure, ``2`` usage error (argparse), ``3`` subsystem unavailable.
3. **Nothing here invents state.** Counts, EMS state and alarms are read from
   the database; where a table does not exist yet the CLI says so rather than
   printing a reassuring zero.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import importlib
import inspect
import io
import json
import logging
import os
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from homestead_twin import __version__

LOG = logging.getLogger("homestead_twin.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

#: Repository root: <root>/src/homestead_twin/cli.py -> parents[2]
ROOT_DIR = Path(__file__).resolve().parents[2]

PROG = "homestead-twin"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CommandError(RuntimeError):
    """A command failed for a reason the operator can act on."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None, exit_code: int | None = None):
        super().__init__(message)
        self.hint = hint
        if exit_code is not None:
            self.exit_code = exit_code


class SubsystemUnavailable(CommandError):
    """A subsystem owned by another module is not importable on this node."""

    exit_code = EXIT_UNAVAILABLE


# ---------------------------------------------------------------------------
# Lazy subsystem resolution
# ---------------------------------------------------------------------------


def _first_parameter_name(func: Callable[..., Any]) -> str | None:
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return None
    return parameters[0].name if parameters else None


def resolve_subsystem(
    module_name: str,
    candidates: Sequence[str] = (),
    *,
    subsystem: str,
    hint: str | None = None,
    first_param: str | None = None,
) -> Any:
    """Import ``module_name`` lazily and return the module or a named callable.

    Raises :class:`SubsystemUnavailable` -- never ``ImportError`` -- so that a
    subsystem owned by another module, or belonging to a later deployment
    phase, can be missing without breaking the rest of the CLI.

    ``first_param`` guards against a name collision. Several subsystems expose
    both a document parser and a database loader with similar names; requiring
    the first parameter to be (for example) ``session`` means the CLI refuses to
    call the wrong one rather than silently reporting a successful no-op.
    """
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # ImportError, but also errors raised at import time
        raise SubsystemUnavailable(
            f"{subsystem} unavailable: could not import {module_name} ({type(exc).__name__}: {exc})",
            hint=hint,
        ) from exc

    if not candidates:
        return module

    rejected: list[str] = []
    for name in candidates:
        attribute = getattr(module, name, None)
        if not callable(attribute):
            continue
        if first_param is not None:
            actual = _first_parameter_name(attribute)
            if actual is not None and actual != first_param:
                rejected.append(f"{name}(first parameter is {actual!r}, expected {first_param!r})")
                continue
        return attribute

    detail = f"{module_name} exposes none of {', '.join(candidates)} as a usable callable"
    if rejected:
        detail += f"; rejected: {'; '.join(rejected)}"
    raise SubsystemUnavailable(f"{subsystem} unavailable: {detail}", hint=hint)


def _call_loader(func: Callable[..., Any], session: Any, data_dir: Path | None) -> Any:
    """Call a subsystem loader, passing ``data_dir`` only if it accepts one.

    Loader signatures are owned by other modules. Rather than guessing, the
    parameter list is inspected and the directory is supplied only when a
    directory-shaped parameter exists.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return func(session)

    kwargs: dict[str, Any] = {}
    if data_dir is not None:
        for name in ("data_dir", "directory", "data_path", "base_dir"):
            if name in parameters:
                kwargs[name] = data_dir
                break
    return func(session, **kwargs)


# ---------------------------------------------------------------------------
# Settings / database helpers
# ---------------------------------------------------------------------------


def build_settings(args: argparse.Namespace) -> Any:
    """Resolve settings, applying the global CLI overrides.

    Without overrides the cached process settings are used, so the CLI observes
    exactly the same ``HOMESTEAD_*`` environment as the API service.
    """
    from homestead_twin.config import Settings, get_settings

    overrides: dict[str, Any] = {}
    if getattr(args, "database_url", None):
        overrides["database_url"] = args.database_url
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = args.data_dir

    if not overrides:
        return get_settings()
    try:
        return Settings(**overrides)
    except Exception as exc:  # pragma: no cover - pydantic validation
        raise CommandError(f"Invalid settings override: {exc}") from exc


def export_setting_overrides(args: argparse.Namespace) -> None:
    """Push CLI overrides into the environment.

    Required for ``serve``: uvicorn's application factory (and its reloader
    subprocess) builds ``Settings`` itself, so overrides must travel as
    ``HOMESTEAD_*`` environment variables.
    """
    if getattr(args, "database_url", None):
        os.environ["HOMESTEAD_DATABASE_URL"] = args.database_url
    if getattr(args, "data_dir", None):
        os.environ["HOMESTEAD_DATA_DIR"] = str(args.data_dir)


def redact_url(url: str) -> str:
    """Hide the password in a SQLAlchemy URL before printing it."""
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # pragma: no cover - defensive
        return re.sub(r"(//[^:/@]+:)[^@]+(@)", r"\1***\2", url)


@contextmanager
def open_session(settings: Any, *, create: bool = False):
    """Yield a session bound to a freshly built engine.

    ``db.configure`` is called so that any subsystem reaching for the module
    level session factory sees the same engine the CLI is using.
    """
    from homestead_twin import db as db_module

    try:
        engine = db_module.build_engine(settings)
    except Exception as exc:
        raise CommandError(
            f"Could not build a database engine for {redact_url(settings.database_url)}: {exc}",
            hint=_database_hint(settings.database_url),
        ) from exc

    db_module.configure(engine)
    if create:
        _create_all(db_module, engine, settings)

    session = db_module.get_session_factory()()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _create_all(db_module: Any, engine: Any, settings: Any) -> None:
    try:
        db_module.create_all(engine)
    except Exception as exc:
        raise CommandError(
            f"Could not create tables in {redact_url(settings.database_url)}: {exc}",
            hint=_database_hint(settings.database_url),
        ) from exc


def workspace_root(settings: Any = None) -> Path:
    """Best-effort location of the deployment working tree.

    In a source checkout this is the repository root. In the container the
    package is installed into a virtualenv, so ``ROOT_DIR`` points inside
    ``site-packages``; the data directory's parent is the reliable anchor
    because the image copies ``data/``, ``schemas/``, ``tools/`` and ``var/``
    side by side under ``/app``.
    """
    candidates: list[Path] = []
    data_dir = getattr(settings, "data_dir", None)
    if data_dir:
        parent = Path(data_dir).resolve().parent
        if parent.is_dir():
            candidates.append(parent)
    candidates.append(ROOT_DIR)
    candidates.append(Path.cwd())

    for candidate in candidates:
        if (candidate / "tools").is_dir() or (candidate / "var").is_dir():
            return candidate
    return candidates[0]


def _database_hint(url: str) -> str:
    if url.startswith(("postgresql", "postgres")):
        return (
            "Check that PostgreSQL is reachable and that the 'postgres' extra is installed "
            "(pip install -e '.[postgres]'). In the compose stack the database is the "
            "'postgres' service; see deploy/docker-compose.yml."
        )
    return (
        "Check HOMESTEAD_DATABASE_URL and that the target directory is writable. "
        "The default SQLite file lives under var/."
    )


def _table_names(engine: Any) -> set[str]:
    from sqlalchemy import inspect as sa_inspect

    try:
        return set(sa_inspect(engine).get_table_names())
    except Exception:  # pragma: no cover - defensive
        return set()


def _metadata():
    """Model metadata with every mapper registered."""
    import homestead_twin.models  # noqa: F401  (registers mappers)
    from homestead_twin.models.base import Base

    return Base.metadata


def _count(session: Any, table: Any) -> int:
    from sqlalchemy import func, select

    return int(session.execute(select(func.count()).select_from(table)).scalar_one())


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _dumps(payload: Any, fmt: str) -> str:
    if fmt == "yaml":
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - PyYAML is a hard dependency
            raise CommandError("YAML output requires PyYAML (pip install PyYAML)") from exc
        # Round-trip through JSON so datetimes and Paths become plain scalars.
        plain = json.loads(json.dumps(payload, default=_json_default))
        return yaml.safe_dump(plain, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False) + "\n"


def _result_payload(result: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalise a subsystem load result into counts plus warnings.

    Result types are owned by other modules, so this reads whatever shape it is
    given: a ``counts()`` method, a ``counts`` mapping, a dataclass, a dict or a
    plain object.
    """
    if result is None:
        return {}, []

    warnings = list(getattr(result, "warnings", None) or [])

    data: dict[str, Any] | None = None
    counts = getattr(result, "counts", None)
    if callable(counts):
        try:
            data = dict(counts())
        except Exception:  # pragma: no cover - defensive
            data = None
    elif isinstance(counts, dict):
        data = dict(counts)

    if data is None:
        if dataclasses.is_dataclass(result) and not isinstance(result, type):
            data = dataclasses.asdict(result)
        elif isinstance(result, dict):
            data = dict(result)
        elif hasattr(result, "__dict__"):
            data = {k: v for k, v in vars(result).items() if not k.startswith("_")}
        else:
            data = {"result": repr(result)}

    data.pop("warnings", None)
    return data, [str(w) for w in warnings]


def _print_counts(title: str, counts: dict[str, Any], warnings: Iterable[str]) -> None:
    print(title)
    if not counts:
        print("  (loader returned no counts)")
    width = max((len(str(k)) for k in counts), default=0)
    for key, value in counts.items():
        if isinstance(value, (list, tuple, set)):
            rendered = f"{len(value)} entr{'y' if len(value) == 1 else 'ies'}"
        elif isinstance(value, dict):
            rendered = f"{len(value)} keys"
        else:
            rendered = str(value)
        print(f"  {str(key).ljust(width)}  {rendered}")
    warnings = list(warnings)
    if warnings:
        print(f"  warnings: {len(warnings)}")
        for warning in warnings[:20]:
            print(f"    - {warning}")
        if len(warnings) > 20:
            print(f"    ... and {len(warnings) - 20} more")


# ---------------------------------------------------------------------------
# Table groups (used by backup and export)
# ---------------------------------------------------------------------------

REGISTRY_TABLES = (
    "asset_classes",
    "point_profiles",
    "point_definitions",
    "assets",
    "asset_relationships",
    "locations",
    "external_identifiers",
    "points",
    "point_bindings",
    "point_samples_index",
    "documents",
    "configuration_revisions",
)
STATE_TABLES = ("current_state",)
HISTORY_TABLES = ("telemetry_samples", "ingest_dead_letters")
ALARM_TABLES = ("alarm_definitions", "incidents", "alarms", "alarm_events", "notification_log")
COMMAND_TABLES = ("commands", "command_results", "operating_modes", "mode_transitions", "audit_log")
ENERGY_TABLES = (
    "power_load_profiles",
    "power_budget_leases",
    "energy_state_snapshot",
    "energy_state_transitions",
    "load_shed_actions",
)
MAINTENANCE_TABLES = (
    "maintenance_plans",
    "work_orders",
    "inspections",
    "calibrations",
    "spare_parts",
    "commissioning_records",
)

EXPORT_GROUPS: dict[str, tuple[str, ...]] = {
    "registry": REGISTRY_TABLES,
    "state": STATE_TABLES,
    "history": HISTORY_TABLES,
    "alarms": ALARM_TABLES,
    "commands": COMMAND_TABLES,
    "energy": ENERGY_TABLES,
    "maintenance": MAINTENANCE_TABLES,
}
EXPORT_CHOICES = tuple(EXPORT_GROUPS) + ("all",)

#: Timestamp column used by ``--since`` / ``--until``, first match wins.
_TIME_COLUMNS = ("ts", "occurred_at", "detected_at", "sent_at", "issued_at", "created_at")


def _time_column(table: Any):
    for name in _TIME_COLUMNS:
        if name in table.c:
            return table.c[name]
    return None


def _dump_table(
    session: Any,
    table: Any,
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    statement = select(table)
    column = _time_column(table) if (since or until) else None
    if column is not None:
        if since is not None:
            statement = statement.where(column >= since)
        if until is not None:
            statement = statement.where(column <= until)
        statement = statement.order_by(column)
    if limit:
        statement = statement.limit(limit)
    return [dict(row) for row in session.execute(statement).mappings()]


def _parse_timestamp(value: str | None, label: str) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise CommandError(
            f"Could not parse --{label} value {value!r}",
            hint="Use an ISO-8601 timestamp, for example 2026-08-07 or 2026-08-07T12:00:00Z.",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace) -> int:
    """Create every table for the configured database."""
    from homestead_twin import db as db_module

    settings = build_settings(args)
    metadata = _metadata()

    try:
        engine = db_module.build_engine(settings)
    except Exception as exc:
        raise CommandError(
            f"Could not build a database engine for {redact_url(settings.database_url)}: {exc}",
            hint=_database_hint(settings.database_url),
        ) from exc

    db_module.configure(engine)
    before = _table_names(engine)
    _create_all(db_module, engine, settings)
    after = _table_names(engine)
    engine.dispose()

    created = sorted(after - before)
    print(f"Database : {redact_url(settings.database_url)}")
    print(f"Node role: {settings.node_role}")
    print(f"Tables   : {len(after)} present, {len(created)} created, {len(metadata.tables)} declared")
    if created and args.verbose:
        for name in created:
            print(f"  + {name}")
    missing = sorted(set(metadata.tables) - after)
    if missing:
        raise CommandError(
            f"{len(missing)} declared table(s) were not created: {', '.join(missing[:10])}",
            hint="Check the database user's CREATE privileges.",
        )
    return EXIT_OK


def cmd_load_registry(args: argparse.Namespace) -> int:
    """Load the machine-readable design package into the registry."""
    settings = build_settings(args)
    load_package = resolve_subsystem(
        "homestead_twin.registry.loader",
        ("load_package",),
        first_param="session",
        subsystem="Registry loader",
        hint=(
            "The registry loader lives in src/homestead_twin/registry/loader.py. "
            "Run 'homestead-twin validate' first to confirm the design package itself is intact."
        ),
    )

    with open_session(settings, create=True) as session:
        try:
            result = _call_loader(load_package, session, args.data_dir)
            session.commit()
        except Exception as exc:
            session.rollback()
            raise CommandError(
                f"Registry load failed: {type(exc).__name__}: {exc}",
                hint="Run 'homestead-twin validate' to check the design package against its schemas.",
            ) from exc

    counts, warnings = _result_payload(result)
    _print_counts("Registry loaded.", counts, warnings)
    if warnings and args.strict:
        raise CommandError(f"{len(warnings)} warning(s) and --strict was requested")
    return EXIT_OK


def _load_stage(name: str, module: str, candidates: Sequence[str], hint: str):
    return {"name": name, "module": module, "candidates": tuple(candidates), "hint": hint}


LOAD_STAGES = (
    _load_stage(
        "registry",
        "homestead_twin.registry.loader",
        ("load_package",),
        "Design package -> assets, points and bindings (data/homestead_asset_register.yaml).",
    ),
    _load_stage(
        "load schedule",
        "homestead_twin.ems.loader",
        ("load_schedule", "load_load_schedule", "sync_schedule", "load_package"),
        "EMS load schedule (data/load_schedule.yaml, SDD sections 31.1 and 49 item 1).",
    ),
    _load_stage(
        "alarm definitions",
        "homestead_twin.alarms.definitions",
        ("sync_definitions", "load_alarm_definitions", "load_definitions", "load_package"),
        "Alarm definitions (data/alarm_definitions.yaml, SDD section 14.3).",
    ),
)


def cmd_load_all(args: argparse.Namespace) -> int:
    """Load the registry, the EMS load schedule and the alarm definitions."""
    settings = build_settings(args)
    failures: list[str] = []
    skipped: list[str] = []

    with open_session(settings, create=True) as session:
        for stage in LOAD_STAGES:
            try:
                loader = resolve_subsystem(
                    stage["module"],
                    stage["candidates"],
                    subsystem=f"{stage['name'].title()} loader",
                    hint=stage["hint"],
                    first_param="session",
                )
            except SubsystemUnavailable as exc:
                if args.skip_missing:
                    print(f"- {stage['name']}: skipped ({exc})")
                    skipped.append(stage["name"])
                    continue
                raise

            try:
                result = _call_loader(loader, session, args.data_dir)
                session.commit()
            except Exception as exc:
                session.rollback()
                message = f"{stage['name']}: {type(exc).__name__}: {exc}"
                print(f"! {message}", file=sys.stderr)
                failures.append(message)
                continue

            counts, warnings = _result_payload(result)
            _print_counts(f"* {stage['name']}", counts, warnings)

    if failures:
        raise CommandError(
            f"{len(failures)} of {len(LOAD_STAGES)} load stage(s) failed",
            hint="Fix the reported stage and re-run; loading is idempotent per stage.",
        )
    if skipped:
        print(f"\nSkipped (not installed on this node): {', '.join(skipped)}")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate the machine-readable design package against its JSON Schemas."""
    root = workspace_root(build_settings(args))
    script = root / "tools" / "validate_bundle.py"
    if not script.is_file():
        raise CommandError(
            f"Validator not found at {script}",
            hint=(
                "tools/ is not shipped in the wheel. Run from a source checkout, or from the "
                "container image, which copies tools/ to /app/tools."
            ),
        )

    # Validating is a read-only check, so by default it does not rewrite the
    # tracked validation_report.json -- otherwise `make ci` and every test run
    # would leave a dirty working tree over nothing but a changed timestamp.
    # Pass --write-report to refresh it deliberately.
    argv = [sys.executable, str(script)]
    argv.append("--report" if getattr(args, "write_report", False) else "--no-report")
    if getattr(args, "write_report", False):
        argv.append(str(root / "validation_report.json"))

    # Fixed argv, no shell. check=False because the validator's non-zero exit is
    # the expected failure signal and is turned into a CommandError below.
    process = subprocess.run(
        argv,
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if process.stdout:
        sys.stdout.write(process.stdout)
    if process.stderr:
        sys.stderr.write(process.stderr)

    if process.returncode != 0:
        raise CommandError(
            f"Design-package validation failed (exit {process.returncode})",
            hint=(
                "Fix data/*.yaml against schemas/*.schema.json. The register, dictionaries and "
                "bindings must cross-reference cleanly before the registry can be loaded."
            ),
        )
    print("Design package valid.")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API (and, on the primary node, the background services)."""
    export_setting_overrides(args)
    settings = build_settings(args)

    uvicorn = resolve_subsystem(
        "uvicorn",
        subsystem="ASGI server",
        hint="pip install 'uvicorn[standard]' (declared in pyproject dependencies).",
    )

    host = args.host or settings.api_host
    port = args.port or settings.api_port
    print(
        f"Serving {settings.api_title} v{__version__} as '{settings.node_role}' "
        f"on http://{host}:{port} (physical control "
        f"{'ENABLED' if settings.allow_physical_control else 'disabled'})"
    )
    uvicorn.run(
        "homestead_twin.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        reload_dirs=[str(ROOT_DIR / "src")] if args.reload else None,
        log_level=args.log_level.lower(),
    )
    return EXIT_OK


def cmd_simulate(args: argparse.Namespace) -> int:
    """Delegate to the simulator CLI (``homestead-simulator``)."""
    simulator_main = resolve_subsystem(
        "simulator.cli",
        ("main",),
        subsystem="Simulator",
        hint=(
            "The simulator lives in src/simulator/. Install the project in editable mode "
            "(pip install -e '.[dev]') or run with PYTHONPATH=src."
        ),
    )
    forwarded = list(args.simulator_args or [])
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    result = simulator_main(forwarded)
    return int(result or EXIT_OK)


def cmd_retention(args: argparse.Namespace) -> int:
    """Apply the SDD section 16.4 data-retention policy to the historian."""
    settings = build_settings(args)
    apply_retention = resolve_subsystem(
        "homestead_twin.ingest.retention",
        ("apply_retention",),
        first_param="session",
        subsystem="Retention",
        hint="Retention lives in src/homestead_twin/ingest/retention.py.",
    )

    now = _parse_timestamp(args.now, "now") or dt.datetime.now(dt.UTC)
    apply_changes = bool(args.apply)

    with open_session(settings) as session:
        try:
            result = apply_retention(session, now, settings)
        except Exception as exc:
            session.rollback()
            raise CommandError(f"Retention failed: {type(exc).__name__}: {exc}") from exc

        if apply_changes:
            session.commit()
        else:
            session.rollback()

    counts, warnings = _result_payload(result)
    header = "Retention applied." if apply_changes else "Retention dry run (nothing committed)."
    _print_counts(header, counts, warnings)
    print(
        f"  raw retention: {settings.historian_raw_retention_days} days (HOMESTEAD_HISTORIAN_RAW_RETENTION_DAYS)"
    )
    if not apply_changes:
        print(
            "  note: the dry run rolls the transaction back. A retention implementation that "
            "commits internally would still persist its changes."
        )
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    """Replicate registry and configuration outside the container.

    SDD section 16.1 mitigation 3: critical configuration and asset data must
    exist outside the combined battery/server container. This produces a single
    portable ``.tar.gz`` holding the registry tables as JSON, redacted runtime
    settings and the machine-readable design package.
    """
    settings = build_settings(args)
    metadata = _metadata()
    now = dt.datetime.now(dt.UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    default_name = f"homestead-{settings.node_role}-{stamp}.tar.gz"
    output = Path(args.output) if args.output else workspace_root(settings) / "var" / "backups" / default_name
    if output.is_dir():
        output = output / default_name
    output.parent.mkdir(parents=True, exist_ok=True)

    groups = list(REGISTRY_TABLES) + list(ALARM_TABLES) + list(COMMAND_TABLES)
    groups += list(ENERGY_TABLES) + list(MAINTENANCE_TABLES) + list(STATE_TABLES)
    if args.include_history:
        groups += list(HISTORY_TABLES)

    dumps: dict[str, list[dict[str, Any]]] = {}
    missing_tables: list[str] = []

    with open_session(settings) as session:
        present = _table_names(session.get_bind())
        for name in groups:
            table = metadata.tables.get(name)
            if table is None or name not in present:
                missing_tables.append(name)
                continue
            dumps[name] = _dump_table(session, table)

    manifest = {
        "kind": "homestead-twin-backup",
        "format_version": 1,
        "created_at": now.isoformat(),
        "platform_version": __version__,
        "node_role": settings.node_role,
        "site_id": settings.site_id,
        "database": redact_url(settings.database_url),
        "includes_history": bool(args.include_history),
        "row_counts": {name: len(rows) for name, rows in dumps.items()},
        "tables_absent": missing_tables,
        "sdd_reference": "16.1 mitigation 3 (configuration replicated outside the container)",
    }

    restore_text = (
        "Homestead Digital Twin backup\n"
        "=============================\n\n"
        f"Created  : {now.isoformat()}\n"
        f"Node role: {settings.node_role}\n\n"
        "Contents\n"
        "  manifest.json      row counts and provenance\n"
        "  settings.json      redacted runtime settings (secrets are NOT included)\n"
        "  tables/*.json      one JSON array per database table\n"
        "  design-package/    data/*.yaml|json and schemas/*.json as loaded on this node\n\n"
        "Restore\n"
        "  1. homestead-twin init-db\n"
        "  2. homestead-twin load-all              # rebuild from the design package, or\n"
        "  2b. restore tables/*.json with your own loader if the package has since changed\n"
        "  3. homestead-twin status                # confirm counts match manifest.json\n\n"
        "This archive contains no MQTT, database or notification credentials. Those are\n"
        "per-device and must be re-issued, not restored (SDD section 15.2).\n\n"
        "Keep at least one copy off the property and one copy offline (SDD sections 15.8, 16.1).\n"
    )

    def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mtime = int(now.timestamp())
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(data))

    settings_payload = json.loads(settings.model_dump_json())
    for key in list(settings_payload):
        if any(token in key for token in ("password", "secret", "token", "key")):
            settings_payload[key] = "***redacted***"
    settings_payload["database_url"] = redact_url(settings.database_url)

    try:
        with tarfile.open(output, "w:gz") as tar:
            _add_bytes(tar, "manifest.json", _dumps(manifest, "json").encode("utf-8"))
            _add_bytes(tar, "RESTORE.txt", restore_text.encode("utf-8"))
            _add_bytes(tar, "settings.json", _dumps(settings_payload, "json").encode("utf-8"))
            for name, rows in dumps.items():
                _add_bytes(tar, f"tables/{name}.json", _dumps(rows, "json").encode("utf-8"))

            data_dir = Path(settings.data_dir)
            if data_dir.is_dir():
                for path in sorted(data_dir.iterdir()):
                    if path.is_file():
                        tar.add(path, arcname=f"design-package/data/{path.name}")
            schema_dir = Path(settings.schema_dir)
            if schema_dir.is_dir():
                for path in sorted(schema_dir.iterdir()):
                    if path.is_file():
                        tar.add(path, arcname=f"design-package/schemas/{path.name}")
    except OSError as exc:
        raise CommandError(f"Could not write {output}: {exc}") from exc

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"Backup   : {output}")
    print(f"Size     : {size_mb:.2f} MiB")
    print(f"SHA-256  : {digest}")
    print(f"Tables   : {len(dumps)} dumped, {sum(len(r) for r in dumps.values())} rows")
    if missing_tables:
        print(f"Absent   : {len(missing_tables)} declared table(s) not present in this database")
    print("Copy this archive off the property and keep one offline copy (SDD 15.8, 16.1).")
    return EXIT_OK


def _status_payload(settings: Any) -> dict[str, Any]:
    metadata = _metadata()
    payload: dict[str, Any] = {
        "platform_version": __version__,
        "node_role": settings.node_role,
        "site_id": settings.site_id,
        "timezone": settings.timezone,
        "database": redact_url(settings.database_url),
        "database_reachable": False,
        "physical_control_enabled": bool(settings.allow_physical_control),
        "mqtt": {
            "enabled": settings.mqtt_enabled,
            "host": settings.mqtt_host,
            "port": settings.mqtt_port,
            "tls": settings.mqtt_tls,
            "base_topic": settings.mqtt_base_topic,
            "authenticated": bool(settings.mqtt_username),
        },
        "historian": {
            "backend": settings.historian_backend,
            "raw_retention_days": settings.historian_raw_retention_days,
        },
        "ems_enabled": bool(settings.ems_enabled and not settings.is_secondary),
        "alarm_engine_enabled": bool(settings.alarm_engine_enabled),
        "notification_backends": settings.notification_backend_list,
        "counts": {},
        "energy_state": None,
        "active_alarms": {"total": 0, "by_severity": {}},
        "missing_tables": [],
        "notes": [],
    }

    if settings.is_secondary and settings.ems_enabled:
        payload["notes"].append(
            "EMS is configured on but suppressed: runtime.build_services never starts the energy "
            "manager on a secondary node (SDD 16.1, no split supervisory control)."
        )

    with open_session(settings) as session:
        engine = session.get_bind()
        present = _table_names(engine)
        payload["database_reachable"] = True
        payload["missing_tables"] = sorted(set(metadata.tables) - present)

        interesting = (
            "assets",
            "points",
            "point_bindings",
            "power_load_profiles",
            "alarm_definitions",
            "current_state",
            "telemetry_samples",
            "ingest_dead_letters",
            "commands",
            "work_orders",
            "commissioning_records",
        )
        for name in interesting:
            table = metadata.tables.get(name)
            if table is not None and name in present:
                payload["counts"][name] = _count(session, table)

        if "energy_state_snapshot" in present:
            from sqlalchemy import select

            table = metadata.tables["energy_state_snapshot"]
            row = session.execute(select(table).limit(1)).mappings().first()
            if row is not None:
                payload["energy_state"] = {
                    "state": row.get("state"),
                    "entered_at": row.get("entered_at"),
                    "data_quality": row.get("data_quality"),
                    "shed_groups_active": row.get("shed_groups_active"),
                    "generator_request": row.get("generator_request"),
                    "last_evaluated_at": row.get("last_evaluated_at"),
                }

        if "alarms" in present:
            from sqlalchemy import func, select

            table = metadata.tables["alarms"]
            open_states = ("detected", "active", "acknowledged", "mitigated")
            statement = (
                select(table.c.severity, func.count())
                .where(table.c.state.in_(open_states))
                .group_by(table.c.severity)
            )
            by_severity = {row[0]: int(row[1]) for row in session.execute(statement)}
            payload["active_alarms"] = {
                "total": sum(by_severity.values()),
                "by_severity": by_severity,
                "counted_states": list(open_states),
            }

    return payload


def cmd_status(args: argparse.Namespace) -> int:
    """Report node role, database, registry counts, EMS state and active alarms."""
    settings = build_settings(args)
    payload = _status_payload(settings)

    if args.json:
        sys.stdout.write(_dumps(payload, "json"))
        return EXIT_OK

    print(f"Homestead Digital Twin {payload['platform_version']}")
    print(f"  node role        : {payload['node_role']}")
    print(f"  site             : {payload['site_id']}  ({payload['timezone']})")
    print(f"  database         : {payload['database']}")
    print(f"  physical control : {'ENABLED' if payload['physical_control_enabled'] else 'disabled'}")
    mqtt = payload["mqtt"]
    mqtt_state = "enabled" if mqtt["enabled"] else "disabled"
    print(
        f"  mqtt             : {mqtt_state} {mqtt['host']}:{mqtt['port']} "
        f"tls={'on' if mqtt['tls'] else 'off'} auth={'yes' if mqtt['authenticated'] else 'no'} "
        f"base={mqtt['base_topic']}"
    )
    print(
        f"  historian        : {payload['historian']['backend']}, raw retention "
        f"{payload['historian']['raw_retention_days']} d"
    )
    print(
        f"  ems              : {'enabled' if payload['ems_enabled'] else 'disabled'}"
        f"   alarms: {'enabled' if payload['alarm_engine_enabled'] else 'disabled'}"
        f"   notify: {', '.join(payload['notification_backends']) or 'none'}"
    )

    print("\nRegistry and history")
    counts = payload["counts"]
    if not counts:
        print("  (no platform tables present -- run 'homestead-twin init-db')")
    width = max((len(k) for k in counts), default=0)
    for name, value in counts.items():
        print(f"  {name.ljust(width)}  {value}")

    energy = payload["energy_state"]
    print("\nEnergy management")
    if energy is None:
        print("  no energy state recorded yet (EMS has not published a snapshot)")
    else:
        print(f"  state            : {energy['state']}")
        print(f"  entered at       : {energy['entered_at'] or 'unknown'}")
        print(f"  data quality     : {energy['data_quality']}")
        print(f"  shed groups      : {energy['shed_groups_active'] or 'none'}")
        print(f"  generator request: {energy['generator_request'] or 'none'}")

    alarms = payload["active_alarms"]
    print("\nAlarms")
    if not payload["counts"].get("alarm_definitions") and alarms["total"] == 0:
        print("  no alarm definitions loaded and no active alarms")
    else:
        print(f"  active           : {alarms['total']}")
        for severity, count in sorted(alarms["by_severity"].items()):
            print(f"    {severity.ljust(10)} {count}")

    if payload["missing_tables"]:
        print(f"\n{len(payload['missing_tables'])} declared table(s) missing -- run 'homestead-twin init-db'")
    for note in payload["notes"]:
        print(f"\nnote: {note}")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """Export registry and historical data in an open format (SDD FR-010)."""
    settings = build_settings(args)
    metadata = _metadata()

    requested = []
    for chunk in args.include:
        requested.extend(part.strip() for part in chunk.split(",") if part.strip())
    if not requested:
        requested = ["registry"]
    if "all" in requested:
        requested = list(EXPORT_GROUPS)

    unknown = [name for name in requested if name not in EXPORT_GROUPS]
    if unknown:
        raise CommandError(
            f"Unknown export group(s): {', '.join(unknown)}",
            hint=f"Valid groups: {', '.join(EXPORT_CHOICES)}",
        )

    since = _parse_timestamp(args.since, "since")
    until = _parse_timestamp(args.until, "until")
    if since and until and since > until:
        raise CommandError("--since is later than --until")

    tables: list[str] = []
    for group in requested:
        for name in EXPORT_GROUPS[group]:
            if name not in tables:
                tables.append(name)

    payload: dict[str, Any] = {
        "kind": "homestead-twin-export",
        "format_version": 1,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "platform_version": __version__,
        "site_id": settings.site_id,
        "node_role": settings.node_role,
        "groups": requested,
        "filters": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "limit_per_table": args.limit,
        },
        "tables": {},
        "tables_absent": [],
    }

    with open_session(settings) as session:
        present = _table_names(session.get_bind())
        for name in tables:
            table = metadata.tables.get(name)
            if table is None or name not in present:
                payload["tables_absent"].append(name)
                continue
            payload["tables"][name] = _dump_table(session, table, since=since, until=until, limit=args.limit)

    payload["row_counts"] = {name: len(rows) for name, rows in payload["tables"].items()}
    text = _dumps(payload, args.format)

    if args.output and args.output != "-":
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Could not write {destination}: {exc}") from exc
        total = sum(payload["row_counts"].values())
        print(f"Exported {total} row(s) from {len(payload['tables'])} table(s) to {destination}")
        if payload["tables_absent"]:
            print(f"Absent: {', '.join(payload['tables_absent'])}")
    else:
        sys.stdout.write(text)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Homestead Digital Twin operator CLI. Every subcommand works over SSH on the "
            "primary node and on the secondary control node; subsystems are imported lazily "
            "so a partially deployed node still reports status and takes backups."
        ),
        epilog=(
            "Exit codes: 0 success, 1 failure, 2 usage error, 3 subsystem unavailable. "
            "All settings are overridable through HOMESTEAD_* environment variables."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--database-url",
        help="Override HOMESTEAD_DATABASE_URL for this invocation.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Override the machine-readable design-package directory (default: data/).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("HOMESTEAD_LOG_LEVEL", "INFO"),
        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
        help="Logging verbosity (default: INFO).",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- init-db --------------------------------------------------------
    init_db = subparsers.add_parser(
        "init-db",
        help="Create every platform table in the configured database.",
        description="Create every platform table. Safe to re-run; existing tables are left alone.",
    )
    init_db.add_argument("-v", "--verbose", action="store_true", help="List the tables created.")
    init_db.set_defaults(func=cmd_init_db)

    # --- load-registry --------------------------------------------------
    load_registry = subparsers.add_parser(
        "load-registry",
        help="Load the machine-readable design package into the registry.",
        description=(
            "Load data/asset_class_dictionary.yaml, data/point_dictionary.yaml, "
            "data/homestead_asset_register.yaml and data/point_bindings.yaml into the registry."
        ),
    )
    load_registry.add_argument(
        "--strict", action="store_true", help="Exit non-zero if the loader reports warnings."
    )
    load_registry.set_defaults(func=cmd_load_registry)

    # --- load-all -------------------------------------------------------
    load_all = subparsers.add_parser(
        "load-all",
        help="Load registry + EMS load schedule + alarm definitions.",
        description=(
            "Run every package loader in order: registry, EMS load schedule "
            "(data/load_schedule.yaml), alarm definitions (data/alarm_definitions.yaml)."
        ),
    )
    load_all.add_argument(
        "--skip-missing",
        action="store_true",
        help="Skip stages whose subsystem is not installed instead of failing.",
    )
    load_all.set_defaults(func=cmd_load_all)

    # --- validate -------------------------------------------------------
    validate = subparsers.add_parser(
        "validate",
        help="Validate the design package against its JSON Schemas.",
        description="Run tools/validate_bundle.py: schema checks plus cross-reference checks.",
    )
    validate.add_argument(
        "--write-report",
        action="store_true",
        help=(
            "Refresh the tracked validation_report.json. Off by default so that "
            "validating never dirties the working tree."
        ),
    )
    validate.set_defaults(func=cmd_validate)

    # --- serve ----------------------------------------------------------
    serve = subparsers.add_parser(
        "serve",
        help="Run the digital twin API.",
        description=(
            "Run the FastAPI application. Background services (ingest, command dispatch, alarm "
            "engine, EMS) start according to node role and the HOMESTEAD_* settings."
        ),
    )
    serve.add_argument("--host", help="Bind address (default: HOMESTEAD_API_HOST).")
    serve.add_argument("--port", type=int, help="Bind port (default: HOMESTEAD_API_PORT).")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="Reload on source change. Development only -- never on a control node.",
    )
    serve.set_defaults(func=cmd_serve)

    # --- simulate -------------------------------------------------------
    simulate = subparsers.add_parser(
        "simulate",
        help="Delegate to the simulator CLI.",
        description=(
            "Forward all remaining arguments to the simulator "
            "(equivalent to 'homestead-simulator ...'). Use -- to separate flags."
        ),
    )
    simulate.add_argument(
        "simulator_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed straight through to the simulator CLI.",
    )
    simulate.set_defaults(func=cmd_simulate)

    # --- retention ------------------------------------------------------
    retention = subparsers.add_parser(
        "retention",
        help="Apply the data-retention policy (SDD 16.4).",
        description=(
            "Apply the retention policy to the historian: raw samples are kept for "
            "HOMESTEAD_HISTORIAN_RAW_RETENTION_DAYS; alarm and command audit records are kept "
            "indefinitely (SDD section 16.4)."
        ),
    )
    mode = retention.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Commit the retention changes.")
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the effect and roll back (default).",
    )
    retention.add_argument(
        "--now",
        help="Evaluate retention as at this ISO-8601 instant instead of now (testing).",
    )
    retention.set_defaults(func=cmd_retention)

    # --- backup ---------------------------------------------------------
    backup = subparsers.add_parser(
        "backup",
        help="Write a portable registry + configuration archive.",
        description=(
            "Dump the registry, configuration and design package to one .tar.gz so that "
            "critical data exists outside the combined battery/server container "
            "(SDD section 16.1 mitigation 3). Credentials are never included."
        ),
    )
    backup.add_argument(
        "-o",
        "--output",
        help="Archive path or directory (default: var/backups/homestead-<role>-<UTC>.tar.gz).",
    )
    backup.add_argument(
        "--include-history",
        action="store_true",
        help="Also dump telemetry_samples and ingest_dead_letters (large).",
    )
    backup.set_defaults(func=cmd_backup)

    # --- status ---------------------------------------------------------
    status = subparsers.add_parser(
        "status",
        help="Print node role, database, counts, EMS state and active alarms.",
        description="Read-only health summary. Works on a database that has not been loaded yet.",
    )
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status.set_defaults(func=cmd_status)

    # --- export ---------------------------------------------------------
    export = subparsers.add_parser(
        "export",
        help="Export registry and historical data in an open format (FR-010).",
        description=(
            "Export selected table groups as JSON or YAML. Groups: " + ", ".join(EXPORT_CHOICES) + "."
        ),
    )
    export.add_argument(
        "--format", choices=["json", "yaml"], default="json", help="Output format (default: json)."
    )
    export.add_argument("-o", "--output", help="Output file, or '-' for stdout (default: stdout).")
    export.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GROUP[,GROUP...]",
        help=f"Table groups to include (default: registry). One of: {', '.join(EXPORT_CHOICES)}.",
    )
    export.add_argument("--since", help="Only rows at or after this ISO-8601 timestamp.")
    export.add_argument("--until", help="Only rows at or before this ISO-8601 timestamp.")
    export.add_argument("--limit", type=int, help="Maximum rows per table.")
    export.set_defaults(func=cmd_export)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE

    try:
        return int(handler(args) or EXIT_OK)
    except CommandError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"{PROG}: hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # the CLI is the last line of defence: never a traceback
        LOG.debug("Unhandled exception in %s", getattr(args, "command", "?"), exc_info=True)
        print(f"{PROG}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"{PROG}: hint: re-run with --log-level DEBUG for a traceback.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
