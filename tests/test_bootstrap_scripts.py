import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BootstrapScriptTests(unittest.TestCase):
    def text(self, name):
        return (ROOT / "scripts" / name).read_text(encoding="utf-8")

    def test_single_node_installer_uses_frozen_version_and_refuses_multi_node(self):
        installer = self.text("install-k3s-single-node.sh")
        self.assertIn("config/platform.env", installer)
        self.assertIn("INSTALL_K3S_VERSION", installer)
        self.assertIn("exactly one registered node", installer)
        self.assertIn("refusing to reconfigure an existing non-single-node cluster", installer)

    def test_plane_requires_digest_lock_and_verification(self):
        installer = self.text("install-k3s-plane.sh")
        verifier = self.text("verify-k3s-plane.sh")
        self.assertIn("--image-lock", installer)
        self.assertIn("tvt_runtime.image_lock render", installer)
        self.assertIn("@sha256:", verifier)
        self.assertIn("apexnodestatus", verifier)

    def test_registry_file_is_installed_private(self):
        configuration = self.text("configure-k3s-registry.sh")
        self.assertIn("-m 0600", configuration)
        self.assertIn("registries.yaml.tvt-backup", configuration)


if __name__ == "__main__":
    unittest.main()
