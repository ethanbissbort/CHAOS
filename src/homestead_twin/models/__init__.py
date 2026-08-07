"""SQLAlchemy models for the Homestead Digital Twin registry.

Importing this package registers every mapper, which is what
``homestead_twin.db.create_all`` relies on.
"""

from homestead_twin.models.alarms import (
    ALARM_STATES,
    SEVERITIES,
    Alarm,
    AlarmDefinition,
    AlarmEvent,
    Incident,
    NotificationLog,
)
from homestead_twin.models.base import Base, JSONType, TimestampMixin, new_uuid, utcnow
from homestead_twin.models.commands import (
    COMMAND_STATES,
    TERMINAL_COMMAND_STATES,
    AuditLogEntry,
    Command,
    CommandResult,
    ModeTransition,
    OperatingMode,
)
from homestead_twin.models.energy import (
    ENERGY_STATES,
    LATCHING_ENERGY_STATES,
    EnergyStateSnapshot,
    EnergyStateTransition,
    LoadShedAction,
    PowerBudgetLease,
    PowerLoadProfile,
)
from homestead_twin.models.maintenance import (
    Calibration,
    CommissioningRecord,
    Inspection,
    MaintenancePlan,
    SparePart,
    WorkOrder,
)
from homestead_twin.models.registry import (
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
from homestead_twin.models.telemetry import CurrentState, IngestDeadLetter, TelemetrySample

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
