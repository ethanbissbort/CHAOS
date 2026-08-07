"""SQLAlchemy models for the Homestead Digital Twin registry.

Importing this package registers every mapper, which is what
``chaos.db.create_all`` relies on.
"""

from chaos.models.alarms import (
    ALARM_STATES,
    SEVERITIES,
    Alarm,
    AlarmDefinition,
    AlarmEvent,
    Incident,
    NotificationLog,
)
from chaos.models.base import Base, JSONType, TimestampMixin, new_uuid, utcnow
from chaos.models.commands import (
    COMMAND_STATES,
    TERMINAL_COMMAND_STATES,
    AuditLogEntry,
    Command,
    CommandResult,
    ModeTransition,
    OperatingMode,
)
from chaos.models.energy import (
    ENERGY_STATES,
    LATCHING_ENERGY_STATES,
    EnergyStateSnapshot,
    EnergyStateTransition,
    LoadShedAction,
    PowerBudgetLease,
    PowerLoadProfile,
)
from chaos.models.maintenance import (
    Calibration,
    CommissioningRecord,
    Inspection,
    MaintenancePlan,
    SparePart,
    WorkOrder,
)
from chaos.models.registry import (
    Asset,
    AssetClass,
    AssetRelationship,
    ConfigurationRevision,
    Document,
    ExternalIdentifier,
    Location,
    Point,
    PointBinding,
    PointDefinition,
    PointProfile,
    PointSampleIndex,
)
from chaos.models.telemetry import CurrentState, IngestDeadLetter, TelemetrySample

__all__ = [
    "ALARM_STATES",
    "COMMAND_STATES",
    "ENERGY_STATES",
    "LATCHING_ENERGY_STATES",
    "SEVERITIES",
    "TERMINAL_COMMAND_STATES",
    "Alarm",
    "AlarmDefinition",
    "AlarmEvent",
    "Asset",
    "AssetClass",
    "AssetRelationship",
    "AuditLogEntry",
    "Base",
    "Calibration",
    "Command",
    "CommandResult",
    "CommissioningRecord",
    "ConfigurationRevision",
    "CurrentState",
    "Document",
    "EnergyStateSnapshot",
    "EnergyStateTransition",
    "ExternalIdentifier",
    "Incident",
    "IngestDeadLetter",
    "Inspection",
    "JSONType",
    "LoadShedAction",
    "Location",
    "MaintenancePlan",
    "ModeTransition",
    "NotificationLog",
    "OperatingMode",
    "Point",
    "PointBinding",
    "PointDefinition",
    "PointProfile",
    "PointSampleIndex",
    "PowerBudgetLease",
    "PowerLoadProfile",
    "SparePart",
    "TelemetrySample",
    "TimestampMixin",
    "WorkOrder",
    "new_uuid",
    "utcnow",
]
