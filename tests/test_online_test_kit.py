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
        self.assertIn('"target_hardware": "intel-285h"', text)
        self.assertIn("override only for a separately audited equivalent", text)

    def test_builder_pins_and_verifies_transfer_artifacts(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("LOCAL_REGISTRY_IMAGE", text)
        self.assertIn("PIPELINE_REVISION", text)
        self.assertIn("PIPELINE_TRAFFIC_ARCHIVE_SHA256", text)
        self.assertIn("PIPELINE_TRAFFIC_ARCHIVE_SIZE", text)
        self.assertIn("sha256sum-amd64.txt", text)
        self.assertIn("--platform linux/amd64", text)
        self.assertIn("sha256sum --check checksums.sha256", text)

    def test_builder_normalizes_and_verifies_transport_permissions(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('find "${kit}" -type d -exec chmod 0755 {} +', text)
        self.assertIn('find "${kit}" -type f ! -perm /111 -exec chmod 0644 {} +', text)
        self.assertIn('find "${kit}" -type f -perm /111 -exec chmod 0755 {} +', text)
        self.assertIn('chmod 0644 "${kit}/checksums.sha256"', text)
        self.assertIn("transport permission verification failed", text)
        self.assertIn("symlink is not allowed in the transport kit", text)

    def test_generated_target_instructions_use_portable_extraction(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("sudo tar --extract --gzip --no-same-owner", text)
        self.assertIn("sudo install -d -o root -g root -m 0755 /opt/tvt", text)
        self.assertIn('sudo chown -R root:', text)
        self.assertIn('sudo chmod -R u=rwX,g=rX,o=', text)
        self.assertIn("source/docs/ONLINE-TEST-KIT-INSTALL.md", text)
        self.assertIn("Re-declare the documented path", text)
        self.assertIn("do not escape them", text)

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
