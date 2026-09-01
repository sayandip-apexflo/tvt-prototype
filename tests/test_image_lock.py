import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tvt_runtime.image_lock import create_image_lock, render_manifest, validate_image_lock


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


class ImageLockTests(unittest.TestCase):
    def test_create_lock_resolves_each_control_image(self):
        with patch(
            "tvt_runtime.image_lock.resolve_registry_digest", return_value=DIGEST
        ) as resolver:
            lock = create_image_lock("http://registry.local:5000", "0.1.0")
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(
            lock["images"]["node-reporter"],
            f"registry.local:5000/apexfabric/node-reporter@{DIGEST}",
        )

    def test_manifest_is_rendered_with_digest_pinned_images(self):
        lock = {
            "version": 1,
            "registry": "registry.local:5000",
            "images": {
                "node-reporter": f"registry.local:5000/apexfabric/node-reporter@{DIGEST}",
                "node-status-controller": f"registry.local:5000/apexfabric/node-status-controller@{DIGEST}",
            },
        }
        rendered = render_manifest(
            ROOT / "deploy/k8s/apexfabric-node-management.yaml", lock
        )
        resources = list(yaml.safe_load_all(rendered))
        images = {
            resource["spec"]["template"]["spec"]["containers"][0]["image"]
            for resource in resources
            if resource["kind"] in {"DaemonSet", "Deployment"}
        }
        self.assertEqual(len(images), 2)
        self.assertTrue(all(f"@{DIGEST}" in image for image in images))
        self.assertNotIn("__APEXFABRIC_REGISTRY__", rendered)

    def test_mutable_or_incomplete_locks_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_image_lock({"version": 1, "images": {}})
        with self.assertRaisesRegex(ValueError, "pinned"):
            validate_image_lock(
                {
                    "version": 1,
                    "registry": "registry.local:5000",
                    "images": {
                        "node-reporter": "registry/reporter:latest",
                        "node-status-controller": "registry/controller:latest",
                    },
                }
            )

    def test_lock_cannot_redirect_a_component_to_another_repository(self):
        with self.assertRaisesRegex(ValueError, "invalid digest reference"):
            validate_image_lock(
                {
                    "version": 1,
                    "registry": "registry.local:5000",
                    "images": {
                        "node-reporter": f"evil.invalid/reporter@{DIGEST}",
                        "node-status-controller": (
                            "registry.local:5000/apexfabric/"
                            f"node-status-controller@{DIGEST}"
                        ),
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
