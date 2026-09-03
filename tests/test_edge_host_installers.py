from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EdgeHostInstallerTests(unittest.TestCase):
    def test_all_installer_shell_is_syntactically_valid(self) -> None:
        scripts = [
            ROOT / "prepare-tvt-edge-host.sh",
            ROOT / "install-tvt-edge-host.sh",
            ROOT / "scripts/build-tvt-edge-release.sh",
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
        lines = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
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


if __name__ == "__main__":
    unittest.main()
