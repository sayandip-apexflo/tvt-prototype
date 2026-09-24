"""Tests for the durable face-enrollment session state machine added in
tvt_edge/enrollment.py + tvt_edge/service.py (see docs/contracts/
tvt-mills-v1/README.md). Uses the same catalog fixture shape as
tests/test_management_plane.py; the pinned apexfabric.control_plane tests
(test_enrollment_windows.py, test_identity.py) are left untouched.
"""

import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tvt_edge.apex_client import ApexClient, ApexUnavailableError
from tvt_edge.cluster.sync import SyncWorker
from tvt_edge.db.models import (
    AuditEvent,
    Base,
    DeploymentSyncState,
    EnrollmentSession,
    SolutionDeployment,
    utc_now,
)
from tvt_edge.enrollment import (
    DEFAULT_ACTIVATION_TIMEOUT_SECONDS,
    eligible_capture_candidate,
    select_first_capture,
)
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService

ROOT = Path(__file__).resolve().parents[1]
CATALOG_DELIVERY = ROOT / "solution-packs/catalog/tvt-mills-pilot-2026.09.18-v1"
CATALOG_ID = "tvt-mills-pilot:2026.09.18-v1"
CATALOG_DIGEST = "sha256:" + "1" * 64
LOGICAL_DEPLOYMENT = "tvt-mills-v1"
RUNTIME_WORKLOAD = "tvt-mills-v1-runtime"


class FakeKubectl:
    """Mirrors tests/test_management_plane.py's FakeKubectl closely enough
    to let SyncWorker.run_once() actually mark a committed revision
    'applied', without pulling in that whole module."""

    def __init__(self, fail_rollout: bool = False):
        self.calls = []
        self.fail_rollout = fail_rollout

    def run(self, *arguments, input_text=None, check=True):
        self.calls.append((arguments, input_text))
        if self.fail_rollout and arguments[:2] == ("rollout", "status"):
            raise ValueError("rollout failed")

        class Result:
            stdout = ""

        result = Result()
        if arguments[:2] == ("get", "nodes"):
            result.stdout = json.dumps({"items": []})
        elif arguments[:2] == (
            "get",
            "deployments,configmaps,secrets,services,networkpolicies,persistentvolumeclaims",
        ):
            result.stdout = json.dumps({"items": []})
        elif arguments[:2] == ("get", "deployments"):
            result.stdout = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "tvt-mills-v1-runtime"},
                            "spec": {"replicas": 1},
                            "status": {"readyReplicas": 1, "availableReplicas": 1},
                        }
                    ]
                }
            )
        return result


class LiveReloadKubectl(FakeKubectl):
    def __init__(self, acknowledged_revision: int):
        super().__init__()
        self.acknowledged_revision = acknowledged_revision

    def run(self, *arguments, input_text=None, check=True):
        if arguments[:2] == ("get", "pods"):
            self.calls.append((arguments, input_text))

            class Result:
                stdout = json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "tvt-mills-v1-runtime-pod",
                                    "creationTimestamp": "2026-09-23T00:00:00Z",
                                },
                                "status": {"phase": "Running"},
                            }
                        ]
                    }
                )

            return Result()
        if arguments[:2] == ("get", "--raw"):
            self.calls.append((arguments, input_text))

            class Result:
                stdout = json.dumps(
                    {"status": "ready", "revision": self.acknowledged_revision}
                )

            return Result()
        return super().run(*arguments, input_text=input_text, check=check)


class FakeApex:
    """Stand-in for ApexClient with the same method surface enrollment.py
    calls (recent_events/list_persons/rename_person), so tests never need a
    real apexfabric-control process."""

    def __init__(self, events=None, persons=None, unavailable=False):
        self.events = events or []
        self.persons = []
        for person in persons or []:
            normalized = dict(person)
            source = normalized.get("enrollment_source_event_id")
            if isinstance(source, str) and source.startswith(f"{LOGICAL_DEPLOYMENT}:"):
                normalized["enrollment_source_event_id"] = source.replace(
                    f"{LOGICAL_DEPLOYMENT}:", f"{RUNTIME_WORKLOAD}:", 1
                )
            self.persons.append(normalized)
        self.unavailable = unavailable
        self.renamed = []
        self.event_queries = []

    def recent_events(self, deployment_id):
        self.event_queries.append(deployment_id)
        if self.unavailable:
            raise ApexUnavailableError("apex unreachable")
        return [event for event in self.events if event["deployment_id"] == deployment_id]

    def list_persons(self, status=None):
        if self.unavailable:
            raise ApexUnavailableError("apex unreachable")
        return list(self.persons)

    def rename_person(self, person_id, display_name):
        self.renamed.append((person_id, display_name))


def capture_event(deployment_id, camera_id, event_id, occurred_at_iso, quality=None):
    if deployment_id == LOGICAL_DEPLOYMENT:
        deployment_id = RUNTIME_WORKLOAD
    return {
        "event_id": f"{deployment_id}:{event_id}",
        "deployment_id": deployment_id,
        "occurred_at": occurred_at_iso,
        "payload": {
            "event_id": event_id,
            "event_type": "enrollment_capture_event",
            "application": "face_enrollment",
            "camera_id": camera_id,
            "timestamp": occurred_at_iso,
            "payload": {"quality": quality} if quality else {},
        },
    }


class EnrollmentSessionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.keyring = CredentialKeyring.generate_for_test()
        self.service = ManagementService(
            self.sessions, self.keyring, catalog_resolver=lambda *_a: CATALOG_DIGEST
        )
        self.service.create_site("plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site")

    def onboard_camera(self, camera_id="camera-01"):
        self.service.create_camera(
            camera_key=camera_id,
            friendly_name="Main entrance",
            manufacturer="Example",
            model="C1",
            identifiers=[{"kind": "mac", "value": f"00:11:22:33:44:{camera_id[-2:]}"}],
            actor="test",
            request_id=f"camera:{camera_id}",
        )
        self.service.configure_stream(
            camera_id,
            scheme="rtsp",
            host="192.0.2.10",
            port=554,
            path="/live/main",
            profile_token="profile-main",
            transport="tcp",
            codec="h264",
            width=1920,
            height=1080,
            fps=15,
            actor="test",
            request_id=f"stream:{camera_id}",
        )
        self.service.rotate_credentials(
            camera_id, {"username": "camera-user", "password": "camera-secret"}, "test",
            f"credential:{camera_id}",
        )
        self.service.set_camera_enabled(camera_id, True, "test", f"enable:{camera_id}")

    def deploy(self, assignments):
        self.service.seed_solution_catalog(CATALOG_DELIVERY, "127.0.0.1:5000")
        self.service.refresh_solutions(actor="test", request_id="refresh")
        request = dict(
            catalog_id=CATALOG_ID,
            deployment_key="tvt-mills-v1",
            assignments=assignments,
            inference_mode="gpu-npu",
            resources={},
            state_size="50Gi",
            namespace="apexfabric",
        )
        preview = self.service.preview_catalog_deployment(**request)
        self.service.commit_catalog_deployment(
            **request,
            preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key="deploy-1",
            actor="test",
            request_id="deploy-1",
        )

    def deploy_with_face_recognition(self, extra_assignments=None):
        self.onboard_camera("camera-01")
        assignments = [
            {
                "camera_id": "camera-01",
                "apps": ["face_recognition", "anpr"],
                "fps": 8,
                "config": {
                    "lines": [
                        {
                            "id": "camera-01_entry",
                            "name": "camera-01 gate (entry)",
                            "type": "line",
                            "points": [[0.1, 0.5], [0.9, 0.5]],
                            "accepted": ["A->B"],
                        }
                    ]
                },
            }
        ]
        if extra_assignments:
            assignments.extend(extra_assignments)
        self.deploy(assignments)

    def apply_desired(self, kubectl=None):
        SyncWorker(
            self.sessions,
            self.keyring,
            kubectl or FakeKubectl(),
            worker_id="test-worker",
            image_puller=lambda _reference: None,
        ).run_once()

    def current_assignment(self, camera_id="camera-01"):
        with self.sessions() as session:
            deployment = session.scalar(select(SolutionDeployment))
            _catalog_id, assignments, *_ = self.service._current_catalog_assignments(
                session, deployment
            )
        return next(item for item in assignments if item["camera_id"] == camera_id)

    def start_session(self, **kwargs):
        if self.service.get_enrollment_designation("tvt-mills-v1") is None:
            self.service.designate_enrollment_camera(
                deployment_key="tvt-mills-v1", camera_id="camera-01", actor="test", request_id="designate-1"
            )
        return self.service.start_enrollment_session(
            deployment_key="tvt-mills-v1", actor="test", request_id="start-1", **kwargs
        )

    def activate(self):
        """Start a session and drive it to 'capturing'."""
        started = self.start_session()
        self.apply_desired()
        results = self.service.reconcile_enrollment_sessions(FakeApex())
        self.assertEqual(results[0]["transition"], "capturing")
        return started

    # 1. Designation accepts only an eligible assigned face-recognition camera.
    def test_designation_rejects_unassigned_camera(self):
        self.onboard_camera("camera-02")  # exists, but not part of this deployment
        self.deploy_with_face_recognition()
        with self.assertRaisesRegex(ValueError, "not assigned"):
            self.service.designate_enrollment_camera(
                deployment_key="tvt-mills-v1", camera_id="camera-02", actor="t", request_id="x"
            )

    def test_designation_rejects_camera_without_face_recognition(self):
        self.onboard_camera("camera-01")
        self.deploy([{"camera_id": "camera-01", "apps": ["anpr"], "fps": 8, "config": {}}])
        with self.assertRaisesRegex(ValueError, "does not run face_recognition"):
            self.service.designate_enrollment_camera(
                deployment_key="tvt-mills-v1", camera_id="camera-01", actor="t", request_id="x"
            )

    def test_designation_accepts_eligible_camera(self):
        self.deploy_with_face_recognition()
        result = self.service.designate_enrollment_camera(
            deployment_key="tvt-mills-v1", camera_id="camera-01", actor="t", request_id="x"
        )
        self.assertEqual(result["camera_id"], "camera-01")
        self.assertEqual(
            self.service.get_enrollment_designation("tvt-mills-v1"), {"deployment_key": "tvt-mills-v1", "camera_id": "camera-01"}
        )

    # 2. Only one active session is allowed.
    def test_only_one_active_session_allowed(self):
        self.deploy_with_face_recognition()
        self.start_session()
        with self.assertRaisesRegex(ValueError, "already active"):
            self.start_session()

    def test_designation_blocked_while_session_active(self):
        self.deploy_with_face_recognition()
        self.start_session()
        with self.assertRaisesRegex(ValueError, "cannot change the enrollment camera"):
            self.service.designate_enrollment_camera(
                deployment_key="tvt-mills-v1", camera_id="camera-01", actor="t", request_id="x"
            )

    # 3. Starting enrollment produces ["face_enrollment"] exclusively.
    def test_start_produces_face_enrollment_exclusively(self):
        self.deploy_with_face_recognition()
        self.start_session()
        self.apply_desired()
        camera = self.current_assignment()
        self.assertEqual(camera["apps"], ["face_enrollment"])
        self.assertEqual(camera["config"], {})

    def test_geometry_edit_waits_for_enrollment_and_restores_latest_config(self):
        self.deploy_with_face_recognition()
        self.apply_desired()
        started = self.activate()

        shape = self.service.create_camera_line(
            "camera-01",
            "Updated gate entry",
            [[0.2, 0.4], [0.8, 0.4]],
            "updated-gate",
            "entry",
            "b",
            "test",
            "geometry-during-enrollment",
        )
        current = self.current_assignment()
        self.assertEqual(current["apps"], ["face_enrollment"])
        self.assertEqual(current["config"], {})
        status = self.service.get_camera_deployment_status("camera-01")
        self.assertEqual(status["overall_state"], "waiting_for_enrollment")
        with self.sessions() as session:
            row = session.get(
                EnrollmentSession, uuid.UUID(started["session_id"])
            )
            self.assertEqual(
                row.prior_config["lines"][0]["id"], shape["shape_key"]
            )

        self.service.cancel_enrollment_session(
            deployment_key="tvt-mills-v1",
            session_id=started["session_id"],
            actor="test",
            request_id="cancel-after-geometry",
        )
        self.service.reconcile_enrollment_sessions(FakeApex())
        restored = self.current_assignment()
        self.assertEqual(restored["apps"], ["face_recognition", "anpr"])
        self.assertEqual(
            restored["config"]["lines"][0]["id"], shape["shape_key"]
        )
        self.apply_desired()
        self.service.reconcile_enrollment_sessions(FakeApex())
        status = self.service.get_camera_deployment_status("camera-01")
        self.assertEqual(status["overall_state"], "applied")
        self.assertEqual(
            status["deployments"][0]["applied_geometry_revision"], 1
        )


    def test_start_live_reloads_acknowledged_revision_without_pod_restart(self):
        self.deploy_with_face_recognition()
        self.apply_desired()
        started = self.start_session()
        with self.sessions() as session:
            row = session.get(EnrollmentSession, uuid.UUID(started["session_id"]))
            revision = row.activation_revision
        client = LiveReloadKubectl(revision)
        SyncWorker(
            self.sessions,
            self.keyring,
            client,
            worker_id="live-reload-worker",
            live_reload_timeout=2,
            image_puller=lambda _reference: None,
        ).run_once()
        calls = [call[0] for call in client.calls]
        self.assertTrue(any(call[:2] == ("get", "--raw") for call in calls))
        self.assertEqual(
            sum(call[:2] == ("patch", "pod") for call in calls),
            1,
        )
        self.assertFalse(any(call[:2] == ("rollout", "restart") for call in calls))
        self.service.reconcile_enrollment_sessions(FakeApex())
        observer = self.service.get_enrollment_status(LOGICAL_DEPLOYMENT)["observer"]
        self.assertEqual(observer["stage"], "waiting_for_face")
        self.assertEqual(observer["target_revision"], revision)
        self.assertEqual(observer["applied_revision"], revision)

    def test_live_reload_timeout_falls_back_to_single_bundle_rollout(self):
        self.deploy_with_face_recognition()
        self.apply_desired()
        self.start_session()
        client = LiveReloadKubectl(acknowledged_revision=1)
        SyncWorker(
            self.sessions,
            self.keyring,
            client,
            worker_id="live-reload-fallback-worker",
            live_reload_timeout=1,
            image_puller=lambda _reference: None,
        ).run_once()
        calls = [call[0] for call in client.calls]
        self.assertFalse(any(call[:2] == ("rollout", "restart") for call in calls))
        self.assertTrue(any(call[:2] == ("rollout", "status") for call in calls))

    # 4. Session remains 'activating' until the desired revision is applied.
    def test_session_stays_activating_until_applied(self):
        self.deploy_with_face_recognition()
        started = self.start_session()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "activating")
        # Reconciling before SyncWorker applies anything must not claim capture mode.
        self.service.reconcile_enrollment_sessions(FakeApex())
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "activating")
        self.apply_desired()
        self.service.reconcile_enrollment_sessions(FakeApex())
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "capturing")
        self.assertEqual(status["session"]["session_id"], started["session_id"])

    # 5. A valid capture creates exactly one pending person.
    def test_valid_capture_creates_one_pending_person(self):
        self.deploy_with_face_recognition()
        self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(
            events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at, {"sharpness": 0.9})],
            persons=[{"person_id": "person-1", "enrollment_source_event_id": "tvt-mills-v1:evt-1", "status": "auto_enrolled", "display_name": None}],
        )
        results = self.service.reconcile_enrollment_sessions(apex)
        self.assertEqual(results[0]["transition"], "restoring")
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["capture_result"], "created")
        self.assertEqual(status["session"]["person_id"], "person-1")
        self.assertEqual(status["session"]["naming_status"], "pending_name")
        pending = self.service.list_people_awaiting_names()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["person_id"], "person-1")
        self.assertEqual(apex.event_queries, [RUNTIME_WORKLOAD])

        observer = status["observer"]
        self.assertEqual(observer["runtime_workload"], RUNTIME_WORKLOAD)
        self.assertEqual(observer["stage"], "restore_queued")
        self.assertNotIn("embedding", json.dumps(observer))

    def test_status_observer_exposes_safe_sync_progress(self):
        self.deploy_with_face_recognition()
        self.start_session()
        status = self.service.get_enrollment_status(LOGICAL_DEPLOYMENT)
        observer = status["observer"]
        self.assertEqual(observer["runtime_workload"], RUNTIME_WORKLOAD)
        self.assertEqual(observer["stage"], "sync_queued")
        self.assertEqual(observer["sync_state"], "pending")
        self.assertEqual(observer["timeline"][0]["stage"], "requested")
        self.assertNotIn("rtsp", json.dumps(observer).lower())

    def test_out_of_window_capture_is_rejected_but_session_keeps_capturing(self):
        self.deploy_with_face_recognition()
        self.activate()
        apex = FakeApex(
            events=[capture_event("tvt-mills-v1", "camera-01", "evt-late", "2099-01-01T00:00:00Z")],
        )
        results = self.service.reconcile_enrollment_sessions(apex)
        self.assertEqual(results[0]["error_code"], "ENROLLMENT_CAPTURE_REJECTED")
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "capturing")
        self.assertIsNone(status["session"]["capture_result"])

    # 6. Normal recognition (face_detection_event) never becomes a capture candidate.
    def test_face_detection_event_is_never_a_capture_candidate(self):
        event = capture_event("tvt-mills-v1", "camera-01", "evt-1", "2026-09-21T09:00:00Z")
        event["payload"]["event_type"] = "face_detection_event"
        event["payload"]["application"] = "face_recognition"
        from datetime import datetime, timezone

        window_start = datetime(2026, 9, 21, 8, 59, tzinfo=timezone.utc)
        window_end = datetime(2026, 9, 21, 9, 5, tzinfo=timezone.utc)
        candidate = eligible_capture_candidate(
            event,
            deployment_key="tvt-mills-v1",
            camera_key="camera-01",
            window_start=window_start,
            window_end=window_end,
        )
        self.assertIsNone(candidate)

    # 7 + duplicate/replay idempotency: a second reconcile tick with the same
    # (or additional) events after acceptance must not re-process anything --
    # the session has already left 'capturing'.
    def test_replayed_capture_after_acceptance_is_ignored(self):
        self.deploy_with_face_recognition()
        self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(
            events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at)],
            persons=[{"person_id": "person-1", "enrollment_source_event_id": "tvt-mills-v1:evt-1", "status": "auto_enrolled", "display_name": None}],
        )
        self.service.reconcile_enrollment_sessions(apex)
        with self.sessions() as session:
            before = session.scalar(select(EnrollmentSession))
            accepted_event_id = before.accepted_event_id
        # Replay the same event (and pretend a second person got created) --
        # must not be reconsidered because the session already left 'capturing'.
        apex.persons.append({"person_id": "person-2", "enrollment_source_event_id": "tvt-mills-v1:evt-1", "status": "auto_enrolled", "display_name": None})
        self.service.reconcile_enrollment_sessions(apex)
        with self.sessions() as session:
            after = session.scalar(select(EnrollmentSession))
            self.assertEqual(after.accepted_event_id, accepted_event_id)
            self.assertEqual(after.person_id, "person-1")
        self.assertEqual(len(self.service.list_people_awaiting_names()), 1)

    # 8. A match to an existing person returns 'duplicate'.
    def test_matched_face_returns_duplicate_and_creates_no_person(self):
        self.deploy_with_face_recognition()
        self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(
            events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at)],
            persons=[],  # no new person: the face matched an existing gallery entry
        )
        self.service.reconcile_enrollment_sessions(apex)
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["capture_result"], "duplicate")
        self.assertIsNone(status["session"]["person_id"])
        self.assertEqual(status["session"]["naming_status"], "not_applicable")
        self.assertEqual(self.service.list_people_awaiting_names(), [])
        # Camera is still restored regardless of the duplicate outcome.
        self.assertEqual(status["session"]["status"], "restoring")

    # 9. Successful capture automatically queues restoration.
    def test_capture_automatically_queues_restoration(self):
        self.deploy_with_face_recognition()
        self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at)])
        self.service.reconcile_enrollment_sessions(apex)
        with self.sessions() as session:
            row = session.scalar(select(EnrollmentSession))
            self.assertIsNotNone(row.restoration_assignment_set_id)
            self.assertIsNotNone(row.restoration_revision)

    # 10 + 11. Restoration reproduces exact prior apps/config/fps and waits
    # for the applied revision before completion.
    def test_restoration_reproduces_prior_state_and_waits_for_applied_revision(self):
        self.deploy_with_face_recognition()
        self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at)])
        self.service.reconcile_enrollment_sessions(apex)
        # Restore commit is queued but not yet applied -- must still be 'restoring'.
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "restoring")
        with self.sessions() as session:
            deployment = session.scalar(select(SolutionDeployment))
            sync = session.get(DeploymentSyncState, deployment.id)
            self.assertNotEqual(sync.state, "applied")  # rollout has not happened yet
        self.apply_desired()
        self.service.reconcile_enrollment_sessions(apex)
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "completed")
        self.assertEqual(status["session"]["result_code"], "ok")
        camera = self.current_assignment()
        self.assertEqual(camera["apps"], ["face_recognition", "anpr"])
        self.assertEqual(camera["fps"], 8)
        self.assertEqual(
            camera["config"]["lines"][0]["id"], "camera-01_entry"
        )

    # 12a. Timeout.
    def test_timeout_restores_camera_and_marks_timed_out(self):
        self.deploy_with_face_recognition()
        self.activate()
        with self.sessions.begin() as session:
            row = session.scalar(select(EnrollmentSession))
            row.capture_deadline_at = utc_now() - timedelta(seconds=1)
        results = self.service.reconcile_enrollment_sessions(FakeApex())
        self.assertEqual(results[0]["error_code"], "ENROLLMENT_TIMEOUT")
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "restoring")
        self.assertEqual(status["session"]["result_code"], "timed_out")
        self.apply_desired()
        self.service.reconcile_enrollment_sessions(FakeApex())
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "timed_out")
        self.assertEqual(status["session"]["result_code"], "timed_out")
        camera = self.current_assignment()
        self.assertEqual(camera["apps"], ["face_recognition", "anpr"])

    def test_activation_timeout_falls_back_to_restoring(self):
        self.deploy_with_face_recognition()
        self.start_session()
        with self.sessions.begin() as session:
            row = session.scalar(select(EnrollmentSession))
            row.started_at = utc_now() - timedelta(seconds=DEFAULT_ACTIVATION_TIMEOUT_SECONDS + 1)
        results = self.service.reconcile_enrollment_sessions(FakeApex())
        self.assertEqual(results[0]["error_code"], "ENROLLMENT_ACTIVATION_FAILED")
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "restoring")
        self.assertEqual(status["session"]["result_code"], "activation_failed")

    # 12b. Cancellation + browser independence (no further API calls needed).
    def test_cancellation_restores_camera_without_further_operator_action(self):
        self.deploy_with_face_recognition()
        started = self.activate()
        cancelled = self.service.cancel_enrollment_session(
            deployment_key="tvt-mills-v1", session_id=started["session_id"], actor="op", request_id="cancel-1"
        )
        self.assertEqual(cancelled["status"], "restoring")
        self.assertEqual(cancelled["result_code"], "cancelled")
        # No more operator calls -- only the background reconciler + SyncWorker
        # (simulating the browser having closed) drive this to completion.
        for _ in range(3):
            self.service.reconcile_enrollment_sessions(FakeApex())
            self.apply_desired()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "cancelled")
        self.assertEqual(status["session"]["result_code"], "cancelled")
        self.assertEqual(
            status["observer"]["target_revision"],
            status["observer"]["applied_revision"],
        )
        camera = self.current_assignment()
        self.assertEqual(camera["apps"], ["face_recognition", "anpr"])

    def test_cancellation_is_idempotent(self):
        self.deploy_with_face_recognition()
        started = self.activate()
        first = self.service.cancel_enrollment_session(
            deployment_key="tvt-mills-v1", session_id=started["session_id"], actor="op", request_id="cancel-1"
        )
        second = self.service.cancel_enrollment_session(
            deployment_key="tvt-mills-v1", session_id=started["session_id"], actor="op", request_id="cancel-2"
        )
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(second["result_code"], "cancelled")

    # 12c. Service restart: a fresh ManagementService bound to the same
    # database resumes a non-terminal session with no special handling.
    def test_service_restart_resumes_non_terminal_session(self):
        self.deploy_with_face_recognition()
        started = self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at)])
        self.service.reconcile_enrollment_sessions(apex)  # -> restoring, commit queued

        restarted_service = ManagementService(
            self.sessions, self.keyring, catalog_resolver=lambda *_a: CATALOG_DIGEST
        )
        self.apply_desired()
        restarted_service.reconcile_enrollment_sessions(apex)
        status = restarted_service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "completed")
        self.assertEqual(status["session"]["session_id"], started["session_id"])

    # 12d. Temporary K3s failure during restoration surfaces 'degraded' and retries.
    def test_k3s_failure_during_restoration_stays_restoring_and_recovers(self):
        self.deploy_with_face_recognition()
        started = self.activate()
        self.service.cancel_enrollment_session(
            deployment_key="tvt-mills-v1", session_id=started["session_id"], actor="op", request_id="cancel-1"
        )
        self.service.reconcile_enrollment_sessions(FakeApex())  # queue the restore commit
        with self.assertRaises(ValueError):
            self.apply_desired(kubectl=FakeKubectl(fail_rollout=True))  # K3s rollout fails
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "restoring")
        self.assertTrue(status["degraded"])
        # Retry succeeds once K3s recovers (skip SyncWorker's backoff delay).
        with self.sessions.begin() as session:
            deployment = session.scalar(select(SolutionDeployment))
            sync = session.get(DeploymentSyncState, deployment.id)
            sync.next_attempt_at = None
        self.apply_desired(kubectl=FakeKubectl())
        self.service.reconcile_enrollment_sessions(FakeApex())
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "cancelled")
        self.assertFalse(status["degraded"])

    # 13. Concurrent configuration changes cannot overwrite an active session's
    # camera, but unrelated camera edits still go through untouched.
    def test_concurrent_edit_of_enrolled_camera_is_blocked(self):
        self.onboard_camera("camera-02")
        self.deploy_with_face_recognition(
            extra_assignments=[{"camera_id": "camera-02", "apps": ["anpr"], "fps": 8, "config": {}}]
        )
        self.start_session()
        with self.sessions() as session:
            deployment = session.scalar(select(SolutionDeployment))
            _cid, assignments, inference_mode, resources, state_size = (
                self.service._current_catalog_assignments(session, deployment)
            )
        conflicting = [
            {**item, "apps": ["anpr"]} if item["camera_id"] == "camera-01" else item
            for item in assignments
        ]
        preview = self.service.preview_catalog_deployment(
            catalog_id=CATALOG_ID, deployment_key="tvt-mills-v1", assignments=conflicting,
            inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
        )
        with self.assertRaisesRegex(ValueError, "under an active enrollment session"):
            self.service.commit_catalog_deployment(
                catalog_id=CATALOG_ID, deployment_key="tvt-mills-v1", assignments=conflicting,
                inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
                preview_bundle_sha256=preview["bundle_sha256"], idempotency_key="conflict-1",
                actor="op", request_id="conflict-1",
            )

    def test_unrelated_camera_edit_during_active_session_is_preserved(self):
        self.onboard_camera("camera-02")
        self.deploy_with_face_recognition(
            extra_assignments=[{"camera_id": "camera-02", "apps": ["anpr"], "fps": 8, "config": {}}]
        )
        self.start_session()
        with self.sessions() as session:
            deployment = session.scalar(select(SolutionDeployment))
            _cid, assignments, inference_mode, resources, state_size = (
                self.service._current_catalog_assignments(session, deployment)
            )
        updated = [
            {**item, "fps": 12} if item["camera_id"] == "camera-02" else item
            for item in assignments
        ]
        preview = self.service.preview_catalog_deployment(
            catalog_id=CATALOG_ID, deployment_key="tvt-mills-v1", assignments=updated,
            inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
        )
        self.service.commit_catalog_deployment(
            catalog_id=CATALOG_ID, deployment_key="tvt-mills-v1", assignments=updated,
            inference_mode=inference_mode, resources=resources, state_size=state_size, namespace="apexfabric",
            preview_bundle_sha256=preview["bundle_sha256"], idempotency_key="unrelated-1",
            actor="op", request_id="unrelated-1",
        )
        camera_02 = self.current_assignment("camera-02")
        self.assertEqual(camera_02["fps"], 12)
        camera_01 = self.current_assignment("camera-01")
        self.assertEqual(camera_01["apps"], ["face_enrollment"])

    # 14. Name validation, unknown-person handling, and naming audit.
    def test_name_validation_and_unknown_person(self):
        self.deploy_with_face_recognition()
        apex = FakeApex()
        with self.assertRaisesRegex(ValueError, "unknown person_id"):
            self.service.set_person_display_name(
                person_id="ghost", display_name="Jane", apex=apex, actor="op", request_id="n1"
            )

    def test_retention_prunes_old_terminal_sessions_but_never_a_pending_name(self):
        self.deploy_with_face_recognition()
        started = self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(
            events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at)],
            persons=[{"person_id": "person-1", "enrollment_source_event_id": "tvt-mills-v1:evt-1", "status": "auto_enrolled", "display_name": None}],
        )
        self.service.reconcile_enrollment_sessions(apex)
        self.apply_desired()
        self.service.reconcile_enrollment_sessions(apex)
        status = self.service.get_enrollment_status("tvt-mills-v1")
        self.assertEqual(status["session"]["status"], "completed")
        self.assertEqual(status["session"]["naming_status"], "pending_name")

        far_future = utc_now() + timedelta(days=400)
        result = self.service.apply_retention(far_future)
        self.assertEqual(result["enrollment_sessions"], 0)  # still pending_name -- never pruned
        with self.sessions() as session:
            self.assertIsNotNone(session.get(EnrollmentSession, uuid.UUID(started["session_id"])))

        self.service.set_person_display_name(
            person_id="person-1", display_name="Jane Doe", apex=apex, actor="op", request_id="n1"
        )
        result = self.service.apply_retention(far_future)
        self.assertEqual(result["enrollment_sessions"], 1)
        with self.sessions() as session:
            self.assertIsNone(session.get(EnrollmentSession, uuid.UUID(started["session_id"])))

    def test_naming_sets_status_and_audits_without_storing_the_name(self):
        self.deploy_with_face_recognition()
        self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(
            events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at)],
            persons=[{"person_id": "person-1", "enrollment_source_event_id": "tvt-mills-v1:evt-1", "status": "auto_enrolled", "display_name": None}],
        )
        self.service.reconcile_enrollment_sessions(apex)
        with self.assertRaisesRegex(ValueError, "required"):
            self.service.set_person_display_name(
                person_id="person-1", display_name="   ", apex=apex, actor="op", request_id="n0"
            )
        result = self.service.set_person_display_name(
            person_id="person-1", display_name="Jane Doe", apex=apex, actor="op", request_id="n1"
        )
        self.assertEqual(result["naming_status"], "named")
        self.assertEqual(apex.renamed, [("person-1", "Jane Doe")])
        audit = [item for item in self.service.list_audit_events(200) if item["action"] == "enrollment.person.name"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["target_id"], "person-1")
        self.assertNotIn("Jane", json.dumps(audit[0]["details"]))
        self.assertEqual(audit[0]["details"], {})

    def test_report_can_name_an_unnamed_person_without_storing_the_name(self):
        apex = FakeApex(persons=[{
            "person_id": "person-1",
            "status": "auto_enrolled",
            "display_name": None,
        }])

        result = self.service.set_report_person_display_name(
            person_id="person-1",
            display_name="Jane Doe",
            apex=apex,
            actor="op",
            request_id="report-name-1",
        )

        self.assertEqual(result, {"person_id": "person-1", "naming_status": "named"})
        self.assertEqual(apex.renamed, [("person-1", "Jane Doe")])
        audit = [
            item for item in self.service.list_audit_events(200)
            if item["action"] == "reports.person.name"
        ]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["target_id"], "person-1")
        self.assertEqual(audit[0]["details"], {})
        self.assertNotIn("Jane", json.dumps(audit[0]))

    def test_report_naming_rejects_an_unknown_person(self):
        with self.assertRaisesRegex(ValueError, "unknown person_id"):
            self.service.set_report_person_display_name(
                person_id="ghost",
                display_name="Jane Doe",
                apex=FakeApex(),
                actor="op",
                request_id="report-name-unknown",
            )

    # 15. No vectors/names/raw payloads/sensitive URLs anywhere except the
    # authorized person-name response.
    def test_no_sensitive_data_in_status_sessions_or_pending_list(self):
        self.deploy_with_face_recognition()
        self.activate()
        status = self.service.get_enrollment_status("tvt-mills-v1")
        activated_at = status["session"]["activated_at"]
        apex = FakeApex(
            events=[capture_event("tvt-mills-v1", "camera-01", "evt-1", activated_at, {"sharpness": 0.9})],
            persons=[{"person_id": "person-1", "enrollment_source_event_id": "tvt-mills-v1:evt-1", "status": "auto_enrolled", "display_name": None}],
        )
        self.service.reconcile_enrollment_sessions(apex)
        blob = json.dumps(self.service.get_enrollment_status("tvt-mills-v1"))
        blob += json.dumps(self.service.list_enrollment_sessions("tvt-mills-v1"))
        blob += json.dumps(self.service.list_people_awaiting_names())
        for forbidden in ("embedding", "rtsp://", "sharpness", "quality", "Jane"):
            self.assertNotIn(forbidden, blob)

    def test_apex_client_post_json_maps_client_error_to_value_error(self):
        client = ApexClient("http://127.0.0.1:1")

        class FakeResult:
            status = 400
            body = b'{"error": "unknown person_id"}'

        client.post = lambda path, body: FakeResult()  # type: ignore[assignment]
        with self.assertRaisesRegex(ValueError, "unknown person_id"):
            client.post_json("/api/persons/rename", {"person_id": "x", "display_name": "y"})


