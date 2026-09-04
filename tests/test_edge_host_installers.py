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
    def test_all_installer_shell_is_syntactically_valid(self) -> None:
        scripts = [
            ROOT / "prepare-tvt-edge-host.sh",
            ROOT / "install-tvt-edge-host.sh",
            ROOT / "scripts/build-tvt-edge-release.sh",
            ROOT / "scripts/make-tvt-edge-release.sh",
            ROOT / "scripts/make-tvt-online-test-kit.sh",
            ROOT / "scripts/verify-tvt-edge-release.sh",
            ROOT / "scripts/lib/tvt-installer-common.sh",
        ]
        subprocess.run(["bash", "-n", *map(str, scripts)], check=True)

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
            "scripts/lib/tvt-installer-common.sh",
            "scripts/install-tvt-hardware-drivers.sh", "scripts/install-local-registry.sh",
            "scripts/install-k3s-single-node.sh", "scripts/publish-control-images.sh",
            "scripts/install-k3s-plane.sh", "scripts/verify-k3s-plane.sh",
            "scripts/import-pipeline-traffic-image.sh", "scripts/verify-pipeline-image-inspect.py",
            "scripts/verify-pipeline-image-sync.sh", "scripts/install-pipeline-image-sync.sh",
            "scripts/bootstrap-postgresql.sh", "scripts/install-tvt-kubeconfig.sh",
            "scripts/install-traffic-qualification.sh",
            "deploy/k8s/apexfabric-foundation.yaml", "deploy/k8s/apexfabric-node-management.yaml",
            "deploy/host/tvt-edge.env.example", "deploy/host/postgresql-tvt.conf",
            "deploy/systemd/tvt-edge.service", "deploy/systemd/tvt-camera-sync.service",
            "solution-packs/schema/deployment-bundle.schema.json",
            "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/provenance.json",
            "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4/image-contract.yaml",
            "tvt_edge/db/migrations/env.py", "packages/apt/runtime.deb",
            "hardware/driver-recipe.json", "hardware/linux-npu-driver.tar.gz",
            "hardware/wheels/openvino.whl",
        }
        for relative in required:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
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
                [str(ROOT / "scripts/verify-tvt-edge-release.sh"), "--bundle", str(bundle)],
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
        for option in ("--site-config", "--k3s-mode", "--resume", "--verify-only"):
            self.assertIn(option, install)
        self.assertIn("installation-report.json", install)

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
            files = {
                "images/registry.tar": b"registry",
                "images/node-reporter.tar": b"reporter",
                "images/node-status-controller.tar": b"controller",
                "images/traffic-edge-runtime-v4.tar": traffic,
                "k3s/install.sh": b"#!/bin/sh\n",
                "k3s/k3s": b"#!/bin/sh\n",
                "hardware/linux-npu-driver.tar.gz": npu,
                "hardware/wheels/openvino.whl": wheel,
                "apt/runtime.deb": b"deb",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (root / "k3s/install.sh").chmod(0o755)
            (root / "k3s/k3s").chmod(0o755)
            recipe = {
                "schema_version": 1,
                "hardware_profile": "intel-285h",
                "os_id": "ubuntu",
                "os_version_id": "24.04",
                "architecture": "amd64",
                "kernel_version": "6.8.0-test",
                "npu": {"sha256": hashlib.sha256(npu).hexdigest()},
                "wheels": {"openvino.whl": hashlib.sha256(wheel).hexdigest()},
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
            subprocess.run(
                base[:2] + ["create", *base[2:], "--output", str(lock),
                 "--release-version", "0.1.0", "--source-commit", "a" * 40,
                 "--platform-config", str(platform), "--pipeline-config", str(pipeline)],
                check=True,
            )
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


if __name__ == "__main__":
    unittest.main()
