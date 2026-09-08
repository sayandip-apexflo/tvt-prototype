from __future__ import annotations

import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-docker-archive-tag.py"
EXPECTED = "localhost/traffic-edge-runtime:intel-285h-2026.08.21-v4"


def write_archive(path: Path, tags: list[str]) -> None:
    manifest = json.dumps([{"Config": "config.json", "RepoTags": tags, "Layers": []}]).encode()
    with tarfile.open(path, mode="w") as archive:
        member = tarfile.TarInfo("manifest.json")
        member.size = len(manifest)
        archive.addfile(member, io.BytesIO(manifest))


class DockerArchiveTagTests(unittest.TestCase):
    def test_accepts_exactly_expected_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "image.tar"
            write_archive(archive, [EXPECTED])
            result = subprocess.run(
                [str(SCRIPT), "--archive", str(archive), "--expected", EXPECTED],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Verified Docker archive image tag", result.stdout)

    def test_rejects_a_different_or_additional_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "image.tar"
            for tags in (["traffic-edge-runtime:v4"], [EXPECTED, "traffic-edge-runtime:v4"]):
                write_archive(archive, tags)
                result = subprocess.run(
                    [str(SCRIPT), "--archive", str(archive), "--expected", EXPECTED],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("archive image tag mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