class SelectFirstCaptureTests(unittest.TestCase):
    """Pure eligibility-rule tests -- no database, no network."""

    def test_first_eligible_capture_wins_by_occurred_at(self):
        events = [
            capture_event("dep", "cam-1", "evt-2", "2026-09-21T09:00:05Z"),
            capture_event("dep", "cam-1", "evt-1", "2026-09-21T09:00:01Z"),
        ]
        from datetime import datetime, timezone

        candidate = select_first_capture(
            events,
            deployment_key="dep",
            camera_key="cam-1",
            window_start=datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 21, 9, 5, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.event_id, "dep:evt-1")

    def test_out_of_window_event_is_rejected(self):
        from datetime import datetime, timezone

        event = capture_event("dep", "cam-1", "evt-1", "2026-09-21T09:10:00Z")
        candidate = eligible_capture_candidate(
            event,
            deployment_key="dep",
            camera_key="cam-1",
            window_start=datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 21, 9, 5, tzinfo=timezone.utc),
        )
        self.assertIsNone(candidate)

    def test_quality_below_threshold_is_rejected(self):
        from datetime import datetime, timezone

        event = capture_event("dep", "cam-1", "evt-1", "2026-09-21T09:00:00Z", {"sharpness": 0.2})
        candidate = eligible_capture_candidate(
            event,
            deployment_key="dep",
            camera_key="cam-1",
            window_start=datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 21, 9, 5, tzinfo=timezone.utc),
            minimum_sharpness=0.5,
        )
        self.assertIsNone(candidate)

    def test_wrong_camera_is_rejected(self):
        from datetime import datetime, timezone

        event = capture_event("dep", "cam-2", "evt-1", "2026-09-21T09:00:00Z")
        candidate = eligible_capture_candidate(
            event,
            deployment_key="dep",
            camera_key="cam-1",
            window_start=datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 21, 9, 5, tzinfo=timezone.utc),
        )
        self.assertIsNone(candidate)


