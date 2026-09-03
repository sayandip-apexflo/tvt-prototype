import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from apexfabric.solution_management.catalog import load_delivery_metadata
from tvt_edge.qualification import (
    CATALOG_ID,
    QualificationOptions,
    TrafficQualifier,
    atomic_write_report,
    parse_sse_events,
)


ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
DIGEST = "sha256:" + "1" * 64
BUNDLE_SHA = "2" * 64
IMAGE = f"127.0.0.1:5000/apexfabric/traffic-edge-runtime@{DIGEST}"


class FakeApi:
    def __init__(self, *, pvc_uid="pvc-uid-1"):
        self.posts = []
        self.pvc_uid = pvc_uid

    def get(self, path):
        if path == "/api/v1/deployments":
            return [
                {
                    "deployment_id": "traffic-v4",
                    "catalog_id": CATALOG_ID,
                    "sync_state": "applied",
                    "desired_revision": 4,
                    "applied_revision": 4,
                    "applied_bundle_sha256": BUNDLE_SHA,
                    "applied_image_digest": DIGEST,
                }
            ]
        if path == "/api/v1/solutions":
            return [
                {
                    "catalog_id": CATALOG_ID,
                    "status": "available",
                    "image": {"reference": IMAGE},
                }
            ]
        if path == "/api/v1/cluster":
            return {
                "nodes": {
                    "items": [
                        {
                            "name": "edge-01",
                            "ready": True,
                            "qualified": True,
                            "architecture": "amd64",
                            "hardware_profile": "intel-285h",
                        }
                    ]
                }
            }
        if path == "/api/v1/cluster/workloads/traffic-v4-runtime/telemetry":
            return {
                "available": True,
                "health": {"status": "ok"},
                "readiness": {"ready": True},
                "metrics": json.dumps(
                    {
                        "format": "application/json",
                        "runtime": {
                            "solution_pack": "traffic",
                            "edge_id": "edge-01",
                            "revision": 4,
                            "plan_loaded": True,
                            "models_ready": True,
                            "camera_count": 1,
                            "configured_cameras": 1,
                            "child_running": True,
                            "child_exit_code": None,
                            "uptime_seconds": 30,
                            "stop_requested": False,
                            "last_error": None,
                        },
                        "events": {
                            "protocol": "server-sent-events",
                            "path": "/events",
                        },
                        "snapshots": {
                            "path_prefix": "/snapshots/",
                            "content_types": ["image/jpeg"],
                            "source": "persistent_state",
                        },
                    }
                ),
            }
        raise AssertionError(f"unexpected API GET {path}")

    def post(self, path, body):
        self.posts.append((path, body))
        if path == "/api/v1/deployments/preview":
            return {
                "image_reference": IMAGE,
                "bundle_sha256": BUNDLE_SHA,
                "bundle": {"image": IMAGE},
                "desired_state": {"source": "file:/run/secrets/apexfabric/camera-01.rtsp"},
            }
        if path == "/api/v1/deployments":
            return {"state": "pending", "desired_revision": 4}
        if path.endswith("/rollback"):
            return {"state": "pending", "desired_revision": 5}
        raise AssertionError(f"unexpected API POST {path}")


