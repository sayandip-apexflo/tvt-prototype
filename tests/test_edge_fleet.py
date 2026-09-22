from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/tvt-edge-fleet.py"
SINGLE_EDGE = ROOT / "scripts/tvt-edge-single.sh"
SPEC = importlib.util.spec_from_file_location("tvt_edge_fleet", MODULE_PATH)
assert SPEC and SPEC.loader
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


def inventory(**overrides):
    document = {
        "os_id": "ubuntu",
        "os_version_id": "24.04",
        "architecture": "amd64",
        "cpu_model": "Intel(R) Core(TM) Ultra 9 285H",
        "kernel_version": "6.8.0-52-generic",
        "axelera_present": False,
        "pci_devices": [
            {"address": "0000:00:02.0", "vendor_id": "0x8086", "device_id": "0x1234", "class": "0x030000"},
        ],
    }
    document.update(overrides)
    return document


class EdgeFleetTests(unittest.TestCase):
    def test_compatibility_key_ignores_bus_address_and_diagnostics(self):
        first = inventory()
        second = inventory(
            collected_at="2026-09-16T00:00:00+00:00",
            memory_mib=65536,
            disk_free_mib=500000,
            pci_devices=[
                {"address": "0000:04:00.0", "vendor_id": "0x8086", "device_id": "0x1234", "class": "0x030000"},
            ],
        )
        self.assertEqual(fleet.compatibility_key(first), fleet.compatibility_key(second))

    def test_compatibility_key_splits_kernel_and_accelerator_variants(self):
        base = fleet.compatibility_key(inventory())
        self.assertNotEqual(base, fleet.compatibility_key(inventory(kernel_version="6.11.0-18-generic")))
        self.assertNotEqual(base, fleet.compatibility_key(inventory(secure_boot="SecureBoot enabled")))
        self.assertNotEqual(
            base,
            fleet.compatibility_key(
                inventory(
                    axelera_present=True,
                    pci_devices=[
                        {"address": "0000:01:00.0", "vendor_id": "0x1f9d", "device_id": "0x0001", "class": "0x120000"},
                    ],
                )
            ),
        )

    def test_fleet_manifest_normalizes_targets_and_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "site.yaml"
            site.write_text("site: demo\n", encoding="utf-8")
            manifest = root / "fleet.yaml"
            manifest.write_text(
                "edges:\n"
                "  - id: edge-01\n"
                f"    host: edge.example.invalid\n    user: deploy\n    site_config: {site}\n",
                encoding="utf-8",
            )
            edges = fleet.load_fleet(manifest)
            self.assertEqual(edges[0]["ssh_target"], "deploy@edge.example.invalid")
            self.assertEqual(edges[0]["site_config"], str(site.resolve()))

            duplicate = root / "duplicate.yaml"
            duplicate.write_text(
                "edges:\n"
                f"  - id: edge-01\n    host: one\n    site_config: {site}\n"
                f"  - id: edge-01\n    host: two\n    site_config: {site}\n",
                encoding="utf-8",
            )
            with self.assertRaises(fleet.FleetError):
                fleet.load_fleet(duplicate)

    def test_cli_exposes_fleet_operations_and_shell_is_valid(self):
        subprocess.run(["bash", "-n", str(ROOT / "scripts/tvt-edge-fleet.sh")], check=True)
        result = subprocess.run(
            [str(ROOT / "scripts/tvt-edge-fleet.sh"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("install", result.stdout)
        self.assertIn("resume", result.stdout)
        self.assertIn("status", result.stdout)

    def test_single_edge_front_door_is_valid_and_documents_commands(self):
        subprocess.run(["bash", "-n", str(SINGLE_EDGE)], check=True)
        result = subprocess.run(
            ["bash", str(SINGLE_EDGE), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("install", result.stderr)
        self.assertIn("resume", result.stderr)
        self.assertIn("status", result.stderr)
        script = SINGLE_EDGE.read_text(encoding="utf-8")
        self.assertIn('"${FLEET_CONTROLLER}"', script)
        self.assertIn("sudo -n true", script)
        self.assertNotIn("prepare-tvt-edge-host.sh", script)
        self.assertNotIn("install-tvt-edge-host.sh", script)

    def test_fleet_resume_requests_explicit_host_installer_resume(self):
        script = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('if self.resume:', script)
        self.assertIn('install_arguments.append("--resume")', script)

    def test_fleet_collects_installation_evidence_after_verification(self):
        script = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("/var/lib/tvt/install/installation-report.json", script)
        self.assertIn('self.edge_dir(edge_id) / "installation-report.json"', script)
        self.assertIn('installation_report=str(evidence_path)', script)

    def test_single_edge_status_reads_its_saved_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "fleet-report.json").write_text(
                json.dumps({"schema_version": 1, "counts": {"verified": 1}}) + "\n",
                encoding="utf-8",
            )
            (root / "run.json").write_text(
                json.dumps({"state_directory": str(state)}) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash", str(SINGLE_EDGE), "status",
                    "--edge-id", "edge-01",
                    "--operator-directory", str(root),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn('"verified": 1', result.stdout)

    def test_example_manifest_is_parseable(self):
        # The example intentionally uses invalid hostnames, so only validate
        # the document shape here rather than trying to load its site files.
        document = fleet.parse_document(ROOT / "config/edge-fleet.example.yaml")
        self.assertIsInstance(document.get("edges"), list)
        self.assertEqual(len(document["edges"]), 2)


if __name__ == "__main__":
    unittest.main()
