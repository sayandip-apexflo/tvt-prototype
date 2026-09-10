from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class EdgeHostInstallerTests(unittest.TestCase):
    def test_host_package_list_excludes_redundant_tools(self) -> None:
        prepare = (ROOT / "prepare-tvt-edge-host.sh").read_text(encoding="utf-8")
        package_install = prepare.split("install_host_packages()", 1)[1].split(
            "enable_host_services()", 1
        )[0]
        for package in (
            "coreutils",
            "git-lfs",
            "git",
            "jq",
            "tar",
            "util-linux",
        ):
            self.assertNotIn(package, package_install)

    def test_docker_socket_is_refreshed_across_package_installation(self) -> None:
        prepare = (ROOT / "prepare-tvt-edge-host.sh").read_text(encoding="utf-8")
        package_install = prepare.split("install_host_packages()", 1)[1].split(
            "enable_host_services()", 1
        )[0]
        service_setup = prepare.split("enable_host_services()", 1)[1].split(
            "install_hardware()", 1
        )[0]

        self.assertIn("systemctl stop docker.service docker.socket", package_install)
        self.assertIn("rm -f -- /run/docker.sock", package_install)
        self.assertIn("systemctl daemon-reload", service_setup)
        self.assertIn("systemctl reset-failed docker.service docker.socket", service_setup)
        self.assertLess(
            service_setup.index("systemctl start docker.socket"),
            service_setup.index("systemctl start docker.service"),
        )
        self.assertIn('tvt_fail "Docker is active but not healthy"', service_setup)

    def test_all_installer_shell_is_syntactically_valid(self) -> None:
        scripts = [
            ROOT / "prepare-tvt-edge-host.sh",
            ROOT / "install-tvt-edge-host.sh",
            ROOT / "scripts/check-tvt-edge-package-absence.sh",
            ROOT / "scripts/verify-tvt-edge-deployment.sh",
            ROOT / "scripts/tvt-edge-operations.sh",
            ROOT / "scripts/make-tvt-edge-release.sh",
            ROOT / "scripts/lib/tvt-installer-common.sh",
        ]
        subprocess.run(["bash", "-n", *map(str, scripts)], check=True)

    def test_post_install_audit_covers_packages_hardware_and_services(self) -> None:
        audit = (ROOT / "scripts/verify-tvt-edge-deployment.sh").read_text(
            encoding="utf-8"
        )
        for expected in (
            "PACKAGE AND DRIVER VERSIONS",
            "HARDWARE AND ACCELERATOR CHECKS",
            "SERVICES AND PLATFORM HEALTH",
            "KUBERNETES PODS",
            "dpkg-query",
            "OpenVINO devices",
            "tvt-local-registry.service",
            'container_image} == "${registry_digest}',
            "verify-k3s-plane",
            "verify-pipeline-image-sync",
            "OVERALL: PASS",
        ):
            self.assertIn(expected, audit)

    def test_edge_package_absence_audit_handles_dpkg_status_abbreviations(self) -> None:
        audit = (
            ROOT / "scripts/check-tvt-edge-package-absence.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('ii*)', audit)
        self.assertIn('un*)', audit)
        self.assertIn('edge_state=PRESENT-EXACT', audit)
        self.assertIn('edge_state=ABSENT', audit)
        self.assertIn('application_wheels=("${BUNDLE}"/wheels/*.whl)', audit)

    def test_application_resources_and_node_readiness_are_portable(self) -> None:
        installer = (ROOT / "install-tvt-edge-host.sh").read_text(encoding="utf-8")
        application = installer.split("install_application()", 1)[1].split(
            "install_registry()", 1
        )[0]
        verification = installer.split("final_verification()", 1)[1].split(
            "write_install_evidence()", 1
        )[0]

        self.assertIn("solution-packs images k3s tvt_edge", application)
        self.assertIn("k3s kubectl get nodes -o name", verification)
        self.assertIn("kubectl wait --for=condition=Ready", verification)
        self.assertNotIn(".status.conditions[?(", verification)

    def test_release_front_door_builds_missing_inputs(self) -> None:
        front_door = (ROOT / "scripts/make-tvt-edge-release.sh").read_text(
            encoding="utf-8"
        )
        operations = (ROOT / "scripts/tvt-edge-operations.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('scripts/tvt-edge-operations.sh" build-release-inputs', front_door)
        self.assertIn('--cache-directory "${CACHE_DIRECTORY}"', front_door)
        for required_stage in (
            "save_registry_image",
            "build_control_image node-reporter reporter",
            "build_control_image node-status-controller status_controller",
            "acquire_k3s",
            "acquire_traffic",
            "acquire_npu_archive",
            "resolve_python_wheels",
            "build_apt_closure",
            "write_hardware_recipe",
            "tvt-release-inputs.py create",
        ):
            self.assertIn(required_stage, operations)

    def test_npu_dependency_fields_are_queried_without_dpkg_labels(self) -> None:
        operations = (ROOT / "scripts/tvt-edge-operations.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            'dpkg-deb -f "${package_file}" Depends Pre-Depends', operations
        )
        self.assertIn("for field in Depends Pre-Depends; do", operations)
        self.assertIn(
            'dpkg-deb -f "${package_file}" "${field}"', operations
        )

    def test_operational_helpers_are_consolidated(self) -> None:
        operations = ROOT / "scripts/tvt-edge-operations.sh"
        result = subprocess.run(
            [str(operations), "--help"], capture_output=True, text=True, check=True
        )
        for operation in (
            "build-release-inputs",
            "build-release",
            "verify-release",
            "install-tvt-hardware-drivers",
            "install-local-registry",
            "install-k3s-single-node",
            "import-pipeline-traffic-image",
            "bootstrap-postgresql",
        ):
            self.assertIn(operation, result.stderr)
        retired = (
            "build-tvt-release-inputs.sh",
            "build-tvt-edge-release.sh",
            "verify-tvt-edge-release.sh",
            "install-tvt-hardware-drivers.sh",
            "install-local-registry.sh",
            "import-pipeline-traffic-image.sh",
        )
        for filename in retired:
            self.assertFalse((ROOT / "scripts" / filename).exists(), filename)

    def make_bundle(self, root: Path) -> None:
        manifest = json.loads(
            (ROOT / "release/manifest.template.json").read_text(encoding="utf-8")
        )
        for relative in manifest["artifacts"].values():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        required = {
            "prepare-tvt-edge-host.sh", "install-tvt-edge-host.sh", "alembic.ini",
            "config/platform.env", "config/pipeline.env",
            "scripts/lib/tvt-installer-common.sh", "scripts/tvt-edge-operations.sh",
            "deploy/k8s/apexfabric-foundation.yaml", "deploy/k8s/apexfabric-node-management.yaml",
            "deploy/host/tvt-edge.env.example", "deploy/host/postgresql-tvt.conf",
            "deploy/systemd/tvt-edge.service", "deploy/systemd/tvt-camera-sync.service",
            "solution-packs/schema/deployment-bundle.schema.json",
            "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/provenance.json",
            "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/image-contract.yaml",
            "tvt_edge/db/migrations/env.py", "packages/apt/runtime.deb",
            "hardware/driver-recipe.json", "hardware/linux-npu-driver.tar.gz",
            "hardware/wheels/openvino.whl",
            "hardware/voyager-wheels/axelera_rt.whl",
        }
        for relative in required:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        (root / "hardware/driver-recipe.json").write_text(
            json.dumps({"voyager": {"enabled": True}}), encoding="utf-8"
        )
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        self.write_checksums(root)

    def write_checksums(self, root: Path) -> None:
        lines = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            if relative == "checksums.sha256":
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {relative}\n")
        (root / "checksums.sha256").write_text("".join(lines), encoding="utf-8")

    def verify_bundle(self, bundle: Path) -> subprocess.CompletedProcess[str]:
        command = (
            f"source {ROOT / 'scripts/lib/tvt-installer-common.sh'}; "
            f"tvt_verify_bundle {bundle}"
        )
        return subprocess.run(["bash", "-c", command], capture_output=True, text=True)

    def test_bundle_verifier_accepts_complete_bundle_and_rejects_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            self.make_bundle(bundle)
            accepted = self.verify_bundle(bundle)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            (bundle / "images/traffic-edge-runtime-v4.tar").write_bytes(b"corrupt")
            rejected = self.verify_bundle(bundle)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("checksum mismatch", rejected.stderr)

    def test_bundle_verifier_rejects_unlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            self.make_bundle(bundle)
            (bundle / "unexpected").write_text("unsigned", encoding="utf-8")
            result = self.verify_bundle(bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum coverage mismatch", result.stderr)

    def test_release_verifier_checks_lock_wheel_and_ui_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            self.make_bundle(bundle)
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            wheel = bundle / manifest["artifacts"]["application_wheel"]
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "tvt_runtime-0.1.0.dist-info/METADATA",
                    "Metadata-Version: 2.2\nName: tvt-runtime\nVersion: 0.1.0\n",
                )
                archive.writestr(
                    "tvt_edge/static/assets/app.js", '"TVT Runtime","0.1.0"'
                )
            locked_paths = [
                "images/registry.tar", "images/node-reporter.tar",
                "images/node-status-controller.tar", "images/traffic-edge-runtime-v4.tar",
                "k3s/install.sh", "k3s/k3s", "hardware/driver-recipe.json",
                "hardware/linux-npu-driver.tar.gz", "hardware/wheels/openvino.whl",
                "hardware/voyager-wheels/axelera_rt.whl",
                "apt/runtime.deb",
            ]
            lock_files = {}
            for relative in locked_paths:
                bundled = bundle / (f"packages/{relative}" if relative.startswith("apt/") else relative)
                lock_files[relative] = {
                    "sha256": hashlib.sha256(bundled.read_bytes()).hexdigest(),
                    "size": bundled.stat().st_size,
                }
            lock = {
                "schema_version": 1,
                "release_version": manifest["release_version"],
                "source_commit": manifest["source_commit"],
                "configuration": {
                    "platform_sha256": hashlib.sha256(
                        (bundle / "config/platform.env").read_bytes()
                    ).hexdigest(),
                    "pipeline_sha256": hashlib.sha256(
                        (bundle / "config/pipeline.env").read_bytes()
                    ).hexdigest(),
                },
                "files": lock_files,
            }
            (bundle / manifest["artifacts"]["input_lock"]).write_text(
                json.dumps(lock), encoding="utf-8"
            )
            self.write_checksums(bundle)
            result = subprocess.run(
                [
                    str(ROOT / "scripts/tvt-edge-operations.sh"),
                    "verify-release", "--bundle", str(bundle),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_paths_use_prebuilt_archives(self) -> None:
        installer = (ROOT / "install-tvt-edge-host.sh").read_text(encoding="utf-8")
        self.assertIn("--image-archive", installer)
        self.assertIn("--archive-dir", installer)
        self.assertIn("--archive-file", installer)
        self.assertNotIn("docker build", installer)
        self.assertNotIn("git clone", installer)

    def test_integrated_installer_preserves_shared_etc_tvt_traversal(self) -> None:
        installer = (ROOT / "install-tvt-edge-host.sh").read_text(encoding="utf-8")
        self.assertIn("install -d -o root -g root -m 0755 /etc/tvt", installer)
        self.assertIn("root:root:755", installer)

    def test_runtime_resource_root_can_be_release_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "TVT_RESOURCE_ROOT": directory}
            result = subprocess.run(
                [
                    str(ROOT / ".venv/bin/python"),
                    "-c",
                    "from tvt_edge.paths import RESOURCE_ROOT; print(RESOURCE_ROOT)",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), str(Path(directory).resolve()))

    def test_failed_stage_is_recorded_and_does_not_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            script = f"""
set -Eeuo pipefail
source {ROOT / 'scripts/lib/tvt-installer-common.sh'}
worker() {{ false; touch {Path(directory) / 'continued'}; }}
tvt_run_stage {state} 0.1.0 sample worker
"""
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            document = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(document["stages"]["sample"]["status"], "failed")
            self.assertFalse((Path(directory) / "continued").exists())

    def test_operator_interfaces_and_reboot_checkpoint_are_present(self) -> None:
        prepare = (ROOT / "prepare-tvt-edge-host.sh").read_text(encoding="utf-8")
        install = (ROOT / "install-tvt-edge-host.sh").read_text(encoding="utf-8")
        for option in ("--bundle", "--mode", "--verify-only", "--log-file"):
            self.assertIn(option, prepare)
        self.assertIn("driver_install_boot_id", prepare)
        self.assertIn("reboot_required", prepare)
        for option in (
            "--site-config", "--k3s-mode", "--prepare-mode",
            "--allow-unverified-hardware", "--resume", "--verify-only",
        ):
            self.assertIn(option, install)
        self.assertIn("complete_post_reboot_preparation", install)
        self.assertIn('"${BUNDLE}/prepare-tvt-edge-host.sh"', install)
        self.assertIn("installation-report.json", install)

    def test_hardware_installer_pins_metis_and_voyager_compatibility_line(self) -> None:
        installer = (ROOT / "scripts/tvt-edge-operations.sh").read_text(
            encoding="utf-8"
        )
        for required in (
            'METIS_DKMS_VERSION="1.4.17"',
            'VOYAGER_RUNTIME_VERSION="1.6.1"',
            'METIS_FIRMWARE_RECOMMENDED="1.6.0"',
            'METIS_BOARD_CONTROLLER_RECOMMENDED="7.4"',
            'AXELERA_APT_KEY_FINGERPRINT="5AE357D1638F21311095816A82F63658F8BBFC11"',
            '"linux-headers-$(uname -r)"',
            'dkms status -m metis',
            'Pin-Priority: 1001',
            'voyager-wheels',
            'TVT_PCI_SYSFS_ROOT',
            '0x1f9d',
            'ocl-icd-libopencl1',
            '"enabled": axelera_enabled',
        ):
            self.assertIn(required, installer)
        self.assertIn('"schema_version": 2', installer)

    def test_application_version_is_canonical_and_consistent(self) -> None:
        result = subprocess.run(
            ["python3", str(ROOT / "scripts/tvt-version.py"), "--check", "--expected", "0.1.0"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "0.1.0")
        self.assertNotIn("0.2.0", (ROOT / "tvt_edge/__init__.py").read_text(encoding="utf-8"))

    def test_release_input_lock_detects_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / "inputs"
            root.mkdir()
            traffic = b"traffic-image"
            npu = b"npu-archive"
            wheel = b"openvino-wheel"
            voyager_wheel = b"voyager-wheel"
            files = {
                "images/registry.tar": b"registry",
                "images/node-reporter.tar": b"reporter",
                "images/node-status-controller.tar": b"controller",
                "images/traffic-edge-runtime-v4.tar": traffic,
                "k3s/install.sh": b"#!/bin/sh\n",
                "k3s/k3s": b"#!/bin/sh\n",
                "hardware/linux-npu-driver.tar.gz": npu,
                "hardware/wheels/openvino.whl": wheel,
                "hardware/voyager-wheels/axelera_rt.whl": voyager_wheel,
                "apt/runtime.deb": b"deb",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (root / "k3s/install.sh").chmod(0o755)
            (root / "k3s/k3s").chmod(0o755)
            recipe = {
                "schema_version": 2,
                "hardware_profile": "intel-285h",
                "os_id": "ubuntu",
                "os_version_id": "24.04",
                "architecture": "amd64",
                "kernel_version": "6.8.0-test",
                "npu": {"sha256": hashlib.sha256(npu).hexdigest()},
                "apt": {"metis-dkms": "1.4.17"},
                "wheels": {"openvino.whl": hashlib.sha256(wheel).hexdigest()},
                "voyager": {
                    "enabled": True,
                    "runtime_version": "1.6.1",
                    "driver_package": "metis-dkms",
                    "driver_version": "1.4.17",
                    "firmware_recommended": "1.6.0",
                    "board_controller_recommended": "7.4",
                    "wheels": {
                        "axelera_rt.whl": hashlib.sha256(voyager_wheel).hexdigest()
                    },
                },
            }
            (root / "hardware/driver-recipe.json").write_text(
                json.dumps(recipe), encoding="utf-8"
            )
            platform = temporary / "platform.env"
            pipeline = temporary / "pipeline.env"
            platform.write_text(
                "K3S_VERSION=v1.2.3+k3s1\n"
                "NODE_MANAGEMENT_IMAGE_VERSION=0.1.0\n"
                "LOCAL_REGISTRY_IMAGE=registry:1@sha256:" + "1" * 64 + "\n",
                encoding="utf-8",
            )
            pipeline.write_text(
                "PIPELINE_REVISION=" + "2" * 40 + "\n"
                "PIPELINE_TRAFFIC_VERSION=v4\n"
                f"PIPELINE_TRAFFIC_ARCHIVE_SHA256={hashlib.sha256(traffic).hexdigest()}\n"
                f"PIPELINE_TRAFFIC_ARCHIVE_SIZE={len(traffic)}\n"
                "PIPELINE_TRAFFIC_ARCHIVE_IMAGE=traffic:v4\n",
                encoding="utf-8",
            )
            lock = root / "release-inputs.lock.json"
            base = [
                "python3", str(ROOT / "scripts/tvt-release-inputs.py"),
                "--input-directory", str(root),
            ]
            create = base[:2] + ["create", *base[2:], "--output", str(lock),
                "--release-version", "0.1.0", "--source-commit", "a" * 40,
                "--platform-config", str(platform), "--pipeline-config", str(pipeline)]
            subprocess.run(create, check=True)
            verify = base[:2] + ["verify", *base[2:], "--lock", str(lock),
                "--release-version", "0.1.0", "--source-commit", "a" * 40,
                "--platform-config", str(platform), "--pipeline-config", str(pipeline)]
            subprocess.run(verify, check=True)
            (root / "notes.txt").write_text("not a release input", encoding="utf-8")
            unexpected = subprocess.run(verify, capture_output=True, text=True)
            self.assertNotEqual(unexpected.returncode, 0)
            self.assertIn("unsupported files", unexpected.stderr)
            (root / "notes.txt").unlink()
            (root / "images/registry.tar").write_bytes(b"changed")
            rejected = subprocess.run(verify, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("does not match its lock", rejected.stderr)

            # An Intel-only closure is valid without Metis or Voyager artifacts.
            (root / "images/registry.tar").write_bytes(b"registry")
            (root / "hardware/voyager-wheels/axelera_rt.whl").unlink()
            recipe["apt"] = {}
            recipe["voyager"] = {
                "enabled": False,
                "runtime_version": None,
                "driver_package": None,
                "driver_version": None,
                "firmware_recommended": None,
                "board_controller_recommended": None,
                "wheels": {},
            }
            (root / "hardware/driver-recipe.json").write_text(
                json.dumps(recipe), encoding="utf-8"
            )
            subprocess.run(create, check=True)
            subprocess.run(verify, check=True)


if __name__ == "__main__":
    unittest.main()
