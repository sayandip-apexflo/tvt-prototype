from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_SCRIPT = ROOT / "scripts/tvt-hardware-inventory.py"
INPUTS_SCRIPT = ROOT / "scripts/tvt-release-inputs.py"


def inventory_document(**overrides):
    document = {
        "schema_version": 1,
        "collected_at": "2026-09-15T00:00:00+00:00",
        "os_id": "ubuntu",
        "os_version_id": "24.04",
        "architecture": "amd64",
        "cpu_model": "Intel(R) Core(TM) Ultra 9 285H",
        "kernel_version": "6.8.0-52-generic",
        "axelera_present": False,
        "axelera_pci_address": None,
        "gpu_render_node": True,
        "npu_node": True,
        "memory_mib": 32768,
        "disk_free_mib": 100000,
        "secure_boot": "SecureBoot disabled",
        "pci_devices": [
            {
                "address": "0000:00:02.0",
                "vendor_id": "0x8086",
                "device_id": "0x1234",
                "class": "0x030000",
                "driver": "xe",
            }
        ],
        "modules": {"i915": False, "xe": True, "intel_vpu": True, "metis": False},
    }
    document.update(overrides)
    return document


def write_inventory(path: Path, document: dict) -> str:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode()).hexdigest()
    path.with_name(f"{path.name}.sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )
    return digest


class EdgeHardwareInventoryTests(unittest.TestCase):
    def test_probe_writes_canonical_inventory_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            pci = work / "pci" / "0000:01:00.0"
            pci.mkdir(parents=True)
            (pci / "vendor").write_text("0x1f9d\n", encoding="utf-8")
            (pci / "device").write_text("0x0001\n", encoding="utf-8")
            (pci / "class").write_text("0x120000\n", encoding="utf-8")
            (work / "cpuinfo").write_text(
                "processor\t: 0\nmodel name\t: Intel(R) Core(TM) Ultra 9 285H\n",
                encoding="utf-8",
            )
            (work / "os-release").write_text(
                'ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8"
            )
            (work / "modules").write_text("xe 1 0 - Live 0x0\n", encoding="utf-8")
            output = work / "inventory.json"
            environment = {
                **__import__("os").environ,
                "TVT_OS_RELEASE_FILE": str(work / "os-release"),
                "TVT_CPUINFO_FILE": str(work / "cpuinfo"),
                "TVT_PCI_SYSFS_ROOT": str(work / "pci"),
                "TVT_KERNEL_RELEASE": "6.8.0-52-generic",
                "TVT_PROC_MODULES_FILE": str(work / "modules"),
                "TVT_DEV_DRI_PATH": str(work / "missing-dri"),
                "TVT_DEV_ACCEL_PATH": str(work / "missing-accel"),
            }
            result = subprocess.run(
                ["python3", str(INVENTORY_SCRIPT), "probe", "--output", str(output)],
                capture_output=True, text=True, env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], 1)
            self.assertTrue(document["axelera_present"])
            self.assertEqual(document["axelera_pci_address"], "0000:01:00.0")
            self.assertEqual(document["kernel_version"], "6.8.0-52-generic")
            sidecar = output.with_name(f"{output.name}.sha256")
            self.assertTrue(sidecar.is_file())
            expected = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertEqual(sidecar.read_text(encoding="utf-8"), f"{expected}  {output.name}\n")
            verify = subprocess.run(
                ["python3", str(INVENTORY_SCRIPT), "verify", "--inventory", str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_probe_refuses_symlinked_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            target = work / "real.json"
            link = work / "link.json"
            link.symlink_to(target)
            result = subprocess.run(
                ["python3", str(INVENTORY_SCRIPT), "probe", "--output", str(link)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_inventory_rejects_wrong_os_and_secret_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            bad_os = work / "bad-os.json"
            write_inventory(bad_os, inventory_document(os_id="debian"))
            rejected = subprocess.run(
                ["python3", str(INVENTORY_SCRIPT), "verify", "--inventory", str(bad_os)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            sneaky = work / "sneaky.json"
            sneaky.write_text(
                json.dumps(inventory_document(token="secret")) + "\n", encoding="utf-8"
            )
            rejected_secret = subprocess.run(
                ["python3", str(INVENTORY_SCRIPT), "verify", "--inventory", str(sneaky)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(rejected_secret.returncode, 0)

    def test_probe_operation_is_registered_and_read_only(self) -> None:
        operations = (ROOT / "scripts/tvt-edge-operations.sh").read_text(encoding="utf-8")
        self.assertIn("probe-edge-hardware) tvt_op_probe_edge_hardware", operations)
        probe = operations.split("tvt_op_probe_edge_hardware()")[1].split(
            "tvt_operations_usage()"
        )[0]
        self.assertIn("--output", probe)
        self.assertIn(".sha256", probe)
        self.assertIn("--ssh", probe)
        self.assertIn("ssh -tt", probe)
        self.assertIn("sudo python3 -c", probe)
        self.assertIn("probe --output -", probe)
        for forbidden in ("apt-get install", "modprobe", "systemctl"):
            self.assertNotIn(forbidden, probe)

    def test_kernel_selection_uses_minimum_comparison_not_a_fixed_kernel_family(self) -> None:
        operations = (ROOT / "scripts/tvt-edge-operations.sh").read_text(encoding="utf-8")
        self.assertIn('dpkg --compare-versions "${EDGE_KERNEL}" ge "${MINIMUM_KERNEL}"', operations)
        self.assertNotIn('[[ ${EDGE_KERNEL} == 6.8* ]]', operations)

    def test_build_release_inputs_requires_edge_inventory(self) -> None:
        result = subprocess.run(
            [str(ROOT / "scripts/tvt-edge-operations.sh"), "build-release-inputs",
             "--input-directory", "/tmp/does-not-matter"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--edge-inventory", result.stderr)

    def test_manifest_template_carries_per_profile_fields(self) -> None:
        manifest = json.loads(
            (ROOT / "release/manifest.template.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["hardware_profile"], "intel-285h")
        self.assertIn(manifest["axelera_variant"], {"intel-only", "metis"})
        self.assertTrue(manifest["kernel_target"])
        self.assertRegex(manifest["edge_inventory_sha256"], r"^[0-9a-f]{64}$")

    def test_hardware_matrix_pins_compatibility_line(self) -> None:
        matrix = (ROOT / "config/hardware-matrix.env").read_text(encoding="utf-8")
        for pin in (
            "HARDWARE_PROFILE=intel-285h",
            "METIS_DKMS_VERSION=1.4.17",
            "VOYAGER_RUNTIME_VERSION=1.6.1",
        ):
            self.assertIn(pin, matrix)

    def test_prepare_fails_fast_on_profile_mismatch(self) -> None:
        prepare = (ROOT / "prepare-tvt-edge-host.sh").read_text(encoding="utf-8")
        self.assertIn("check_bundle_profile", prepare)
        self.assertIn("axelera_variant", prepare)
        self.assertIn("kernel_target", prepare)
        self.assertIn("rebuild with this edge's probe inventory", prepare)


class EdgeInventoryLockTests(unittest.TestCase):
    def make_inputs(self, root: Path, *, axelera: bool, kernel: str):
        traffic = b"traffic-image"
        npu = b"npu-archive"
        wheel = b"openvino-wheel"
        voyager_wheel = b"voyager-wheel"
        files = {
            "images/registry.tar": b"registry",
            "images/node-reporter.tar": b"reporter",
            "images/node-status-controller.tar": b"controller",
            "images/tvt-edge-runtime-intel-285h-2026.09.18-v1.oci.tar": traffic,
            "images/ui.tar": b"ui",
            "k3s/install.sh": b"#!/bin/sh\n",
            "k3s/k3s": b"#!/bin/sh\n",
            "hardware/linux-npu-driver.tar.gz": npu,
            "hardware/wheels/openvino.whl": wheel,
            "apt/runtime.deb": b"deb",
        }
        if axelera:
            files["hardware/voyager-wheels/axelera_rt.whl"] = voyager_wheel
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (root / "k3s/install.sh").chmod(0o755)
        (root / "k3s/k3s").chmod(0o755)
        inventory_path = root.parent / "edge-inventory.json"
        digest = write_inventory(
            inventory_path,
            inventory_document(
                kernel_version=kernel,
                axelera_present=axelera,
                axelera_pci_address="0000:01:00.0" if axelera else None,
            ),
        )
        bundled_inventory = root / "hardware/edge-inventory.json"
        bundled_inventory.write_bytes(inventory_path.read_bytes())
        recipe = {
            "schema_version": 3,
            "policy": "edge-inventory-driven",
            "hardware_profile": "intel-285h",
            "os_id": "ubuntu",
            "os_version_id": "24.04",
            "architecture": "amd64",
            "kernel_target": kernel,
            "inventory_sha256": digest,
            "npu": {"sha256": hashlib.sha256(npu).hexdigest()},
            "apt": {"metis-dkms": "1.4.17"} if axelera else {},
            "wheels": {"openvino.whl": hashlib.sha256(wheel).hexdigest()},
            "voyager": {
                "enabled": axelera,
                "runtime_version": "1.6.1" if axelera else None,
                "driver_package": "metis-dkms" if axelera else None,
                "driver_version": "1.4.17" if axelera else None,
                "firmware_recommended": "1.6.0" if axelera else None,
                "board_controller_recommended": "7.4" if axelera else None,
                "wheels": (
                    {"axelera_rt.whl": hashlib.sha256(voyager_wheel).hexdigest()}
                    if axelera else {}
                ),
            },
        }
        (root / "hardware/driver-recipe.json").write_text(
            json.dumps(recipe), encoding="utf-8"
        )
        return traffic, inventory_path

    def make_configs(self, parent: Path, traffic: bytes):
        platform = parent / "platform.env"
        pipeline = parent / "pipeline.env"
        platform.write_text(
            "K3S_VERSION=v1.2.3+k3s1\n"
            "NODE_MANAGEMENT_IMAGE_VERSION=0.1.0\n"
            "UI_IMAGE_VERSION=alerts-v2\n"
            "LOCAL_REGISTRY_IMAGE=registry:1@sha256:" + "1" * 64 + "\n",
            encoding="utf-8",
        )
        pipeline.write_text(
            "PIPELINE_REVISION=" + "2" * 40 + "\n"
            "PIPELINE_TRAFFIC_VERSION=v4\n"
            "PIPELINE_TRAFFIC_CATALOG_ID=traffic:v4\n"
            "PIPELINE_TRAFFIC_ARCHIVE_URL=https://example.invalid/traffic.tar\n"
            f"PIPELINE_TRAFFIC_ARCHIVE_SHA256={hashlib.sha256(traffic).hexdigest()}\n"
            f"PIPELINE_TRAFFIC_ARCHIVE_SIZE={len(traffic)}\n"
            "PIPELINE_TRAFFIC_ARCHIVE_IMAGE=traffic:v4\n",
            encoding="utf-8",
        )
        return platform, pipeline

    def lock_commands(self, root: Path, lock: Path, platform: Path, pipeline: Path,
                      inventory: Path):
        base = ["python3", str(INPUTS_SCRIPT), "--input-directory", str(root)]
        matrix = str(ROOT / "config/hardware-matrix.env")
        create = base[:2] + ["create", *base[2:], "--output", str(lock),
            "--release-version", "0.1.0", "--source-commit", "a" * 40,
            "--platform-config", str(platform), "--pipeline-config", str(pipeline),
            "--hardware-matrix", matrix, "--edge-inventory", str(inventory)]
        verify = base[:2] + ["verify", *base[2:], "--lock", str(lock),
            "--release-version", "0.1.0", "--source-commit", "a" * 40,
            "--platform-config", str(platform), "--pipeline-config", str(pipeline),
            "--hardware-matrix", matrix, "--edge-inventory", str(inventory)]
        return create, verify

    def test_lock_binds_inventory_and_rejects_axelera_flip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / "inputs"
            root.mkdir()
            traffic, inventory = self.make_inputs(root, axelera=True, kernel="6.8.0-1")
            platform, pipeline = self.make_configs(temporary, traffic)
            lock = root / "release-inputs.lock.json"
            create, verify = self.lock_commands(root, lock, platform, pipeline, inventory)
            subprocess.run(create, check=True)
            subprocess.run(verify, check=True)
            document = json.loads(lock.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], 2)
            self.assertEqual(document["edge_inventory"]["kernel_target"], "6.8.0-1")
            self.assertTrue(document["edge_inventory"]["axelera_present"])
            # A flipped inventory (Intel-only edge) must not verify against a
            # Metis lock.
            flipped = temporary / "flipped.json"
            write_inventory(flipped, inventory_document(
                kernel_version="6.8.0-1", axelera_present=False,
                axelera_pci_address=None))
            flipped_verify = [value if value != str(inventory) else str(flipped)
                              for value in verify]
            rejected = subprocess.run(flipped_verify, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Axelera", rejected.stderr)

    def test_lock_rejects_kernel_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / "inputs"
            root.mkdir()
            traffic, inventory = self.make_inputs(root, axelera=False, kernel="6.8.0-1")
            platform, pipeline = self.make_configs(temporary, traffic)
            lock = root / "release-inputs.lock.json"
            create, verify = self.lock_commands(root, lock, platform, pipeline, inventory)
            subprocess.run(create, check=True)
            other = temporary / "other-kernel.json"
            write_inventory(other, inventory_document(kernel_version="6.8.0-2"))
            other_verify = [value if value != str(inventory) else str(other)
                            for value in verify]
            rejected = subprocess.run(other_verify, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("kernel", rejected.stderr.lower())


if __name__ == "__main__":
    unittest.main()