class EnrollmentHttpRouteTests(unittest.TestCase):
    """Exercises the new bounded, audited endpoints end to end, mirroring
    test_management_plane.py's test_enrollment_http_routes_start_stop_and_list
    for the old EnrollmentWindow flow."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.keyring = CredentialKeyring.generate_for_test()
        self.service = ManagementService(
            self.sessions, self.keyring, catalog_resolver=lambda *_a: CATALOG_DIGEST
        )
        self.service.create_site("plant-01", "edge-01", "Plant 01", "Asia/Kolkata", "test", "site")
        self.service.create_camera(
            camera_key="camera-01", friendly_name="Main", manufacturer="X", model="Y",
            identifiers=[{"kind": "mac", "value": "00:11:22:33:44:55"}], actor="test", request_id="cam1",
        )
        self.service.configure_stream(
            "camera-01", scheme="rtsp", host="192.0.2.10", port=554, path="/live/main",
            profile_token="p", transport="tcp", codec="h264", width=1920, height=1080, fps=15,
            actor="test", request_id="s1",
        )
        self.service.rotate_credentials("camera-01", {"username": "u", "password": "p"}, "test", "c1")
        self.service.set_camera_enabled("camera-01", True, "test", "e1")
        self.service.seed_solution_catalog(CATALOG_DELIVERY, "127.0.0.1:5000")
        self.service.refresh_solutions(actor="test", request_id="refresh")
        request = dict(
            catalog_id=CATALOG_ID, deployment_key="tvt-mills-v1",
            assignments=[{
                "camera_id": "camera-01", "apps": ["face_recognition", "anpr"], "fps": 8, "config": {},
            }],
            inference_mode="gpu-npu", resources={}, state_size="50Gi", namespace="apexfabric",
        )
        preview = self.service.preview_catalog_deployment(**request)
        self.service.commit_catalog_deployment(
            **request, preview_bundle_sha256=preview["bundle_sha256"],
            idempotency_key="deploy-1", actor="test", request_id="deploy-1",
        )

    def test_designate_start_status_cancel_and_people_routes(self):
        import asyncio

        import httpx

        from tvt_edge.api import create_app

        app = create_app(self.sessions, self.keyring)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                designate = await client.post(
                    "/api/v1/deployments/tvt-mills-v1/enrollment/camera", json={"camera_id": "camera-01"}
                )
                camera = await client.get("/api/v1/deployments/tvt-mills-v1/enrollment/camera")
                start = await client.post(
                    "/api/v1/deployments/tvt-mills-v1/enrollment/sessions", json={}
                )
                status = await client.get("/api/v1/deployments/tvt-mills-v1/enrollment/status")
                sessions_list = await client.get("/api/v1/deployments/tvt-mills-v1/enrollment/sessions")
                cancel = await client.post(
                    f"/api/v1/deployments/tvt-mills-v1/enrollment/sessions/{start.json()['session_id']}/cancel"
                )
                people = await client.get("/api/v1/enrollment/people")
            return designate, camera, start, status, sessions_list, cancel, people

        designate, camera, start, status, sessions_list, cancel, people = asyncio.run(exercise())
        self.assertEqual(designate.status_code, 200)
        self.assertEqual(camera.json()["camera_id"], "camera-01")
        self.assertEqual(start.status_code, 200)
        self.assertEqual(start.json()["status"], "activating")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["session"]["status"], "activating")
        self.assertEqual(len(sessions_list.json()), 1)
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.json()["status"], "restoring")
        self.assertEqual(people.status_code, 200)
        self.assertEqual(people.json(), [])

    def test_naming_route_surfaces_unknown_person_as_client_error(self):
        import asyncio

        import httpx

        from tvt_edge.api import create_app

        app = create_app(self.sessions, self.keyring)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                return await client.post(
                    "/api/v1/enrollment/people/ghost/name", json={"display_name": "Jane"}
                )

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("Jane", response.text)


if __name__ == "__main__":
    unittest.main()
