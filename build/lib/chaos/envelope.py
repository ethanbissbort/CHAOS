"""Wire envelopes for telemetry, commands, events and availability.

These are the documented schemas required by SDD FR-003, FR-004 and MVP
acceptance criterion 3 ("MQTT telemetry follows one documented schema").
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chaos.models.base import utcnow

SCHEMA_VERSION = 1

QUALITY_CODES = (
    "good",
    "uncertain",
    "bad",
    "stale",
    "substituted",
    "calculated",
    "maintenance",
)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)

    def to_payload(self) -> bytes:
        return self.to_json().encode("utf-8")


class TelemetryEnvelope(_Envelope):
    """SDD section 10.2 standard telemetry envelope."""

    ts: dt.datetime = Field(default_factory=utcnow)
    asset_id: str
    point: str
    value: Any
    unit: str | None = None
    quality: str = "good"
    source: str | None = None
    sequence: int | None = None
    schema_version: int = SCHEMA_VERSION

    @field_validator("quality")
    @classmethod
    def _check_quality(cls, value: str) -> str:
        if value not in QUALITY_CODES:
            raise ValueError(f"quality must be one of {QUALITY_CODES}, got {value!r}")
        return value

    @property
    def point_id(self) -> str:
        return f"{self.asset_id}/{self.point}"


class CommandEnvelope(_Envelope):
    """SDD section 10.3 command envelope."""

    command_id: str
    issued_at: dt.datetime = Field(default_factory=utcnow)
    issued_by: str
    asset_id: str
    command: str
    value: Any = None
    reason: str
    expires_at: dt.datetime | None = None
    requires_ack: bool = True
    operating_mode: str | None = None
    priority: int = 100
    schema_version: int = SCHEMA_VERSION


class CommandAckEnvelope(_Envelope):
    """Acknowledgement / final result published by a device or controller."""

    command_id: str
    asset_id: str
    result: Literal["accepted", "rejected", "succeeded", "failed", "expired"]
    detail: str | None = None
    reported_by: str | None = None
    reported_at: dt.datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] | None = None
    schema_version: int = SCHEMA_VERSION


class EventEnvelope(_Envelope):
    """Timestamped discrete occurrence (point class ``EVENT``)."""

    ts: dt.datetime = Field(default_factory=utcnow)
    asset_id: str
    event: str
    detail: dict[str, Any] | None = None
    source: str | None = None
    schema_version: int = SCHEMA_VERSION


class AvailabilityEnvelope(_Envelope):
    """Retained availability / last-will payload (SDD section 8.2)."""

    asset_id: str
    state: Literal["online", "offline", "degraded", "unknown"]
    ts: dt.datetime = Field(default_factory=utcnow)
    source: str | None = None
    schema_version: int = SCHEMA_VERSION


def parse_telemetry(payload: bytes | str) -> TelemetryEnvelope:
    return TelemetryEnvelope.model_validate(_loads(payload))


def parse_command(payload: bytes | str) -> CommandEnvelope:
    return CommandEnvelope.model_validate(_loads(payload))


def parse_command_ack(payload: bytes | str) -> CommandAckEnvelope:
    return CommandAckEnvelope.model_validate(_loads(payload))


def parse_availability(payload: bytes | str) -> AvailabilityEnvelope:
    return AvailabilityEnvelope.model_validate(_loads(payload))


def _loads(payload: bytes | str) -> Any:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return json.loads(payload)
