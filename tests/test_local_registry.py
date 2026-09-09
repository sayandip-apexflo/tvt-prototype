import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LocalRegistryTests(unittest.TestCase):
    def text(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def platform_value(self, name):
        match = re.search(
            rf"^{re.escape(name)}=(\S+)$",
            self.text("config/platform.env"),
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(match, f"missing {name} in config/platform.env")
        return match.group(1)

    def test_phase_one_shell_scripts_have_valid_syntax(self):
        scripts = [ROOT / "scripts/tvt-edge-operations.sh"]
        subprocess.run(["bash", "-n", *map(str, scripts)], check=True)

    def test_registry_and_smoke_images_are_digest_pinned_for_amd64(self):
        image_pattern = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")
        self.assertRegex(self.platform_value("LOCAL_REGISTRY_IMAGE"), image_pattern)
        self.assertRegex(
            self.platform_value("LOCAL_REGISTRY_SMOKE_IMAGE"), image_pattern
        )
        self.assertIn(
            "--platform linux/amd64", self.text("scripts/tvt-edge-operations.sh")
        )

    def test_registry_is_loopback_only_and_persistent(self):
        service = self.text("deploy/systemd/tvt-local-registry.service.in")
        self.assertIn("--publish 127.0.0.1:5000:5000", service)
        self.assertNotIn("0.0.0.0:5000", service)
        self.assertIn(
            "--volume /var/lib/tvt/registry:/var/lib/registry", service
        )
        self.assertIn("IPAddressDeny=any", service)
        self.assertIn("IPAddressAllow=localhost", service)

    def test_systemd_owns_registry_lifecycle_and_orders_k3s(self):
        service = self.text("deploy/systemd/tvt-local-registry.service.in")
        drop_in = self.text("deploy/systemd/k3s-tvt-local-registry.conf")
        installer = self.text("scripts/tvt-edge-operations.sh")
        self.assertIn("Requires=docker.service", service)
        self.assertIn("After=docker.service", service)
        self.assertIn("Before=k3s.service", service)
        self.assertIn("Restart=on-failure", service)
        self.assertNotIn("--restart=", service)
        self.assertIn("Requires=tvt-local-registry.service", drop_in)
        self.assertIn("After=tvt-local-registry.service", drop_in)
        self.assertIn("20-tvt-local-registry.conf", installer)
        self.assertIn("systemctl enable tvt-local-registry.service", installer)

    def test_k3s_defaults_to_the_http_loopback_registry(self):
        environment = self.text("config/platform.env")
        configuration = self.text("scripts/tvt-edge-operations.sh")
        k3s_installer = configuration
        template = self.text("deploy/config/registries.yaml.in")
        self.assertIn("LOCAL_REGISTRY_ADDRESS=127.0.0.1:5000", environment)
        self.assertIn('REGISTRY="${LOCAL_REGISTRY_ADDRESS}"', configuration)
        self.assertIn('endpoint:', template)
        self.assertIn('"${LOCAL_REGISTRY_ADDRESS}" --scheme http', k3s_installer)
        self.assertIn("tvt-local-registry.service", k3s_installer)

    def test_registry_configuration_rejects_unsafe_addresses_before_writing(self):
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/tvt-edge-operations.sh"),
                "configure-k3s-registry",
                "--registry",
                "127.0.0.1:5000|unsafe",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("HOST[:PORT]", result.stderr)

    def test_operator_documentation_uses_scripts_instead_of_embedded_installers(self):
        commands = self.text("COMMANDS.md")
        readme = self.text("README.md")
        self.assertIn("tvt-edge-operations.sh install-local-registry", commands)
        self.assertIn("127.0.0.1:5000", readme)


if __name__ == "__main__":
    unittest.main()