class FakeCommands:
    def __init__(self, *, events=True, pvc_uid="pvc-uid-1", event_override=None):
        self.calls = []
        self.events = events
        self.pvc_uid = pvc_uid
        self.event_override = event_override

    @staticmethod
    def result(arguments, stdout="", returncode=0):
        return subprocess.CompletedProcess(arguments, returncode, stdout, "")

    def run(self, arguments, *, timeout=10, check=True):
        self.calls.append((arguments, timeout, check))
        if arguments == ["dpkg", "--print-architecture"]:
            return self.result(arguments, "amd64\n")
        if arguments[:3] == ["systemctl", "is-active", "--quiet"]:
            return self.result(arguments)
        if arguments[0] in {
            "vainfo",
            "clinfo",
            "/opt/apexfabric/openvino-env/bin/python",
            "curl",
        }:
            return self.result(arguments)
        if arguments[:3] == ["k3s", "crictl", "inspecti"]:
            return self.result(arguments, "{}")
        if "deployment" in arguments and "get" in arguments:
            return self.result(arguments, json.dumps(self.deployment()))
        if "secret" in arguments and "get" in arguments:
            return self.result(arguments, "secret/example\n")
        if "pods" in arguments:
            return self.result(arguments, json.dumps({"items": [self.pod()]}))
        if "persistentvolumeclaims" in arguments:
            return self.result(
                arguments,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "traffic-v4-runtime-state",
                                    "uid": self.pvc_uid,
                                },
                                "status": {"phase": "Bound"},
                            }
                        ]
                    }
                ),
            )
        if "--raw" in arguments:
            if not self.events:
                return self.result(arguments, ": heartbeat\n\n", 1)
            event = self.event_override or json.loads(
                (DELIVERY / "analytics-event.example.json").read_text()
            )
            return self.result(
                arguments,
                f": heartbeat\n\nevent: analytics\ndata: {json.dumps(event)}\n\n",
                1,
            )
        raise AssertionError(f"unexpected command {arguments}")

    @staticmethod
    def deployment():
        mounts = [
            {"name": "desired-state", "mountPath": "/configs/desired_state.json"},
            {"name": "plans", "mountPath": "/plans"},
            {"name": "tmp", "mountPath": "/tmp/apexfabric"},
            {"name": "state", "mountPath": "/state"},
            {"name": "dri", "mountPath": "/dev/dri"},
            {"name": "accel", "mountPath": "/dev/accel"},
            {
                "name": "camera-source",
                "mountPath": "/run/secrets/apexfabric/camera-01.rtsp",
            },
        ]
        compiler_mounts = [item for item in mounts if item["mountPath"] != "/state"]
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "runtime", "image": IMAGE, "volumeMounts": mounts}
                        ],
                        "initContainers": [
                            {
                                "name": "plan-compiler",
                                "image": IMAGE,
                                "volumeMounts": compiler_mounts,
                            }
                        ],
                        "volumes": [
                            {
                                "name": "desired-state",
                                "secret": {
                                    "secretName": "traffic-v4-desired-state"
                                },
                            },
                            {
                                "name": "camera-source",
                                "secret": {
                                    "secretName": "traffic-v4-camera-sources"
                                },
                            },
                            {
                                "name": "state",
                                "persistentVolumeClaim": {
                                    "claimName": "traffic-v4-runtime-state"
                                },
                            },
                        ],
                    }
                }
            }
        }

    @staticmethod
    def pod():
        return {
            "metadata": {"name": "traffic-v4-runtime-abc"},
            "status": {
                "phase": "Running",
                "initContainerStatuses": [
                    {
                        "name": "plan-compiler",
                        "state": {"terminated": {"exitCode": 0}},
                    }
                ],
                "containerStatuses": [
                    {
                        "name": "runtime",
                        "ready": True,
                        "imageID": IMAGE,
                    }
                ],
            },
        }


