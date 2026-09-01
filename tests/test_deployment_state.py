import argparse
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tvt_runtime.cli import apply_command, local_command, replace_registry
from tvt_runtime.lifecycle import camera_contract_signature, with_desired_state
from tvt_runtime.state import DeploymentStore, ensure_bundle_has_no_inline_secrets


ROOT = Path(__file__).resolve().parents[1]


class FakeKubectl:
    def __init__(self):
        self.calls = []

    def run(self, *arguments, input_text=None, check=True):
        self.calls.append((arguments, input_text))

        class Result:
            stdout = ""

        return Result()


class DeploymentStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = yaml.safe_load(
            (
                ROOT
                / "solution-packs/traffic/traffic-edge-runtime-intel-285h.yaml"
            ).read_text(encoding="utf-8")
        )
        replace_registry(cls.bundle, "registry.local:5000")
        cls.secret_inputs = {
            "desired_state": json.loads(
                (
                    ROOT
                    / "solution-packs/traffic/traffic.desired_state.example.json"
                ).read_text(encoding="utf-8")
            ),
            "camera_sources": {
                "cam4": "rtsp://user:password@192.0.2.14/live",
                "cam5": "rtsp://user:password@192.0.2.15/live",
            },
        }

    def test_store_tracks_active_and_previous_non_secret_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeploymentStore(Path(directory))
            running_revision = store.record_success(
                self.bundle,
                "apexfabric",
                "registry.local:5000",
                "apply",
                {"observed": []},
            )
            stopped = with_desired_state(self.bundle, "Stopped")
            stopped_revision = store.record_success(
                stopped,
                "apexfabric",
                "registry.local:5000",
                "stop",
                {"observed": []},
            )
            self.assertNotEqual(running_revision, stopped_revision)
            self.assertEqual(
                store.get_deployment(self.bundle["deployment_id"])["desired_state"],
                "Stopped",
            )
            self.assertEqual(
                store.previous_revision(self.bundle["deployment_id"]),
                running_revision,
            )
            self.assertEqual(len(store.history(self.bundle["deployment_id"])), 2)
            self.assertEqual(os.stat(store.database).st_mode & 0o777, 0o600)

    def test_apply_persists_no_ephemeral_camera_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = Path(directory) / "secret-inputs.json"
            inputs.write_text(json.dumps(self.secret_inputs), encoding="utf-8")
            store = DeploymentStore(Path(directory) / "state")
            args = argparse.Namespace(
                kubeconfig=None,
                secret_inputs=inputs,
                namespace="apexfabric",
                dry_run=False,
                rollout_timeout=180,
                registry="registry.local:5000",
            )
            client = FakeKubectl()
            report = {
                "revision": "renderer-revision",
                "applied": [],
                "removed": [],
                "observed": [
                    {
                        "name": "traffic-edge-intel-285h-runtime",
                        "desired_replicas": 1,
                        "ready_replicas": 1,
                        "available_replicas": 1,
                    }
                ],
            }
            with patch("tvt_runtime.cli.kubectl_client", return_value=client), patch(
                "tvt_runtime.cli.reconcile", return_value=report
            ):
                result = apply_command(args, self.bundle, store)
            self.assertIn("active_revision", result)
            database_bytes = store.database.read_bytes()
            self.assertNotIn(b"user:password", database_bytes)
            secret_apply = client.calls[0][1]
            self.assertIn("user:password", secret_apply)
            self.assertTrue(
                any(call[0][:2] == ("rollout", "restart") for call in client.calls)
            )

    def test_declarative_stop_dry_run_does_not_change_active_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeploymentStore(Path(directory))
            active = store.record_success(
                self.bundle,
                "apexfabric",
                "registry.local:5000",
                "apply",
                {"observed": []},
            )
            args = argparse.Namespace(
                command="stop",
                deployment_id=self.bundle["deployment_id"],
                state_dir=Path(directory),
                kubeconfig=None,
                rollout_timeout=180,
                dry_run=True,
            )
            result = local_command(args)
            deployment = next(
                item for item in result["desired_objects"]
                if item["kind"] == "Deployment"
            )
            self.assertEqual(deployment["name"], "traffic-edge-intel-285h-runtime")
            self.assertEqual(
                store.get_deployment(self.bundle["deployment_id"])["active_revision"],
                active,
            )

    def test_camera_assignment_change_is_detected_for_rollback(self):
        changed = copy.deepcopy(self.bundle)
        changed["applications"][0]["cameras"] = ["cam4"]
        changed["applications"][0]["external_mounts"] = changed["applications"][0][
            "external_mounts"
        ][:1]
        self.assertNotEqual(
            camera_contract_signature(self.bundle), camera_contract_signature(changed)
        )

    def test_inline_bundle_secrets_are_rejected_before_persistence(self):
        unsafe = copy.deepcopy(self.bundle)
        unsafe["applications"][0]["secrets"] = {"token": "do-not-store"}
        with self.assertRaisesRegex(ValueError, "ephemeral secret_inputs"):
            ensure_bundle_has_no_inline_secrets(unsafe)

    def test_failure_history_redacts_rtsp_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeploymentStore(Path(directory))
            store.record_failure(
                "traffic-edge-intel-285h",
                "apply",
                "failed to open rtsp://user:password@192.0.2.14/live",
                attempted_revision="f" * 64,
                secret_update_attempted=True,
            )
            database_bytes = store.database.read_bytes()
            self.assertNotIn(b"user:password", database_bytes)
            self.assertIn(b"REDACTED_RTSP_URL", database_bytes)

    def test_failed_secret_update_requires_inputs_to_restore_last_good_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeploymentStore(Path(directory))
            active = store.record_success(
                self.bundle,
                "apexfabric",
                "registry.local:5000",
                "apply",
                {"observed": []},
            )
            store.record_failure(
                self.bundle["deployment_id"],
                "apply",
                "rollout failed",
                attempted_revision="f" * 64,
                secret_update_attempted=True,
            )
            deployment = store.get_deployment(self.bundle["deployment_id"])
            self.assertEqual(deployment["active_revision"], active)
            self.assertEqual(deployment["last_event"]["outcome"], "failed")
            args = argparse.Namespace(
                command="rollback",
                deployment_id=self.bundle["deployment_id"],
                revision=None,
                secret_inputs=None,
                state_dir=Path(directory),
                kubeconfig=None,
                rollout_timeout=180,
                dry_run=True,
            )
            with self.assertRaisesRegex(ValueError, "restore camera Secrets"):
                local_command(args)


if __name__ == "__main__":
    unittest.main()
