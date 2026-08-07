"""Notification channels, routing and escalation (SDD FR-007, 14.3).

    "FR-007: The platform shall support email and push alerts, with optional
    CUCM/voice escalation for critical events."

Only the ``log`` channel has a working implementation. ``email``, ``push`` and
``voice`` are honest stubs: they record a :class:`NotificationLog` row with
``status="not_configured"`` and a detail explaining exactly what is missing.
They never return success, and nothing in this module reports that an alert was
delivered when it was not. An alerting path you believe in but that does not
exist is worse than no alerting path.

Routing rules:

* **The incident is the unit of notification, not the alarm.** A member alarm of
  an incident is suppressed by the correlation engine and never notified
  individually -- that is what stops a power-container outage becoming a
  hundred messages (SDD 14.2).
* **Severity chooses the channels**, and the definition's ``escalation_path``
  can override the routing per stage.
* **Escalation is time-driven and explicit.** Stage N fires once
  ``after_s`` seconds have elapsed since the alarm activated, and only while the
  alarm is still unacknowledged. Acknowledging stops escalation; it does not
  clear the alarm.
* **Re-notification** repeats the highest reached stage every
  ``renotify_after_s`` seconds for alarms that stay unacknowledged.

Every attempt on every channel is a row, so "why did nobody get called" is
answerable after the fact.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.alarms.definitions import definition_meta
from homestead_twin.alarms.evaluator import (
    ACTIVE_STATES,
    SuppressionReason,
    as_utc,
    elapsed_s,
    severity_rank,
)
from homestead_twin.config import Settings, get_settings
from homestead_twin.models.alarms import Alarm, AlarmDefinition, Incident, NotificationLog
from homestead_twin.models.base import utcnow

logger = logging.getLogger(__name__)

CHANNEL_NAMES = ("log", "email", "push", "voice")

#: Default severity -> channel routing. A definition's escalation_path overrides it.
SEVERITY_ROUTING: dict[str, tuple[str, ...]] = {
    "info": ("log",),
    "warning": ("log",),
    "major": ("log", "email"),
    "critical": ("log", "email", "push"),
    "emergency": ("log", "email", "push", "voice"),
}

#: Severities at or above which an unacknowledged alarm keeps being escalated.
ESCALATING_SEVERITIES = frozenset({"critical", "emergency"})

STATUS_SENT = "sent"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class NotificationMessage:
    """One thing to tell somebody."""

    subject: str
    body: str
    severity: str
    stage: int
    reason: str
    alarm_id: str | None = None
    incident_id: str | None = None
    recipient_role: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelResult:
    status: str
    detail: str | None = None
    recipient: str | None = None


class NotificationChannel(Protocol):
    name: str
    available: bool

    def send(self, message: NotificationMessage) -> ChannelResult: ...


class LogChannel:
    """Always available. Writes the platform log line that the row mirrors."""

    name = "log"
    available = True

    def send(self, message: NotificationMessage) -> ChannelResult:
        logger.warning(
            "ALARM[%s] stage=%d %s -- %s",
            message.severity.upper(),
            message.stage,
            message.subject,
            message.reason,
        )
        return ChannelResult(status=STATUS_SENT, detail="written to the platform log", recipient="local-log")


class _UnconfiguredChannel:
    """Base for a channel that has no working implementation yet.

    It records the attempt and says so. It does not pretend.
    """

    name = "unconfigured"
    available = False
    requirement = "no transport configured"

    def send(self, message: NotificationMessage) -> ChannelResult:
        logger.info(
            "Notification channel %r is not configured; recording the attempt for %s",
            self.name,
            message.incident_id or message.alarm_id,
        )
        return ChannelResult(
            status=STATUS_NOT_CONFIGURED,
            detail=(
                f"Channel '{self.name}' has no working implementation in this release. "
                f"Required before it can deliver: {self.requirement}. "
                "No message was sent."
            ),
            recipient=None,
        )


class EmailChannel(_UnconfiguredChannel):
    name = "email"
    requirement = (
        "an SMTP relay reachable without internet access (SDD 5.1 local-first), "
        "sender identity, and an operator address book"
    )


class PushChannel(_UnconfiguredChannel):
    name = "push"
    requirement = (
        "a push provider or self-hosted notification service, device registration, "
        "and a policy for what leaves the property network"
    )


class VoiceChannel(_UnconfiguredChannel):
    """CUCM/voice escalation (SDD FR-007, FR-903)."""

    name = "voice"
    requirement = (
        "the CUCM VM commissioned (it.application_service.rack_01.cucm_01 is still a "
        "planned functional position), a SIP route to the voice endpoints on VLAN 60, "
        "and an escalation directory"
    )


_CHANNEL_TYPES: dict[str, type] = {
    "log": LogChannel,
    "email": EmailChannel,
    "push": PushChannel,
    "voice": VoiceChannel,
}


class _NotEnabledChannel:
    """A channel an alarm's escalation path names but this node has not enabled.

    The attempt is still recorded. An escalation path whose channels quietly do
    not exist on this node is how "we called you" becomes "nobody called".
    """

    available = False

    def __init__(self, wrapped: NotificationChannel) -> None:
        self._wrapped = wrapped
        self.name = wrapped.name

    def send(self, message: NotificationMessage) -> ChannelResult:
        return ChannelResult(
            status=STATUS_NOT_CONFIGURED,
            detail=(
                f"Channel '{self.name}' is named in this alarm's escalation path but is not "
                "enabled in HOMESTEAD_NOTIFICATION_BACKENDS on this node. No message was sent."
            ),
            recipient=None,
        )


def build_channels(settings: Settings | None = None) -> dict[str, NotificationChannel]:
    """Instantiate the channels named by ``settings.notification_backends``.

    ``log`` is always present: losing the configured backends must not make the
    platform silent.
    """
    settings = settings or get_settings()
    channels: dict[str, NotificationChannel] = {"log": LogChannel()}
    for name in settings.notification_backend_list:
        key = name.strip().lower()
        channel_type = _CHANNEL_TYPES.get(key)
        if channel_type is None:
            logger.warning("Unknown notification backend %r; ignoring", name)
            continue
        channels[key] = channel_type()
    return channels


@dataclass
class NotificationResult:
    sent: list[NotificationLog] = field(default_factory=list)
    not_configured: list[NotificationLog] = field(default_factory=list)
    #: One dispatch = one escalation stage for one incident or alarm, fanned out
    #: across that stage's channels. This is the number a human would call
    #: "how many times was I told about this".
    dispatches: int = 0
    skipped_duplicate: int = 0
    escalated: int = 0

    @property
    def records(self) -> list[NotificationLog]:
        return [*self.sent, *self.not_configured]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dispatches": self.dispatches,
            "sent": len(self.sent),
            "not_configured": len(self.not_configured),
            "skipped_duplicate": self.skipped_duplicate,
            "escalated": self.escalated,
        }


class Notifier:
    """Routes incidents and standalone alarms to the configured channels."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        *,
        channels: dict[str, NotificationChannel] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.channels = channels if channels is not None else build_channels(self.settings)
        self._definitions: dict[str, AlarmDefinition] | None = None

    @property
    def definitions(self) -> dict[str, AlarmDefinition]:
        if self._definitions is None:
            self._definitions = {
                d.alarm_key: d for d in self.session.scalars(select(AlarmDefinition)).all()
            }
        return self._definitions

    # -- routing -----------------------------------------------------------

    def escalation_stages(self, definition: AlarmDefinition | None, severity: str) -> list[dict]:
        """Stages for this alarm: the definition's path, or a severity default."""
        if definition is not None and definition.escalation_path:
            stages = []
            for index, stage in enumerate(definition.escalation_path, start=1):
                stages.append(
                    {
                        "stage": int(stage.get("stage", index)),
                        "after_s": int(stage.get("after_s", 0)),
                        "channels": [str(c).lower() for c in stage.get("channels") or ()],
                        "role": stage.get("role", "operator"),
                    }
                )
            return sorted(stages, key=lambda s: (s["after_s"], s["stage"]))
        return [
            {
                "stage": 1,
                "after_s": 0,
                "channels": list(SEVERITY_ROUTING.get(severity, ("log",))),
                "role": "operator",
            }
        ]

    def _resolve_channels(self, names: list[str]) -> list[NotificationChannel]:
        """Resolve escalation-path channel names to channel objects.

        A channel named in the escalation path but absent from this node's
        ``notification_backends`` is still attempted, through a stub that records
        why nothing was delivered. Silently dropping it would leave an escalation
        path that looks complete on paper and reaches nobody in practice.
        """
        resolved: list[NotificationChannel] = []
        for name in names:
            channel = self.channels.get(name)
            if channel is None:
                channel_type = _CHANNEL_TYPES.get(name)
                if channel_type is None:
                    logger.warning("Escalation path names unknown channel %r", name)
                    continue
                channel = _NotEnabledChannel(channel_type())
            resolved.append(channel)
        return resolved

    # -- deduplication ------------------------------------------------------

    def _existing_stages(self, *, incident_id: str | None, alarm_id: str | None) -> dict[int, dt.datetime]:
        statement = select(NotificationLog)
        if incident_id:
            statement = statement.where(NotificationLog.incident_id == incident_id)
        elif alarm_id:
            statement = statement.where(
                NotificationLog.alarm_id == alarm_id, NotificationLog.incident_id.is_(None)
            )
        else:
            return {}
        stages: dict[int, dt.datetime] = {}
        for row in self.session.scalars(statement).all():
            stage = _stage_of(row)
            if stage is None:
                continue
            sent_at = as_utc(row.sent_at)
            previous = stages.get(stage)
            if previous is None or sent_at > previous:
                stages[stage] = sent_at
        return stages

    # -- dispatch -----------------------------------------------------------

    def _dispatch(
        self, message: NotificationMessage, now: dt.datetime, result: NotificationResult
    ) -> None:
        stage_channels = message.context.get("channels") or ["log"]
        result.dispatches += 1
        for channel in self._resolve_channels(list(stage_channels)):
            outcome = channel.send(message)
            record = NotificationLog(
                alarm_id=message.alarm_id,
                incident_id=message.incident_id,
                channel=channel.name,
                recipient=outcome.recipient or message.recipient_role,
                subject=message.subject[:300],
                body=message.body,
                status=outcome.status,
                detail=json.dumps(
                    {
                        "stage": message.stage,
                        "severity": message.severity,
                        "reason": message.reason,
                        "role": message.recipient_role,
                        "channel_detail": outcome.detail,
                        **{k: v for k, v in message.context.items() if k != "channels"},
                    },
                    default=str,
                ),
                sent_at=now,
            )
            self.session.add(record)
            if outcome.status == STATUS_SENT:
                result.sent.append(record)
            else:
                result.not_configured.append(record)
        self.session.flush()

    # -- public API ---------------------------------------------------------

    def notify_incident(
        self, incident: Incident, now: dt.datetime | None = None, *, result: NotificationResult | None = None
    ) -> NotificationResult:
        """Notify (or escalate) one incident. Members are never notified separately."""
        now = now or utcnow()
        result = result or NotificationResult()
        if incident.state != "open":
            return result

        members = list(
            self.session.scalars(select(Alarm).where(Alarm.incident_id == incident.id)).all()
        )
        root = next((m for m in members if m.id == incident.root_cause_alarm_id), None)
        if root is None:
            root = max(members, key=lambda m: severity_rank(m.severity), default=None)
        if root is None:
            return result

        # An incident whose alarms are *all* suppressed by maintenance mode or a
        # declared suppression condition stays silent. The alarms and the reason
        # are still recorded; only the message is withheld (SDD 11).
        if all(
            SuppressionReason.kind(m.suppression_reason)
            in (SuppressionReason.MAINTENANCE, SuppressionReason.CONDITION)
            for m in members
            if m.suppressed
        ) and all(m.suppressed for m in members):
            return result

        definition = self.definitions.get(root.alarm_key)
        anchor = root.activated_at or root.detected_at
        acknowledged = any(m.state in ("acknowledged", "mitigated") for m in members)
        before = result.dispatches
        self._notify_target(
            subject=f"[{incident.severity.upper()}] {incident.title}",
            body=self._incident_body(incident, root, members, definition),
            severity=incident.severity,
            definition=definition,
            anchor=anchor,
            acknowledged=acknowledged,
            incident_id=incident.id,
            alarm_id=root.id,
            now=now,
            result=result,
            context={
                "member_count": len(members),
                "member_alarm_keys": sorted({m.alarm_key for m in members}),
                "root_alarm_key": root.alarm_key,
                "root_asset_id": root.asset_id,
                "procedure_ref": definition.procedure_ref if definition else None,
            },
        )
        if result.dispatches > before:
            for member in members:
                member.notified = True
        return result

    def notify_alarm(
        self, alarm: Alarm, now: dt.datetime | None = None, *, result: NotificationResult | None = None
    ) -> NotificationResult:
        """Notify a standalone alarm that is not a member of any incident."""
        now = now or utcnow()
        result = result or NotificationResult()
        if alarm.suppressed or alarm.state not in ACTIVE_STATES:
            return result
        definition = self.definitions.get(alarm.alarm_key)
        anchor = alarm.activated_at or alarm.detected_at
        before = result.dispatches
        self._notify_target(
            subject=f"[{alarm.severity.upper()}] {alarm.message or alarm.alarm_key}",
            body=self._alarm_body(alarm, definition),
            severity=alarm.severity,
            definition=definition,
            anchor=anchor,
            acknowledged=alarm.state in ("acknowledged", "mitigated"),
            incident_id=None,
            alarm_id=alarm.id,
            now=now,
            result=result,
            context={
                "alarm_key": alarm.alarm_key,
                "asset_id": alarm.asset_id,
                "point_id": alarm.point_id,
                "procedure_ref": definition.procedure_ref if definition else None,
            },
        )
        if result.dispatches > before:
            alarm.notified = True
        return result

    def _notify_target(
        self,
        *,
        subject: str,
        body: str,
        severity: str,
        definition: AlarmDefinition | None,
        anchor: dt.datetime,
        acknowledged: bool,
        incident_id: str | None,
        alarm_id: str | None,
        now: dt.datetime,
        result: NotificationResult,
        context: dict[str, Any],
    ) -> None:
        stages = self.escalation_stages(definition, severity)
        already = self._existing_stages(incident_id=incident_id, alarm_id=alarm_id)
        elapsed = max(0.0, elapsed_s(now, anchor))
        meta = definition_meta(definition) if definition is not None else {}
        renotify_after = int(meta.get("renotify_after_s") or 0)

        highest_due = None
        for stage in stages:
            if elapsed + 1e-9 < stage["after_s"]:
                continue
            # Escalation past the first stage only happens while unacknowledged:
            # acknowledging stops escalation (it does not clear the alarm).
            if stage["stage"] != stages[0]["stage"] and acknowledged:
                continue
            highest_due = stage
            if stage["stage"] in already:
                result.skipped_duplicate += 1
                continue
            self._dispatch(
                NotificationMessage(
                    subject=subject,
                    body=body,
                    severity=severity,
                    stage=stage["stage"],
                    reason=f"escalation stage {stage['stage']} at +{stage['after_s']} s",
                    alarm_id=alarm_id,
                    incident_id=incident_id,
                    recipient_role=stage.get("role"),
                    context={**context, "channels": stage["channels"]},
                ),
                now,
                result,
            )
            if stage["stage"] != stages[0]["stage"]:
                result.escalated += 1

        # Re-notify the highest reached stage while the alarm stays unacknowledged.
        if (
            highest_due is not None
            and renotify_after > 0
            and not acknowledged
            and severity in ESCALATING_SEVERITIES
        ):
            last = already.get(highest_due["stage"])
            if last is not None and elapsed_s(now, last) + 1e-9 >= renotify_after:
                self._dispatch(
                    NotificationMessage(
                        subject=f"{subject} (still unacknowledged)",
                        body=body,
                        severity=severity,
                        stage=highest_due["stage"],
                        reason=(
                            f"re-notification: unacknowledged for "
                            f"{elapsed_s(now, last):.0f} s "
                            f"(renotify_after_s={renotify_after})"
                        ),
                        alarm_id=alarm_id,
                        incident_id=incident_id,
                        recipient_role=highest_due.get("role"),
                        context={**context, "channels": highest_due["channels"], "renotify": True},
                    ),
                    now,
                    result,
                )
                result.escalated += 1

    def dispatch_pending(self, now: dt.datetime | None = None) -> NotificationResult:
        """Notify every open incident and every unsuppressed standalone alarm."""
        now = now or utcnow()
        result = NotificationResult()
        self._definitions = None

        for incident in self.session.scalars(
            select(Incident).where(Incident.state == "open").order_by(Incident.opened_at.asc())
        ).all():
            self.notify_incident(incident, now, result=result)

        statement = (
            select(Alarm)
            .where(
                Alarm.state.in_(ACTIVE_STATES),
                Alarm.suppressed.is_(False),
                Alarm.incident_id.is_(None),
            )
            .order_by(Alarm.detected_at.asc())
        )
        for alarm in self.session.scalars(statement).all():
            self.notify_alarm(alarm, now, result=result)
        return result

    # -- bodies -------------------------------------------------------------

    def _incident_body(
        self,
        incident: Incident,
        root: Alarm,
        members: list[Alarm],
        definition: AlarmDefinition | None,
    ) -> str:
        lines = [
            incident.summary or incident.title,
            "",
            f"Root cause alarm: {root.alarm_key} on {root.asset_id} ({root.severity}).",
            f"Member alarms: {len(members)}.",
        ]
        for member in sorted(members, key=lambda m: as_utc(m.detected_at))[:20]:
            marker = "root" if member.id == root.id else "symptom"
            lines.append(f"  - [{marker}] {member.alarm_key} :: {member.asset_id} :: {member.state}")
        if len(members) > 20:
            lines.append(f"  ... and {len(members) - 20} more")
        lines.extend(self._context_lines(definition))
        return "\n".join(lines)

    def _alarm_body(self, alarm: Alarm, definition: AlarmDefinition | None) -> str:
        lines = [
            alarm.message or alarm.alarm_key,
            "",
            f"Asset: {alarm.asset_id}",
            f"Point: {alarm.point_id}",
            f"State: {alarm.state}  Severity: {alarm.severity}",
            f"Detected: {alarm.detected_at.isoformat()}",
        ]
        lines.extend(self._context_lines(definition))
        return "\n".join(lines)

    def _context_lines(self, definition: AlarmDefinition | None) -> list[str]:
        """SDD FR-008 context travels with the notification, not just the UI."""
        if definition is None:
            return []
        meta = definition_meta(definition)
        lines = ["", f"Procedure: {definition.procedure_ref} ({meta.get('procedure_status')})"]
        if definition.operator_action:
            lines.append(f"Operator action: {definition.operator_action}")
        if definition.automatic_action:
            lines.append(f"Automatic action: {definition.automatic_action}")
        if definition.probable_causes:
            lines.append("Probable causes: " + "; ".join(definition.probable_causes))
        if definition.affected_assets:
            lines.append("Affected assets: " + ", ".join(definition.affected_assets))
        status = meta.get("threshold_status")
        if status == "commissioning_default":
            lines.append(
                "Threshold status: commissioning default -- this trip point is not a decided "
                f"setpoint. Basis: {meta.get('threshold_basis')}"
            )
        return lines


def _stage_of(record: NotificationLog) -> int | None:
    if not record.detail:
        return None
    try:
        return int(json.loads(record.detail).get("stage"))
    except (ValueError, TypeError):
        return None


def notification_detail(record: NotificationLog) -> dict[str, Any]:
    """Parse the structured detail written by :class:`Notifier`."""
    if not record.detail:
        return {}
    try:
        parsed = json.loads(record.detail)
    except ValueError:
        return {"raw": record.detail}
    return parsed if isinstance(parsed, dict) else {"raw": record.detail}
