import json
import unittest

from apexfabric.control_plane.storage_failover import reconcile_local_storage_failover


class Result:
    def __init__(self, value):
        self.stdout = json.dumps(value) if isinstance(value, dict) else value


class StorageFailoverTests(unittest.TestCase):
    def test_retains_old_claim_and_moves_deployment_to_fresh_claim(self):
        calls = []
        resources = {
            "nodes": {"items": [
                {"metadata": {"name": "edge-old", "labels": {"apexfabric.com/qualified": "true", "kubernetes.io/arch": "amd64"}},
                 "status": {"conditions": [{"type": "Ready", "status": "Unknown", "lastTransitionTime": "2026-01-01T00:00:00Z"}]}},
                {"metadata": {"name": "edge-new", "labels": {"apexfabric.com/qualified": "true", "kubernetes.io/arch": "amd64"}},
                 "status": {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-01-01T00:00:00Z"}]}},
            ]},
            "deployments": {"items": [{
                "metadata": {"name": "site-runtime", "labels": {"apexfabric.com/deployment-id": "site", "apexfabric.com/application": "runtime"},
                             "annotations": {"apexfabric.com/local-storage-failover": "retain-and-recreate", "apexfabric.com/failover-after-seconds": "120"}},
                "spec": {"template": {"metadata": {"labels": {}}, "spec": {
                    "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]}]}]}}},
                    "volumes": [{"name": "persistent-state", "persistentVolumeClaim": {"claimName": "site-runtime-state"}}],
                    "containers": [{"name": "runtime", "image": "example/runtime:1"}],
                }}},
            }]},
            "pods": {"items": [{"metadata": {"name": "site-runtime-old", "labels": {"apexfabric.com/deployment-id": "site", "apexfabric.com/application": "runtime"}}, "spec": {"nodeName": "edge-old"}}]},
            "persistentvolumeclaims": {"items": [{"metadata": {"name": "site-runtime-state", "labels": {"apexfabric.com/deployment-id": "site"}},
                                                     "spec": {"storageClassName": "local-path", "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "20Gi"}}, "volumeName": "old-pv"}}]},
        }

        def kubectl(*args, input_text=None, check=True):
            calls.append((args, input_text, check))
            if args[0] == "get":
                return Result(resources[args[1]])
            return Result("")

        actions = reconcile_local_storage_failover(kubectl, now=1_800_000_000)
        self.assertEqual(len(actions), 1)
        applied = json.loads(next(body for args, body, _ in calls if args[:2] == ("apply", "-f")))
        self.assertEqual(applied["metadata"]["annotations"]["apexfabric.com/replaces-claim"], "site-runtime-state")
        self.assertNotIn("volumeName", applied["spec"])
        patch_call = next(call for call in calls if call[0][:2] == ("patch", "deployment"))
        patch = json.loads(patch_call[0][-1])
        new_claim = patch["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        self.assertTrue(new_claim.startswith("site-runtime-state-fo-"))
        self.assertTrue(any(args[:2] == ("delete", "pod") and "site-runtime-old" in args for args, _, _ in calls))


if __name__ == "__main__":
    unittest.main()
