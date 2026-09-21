from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts/validate-solution-delivery.py"
CATALOG = ROOT / "solution-packs/catalog/tvt-mills-pilot-2026.09.18-v1"
CONFIG = ROOT / "config/pipeline.env"
EXPECTED_TAG = "localhost/tvt-edge-runtime:intel-285h-2026.09.18-v1"


def write_oci_archive(path: Path, tag: str) -> None:
    documents = {
        "oci-layout": {"imageLayoutVersion": "1.0.0"},
        "index.json": {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + "0" * 64,
                    "size": 2,
                    "annotations": {"org.opencontainers.image.ref.name": tag},
                }
            ],
        },
    }
    with tarfile.open(path, "w") as archive:
        for name, document in documents.items():
            payload = json.dumps(document).encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_oci_archive_tag_verifier_accepts_the_pinned_tag() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "image.oci.tar"
        write_oci_archive(archive, EXPECTED_TAG)
        result = subprocess.run(
            [
                str(ROOT / "scripts/tvt-edge-operations.sh"),
                "verify-docker-archive-tag",
                "--archive",
                str(archive),
                "--expected",
                EXPECTED_TAG,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_delivery_validator_rejects_an_archive_not_bound_by_provenance() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "tvt-edge-runtime-intel-285h-2026.09.18-v1.oci.tar"
        write_oci_archive(archive, EXPECTED_TAG)
        result = subprocess.run(
            [
                str(ROOT / ".venv/bin/python"),
                str(VALIDATOR),
                "--catalog",
                str(CATALOG),
                "--config",
                str(CONFIG),
                "--archive",
                str(archive),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "archive size mismatch" in result.stderr
