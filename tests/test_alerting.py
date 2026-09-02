import asyncio
import json
import unittest
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from tvt_edge.alerting import AlertingService, create_alert_app
from tvt_edge.alerting.email_sender import DeliveryFailure
from tvt_edge.alerting.outbox import OutboxWorker
from tvt_edge.db.models import (
    AlertTransition,
    Base,
    NotificationAttempt,
    NotificationOutbox,
    NotificationPolicy,
    utc_now,
)


TOKEN = "test-alertmanager-token-with-entropy"


def webhook(status="firing", ends_at="0001-01-01T00:00:00Z"):
    return {
        "version": "4",
        "groupKey": "{}:{alertname=\"CameraMediaMissing\"}",
        "truncatedAlerts": 0,
        "status": status,
        "receiver": "tvt-host",
        "groupLabels": {"alertname": "CameraMediaMissing"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager.monitoring",
        "alerts": [
            {
                "status": status,
                "labels": {
                    "site_id": "plant-01",
                    "alertname": "CameraMediaMissing",
                    "severity": "critical",
                    "service": "face-recognition",
                    "camera_id": "camera-03",
                },
                "annotations": {"summary": "Camera media is unavailable"},
                "startsAt": "2026-09-01T08:30:00Z",
                "endsAt": ends_at,
                "generatorURL": "http://prometheus/graph",
                "fingerprint": "alertmanager-fingerprint-is-not-trusted",
            }
        ],
    }


class FakeSender:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    def send(self, item, alert, transition):
        self.messages.append(
            (item.message_id, alert.alert_name, transition.transition_type)
        )
        if len(self.messages) <= self.failures:
            raise DeliveryFailure("SMTP_UNAVAILABLE", True)
        return 250


class AlertingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            session.add(
                NotificationPolicy(
                    site_key="plant-01",
                    name="critical-operations",
                    severity="critical",
                    recipients=["operator@example.com"],
                    repeat_interval_seconds=1800,
                    send_resolved=True,
                )
            )

    def receive(self, body, token=TOKEN):
        app = create_alert_app(self.sessions, TOKEN)
        endpoint = next(
            route.endpoint
            for route in app.routes
            if route.path == "/internal/v1/alerts/alertmanager"
        )
        encoded = json.dumps(body).encode()
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/internal/v1/alerts/alertmanager",
            "raw_path": b"/internal/v1/alerts/alertmanager",
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(encoded)).encode()),
            ],
        }
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": encoded, "more_body": False}

        return asyncio.run(endpoint(Request(scope, receive)))

    def test_receiver_authenticates_deduplicates_and_queues_atomically(self):
        with self.assertRaises(HTTPException) as error:
            self.receive(webhook(), token="wrong-token-with-enough-entropy")
        self.assertEqual(error.exception.status_code, 401)
        accepted = self.receive(webhook())
        duplicate = self.receive(webhook())
        self.assertEqual(accepted["notifications_queued"], 1)
        self.assertEqual(duplicate["duplicates"], 1)
        with self.sessions() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(AlertTransition)), 1
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(NotificationOutbox)), 1
            )

    def test_acknowledgement_survives_refresh_and_resolution_queues_recovery(self):
        self.receive(webhook())
        service = AlertingService(self.sessions)
        stored = service.list_alerts()[0]
        stored_id = uuid.UUID(stored["alert_id"])
        service.acknowledge(stored_id, "operator", "req-1")
        self.receive(webhook())
        self.assertEqual(service.list_alerts()[0]["state"], "acknowledged")
        with self.sessions.begin() as session:
            firing = session.scalar(select(NotificationOutbox))
            firing.state = "sent"
            firing.sent_at = utc_now()
        resolved = self.receive(webhook("resolved", "2026-09-01T09:00:00Z"))
        self.assertEqual(resolved["notifications_queued"], 1)
        self.assertEqual(service.list_alerts()[0]["state"], "resolved")
        self.assertEqual(len(service.notifications(stored_id)), 2)

    def test_transient_delivery_is_persisted_and_retried(self):
        self.receive(webhook())
        sender = FakeSender(failures=1)
        worker = OutboxWorker(self.sessions, sender)
        worker.run_once()
        with self.sessions.begin() as session:
            item = session.scalar(select(NotificationOutbox))
            self.assertEqual(item.state, "pending")
            item.next_attempt_at = utc_now() - timedelta(seconds=1)
        worker.run_once()
        with self.sessions() as session:
            item = session.scalar(select(NotificationOutbox))
            self.assertEqual(item.state, "sent")
            self.assertEqual(item.attempt_count, 2)
            self.assertEqual(
                session.scalar(select(func.count()).select_from(NotificationAttempt)), 2
            )
        self.assertEqual(sender.messages[0][0], sender.messages[1][0])

    def test_out_of_order_events_do_not_reopen_or_resolve_the_wrong_occurrence(self):
        self.receive(webhook())
        self.receive(webhook("resolved", "2026-09-01T09:00:00Z"))
        service = AlertingService(self.sessions)
        self.assertEqual(service.list_alerts()[0]["state"], "resolved")

        # Alertmanager can retry a firing event after its resolution.
        self.receive(webhook())
        self.assertEqual(service.list_alerts()[0]["state"], "resolved")

        next_occurrence = webhook()
        next_occurrence["alerts"][0]["startsAt"] = "2026-09-01T10:00:00Z"
        self.receive(next_occurrence)
        self.assertEqual(service.list_alerts()[0]["state"], "active")
        self.assertEqual(service.list_alerts()[0]["occurrence_count"], 2)

        # An old resolution must not resolve the later occurrence.
        old_resolution = webhook("resolved", "2026-09-01T09:05:00Z")
        self.receive(old_resolution)
        self.assertEqual(service.list_alerts()[0]["state"], "active")

    def test_unknown_label_is_rejected_without_database_writes(self):
        body = webhook()
        body["alerts"][0]["labels"]["password"] = "do-not-store"
        with self.assertRaises(HTTPException) as error:
            self.receive(body)
        self.assertEqual(error.exception.status_code, 422)
        self.assertNotIn("do-not-store", str(error.exception.detail))
        self.assertEqual(AlertingService(self.sessions).list_alerts(), [])

    def test_credential_like_annotation_text_is_redacted_before_persistence(self):
        body = webhook()
        body["alerts"][0]["annotations"]["description"] = (
            "probe failed password=camera-secret at "
            "https://operator:api-key@example.invalid/status"
        )
        self.receive(body)
        description = AlertingService(self.sessions).list_alerts()[0]["annotations"][
            "description"
        ]
        self.assertNotIn("camera-secret", description)
        self.assertNotIn("operator:api-key", description)
        self.assertIn("[REDACTED]", description)


if __name__ == "__main__":
    unittest.main()
