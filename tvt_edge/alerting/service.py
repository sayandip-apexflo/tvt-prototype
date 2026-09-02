"""Transactional alert state, acknowledgement, and durable outbox creation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tvt_edge.alerting.policy import matching_policies, validate_recipients
from tvt_edge.db.models import (
    AlertInstance,
    AlertTransition,
    AuditEvent,
    NotificationAttempt,
    NotificationOutbox,
    NotificationPolicy,
    utc_now,
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_timestamp(value: datetime | None) -> str:
    return _iso(value) or ""


def _same_timestamp(left: datetime, right: datetime) -> bool:
    return _canonical_timestamp(left) == _canonical_timestamp(right)


def _after(left: datetime, right: datetime) -> bool:
    left_value = left.replace(tzinfo=timezone.utc) if left.tzinfo is None else left
    right_value = right.replace(tzinfo=timezone.utc) if right.tzinfo is None else right
    return left_value > right_value


def alert_fingerprint(labels: dict[str, str]) -> str:
    parts = [
        labels.get("site_id", ""),
        labels.get("alertname", ""),
        labels.get("service", ""),
        labels.get("camera_id", ""),
        labels.get("use_case", ""),
    ]
    canonical = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def transition_key(event: dict[str, Any], fingerprint: str) -> str:
    canonical = json.dumps(
        [
            event["source"],
            fingerprint,
            _canonical_timestamp(event["starts_at"]),
            event["status"],
            _canonical_timestamp(event.get("ends_at")),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _message_id(key: str, policy_id: uuid.UUID, kind: str, suffix: str = "") -> str:
    digest = hashlib.sha256(
        f"{key}:{policy_id}:{kind}:{suffix}".encode()
    ).hexdigest()[:48]
    return f"tvt-{digest}@alerts.local"


class AlertingService:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def ingest(self, event: dict[str, Any]) -> dict[str, Any]:
        """Persist one normalized event and its email decision atomically."""

        return self.ingest_many([event])[0]

    def ingest_many(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Persist all alerts from one accepted webhook in one transaction."""

        for attempt in range(2):
            try:
                with self.sessions.begin() as session:
                    return [self._ingest(session, event) for event in events]
            except IntegrityError:
                if attempt:
                    raise
                # A concurrent insert won the fingerprint or idempotency race.
                continue
        raise RuntimeError("unreachable")

    def _ingest(self, session: Session, event: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        labels = event["labels"]
        fingerprint = alert_fingerprint(labels)
        instance = session.scalar(
            select(AlertInstance)
            .where(AlertInstance.fingerprint == fingerprint)
            .with_for_update()
        )
        created = instance is None
        if instance is None:
            instance = AlertInstance(
                fingerprint=fingerprint,
                site_key=labels["site_id"],
                source=event["source"],
                alert_name=labels["alertname"],
                severity=labels["severity"],
                service=labels["service"],
                camera_key=labels.get("camera_id"),
                use_case=labels.get("use_case"),
                state="active",
                occurrence_starts_at=event["starts_at"],
                first_seen_at=now,
                last_seen_at=now,
                safe_labels=labels,
                safe_annotations=event["annotations"],
                last_group_key=event.get("group_key") or None,
            )
            session.add(instance)
            session.flush()

        key = transition_key(event, fingerprint)
        duplicate = session.scalar(
            select(AlertTransition.id).where(AlertTransition.idempotency_key == key)
        )
        instance.last_seen_at = now
        instance.safe_labels = labels
        instance.safe_annotations = event["annotations"]
        instance.last_group_key = event.get("group_key") or instance.last_group_key
        if duplicate is not None:
            return {
                "alert_id": str(instance.id),
                "fingerprint": fingerprint,
                "duplicate": True,
                "notifications_queued": 0,
            }

        same_occurrence = _same_timestamp(
            instance.occurrence_starts_at, event["starts_at"]
        )
        newer_occurrence = not same_occurrence and _after(
            event["starts_at"], instance.occurrence_starts_at
        )
        state_changed = created
        if event["status"] == "firing":
            if newer_occurrence:
                instance.occurrence_starts_at = event["starts_at"]
                instance.occurrence_count += 1
                instance.acknowledged_at = None
                instance.acknowledged_by = None
                instance.resolved_at = None
                instance.state = "active"
                state_changed = True
            elif instance.state == "resolved" and same_occurrence:
                # A late firing refresh must not reopen a resolved occurrence.
                state_changed = False
        elif same_occurrence and instance.state != "resolved":
            instance.state = "resolved"
            instance.resolved_at = event["ends_at"]
            state_changed = True

        source_timestamp = event.get("ends_at") or event["starts_at"]
        transition = AlertTransition(
            alert_id=instance.id,
            transition_type=event["status"],
            occurrence_starts_at=event["starts_at"],
            source_timestamp=source_timestamp,
            received_at=now,
            idempotency_key=key,
            redacted_payload={
                "schema_version": "1.0",
                "source": event["source"],
                "status": event["status"],
                "starts_at": _iso(event["starts_at"]),
                "ends_at": _iso(event.get("ends_at")),
                "labels": labels,
                "annotations": event["annotations"],
            },
        )
        session.add(transition)
        session.flush()

        queued = 0
        if state_changed:
            for policy in matching_policies(session, instance):
                if event["status"] == "firing":
                    queued += self._queue(
                        session, instance, transition, policy, "firing", key
                    )
                elif policy.send_resolved and self._firing_was_sent(
                    session, instance.id, event["starts_at"], policy.id
                ):
                    queued += self._queue(
                        session, instance, transition, policy, "resolved", key
                    )
        return {
            "alert_id": str(instance.id),
            "fingerprint": fingerprint,
            "duplicate": False,
            "notifications_queued": queued,
        }

    @staticmethod
    def _firing_was_sent(
        session: Session,
        alert_id: uuid.UUID,
        starts_at: datetime,
        policy_id: uuid.UUID,
    ) -> bool:
        return session.scalar(
            select(NotificationOutbox.id)
            .join(AlertTransition, AlertTransition.id == NotificationOutbox.transition_id)
            .where(
                NotificationOutbox.alert_id == alert_id,
                NotificationOutbox.policy_id == policy_id,
                NotificationOutbox.state == "sent",
                NotificationOutbox.notification_type.in_(("firing", "reminder")),
                AlertTransition.occurrence_starts_at == starts_at,
            )
            .limit(1)
        ) is not None

    @staticmethod
    def _queue(
        session: Session,
        alert: AlertInstance,
        transition: AlertTransition,
        policy: NotificationPolicy,
        kind: str,
        key: str,
        suffix: str = "",
    ) -> int:
        recipients = validate_recipients(policy.recipients)
        message_id = _message_id(key, policy.id, kind, suffix)
        exists = session.scalar(
            select(NotificationOutbox.id).where(
                NotificationOutbox.message_id == message_id
            )
        )
        if exists is not None:
            return 0
        session.add(
            NotificationOutbox(
                alert_id=alert.id,
                transition_id=transition.id,
                policy_id=policy.id,
                notification_type=kind,
                message_id=message_id,
                recipients=recipients,
                state="pending",
            )
        )
        return 1

    def list_alerts(
        self, limit: int = 100, include_resolved: bool = True
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self.sessions() as session:
            query = select(AlertInstance)
            if not include_resolved:
                query = query.where(AlertInstance.state != "resolved")
            alerts = session.scalars(
                query.order_by(desc(AlertInstance.last_seen_at)).limit(limit)
            ).all()
            return [self._serialize_alert(item) for item in alerts]

    def acknowledge(
        self, alert_id: uuid.UUID, actor: str, request_id: str
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            alert = session.scalar(
                select(AlertInstance)
                .where(AlertInstance.id == alert_id)
                .with_for_update()
            )
            if alert is None:
                raise ValueError("alert not found")
            if alert.state == "resolved":
                raise ValueError("resolved alert cannot be acknowledged")
            if alert.state != "acknowledged":
                alert.state = "acknowledged"
                alert.acknowledged_at = utc_now()
                alert.acknowledged_by = actor
                session.add(
                    AuditEvent(
                        actor=actor,
                        request_id=request_id,
                        action="alert.acknowledge",
                        target_type="alert",
                        target_id=str(alert.id),
                        result="succeeded",
                        details={"fingerprint": alert.fingerprint},
                    )
                )
            session.flush()
            return self._serialize_alert(alert)

    def notifications(
        self, alert_id: uuid.UUID, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self.sessions() as session:
            if session.get(AlertInstance, alert_id) is None:
                raise ValueError("alert not found")
            items = session.scalars(
                select(NotificationOutbox)
                .where(NotificationOutbox.alert_id == alert_id)
                .order_by(desc(NotificationOutbox.created_at))
                .limit(limit)
            ).all()
            result = []
            for item in items:
                attempts = session.scalars(
                    select(NotificationAttempt)
                    .where(NotificationAttempt.outbox_id == item.id)
                    .order_by(NotificationAttempt.attempt_number)
                ).all()
                result.append(
                    {
                        "notification_id": str(item.id),
                        "type": item.notification_type,
                        "message_id": item.message_id,
                        "state": item.state,
                        "attempt_count": item.attempt_count,
                        "next_attempt_at": _iso(item.next_attempt_at),
                        "sent_at": _iso(item.sent_at),
                        "expired_at": _iso(item.expired_at),
                        "recipient_count": len(item.recipients),
                        "attempts": [
                            {
                                "attempt_number": attempt.attempt_number,
                                "started_at": _iso(attempt.started_at),
                                "finished_at": _iso(attempt.finished_at),
                                "result": attempt.result,
                                "smtp_code": attempt.smtp_code,
                                "error_category": attempt.error_category,
                            }
                            for attempt in attempts
                        ],
                    }
                )
            return result

    @staticmethod
    def _serialize_alert(alert: AlertInstance) -> dict[str, Any]:
        return {
            "alert_id": str(alert.id),
            "fingerprint": alert.fingerprint,
            "site_id": alert.site_key,
            "alertname": alert.alert_name,
            "severity": alert.severity,
            "service": alert.service,
            "camera_id": alert.camera_key,
            "use_case": alert.use_case,
            "state": alert.state,
            "starts_at": _iso(alert.occurrence_starts_at),
            "first_seen_at": _iso(alert.first_seen_at),
            "last_seen_at": _iso(alert.last_seen_at),
            "occurrence_count": alert.occurrence_count,
            "acknowledged_at": _iso(alert.acknowledged_at),
            "acknowledged_by": alert.acknowledged_by,
            "resolved_at": _iso(alert.resolved_at),
            "annotations": alert.safe_annotations,
        }
