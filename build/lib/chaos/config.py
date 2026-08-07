"""Platform configuration.

Every setting is overridable through the environment so that the same image can
run as the primary node in the power container or as the physically separate
secondary control node described in SDD section 16.1.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: <root>/src/chaos/config.py -> parents[2]
ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
SCHEMA_DIR = ROOT_DIR / "schemas"


class Settings(BaseSettings):
    """Runtime settings for the digital twin core service."""

    model_config = SettingsConfigDict(
        env_prefix="CHAOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Identity -------------------------------------------------------
    node_role: str = "primary"  # primary | secondary
    site_id: str = "site.site.primary.01"
    timezone: str = "America/Toronto"

    # --- Storage --------------------------------------------------------
    # SQLite keeps the platform runnable on a laptop and in CI. PostgreSQL +
    # PostGIS is the deployment target (see deploy/docker-compose.yml).
    database_url: str = f"sqlite:///{ROOT_DIR / 'var' / 'homestead.db'}"
    sql_echo: bool = False

    # --- Machine-readable design package --------------------------------
    data_dir: Path = DATA_DIR
    schema_dir: Path = SCHEMA_DIR

    # --- MQTT -----------------------------------------------------------
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_tls: bool = False
    mqtt_client_id: str = "chaos-core"
    mqtt_base_topic: str = "chaos"
    mqtt_enabled: bool = True

    # --- Historian ------------------------------------------------------
    # "sql" writes samples to the relational historian table (works everywhere).
    historian_backend: str = "sql"
    historian_raw_retention_days: int = 90

    # --- Telemetry semantics -------------------------------------------
    default_stale_after_s: int = 60

    # --- API ------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_title: str = "Project CHAOS — Homestead Digital Twin API"
    api_root_path: str = ""

    # --- Control safety -------------------------------------------------
    # Global master switch. Physical control stays disabled until a subsystem
    # has passed the SDD section 19 commissioning sequence.
    allow_physical_control: bool = False
    command_default_ttl_s: int = 300

    # --- EMS ------------------------------------------------------------
    ems_enabled: bool = True
    ems_tick_interval_s: int = 5

    # --- Alarms / notification -----------------------------------------
    alarm_engine_enabled: bool = True
    notification_backends: str = "log"  # comma separated: log,email,push,voice

    @property
    def is_secondary(self) -> bool:
        return self.node_role.lower() == "secondary"

    @property
    def notification_backend_list(self) -> list[str]:
        return [b.strip() for b in self.notification_backends.split(",") if b.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
