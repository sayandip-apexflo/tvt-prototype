from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/make-tvt-online-test-kit.sh"


class OnlineTestKitTests(unittest.TestCase):
    def test_builder_is_valid_shell_and_documents_online_boundary(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"kit_type": "online-test"', text)
        self.assertIn('"production_offline_release": False', text)
        self.assertIn('"target_requires_network": True', text)
        self.assertIn("--allow-unverified-hardware", text)
        self.assertIn("Phase-5 host.platform qualification will fail", text)

    def test_builder_pins_and_verifies_transfer_artifacts(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("LOCAL_REGISTRY_IMAGE", text)
        self.assertIn("PIPELINE_REVISION", text)
        self.assertIn("PIPELINE_TRAFFIC_ARCHIVE_SHA256", text)
        self.assertIn("PIPELINE_TRAFFIC_ARCHIVE_SIZE", text)
        self.assertIn("sha256sum-amd64.txt", text)
        self.assertIn("--platform linux/amd64", text)
        self.assertIn("sha256sum --check checksums.sha256", text)

    def test_builder_does_not_bundle_credentials_or_fake_offline_inputs(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("pipeline-credentials-file", text)
        self.assertIn('"contains_credentials": False', text)
        self.assertIn('"ubuntu-apt-closure"', text)
        self.assertIn('"intel-driver-recipe"', text)
        self.assertNotIn("release-inputs.lock.json", text)

    def test_help_is_read_only(self) -> None:
        result = subprocess.run(
            [str(SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("online, experimental TVT", result.stderr)
        self.assertIn("--output-directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
