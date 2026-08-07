"""Alarm and notification subsystem (SDD sections 14, 16.1, 37 and FR-007/FR-008).

The subsystem is four cooperating pieces:

``definitions``
    Loads ``data/alarm_definitions.yaml`` into :class:`~homestead_twin.models.alarms.AlarmDefinition`
    rows, idempotently, and refuses definitions whose points or assets do not
    resolve against the machine-readable design package.

``evaluator``
    Walks the enabled definitions against the current-state cache, applies
    on-delay, off-delay and hysteresis, drives the SDD 14.2 lifecycle
    ``detected -> active -> acknowledged -> mitigated -> cleared -> reviewed``
    and honours maintenance-mode behaviour and measurement quality.

``correlation``
    Groups alarms into :class:`~homestead_twin.models.alarms.Incident` records by
    walking the registry relationship graph, so that a power-container outage
    produces one incident with many member alarms rather than hundreds of
    notifications (SDD 14.2).

``notify``
    Routes an incident (not each member alarm) to the configured channels,
    escalates unacknowledged critical alarms, and records every attempt --
    including the honest ``not_configured`` result for channels that have no
    working implementation yet.

Two rules run through all of it:

1. Severity ``emergency`` means local alarms and protective actions execute
   independently of this platform (SDD 14.1). Nothing here performs a
   protective action; the platform reports.
2. Nothing is silently dropped. A suppressed alarm is still a row with
   ``suppressed=True`` and a machine-readable reason.
"""

from __future__ import annotations

from homestead_twin.alarms.correlation import (
    CorrelationEngine,
    CorrelationResult,
    DependencyGraph,
)
from homestead_twin.alarms.definitions import (
    DEFAULT_DEFINITIONS_PATH,
    DefinitionError,
    DefinitionSyncResult,
    definition_meta,
    load_document,
    sync_definitions,
    validate_document,
)
from homestead_twin.alarms.evaluator import (
    ACTIVE_STATES,
    OPEN_STATES,
    QUALITY_BLOCKING,
    AlarmEvaluator,
    EvaluationResult,
    SuppressionReason,
)
from homestead_twin.alarms.notify import (
    CHANNEL_NAMES,
    NotificationResult,
    Notifier,
    build_channels,
)
from homestead_twin.alarms.service import AlarmCycleResult, AlarmEngineService

__all__ = [
    "ACTIVE_STATES",
    "CHANNEL_NAMES",
    "DEFAULT_DEFINITIONS_PATH",
    "OPEN_STATES",
    "QUALITY_BLOCKING",
    "AlarmCycleResult",
    "AlarmEngineService",
    "AlarmEvaluator",
    "CorrelationEngine",
    "CorrelationResult",
    "DefinitionError",
    "DefinitionSyncResult",
    "DependencyGraph",
    "EvaluationResult",
    "NotificationResult",
    "Notifier",
    "SuppressionReason",
    "build_channels",
    "definition_meta",
    "load_document",
    "sync_definitions",
    "validate_document",
]
