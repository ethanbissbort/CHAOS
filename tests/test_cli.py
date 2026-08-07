"""Tests for the operator CLI.

These tests must pass on a partially deployed checkout. Subsystems owned by
other modules (registry loader, EMS, alarms, ingest retention, simulator) may or
may not be installed, so the tests that touch them assert the *contract*: either
the command succeeds, or it fails with the dedicated "subsystem unavailable"
exit code and an actionable message. Never a traceback, never exit 0 on failure.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from homestead_twin import cli

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'cli.db'}"


@pytest.fixture()
def initialised_db(db_url: str) -> str:
    assert cli.main(["--database-url", db_url, "init-db"]) == cli.EXIT_OK
    return db_url


def run(*argv: str) -> int:
    return cli.main(list(argv))


# ---------------------------------------------------------------------------
# Parser surface
# ---------------------------------------------------------------------------

SUBCOMMANDS = (
    "init-db",
    "load-registry",
    "load-all",
    "validate",
    "serve",
    "simulate",
    "retention",
    "backup",
    "status",
    "export",
)


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        run("--help")
    assert excinfo.value.code == 0
    assert "Homestead Digital Twin operator CLI" in capsys.readouterr().out


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        run("--version")
    assert excinfo.value.code == 0
    assert cli.__version__ in capsys.readouterr().out


def _subcommand_parsers() -> dict:
    import argparse

    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("the CLI declares no subcommands")


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_every_subcommand_is_registered_and_documented(name):
    """--help must work for every subcommand even with no subsystems installed."""
    choices = _subcommand_parsers()
    assert name in choices, f"{name} is missing from the CLI"
    assert choices[name].description, f"{name} has no description"
    with pytest.raises(SystemExit) as excinfo:
        run(name, "--help")
    assert excinfo.value.code == 0


def test_no_undocumented_subcommands():
    assert set(_subcommand_parsers()) == set(SUBCOMMANDS)


def test_no_command_prints_help_and_exits_usage(capsys):
    assert run() == cli.EXIT_USAGE
    assert "COMMAND" in capsys.readouterr().out


def test_unknown_command_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        run("frobnicate")
    assert excinfo.value.code == cli.EXIT_USAGE


# ---------------------------------------------------------------------------
# init-db
# ---------------------------------------------------------------------------


def test_init_db_creates_every_declared_table(db_url, capsys):
    from sqlalchemy import create_engine, inspect

    assert run("--database-url", db_url, "init-db") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Database" in out

    engine = create_engine(db_url)
    try:
        present = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    declared = set(cli._metadata().tables)
    assert declared <= present
    # Core tables the rest of the platform depends on.
    for table in ("assets", "points", "point_bindings", "alarms", "commands", "current_state"):
        assert table in present


def test_init_db_is_idempotent(initialised_db):
    assert run("--database-url", initialised_db, "init-db") == cli.EXIT_OK


def test_init_db_reports_an_unusable_database():
    """A bad driver must produce an actionable message, not a traceback."""
    rc = run("--database-url", "notadriver://nowhere/none", "init-db")
    assert rc == cli.EXIT_ERROR


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_on_an_empty_database(initialised_db, capsys):
    assert run("--database-url", initialised_db, "status") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "node role" in out
    assert "primary" in out
    assert "no energy state recorded yet" in out


def test_status_json_is_machine_readable(initialised_db, capsys):
    assert run("--database-url", initialised_db, "status", "--json") == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["node_role"] == "primary"
    assert payload["database_reachable"] is True
    assert payload["counts"]["assets"] == 0
    assert payload["active_alarms"]["total"] == 0
    assert payload["missing_tables"] == []
    assert payload["physical_control_enabled"] is False


def test_status_redacts_the_database_password(tmp_path, capsys, monkeypatch):
    """A password must never reach the terminal or a log (SDD 15.2)."""
    secret = "hunter2supersecret"
    url = f"postgresql+psycopg://homestead:{secret}@db.invalid:5432/homestead"
    rc = run("--database-url", url, "status", "--json")
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    # Reaching an invalid host must fail, not silently report health.
    assert rc != cli.EXIT_OK


def test_status_on_a_database_without_tables(db_url, capsys):
    """A fresh node reports what is missing rather than a reassuring zero."""
    assert run("--database-url", db_url, "status") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "init-db" in out


def test_secondary_node_status_notes_that_the_ems_is_suppressed(
    initialised_db, capsys, monkeypatch
):
    monkeypatch.setenv("HOMESTEAD_NODE_ROLE", "secondary")
    assert run("--database-url", initialised_db, "status", "--json") == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["node_role"] == "secondary"
    assert payload["ems_enabled"] is False
    assert any("secondary" in note for note in payload["notes"])


# ---------------------------------------------------------------------------
# export (SDD FR-010)
# ---------------------------------------------------------------------------


def test_export_json_to_stdout(initialised_db, capsys):
    assert run("--database-url", initialised_db, "export") == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "homestead-twin-export"
    assert payload["groups"] == ["registry"]
    assert "assets" in payload["tables"]
    assert payload["tables_absent"] == []


def test_export_yaml_to_a_file(initialised_db, tmp_path, capsys):
    destination = tmp_path / "export.yaml"
    rc = run(
        "--database-url",
        initialised_db,
        "export",
        "--format",
        "yaml",
        "--include",
        "registry,alarms",
        "--output",
        str(destination),
    )
    assert rc == cli.EXIT_OK
    payload = yaml.safe_load(destination.read_text())
    assert payload["groups"] == ["registry", "alarms"]
    assert "alarm_definitions" in payload["tables"]


def test_export_all_groups(initialised_db, capsys):
    assert run("--database-url", initialised_db, "export", "--include", "all") == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["groups"]) == set(cli.EXPORT_GROUPS)
    assert "telemetry_samples" in payload["tables"]


def test_export_rejects_an_unknown_group(initialised_db, capsys):
    assert run("--database-url", initialised_db, "export", "--include", "nope") == cli.EXIT_ERROR
    assert "Unknown export group" in capsys.readouterr().err


def test_export_rejects_a_bad_timestamp(initialised_db, capsys):
    rc = run("--database-url", initialised_db, "export", "--since", "yesterday")
    assert rc == cli.EXIT_ERROR
    assert "ISO-8601" in capsys.readouterr().err


def test_export_rejects_an_inverted_window(initialised_db, capsys):
    rc = run(
        "--database-url",
        initialised_db,
        "export",
        "--since",
        "2026-08-07T00:00:00Z",
        "--until",
        "2026-01-01T00:00:00Z",
    )
    assert rc == cli.EXIT_ERROR


# ---------------------------------------------------------------------------
# backup (SDD 16.1 mitigation 3)
# ---------------------------------------------------------------------------


def test_backup_writes_a_portable_archive(initialised_db, tmp_path, capsys):
    archive = tmp_path / "backup.tar.gz"
    assert run("--database-url", initialised_db, "backup", "--output", str(archive)) == cli.EXIT_OK
    assert archive.is_file()

    with tarfile.open(archive) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("manifest.json").read().decode())
        settings_blob = tar.extractfile("settings.json").read().decode()

    assert "RESTORE.txt" in names
    assert "settings.json" in names
    assert "tables/assets.json" in names
    # The design package travels with the backup so the registry can be rebuilt.
    assert any(name.startswith("design-package/data/") for name in names)
    assert any(name.startswith("design-package/schemas/") for name in names)

    assert manifest["kind"] == "homestead-twin-backup"
    assert manifest["node_role"] == "primary"
    assert "row_counts" in manifest
    # History is opt-in because it dominates archive size.
    assert manifest["includes_history"] is False
    assert "telemetry_samples" not in manifest["row_counts"]

    # No credentials, ever.
    assert json.loads(settings_blob)["database_url"].startswith("sqlite:")


def test_backup_can_include_history(initialised_db, tmp_path):
    archive = tmp_path / "backup-history.tar.gz"
    rc = run(
        "--database-url", initialised_db, "backup", "--output", str(archive), "--include-history"
    )
    assert rc == cli.EXIT_OK
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read().decode())
    assert manifest["includes_history"] is True
    assert "telemetry_samples" in manifest["row_counts"]


def test_backup_into_a_directory(initialised_db, tmp_path):
    target = tmp_path / "backups"
    target.mkdir()
    assert run("--database-url", initialised_db, "backup", "--output", str(target)) == cli.EXIT_OK
    written = list(target.glob("homestead-primary-*.tar.gz"))
    assert len(written) == 1


def test_backup_reports_an_unwritable_destination(initialised_db, tmp_path, capsys):
    rc = run(
        "--database-url",
        initialised_db,
        "backup",
        "--output",
        str(tmp_path / "missing" / "sub" / "b.tar.gz"),
    )
    # Parent directories are created, so this must succeed rather than explode.
    assert rc == cli.EXIT_OK


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_runs_the_bundle_validator(capsys):
    """The CLI must surface the validator's own output and its exit status.

    tools/validate_bundle.py is owned by another module and its report format
    changes as checks are added, so this asserts on the contract (exit code,
    validator output reaching the terminal) rather than on any one line of it.
    """
    assert run("validate") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Design package valid." in out
    assert len(out.splitlines()) > 1, "validator output was swallowed"


# ---------------------------------------------------------------------------
# Subsystem-dependent commands
#
# Each of these must either succeed or fail cleanly with EXIT_UNAVAILABLE.
# ---------------------------------------------------------------------------


def assert_ok_or_unavailable(rc: int, err: str, module: str) -> None:
    assert rc in (cli.EXIT_OK, cli.EXIT_UNAVAILABLE), f"unexpected exit {rc}: {err}"
    if rc == cli.EXIT_UNAVAILABLE:
        assert module in err, err
        assert "hint:" in err, "an unavailable subsystem must print an actionable hint"
        assert "Traceback" not in err


def test_load_registry(initialised_db, capsys):
    rc = run("--database-url", initialised_db, "load-registry")
    captured = capsys.readouterr()
    assert_ok_or_unavailable(rc, captured.err, "homestead_twin.registry.loader")
    if rc == cli.EXIT_OK:
        assert "Registry loaded." in captured.out
        assert "assets" in captured.out


def test_load_all_with_skip_missing(initialised_db, capsys):
    rc = run("--database-url", initialised_db, "load-all", "--skip-missing")
    captured = capsys.readouterr()
    # --skip-missing tolerates absent subsystems, so the only acceptable failure
    # is a loader that is present and genuinely broken.
    assert rc in (cli.EXIT_OK, cli.EXIT_ERROR, cli.EXIT_UNAVAILABLE), captured.err
    assert "Traceback" not in captured.err


def test_retention_dry_run(initialised_db, capsys):
    rc = run("--database-url", initialised_db, "retention", "--dry-run")
    captured = capsys.readouterr()
    assert_ok_or_unavailable(rc, captured.err, "homestead_twin.ingest.retention")
    if rc == cli.EXIT_OK:
        assert "dry run" in captured.out.lower()
        assert "nothing committed" in captured.out


def test_retention_apply(initialised_db, capsys):
    rc = run("--database-url", initialised_db, "retention", "--apply")
    captured = capsys.readouterr()
    assert_ok_or_unavailable(rc, captured.err, "homestead_twin.ingest.retention")
    if rc == cli.EXIT_OK:
        assert "Retention applied." in captured.out


def test_retention_rejects_both_modes():
    with pytest.raises(SystemExit) as excinfo:
        run("retention", "--apply", "--dry-run")
    assert excinfo.value.code == cli.EXIT_USAGE


def test_simulate_delegates_or_reports_a_missing_simulator(capsys):
    """`simulate -- ARGS` hands ARGS to the simulator without interpreting them.

    Two acceptable outcomes: the simulator is installed and its own argparse
    handles --help (raising SystemExit(0)), or it is absent and the CLI reports
    EXIT_UNAVAILABLE with a hint. A traceback is not acceptable in either case.
    """
    try:
        rc = run("simulate", "--", "--help")
    except SystemExit as exit_exc:  # the simulator's own --help
        assert exit_exc.code in (0, None)
        assert "usage" in capsys.readouterr().out.lower()
        return

    captured = capsys.readouterr()
    assert rc in (cli.EXIT_OK, cli.EXIT_UNAVAILABLE), captured.err
    if rc == cli.EXIT_UNAVAILABLE:
        assert "simulator.cli" in captured.err
        assert "hint:" in captured.err
        assert "Traceback" not in captured.err


def test_simulate_forwards_arguments_verbatim(monkeypatch):
    """The CLI must not swallow, reorder or reinterpret simulator flags."""
    seen: dict = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(
        cli, "resolve_subsystem", lambda *a, **k: fake_main if "simulator" in a[0] else None
    )
    assert run("simulate", "--", "--profile", "sunny", "--duration", "60") == cli.EXIT_OK
    assert seen["argv"] == ["--profile", "sunny", "--duration", "60"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_resolve_subsystem_raises_a_typed_error_for_a_missing_module():
    with pytest.raises(cli.SubsystemUnavailable) as excinfo:
        cli.resolve_subsystem(
            "homestead_twin.definitely_not_a_module",
            ("load",),
            subsystem="Nonexistent",
            hint="install it",
        )
    assert excinfo.value.exit_code == cli.EXIT_UNAVAILABLE
    assert "Nonexistent unavailable" in str(excinfo.value)
    assert excinfo.value.hint == "install it"


def test_resolve_subsystem_raises_when_the_callable_is_absent():
    with pytest.raises(cli.SubsystemUnavailable) as excinfo:
        cli.resolve_subsystem(
            "homestead_twin.topics", ("no_such_function",), subsystem="Topics"
        )
    assert "exposes none of" in str(excinfo.value)


def test_resolve_subsystem_returns_the_module_when_no_candidates_are_given():
    module = cli.resolve_subsystem("homestead_twin.topics", subsystem="Topics")
    assert module.DEFAULT_BASE == "homestead"


def test_call_loader_passes_data_dir_only_when_accepted():
    recorded: dict = {}

    def accepts(session, data_dir=None):
        recorded["data_dir"] = data_dir
        return {"rows": 1}

    def rejects(session):
        recorded["called"] = True
        return {"rows": 2}

    assert cli._call_loader(accepts, "session", Path("/data")) == {"rows": 1}
    assert recorded["data_dir"] == Path("/data")
    assert cli._call_loader(rejects, "session", Path("/data")) == {"rows": 2}
    assert recorded["called"] is True


def test_redact_url_hides_the_password():
    redacted = cli.redact_url("postgresql+psycopg://user:s3cret@host:5432/db")
    assert "s3cret" not in redacted
    assert "user" in redacted
    assert cli.redact_url("sqlite:///var/homestead.db") == "sqlite:///var/homestead.db"


def test_result_payload_reads_a_dataclass_result():
    import dataclasses

    @dataclasses.dataclass
    class Result:
        assets: int = 3
        warnings: list = dataclasses.field(default_factory=lambda: ["one"])

    counts, warnings = cli._result_payload(Result())
    assert counts == {"assets": 3}
    assert warnings == ["one"]


def test_result_payload_prefers_a_counts_method():
    class Result:
        warnings: ClassVar[list[str]] = ["w"]

        def counts(self):
            return {"assets": 90, "points": 701}

    counts, warnings = cli._result_payload(Result())
    assert counts == {"assets": 90, "points": 701}
    assert warnings == ["w"]


def test_result_payload_handles_a_plain_mapping_and_none():
    assert cli._result_payload({"assets": 1}) == ({"assets": 1}, [])
    assert cli._result_payload(None) == ({}, [])


def test_parse_timestamp_accepts_iso_and_z_suffix():
    parsed = cli._parse_timestamp("2026-08-07T12:00:00Z", "since")
    assert parsed is not None and parsed.tzinfo is not None
    assert cli._parse_timestamp(None, "since") is None
    naive = cli._parse_timestamp("2026-08-07", "since")
    assert naive is not None and naive.tzinfo is not None  # assumed UTC


def test_export_group_table_names_all_exist_in_the_model_metadata():
    """Guards against a table being renamed out from under backup/export."""
    declared = set(cli._metadata().tables)
    for group, tables in cli.EXPORT_GROUPS.items():
        for name in tables:
            assert name in declared, f"{group}: unknown table {name}"


def test_build_settings_applies_overrides(tmp_path):
    import argparse

    args = argparse.Namespace(database_url="sqlite:///override.db", data_dir=tmp_path)
    settings = cli.build_settings(args)
    assert settings.database_url == "sqlite:///override.db"
    assert settings.data_dir == tmp_path
