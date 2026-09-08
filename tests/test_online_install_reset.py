from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install-tvt-online-steps-1-10.sh"
RESET = ROOT / "scripts/reset-tvt-online-test-host.sh"


class OnlineInstallResetTests(unittest.TestCase):
    def test_scripts_have_valid_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(INSTALLER), str(RESET)], check=True)

    def test_installer_is_resumable_and_uses_only_bundled_k3s(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("/var/lib/tvt-online-test-install", text)
        self.assertIn("driver-install-boot-id", text)
        self.assertIn("REBOOT_EXIT=194", text)
        self.assertIn('--installer "${KIT_ROOT}/k3s/install.sh"', text)
        self.assertIn('--k3s-binary "${KIT_ROOT}/k3s/k3s"', text)
        self.assertNotIn("--download-installer", text)
        self.assertIn("capture_package_delta", text)
        self.assertIn("baseline-k3s-state", text)
        self.assertIn("--approve-k3s-agent-removal", text)

    def test_installer_covers_steps_without_automatic_workloads(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        for required in (
            "sha256sum --check",
            "checksums.sha256",
            "install-tvt-hardware-drivers.sh",
            "vainfo --display drm --device /dev/dri/renderD128",
            "metis-dkms 1.4.17",
            "voyager-1.6.1",
            "install-local-registry.sh",
            "install-k3s-single-node.sh",
            "install-k3s-plane.sh",
            "import-pipeline-traffic-image.sh",
            "bootstrap-postgresql.sh",
            "install-tvt-kubeconfig.sh",
            "init-site",
            "refresh-solutions",
        ):
            self.assertIn(required, text)
        self.assertIn(
            "Traffic workloads, cameras, monitoring, and email notifications were not deployed or configured",
            text,
        )

    def test_reset_defaults_to_plan_and_requires_typed_confirmation(self) -> None:
        result = subprocess.run([str(RESET)], capture_output=True, text=True, check=True)
        self.assertIn("plan only; no changes made", result.stdout)
        text = RESET.read_text(encoding="utf-8")
        self.assertIn("--execute", text)
        self.assertIn("ERASE-TVT-ONLINE-TEST", text)
        self.assertIn("paired installer baseline is missing", text)
        self.assertIn("refusing unsafe recursive removal target", text)

    def test_full_stack_reset_is_explicit_and_still_plan_only_by_default(self) -> None:
        result = subprocess.run(
            [str(RESET), "--force-full-stack"], capture_output=True, text=True, check=True
        )
        self.assertIn("FULL-STACK PURGE", result.stdout)
        self.assertIn("plan only; no changes made", result.stdout)
        self.assertIn("ERASE-ENTIRE-TVT-STACK", result.stdout)
        text = RESET.read_text(encoding="utf-8")
        self.assertIn("--force-full-stack", text)
        self.assertIn("K3s residue remains", text)
        self.assertIn("container runtime data remains", text)
        self.assertNotIn("apt-get autoremove", text)

    def test_reset_is_baseline_driven_and_does_not_restore_agent_secrets(self) -> None:
        text = RESET.read_text(encoding="utf-8")
        self.assertIn("new-packages.txt", text)
        self.assertIn("changed-packages.tsv", text)
        self.assertIn("axelera-apt-preexisting", text)
        self.assertIn("removed-packages.tsv", text)
        self.assertIn("new-docker-tags.txt", text)
        self.assertIn("APT would remove packages outside the captured install delta", text)
        self.assertIn("does not and cannot restore a pre-existing K3s agent", text)
        self.assertIn("/usr/local/bin/k3s-uninstall.sh", text)
        self.assertNotIn("k3s-agent-token", text)
        self.assertNotIn("chmod 777", text)


if __name__ == "__main__":
    unittest.main()
