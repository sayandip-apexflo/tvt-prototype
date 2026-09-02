"""Lease-based persistent notification outbox and retry worker."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session, sessionmaker

from tvt_edge.alerting.email_sender import DeliveryFailure
from tvt_edge.alerting.policy import matching_policies
from tvt_edge.alerting.service import AlertingService, _canonical_timestamp
from tvt_edge.db.models import (
    AlertInstance,
    AlertTransition,
    NotificationAttempt,
    NotificationOutbox,
    utc_now,
)

if TYPE_CHECKING:
    from tvt_edge.observability.metrics import AlertDispatcherMetrics


RETRY_SECONDS = (60, 120, 300, 600, 1800, 3600)


class EmailSender(Protocol):
    def send(
        self,
        item: NotificationOutbox,
        alert: AlertInstance,
        transition: AlertTransition,
    ) -> int: ...


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class OutboxWorker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        sender: EmailSender,
        *,
        metrics: AlertDispatcherMetrics | None = None,
        max_delivery_age: timedelta = timedelta(hours=24),
        lease_duration: timedelta = timedelta(minutes=2),
    ):
        self.sessions = sessions
        self.sender = sender
        self.max_delivery_age = max_delivery_age
        self.lease_duration = lease_duration
        self.service = AlertingService(sessions)
        self.metrics = metrics

    def run_once(self) -> dict[str, int]:
        reminders = self.enqueue_due_reminders()
        claimed = self._claim()
        if claimed is None:
            return {"reminders_queued": reminders, "processed": 0}
        item_id, token = claimed
        self._deliver(item_id, token)
        return {"reminders_queued": reminders, "processed": 1}

    def _claim(self) -> tuple[uuid.UUID, uuid.UUID] | None:
        now = utc_now()
        token = uuid.uuid4()
        with self.sessions.begin() as session:
            item = session.scalar(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.next_attempt_at <= now,
                    or_(
                        NotificationOutbox.state == "pending",
                        and_(
                            NotificationOutbox.state == "delivering",
                            NotificationOutbox.claim_until < now,
                        ),
                    ),
                )
                .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if item is None:
                return None
            item.state = "delivering"
            item.claim_token = token
            item.claim_until = now + self.lease_duration
            item.attempt_count += 1
            return item.id, token

    def _deliver(self, item_id: uuid.UUID, token: uuid.UUID) -> None:
        started_at = utc_now()
        with self.sessions() as session:
            item = session.get(NotificationOutbox, item_id)
            if item is None or item.claim_token != token:
                return
            alert = session.get(AlertInstance, item.alert_id)
            transition = session.get(AlertTransition, item.transition_id)
            missing_reference = alert is None or transition is None
            expired = utc_now() - _aware(item.created_at) >= self.max_delivery_age
            if not missing_reference:
                session.expunge(item)
                session.expunge(alert)
                session.expunge(transition)
        if missing_reference:
            failure = DeliveryFailure("OUTBOX_REFERENCE_MISSING", False)
            self._finish(item_id, token, started_at=started_at, failure=failure)
            return
        if expired:
            self._finish(item_id, token, started_at=started_at, expired=True)
            return
        try:
            smtp_code = self.sender.send(item, alert, transition)
        except DeliveryFailure as error:
            self._finish(item_id, token, started_at=started_at, failure=error)
        except Exception:
            # No exception text is persisted because SMTP libraries can echo
            # addresses or credentials in their messages.
            self._finish(
                item_id,
                token,
                started_at=started_at,
                failure=DeliveryFailure("SMTP_INTERNAL_ERROR", True),
            )
        else:
            self._finish(
                item_id, token, started_at=started_at, smtp_code=smtp_code
            )

    def _finish(
        self,
        item_id: uuid.UUID,
        token: uuid.UUID,
        *,
        started_at: datetime,
        smtp_code: int | None = None,
        failure: DeliveryFailure | None = None,
        expired: bool = False,
    ) -> None:
        now = utc_now()
        with self.sessions.begin() as session:
            item = session.scalar(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.id == item_id,
                    NotificationOutbox.claim_token == token,
                )
                .with_for_update()
            )
            if item is None:
                return
            if expired:
                result = "expired"
                item.state = "expired"
                item.expired_at = now
                category = "MAX_DELIVERY_AGE"
            elif failure is None:
                result = "sent"
                item.state = "sent"
                item.sent_at = now
                category = None
            elif failure.transient:
                result = "transient_failure"
                age = now - _aware(item.created_at)
                delay = RETRY_SECONDS[min(item.attempt_count - 1, len(RETRY_SECONDS) - 1)]
                if age + timedelta(seconds=delay) >= self.max_delivery_age:
                    item.state = "expired"
                    item.expired_at = now
                    result = "expired"
                    category = "MAX_DELIVERY_AGE"
                else:
                    item.state = "pending"
                    item.next_attempt_at = now + timedelta(seconds=delay)
                    category = failure.category
            else:
                result = "permanent_failure"
                item.state = "failed"
                category = failure.category
            item.claim_token = None
            item.claim_until = None
            session.add(
                NotificationAttempt(
                    outbox_id=item.id,
                    attempt_number=item.attempt_count,
                    started_at=started_at,
                    finished_at=now,
                    result=result,
                    smtp_code=smtp_code if failure is None else failure.smtp_code,
                    error_category=category,
                )
            )
        if self.metrics is not None:
            self.metrics.delivery(
                result,
                max(0.0, (now - _aware(started_at)).total_seconds()),
                completed_at=now.timestamp() if result == "sent" else None,
            )

    def enqueue_due_reminders(self) -> int:
        now = utc_now()
        queued = 0
        with self.sessions.begin() as session:
            alerts = session.scalars(
                select(AlertInstance)
                .where(AlertInstance.state == "active")
                .with_for_update(skip_locked=True)
            ).all()
            for alert in alerts:
                for policy in matching_policies(session, alert):
                    interval = policy.repeat_interval_seconds
                    if interval is None or interval <= 0:
                        continue
                    last = session.scalar(
                        select(NotificationOutbox)
                        .join(
                            AlertTransition,
                            AlertTransition.id == NotificationOutbox.transition_id,
                        )
                        .where(
                            NotificationOutbox.alert_id == alert.id,
                            NotificationOutbox.policy_id == policy.id,
                            NotificationOutbox.state == "sent",
                            NotificationOutbox.notification_type.in_(
                                ("firing", "reminder")
                            ),
                            AlertTransition.occurrence_starts_at
                            == alert.occurrence_starts_at,
                        )
                        .order_by(desc(NotificationOutbox.sent_at))
                        .limit(1)
                    )
                    if last is None or last.sent_at is None:
                        continue
                    due_at = _aware(last.sent_at) + timedelta(seconds=interval)
                    if due_at > now:
                        continue
                    transition = session.scalar(
                        select(AlertTransition)
                        .where(
                            AlertTransition.alert_id == alert.id,
                            AlertTransition.transition_type == "firing",
                            AlertTransition.occurrence_starts_at
                            == alert.occurrence_starts_at,
                        )
                        .order_by(desc(AlertTransition.received_at))
                        .limit(1)
                    )
                    if transition is None:
                        continue
                    queued += self.service._queue(
                        session,
                        alert,
                        transition,
                        policy,
                        "reminder",
                        transition.idempotency_key,
                        suffix=_canonical_timestamp(due_at),
                    )
        return queued
