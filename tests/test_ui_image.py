import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tvt_runtime.ui_image import create_ui_lock, render_ui_manifest, validate_ui_lock


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


class UiImageTests(unittest.TestCase):
    def test_create_lock_resolves_the_ui_image(self):
        with patch(
            "tvt_runtime.ui_image.resolve_registry_digest", return_value=DIGEST
        ) as resolver:
            lock = create_ui_lock("http://registry.local:5000", "alerts-v2")
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(
            lock["images"]["ui"],
            f"registry.local:5000/apexfabric/ui@{DIGEST}",
        )

    def test_manifest_is_rendered_with_digest_pinned_image(self):
        lock = {
            "version": 1,
            "registry": "registry.local:5000",
            "images": {"ui": f"registry.local:5000/apexfabric/ui@{DIGEST}"},
        }
        rendered = render_ui_manifest(ROOT / "deploy/single-box/ui.yaml", lock)
        resources = list(yaml.safe_load_all(rendered))
        deployment = next(
            resource
            for resource in resources
            if resource.get("kind") == "Deployment"
            and resource.get("metadata", {}).get("name") == "apexfabric-ui"
        )
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, f"registry.local:5000/apexfabric/ui@{DIGEST}")
        self.assertNotIn("__APEXFABRIC_REGISTRY__", rendered)

    def test_mutable_or_incomplete_locks_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_ui_lock({"version": 1, "images": {}})
        with self.assertRaisesRegex(ValueError, "pinned"):
            validate_ui_lock(
                {
                    "version": 1,
                    "registry": "registry.local:5000",
                    "images": {"ui": "registry/ui:latest"},
                }
            )

    def test_lock_cannot_redirect_the_ui_image_to_another_repository(self):
        with self.assertRaisesRegex(ValueError, "invalid digest reference"):
            validate_ui_lock(
                {
                    "version": 1,
                    "registry": "registry.local:5000",
                    "images": {"ui": f"evil.invalid/ui@{DIGEST}"},
                }
            )


if __name__ == "__main__":
    unittest.main()
