import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PipelineImageSyncTests(unittest.TestCase):
    def text(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_service_orders_after_registry_and_only_imports(self):
        service = self.text("deploy/systemd/tvt-pipeline-image-sync.service")
        self.assertIn("Type=oneshot", service)
        self.assertIn("Requires=docker.service tvt-local-registry.service", service)
        self.assertIn("After=network-online.target docker.service tvt-local-registry.service", service)
        self.assertIn("EnvironmentFile=-/etc/tvt/pipeline-image-sync.env", service)
        self.assertIn("--mode archive", service)
        self.assertIn("/var/lib/tvt/pipeline/traffic-image.lock.json", service)
        self.assertNotIn("kubectl", service)
        self.assertNotIn("k3s", service.lower())

    def test_timer_is_persistent_and_bounded_to_the_oneshot(self):
        timer = self.text("deploy/systemd/tvt-pipeline-image-sync.timer")
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=tvt-pipeline-image-sync.service", timer)
        self.assertIn("OnUnitInactiveSec=24h", timer)

    def test_importer_uses_nonblocking_concurrency_lock(self):
        importer = self.text("scripts/import-pipeline-traffic-image.sh")
        self.assertIn('exec 9>"${CONCURRENCY_LOCK}"', importer)
        self.assertIn("flock --wait 0 9", importer)
        self.assertIn("exit 75", importer)

    def test_installer_uses_operational_state_and_private_optional_credentials(self):
        installer = self.text("scripts/install-pipeline-image-sync.sh")
        service = self.text("deploy/systemd/tvt-pipeline-image-sync.service")
        self.assertIn("/var/lib/tvt/pipeline", installer)
        self.assertIn("-m 0600", installer)
        self.assertIn("pipeline-image-sync.env", installer)
        self.assertIn("systemctl enable --now tvt-pipeline-image-sync.timer", installer)
        self.assertIn("TVT_PIPELINE_STATE_DIR=/var/lib/tvt/pipeline", service)
        example = self.text("deploy/host/tvt-pipeline-image-sync.env.example")
        self.assertNotIn("ghp_", example)
        self.assertNotIn("github_pat_", example)

    def test_verifier_recomputes_manifest_digest_without_deploying(self):
        verifier = self.text("scripts/verify-pipeline-image-sync.sh")
        self.assertIn("Docker-Content-Digest", verifier.lower().replace("docker-content-digest", "Docker-Content-Digest"))
        self.assertIn("sha256sum", verifier)
        self.assertIn("known-good image lock", verifier)
        self.assertNotIn("kubectl", verifier)

    def test_all_phase3_shell_scripts_have_valid_syntax(self):
        for path in (
            "scripts/import-pipeline-traffic-image.sh",
            "scripts/install-pipeline-image-sync.sh",
            "scripts/verify-pipeline-image-sync.sh",
            "scripts/bootstrap-postgresql.sh",
        ):
            result = subprocess.run(
                ["bash", "-n", str(ROOT / path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
