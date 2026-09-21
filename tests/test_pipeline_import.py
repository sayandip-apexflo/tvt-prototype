import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/tvt-edge-operations.sh"
MANIFEST = b'{"schemaVersion":2}'
DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()


class PipelineImportTests(unittest.TestCase):
    def text(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def operation(self, source):
        operations = self.text("scripts/tvt-edge-operations.sh")
        return operations.split(f"# Source: scripts/{source}\n", 1)[1].split(
            "\n)\n\ntvt_op_", 1
        )[0]

    def pipeline_values(self):
        values = {}
        for line in self.text("config/pipeline.env").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value.strip("'")
        return values

    def fake_environment(self, directory, *, docker_succeeds=True):
        fake_bin = directory / "bin"
        fake_bin.mkdir()
        docker = fake_bin / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n" + ("exit 0\n" if docker_succeeds else "exit 1\n"),
            encoding="utf-8",
        )
        git = fake_bin / "git"
        git.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"${1:-} ${2:-}\" == 'lfs version' ]]; then exit 0; fi\n"
            "exit 99\n",
            encoding="utf-8",
        )
        curl = fake_bin / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "headers=''\n"
            "output=''\n"
            "while (($#)); do\n"
            "  case \"$1\" in\n"
            "    --dump-header) headers=\"$2\"; shift 2 ;;\n"
            "    --output) output=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "if [[ -n \"${headers}\" ]]; then\n"
            f"  printf 'Docker-Content-Digest: {DIGEST}\\r\\n' >\"${{headers}}\"\n"
            f"  printf '%s' '{MANIFEST.decode()}' >\"${{output}}\"\n"
            "fi\n",
            encoding="utf-8",
        )
        for executable in (docker, git, curl):
            executable.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        return environment

    def matching_lock(self, values):
        return {
            "format_version": 2,
            "catalog_id": values["PIPELINE_TRAFFIC_CATALOG_ID"],
            "pipeline": {
                "repository": values["PIPELINE_REPOSITORY"],
                "commit": values["PIPELINE_REVISION"],
                "delivery_directory": values["PIPELINE_TRAFFIC_DELIVERY_DIR"],
            },
            "archive": {
                "filename": values["PIPELINE_TRAFFIC_ARCHIVE"],
                "sha256": values["PIPELINE_TRAFFIC_ARCHIVE_SHA256"],
                "size": int(values["PIPELINE_TRAFFIC_ARCHIVE_SIZE"]),
            },
            "source": {"mode": "bundled"},
            "image": {
                "registry": "127.0.0.1:5000",
                "repository": values["PIPELINE_TRAFFIC_LOCAL_REPOSITORY"],
                "tag": values["PIPELINE_TRAFFIC_LOCAL_TAG"],
                "digest": DIGEST,
                "reference": (
                    "127.0.0.1:5000/"
                    f"{values['PIPELINE_TRAFFIC_LOCAL_REPOSITORY']}@{DIGEST}"
                ),
                "architecture": "amd64",
            },
            "metadata": {
                "image_contract_sha256": values[
                    "PIPELINE_TRAFFIC_CONTRACT_SHA256"
                ],
                "desired_state_schema_sha256": values[
                    "PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256"
                ],
                "metrics_schema_sha256": values[
                    "PIPELINE_TRAFFIC_METRICS_SCHEMA_SHA256"
                ],
                "analytics_event_schema_sha256": values[
                    "PIPELINE_TRAFFIC_ANALYTICS_EVENT_SCHEMA_SHA256"
                ],
                "analytics_event_example_sha256": values[
                    "PIPELINE_TRAFFIC_ANALYTICS_EVENT_EXAMPLE_SHA256"
                ],
            },
            "compatibility": {
                "plan_compiler": values[
                    "PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID"
                ],
                "sha256": values["PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256"],
            },
            "verification_timestamp": "2026-09-03T00:00:00+00:00",
        }

    def test_exact_tvt_mills_delivery_pins(self):
        values = self.pipeline_values()
        self.assertEqual(
            values["PIPELINE_REVISION"],
            "ab85058ab961bb7aeb4ed9c96f1a35ec5c37f934",
        )
        self.assertEqual(values["PIPELINE_TRAFFIC_DELIVERY_DIR"], ".")
        self.assertEqual(
            values["PIPELINE_TRAFFIC_CATALOG_ID"],
            "tvt-mills-pilot:2026.09.18-v1",
        )
        self.assertEqual(
            values["PIPELINE_TRAFFIC_ARCHIVE"],
            "tvt-edge-runtime-intel-285h-2026.09.18-v1.oci.tar",
        )
        self.assertEqual(
            values["PIPELINE_TRAFFIC_ARCHIVE_SHA256"],
            "a236a4c1c3053e6a78270d98426b72c5fcbe681273b6f5f35d5631d25a88f4a8",
        )
        self.assertEqual(values["PIPELINE_TRAFFIC_ARCHIVE_SIZE"], "967496192")
        self.assertEqual(
            values["PIPELINE_TRAFFIC_ARCHIVE_IMAGE"],
            "localhost/tvt-edge-runtime:intel-285h-2026.09.18-v1",
        )
        self.assertEqual(
            values["PIPELINE_TRAFFIC_LOCAL_TAG"],
            "intel-285h-2026.09.18-v1",
        )
        self.assertEqual(
            values["PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID"],
            "tvt-direct-desired-state-v1",
        )
        self.assertNotIn("latest", self.text("config/pipeline.env").lower())

    def test_import_requires_the_bundled_archive_and_metadata(self):
        script = self.operation("import-pipeline-traffic-image.sh")
        self.assertIn("--archive-file and --metadata-directory are required", script)
        self.assertIn("source-build mode is not supported", script)
        self.assertIn('SOURCE_MODE=bundled', script)
        self.assertNotIn("PIPELINE_DELIVERY_BRANCH", script)

    def test_oci_loader_initializes_archive_before_derived_local(self):
        script = self.operation("import-pipeline-traffic-image.sh")
        self.assertIn(
            'local archive="$1"\n  local load_archive="${archive}"',
            script,
        )
        self.assertNotIn(
            'local archive="$1" load_archive="${archive}"',
            script,
        )

    def test_import_rejects_invalid_archive_and_image_contract(self):
        script = self.operation("import-pipeline-traffic-image.sh")
        combined = script + self.operation("verify-pipeline-image-inspect.py")
        for required in (
            "PIPELINE_TRAFFIC_ARCHIVE_SIZE",
            "sha256sum --check --status",
            'image.get("Architecture") != "amd64"',
            "org.opencontainers.image.version",
            "io.apexfabric.contract.version",
            "io.apexfabric.hardware.profile",
            "io.apexfabric.models.delivery",
            'config.get("User")',
            'config.get("ExposedPorts")',
            "edge-main.py",
            "edge-agent.py",
            "vehicle.xml",
            "license_plate.bin",
            "ocr.xml",
            "adaface_ir101_int8.xml",
            "det_500m.onnx",
        ):
            self.assertIn(required, combined)
        self.assertNotIn("docker run", script)
        self.assertIn('docker create "${source_image}"', script)
        self.assertIn("docker start --attach", script)
        self.assertIn("TVT Mills compatibility image build", script)

    def test_source_build_is_rejected_for_the_vendor_release(self):
        script = self.operation("import-pipeline-traffic-image.sh")
        self.assertIn('[[ "${MODE}" == archive ]]', script)
        self.assertIn("source-build mode is not supported", script)
        service = self.text("deploy/systemd/tvt-pipeline-image-sync.service")
        self.assertIn("--mode archive", service)
        self.assertNotIn("--mode build", service)

    def test_matching_lock_and_verified_registry_digest_are_idempotent(self):
        values = self.pipeline_values()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            lock_path = temporary_path / "traffic-image.lock.json"
            lock_path.write_text(json.dumps(self.matching_lock(values)), encoding="utf-8")
            lock_path.chmod(0o600)
            work_dir = temporary_path / "work"
            archive_path = temporary_path / "image.oci.tar"
            archive_path.write_bytes(b"not-read-on-idempotent-path")
            metadata_path = temporary_path / "metadata"
            metadata_path.mkdir()
            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "import-pipeline-traffic-image",
                    "--work-dir",
                    str(work_dir),
                    "--lock-output",
                    str(lock_path),
                    "--archive-file",
                    str(archive_path),
                    "--metadata-directory",
                    str(metadata_path),
                ],
                env=self.fake_environment(temporary_path),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already imported", result.stdout)
            self.assertFalse(work_dir.exists())

    def test_failure_preserves_previous_known_good_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            lock_path = temporary_path / "traffic-image.lock.json"
            original = b'{"last_known_good": true}\n'
            lock_path.write_bytes(original)
            archive_path = temporary_path / "image.oci.tar"
            archive_path.write_bytes(b"invalid")
            metadata_path = temporary_path / "metadata"
            metadata_path.mkdir()
            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "import-pipeline-traffic-image",
                    "--work-dir",
                    str(temporary_path / "work"),
                    "--lock-output",
                    str(lock_path),
                    "--archive-file",
                    str(archive_path),
                    "--metadata-directory",
                    str(metadata_path),
                ],
                env=self.fake_environment(temporary_path, docker_succeeds=False),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(lock_path.read_bytes(), original)

    def test_lock_is_atomic_private_and_contains_phase3_fields(self):
        script = self.operation("import-pipeline-traffic-image.sh")
        self.assertIn('chmod 0600 "${temporary_lock}"', script)
        self.assertIn('mv -f "${temporary_lock}" "${LOCK_OUTPUT}"', script)
        for field in (
            '"format_version": 2',
            '"catalog_id"',
            '"delivery_directory"',
            '"reference"',
            '"image_contract_sha256"',
            '"desired_state_schema_sha256"',
            '"metrics_schema_sha256"',
            '"analytics_event_schema_sha256"',
            '"analytics_event_example_sha256"',
            '"compatibility"',
            '"plan_compiler"',
            '"verification_timestamp"',
        ):
            self.assertIn(field, script)


if __name__ == "__main__":
    unittest.main()
