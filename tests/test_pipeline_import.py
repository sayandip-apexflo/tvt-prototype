import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/import-pipeline-traffic-image.sh"
INSPECTOR = ROOT / "scripts/verify-pipeline-image-inspect.py"
MANIFEST = b'{"schemaVersion":2}'
DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()


class PipelineImportTests(unittest.TestCase):
    def text(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

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
            "source": {"mode": "archive"},
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
            },
            "verification_timestamp": "2026-09-03T00:00:00+00:00",
        }

    def test_exact_v4_delivery_pins(self):
        values = self.pipeline_values()
        self.assertEqual(
            values["PIPELINE_REVISION"],
            "6513562c9d27eba511322280e19e054c3948ae4d",
        )
        self.assertEqual(
            values["PIPELINE_TRAFFIC_DELIVERY_DIR"],
            "delivery/apexfabric-v1/intel-285h/traffic",
        )
        self.assertEqual(values["PIPELINE_TRAFFIC_ARCHIVE"], "image-2026.08.21-v4.tar")
        self.assertEqual(
            values["PIPELINE_TRAFFIC_ARCHIVE_SHA256"],
            "a6787bba6a27bc486f90b4c4dd41681d051c7c834568d99bc4a884d177d10e0f",
        )
        self.assertEqual(values["PIPELINE_TRAFFIC_ARCHIVE_SIZE"], "1930041856")
        self.assertEqual(
            values["PIPELINE_TRAFFIC_ARCHIVE_IMAGE"],
            "traffic-edge-runtime:intel-285h-2026.08.21-v4",
        )
        self.assertEqual(values["PIPELINE_TRAFFIC_LOCAL_TAG"], "intel-285h-2026.08.21-v4")
        self.assertNotIn("latest", self.text("config/pipeline.env").lower())

    def test_import_fetches_commit_and_archive_without_tracking_branch_head(self):
        script = self.text("scripts/import-pipeline-traffic-image.sh")
        self.assertIn('fetch --no-tags origin "${PIPELINE_REVISION}"', script)
        self.assertIn('lfs pull --include="${archive_relative}"', script)
        self.assertIn('origin "${PIPELINE_REVISION}"', script)
        self.assertNotIn("PIPELINE_DELIVERY_BRANCH", script)
        self.assertNotIn("release-tag fetch", script)

    def test_import_rejects_invalid_archive_and_image_contract(self):
        script = self.text("scripts/import-pipeline-traffic-image.sh")
        inspection = self.text("scripts/verify-pipeline-image-inspect.py")
        combined = script + inspection
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
            "solution_image_entrypoint.py",
            "edge_agent.py",
            "vehicle.xml",
            "license_plate.bin",
            "ocr.xml",
        ):
            self.assertIn(required, combined)
        self.assertNotIn("docker run", script)
        self.assertIn('docker create "${source_image}"', script)

    def test_image_inspection_rejects_architecture_and_label_mismatch(self):
        values = self.pipeline_values()
        document = [
            {
                "Architecture": "amd64",
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.source": values[
                            "PIPELINE_TRAFFIC_OCI_SOURCE"
                        ],
                        "org.opencontainers.image.title": values[
                            "PIPELINE_TRAFFIC_OCI_TITLE"
                        ],
                        "org.opencontainers.image.version": values[
                            "PIPELINE_TRAFFIC_VERSION"
                        ],
                        "io.apexfabric.contract.version": values[
                            "PIPELINE_TRAFFIC_CONTRACT_VERSION"
                        ],
                        "io.apexfabric.hardware.profile": values[
                            "PIPELINE_TRAFFIC_HARDWARE_PROFILE"
                        ],
                        "io.apexfabric.models.delivery": values[
                            "PIPELINE_TRAFFIC_MODELS_DELIVERY"
                        ],
                    },
                    "User": values["PIPELINE_TRAFFIC_CONTAINER_USER"],
                    "ExposedPorts": {values["PIPELINE_TRAFFIC_CONTAINER_PORT"]: {}},
                    "Cmd": values["PIPELINE_TRAFFIC_CONTAINER_COMMAND"].split(),
                },
            }
        ]
        arguments = [
            str(INSPECTOR),
            "PLACEHOLDER",
            "--source",
            values["PIPELINE_TRAFFIC_OCI_SOURCE"],
            "--title",
            values["PIPELINE_TRAFFIC_OCI_TITLE"],
            "--version",
            values["PIPELINE_TRAFFIC_VERSION"],
            "--contract-version",
            values["PIPELINE_TRAFFIC_CONTRACT_VERSION"],
            "--hardware-profile",
            values["PIPELINE_TRAFFIC_HARDWARE_PROFILE"],
            "--models-delivery",
            values["PIPELINE_TRAFFIC_MODELS_DELIVERY"],
            "--user",
            values["PIPELINE_TRAFFIC_CONTAINER_USER"],
            "--port",
            values["PIPELINE_TRAFFIC_CONTAINER_PORT"],
            "--command",
            values["PIPELINE_TRAFFIC_CONTAINER_COMMAND"],
        ]
        with tempfile.TemporaryDirectory() as temporary:
            inspection_path = Path(temporary) / "inspect.json"
            arguments[1] = str(inspection_path)
            inspection_path.write_text(json.dumps(document), encoding="utf-8")
            accepted = subprocess.run(arguments, capture_output=True, text=True)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            document[0]["Architecture"] = "arm64"
            inspection_path.write_text(json.dumps(document), encoding="utf-8")
            wrong_arch = subprocess.run(arguments, capture_output=True, text=True)
            self.assertNotEqual(wrong_arch.returncode, 0)
            self.assertIn("architecture", wrong_arch.stderr)
            document[0]["Architecture"] = "amd64"
            document[0]["Config"]["Labels"][
                "io.apexfabric.models.delivery"
            ] = "external"
            inspection_path.write_text(json.dumps(document), encoding="utf-8")
            wrong_label = subprocess.run(arguments, capture_output=True, text=True)
            self.assertNotEqual(wrong_label.returncode, 0)
            self.assertIn("io.apexfabric.models.delivery", wrong_label.stderr)

    def test_source_build_is_distinct_qualification_mode(self):
        script = self.text("scripts/import-pipeline-traffic-image.sh")
        self.assertIn("qualification mode", script)
        self.assertIn("PIPELINE_UBUNTU_BASE_IMAGE", script)
        self.assertIn("docker build", script)
        self.assertIn('selected_local_tag="${PIPELINE_TRAFFIC_LOCAL_TAG}-source-build"', script)
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
            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work-dir",
                    str(work_dir),
                    "--lock-output",
                    str(lock_path),
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
            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--work-dir",
                    str(temporary_path / "work"),
                    "--lock-output",
                    str(lock_path),
                ],
                env=self.fake_environment(temporary_path, docker_succeeds=False),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(lock_path.read_bytes(), original)

    def test_lock_is_atomic_private_and_contains_phase3_fields(self):
        script = self.text("scripts/import-pipeline-traffic-image.sh")
        self.assertIn('chmod 0600 "${temporary_lock}"', script)
        self.assertIn('mv -f "${temporary_lock}" "${LOCK_OUTPUT}"', script)
        for field in (
            '"format_version": 2',
            '"catalog_id"',
            '"delivery_directory"',
            '"reference"',
            '"image_contract_sha256"',
            '"desired_state_schema_sha256"',
            '"verification_timestamp"',
        ):
            self.assertIn(field, script)


if __name__ == "__main__":
    unittest.main()