class TrafficQualificationTests(unittest.TestCase):
    def qualifier(self, api=None, commands=None):
        metadata = load_delivery_metadata(DELIVERY)
        provenance = metadata["provenance"]
        image_lock = {
            "format_version": 2,
            "catalog_id": CATALOG_ID,
            "pipeline": {
                "repository": provenance["pipeline"]["repository"],
                "commit": provenance["pipeline"]["commit"],
                "delivery_directory": provenance["delivery"]["directory"],
            },
            "archive": provenance["archive"],
            "source": {"mode": "archive"},
            "image": {
                "reference": IMAGE,
                "digest": DIGEST,
                "architecture": "amd64",
            },
            "metadata": {
                "image_contract_sha256": metadata["checksums"]["image-contract.yaml"],
                "desired_state_schema_sha256": metadata["checksums"]["desired-state.schema.json"],
                "metrics_schema_sha256": metadata["checksums"]["metrics.schema.json"],
                "analytics_event_schema_sha256": metadata["checksums"][
                    "analytics-event.schema.json"
                ],
                "analytics_event_example_sha256": metadata["checksums"][
                    "analytics-event.example.json"
                ],
            },
        }
        return TrafficQualifier(
            api or FakeApi(),
            commands or FakeCommands(),
            DELIVERY,
            path_exists=lambda path: path
            != Path("/var/lib/tvt/hardware-driver-reboot-required"),
            cpuinfo_reader=lambda: "model name: Intel Core Ultra 9 285H",
            image_lock_reader=lambda: image_lock,
        )

    def test_vendored_runtime_contracts_match_pinned_provenance(self):
        metadata = load_delivery_metadata(DELIVERY)
        self.assertEqual(
            metadata["checksums"]["metrics.schema.json"],
            "dae9f30aa893f96be7f030b1184245f8547bd2446928dad9ac61bd83a763a59c",
        )
        self.assertEqual(
            metadata["checksums"]["analytics-event.schema.json"],
            "a93247a681f717fe5e3609658270c9687ed4a978b4d60e964d98f4a659b15d2f",
        )
        self.assertEqual(metadata["analytics_event_example"]["solution_pack"], "traffic")

    def test_full_qualification_validates_runtime_events_and_safe_invariants(self):
        commands = FakeCommands()
        report = self.qualifier(commands=commands).qualify(
            QualificationOptions("traffic-v4", strict_events=True, wait_seconds=0)
        )
        self.assertEqual(report["outcome"], "passed")
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(report["invariants"]["pvc_uid"], "pvc-uid-1")
        analytics = next(
            item for item in report["checks"] if item["id"] == "runtime.analytics_events"
        )
        self.assertEqual(analytics["evidence"]["validated_event_count"], 1)
        self.assertTrue(
            any(call[0][:3] == ["k3s", "crictl", "inspecti"] for call in commands.calls)
        )
        secret_calls = [call for call in commands.calls if "secret" in call[0]]
        self.assertTrue(secret_calls)
        self.assertTrue(all("-o" in call[0] and "name" in call[0] for call in secret_calls))

    def test_preview_commit_uses_public_api_and_rejects_secret_input(self):
        api = FakeApi()
        request = {
            "catalog_id": CATALOG_ID,
            "deployment_id": "traffic-v4",
            "assignments": [{"camera_id": "camera-01", "apps": ["anpr"]}],
        }
        report = self.qualifier(api=api).qualify(
            QualificationOptions(
                "traffic-v4",
                deployment_request=request,
                commit_preview=True,
                wait_seconds=0,
            )
        )
        self.assertEqual(report["outcome"], "passed")
        self.assertEqual(api.posts[0][0], "/api/v1/deployments/preview")
        self.assertEqual(api.posts[1][0], "/api/v1/deployments")
        self.assertEqual(api.posts[1][1]["preview_bundle_sha256"], BUNDLE_SHA)

        unsafe_api = FakeApi()
        unsafe = {**request, "password": "do-not-store"}
        unsafe_report = self.qualifier(api=unsafe_api).qualify(
            QualificationOptions(
                "traffic-v4",
                deployment_request=unsafe,
                commit_preview=True,
                wait_seconds=0,
            )
        )
        self.assertEqual(unsafe_report["outcome"], "failed")
        self.assertEqual(unsafe_api.posts, [])
        self.assertNotIn("do-not-store", json.dumps(unsafe_report))

    def test_events_are_optional_unless_strict(self):
        optional = self.qualifier(commands=FakeCommands(events=False)).qualify(
            QualificationOptions("traffic-v4", wait_seconds=0)
        )
        self.assertEqual(optional["outcome"], "passed")
        event_check = next(
            item for item in optional["checks"] if item["id"] == "runtime.analytics_events"
        )
        self.assertEqual(event_check["status"], "skipped")
        strict = self.qualifier(commands=FakeCommands(events=False)).qualify(
            QualificationOptions("traffic-v4", strict_events=True, wait_seconds=0)
        )
        self.assertEqual(strict["outcome"], "failed")

    def test_invalid_event_values_are_not_copied_into_evidence(self):
        private_value = "PRIVATE-PLATE-VALUE"
        invalid_event = {"plate": private_value}
        report = self.qualifier(
            commands=FakeCommands(event_override=invalid_event)
        ).qualify(QualificationOptions("traffic-v4", wait_seconds=0))
        self.assertEqual(report["outcome"], "failed")
        self.assertNotIn(private_value, json.dumps(report))

    def test_post_reboot_and_rollback_check_complete_invariants(self):
        baseline = self.qualifier().qualify(
            QualificationOptions("traffic-v4", checkpoint="pre-reboot", wait_seconds=0)
        )
        matching = self.qualifier().qualify(
            QualificationOptions(
                "traffic-v4",
                checkpoint="post-reboot",
                baseline=baseline,
                wait_seconds=0,
            )
        )
        self.assertEqual(matching["outcome"], "passed")
        changed = self.qualifier(commands=FakeCommands(pvc_uid="different-pvc")).qualify(
            QualificationOptions(
                "traffic-v4",
                checkpoint="post-rollback",
                baseline=baseline,
                rollback_bundle_sha256=BUNDLE_SHA,
                wait_seconds=0,
            )
        )
        self.assertEqual(changed["outcome"], "failed")
        checkpoint = next(
            item for item in changed["checks"] if item["id"] == "checkpoint.post-rollback"
        )
        self.assertEqual(checkpoint["evidence"]["difference_keys"], ["pvc_uid"])

    def test_rollback_must_exactly_match_a_passing_baseline(self):
        baseline = self.qualifier().qualify(
            QualificationOptions("traffic-v4", checkpoint="pre-reboot", wait_seconds=0)
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.qualifier().qualify(
                QualificationOptions(
                    "traffic-v4",
                    checkpoint="post-rollback",
                    baseline=baseline,
                    rollback_bundle_sha256="9" * 64,
                    wait_seconds=0,
                )
            )
        self.assertEqual(baseline["outcome"], "passed")

    def test_report_is_atomic_private_and_verifiable(self):
        report = self.qualifier().qualify(
            QualificationOptions("traffic-v4", wait_seconds=0)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qualification.json"
            atomic_write_report(path, report)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            result = subprocess.run(
                [
                    str(ROOT / ".venv/bin/python"),
                    str(ROOT / "scripts/verify-traffic-qualification.py"),
                    str(path),
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn('"outcome": "verified"', result.stdout)
            self.assertNotIn("rtsp://", path.read_text())
            tampered = json.loads(path.read_text())
            tampered["checks"] = tampered["checks"][1:]
            tampered["summary"]["passed"] -= 1
            path.write_text(json.dumps(tampered), encoding="utf-8")
            rejected = subprocess.run(
                [
                    str(ROOT / ".venv/bin/python"),
                    str(ROOT / "scripts/verify-traffic-qualification.py"),
                    str(path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("missing required checks", rejected.stderr)

    def test_sse_parser_rejects_non_object_payload(self):
        self.assertEqual(parse_sse_events(": heartbeat\n\n"), [])
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_sse_events("data: [1, 2]\n\n")

    def test_qualification_scripts_have_valid_shell_syntax(self):
        for name in (
            "qualify-traffic-edge.sh",
            "install-traffic-qualification.sh",
        ):
            subprocess.run(
                ["bash", "-n", str(ROOT / "scripts" / name)], check=True
            )


if __name__ == "__main__":
    unittest.main()
