import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import sessionmaker

from apexfabric.solution_management.catalog import (
    CatalogError,
    load_delivery_metadata,
    resolve_registry_digest,
)
from tvt_edge.api import create_app
from tvt_edge.db.models import (
    Base,
    Site,
    SolutionCatalogEntry,
    SolutionDeployment,
)
from tvt_edge.security import CredentialKeyring
from tvt_edge.service import ManagementService


ROOT = Path(__file__).resolve().parents[1]
DELIVERY = (
    ROOT / "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
)
MANIFEST = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()


class FakeManifestResponse:
    def __init__(self, digest=DIGEST):
        self.headers = {"Docker-Content-Digest": digest}

    def read(self, _limit):
        return MANIFEST

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SolutionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.keyring = CredentialKeyring.generate_for_test()

    def test_vendored_metadata_and_provenance_are_consistent(self):
        metadata = load_delivery_metadata(DELIVERY)
        self.assertEqual(
            metadata["catalog_id"], "traffic-edge-runtime:2026.08.21-v4"
        )
        self.assertEqual(metadata["version"], "2026.08.21-v4")
        self.assertEqual(metadata["architectures"], ["amd64"])
        self.assertEqual(metadata["hardware_profile"], "intel-285h")
        self.assertEqual(metadata["contract"]["models"]["delivery"], "baked-in")
        provenance = metadata["provenance"]
        self.assertEqual(
            provenance["pipeline"]["commit"],
            "6513562c9d27eba511322280e19e054c3948ae4d",
        )
        self.assertEqual(provenance["archive"]["filename"], "image-2026.08.21-v4.tar")
        self.assertEqual(provenance["archive"]["size"], 1930041856)
        self.assertEqual(
            provenance["archive"]["sha256"],
            "a6787bba6a27bc486f90b4c4dd41681d051c7c834568d99bc4a884d177d10e0f",
        )
        configured = {}
        for line in (ROOT / "config/pipeline.env").read_text(
            encoding="utf-8"
        ).splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                configured[key] = value.strip("'")
        self.assertEqual(provenance["pipeline"]["repository"], configured["PIPELINE_REPOSITORY"])
        self.assertEqual(provenance["pipeline"]["commit"], configured["PIPELINE_REVISION"])
        self.assertEqual(provenance["delivery"]["branch"], configured["PIPELINE_DELIVERY_BRANCH"])
        self.assertEqual(provenance["delivery"]["directory"], configured["PIPELINE_TRAFFIC_DELIVERY_DIR"])
        self.assertEqual(provenance["delivery"]["version"], configured["PIPELINE_TRAFFIC_VERSION"])
        self.assertEqual(provenance["archive"]["filename"], configured["PIPELINE_TRAFFIC_ARCHIVE"])
        self.assertEqual(provenance["archive"]["size"], int(configured["PIPELINE_TRAFFIC_ARCHIVE_SIZE"]))
        self.assertEqual(provenance["archive"]["sha256"], configured["PIPELINE_TRAFFIC_ARCHIVE_SHA256"])
        self.assertEqual(metadata["checksums"]["image-contract.yaml"], configured["PIPELINE_TRAFFIC_CONTRACT_SHA256"])
        self.assertEqual(metadata["checksums"]["desired-state.schema.json"], configured["PIPELINE_TRAFFIC_DESIRED_STATE_SCHEMA_SHA256"])

    def test_vendored_metadata_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "delivery"
            shutil.copytree(DELIVERY, copied)
            contract = copied / "image-contract.yaml"
            contract.write_text(
                contract.read_text(encoding="utf-8") + "# drift\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CatalogError, "checksum mismatch"):
                load_delivery_metadata(copied)

    def test_catalog_model_compiles_for_postgresql_and_has_migration(self):
        statement = str(
            CreateTable(SolutionCatalogEntry.__table__).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("solution_catalog_entries", statement)
        self.assertIn("JSON", statement)
        migration = (
            ROOT
            / "tvt_edge/db/migrations/versions/0003_solution_catalog.py"
        ).read_text(encoding="utf-8")
        self.assertIn('down_revision = "0002_alerting_foundation"', migration)
        self.assertIn('Base.metadata.tables["solution_catalog_entries"]', migration)
        catalog_source = (
            ROOT / "apexfabric/solution_management/catalog.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", catalog_source)

    def test_seed_is_idempotent(self):
        service = ManagementService(self.sessions, self.keyring)
        first = service.seed_solution_catalog(DELIVERY, "127.0.0.1:5000")
        second = service.seed_solution_catalog(DELIVERY, "127.0.0.1:5000")
        self.assertEqual(first["catalog_id"], second["catalog_id"])
        with self.sessions() as session:
            count = session.scalar(select(func.count()).select_from(SolutionCatalogEntry))
        self.assertEqual(count, 1)

    def test_successful_refresh_does_not_alter_deployment(self):
        service = ManagementService(
            self.sessions, self.keyring, catalog_resolver=lambda *_args: DIGEST
        )
        service.seed_solution_catalog(DELIVERY, "127.0.0.1:5000")
        with self.sessions.begin() as session:
            site = Site(
                site_key="site-01",
                edge_id="edge-01",
                display_name="Site 01",
                timezone_name="UTC",
            )
            session.add(site)
            session.flush()
            session.add(
                SolutionDeployment(
                    site_id=site.id,
                    deployment_key="existing",
                    solution_id="existing-solution",
                    namespace="apexfabric",
                    registry="127.0.0.1:5000",
                )
            )
        before = service.list_deployments()
        refreshed = service.refresh_solutions()
        after = service.list_deployments()
        self.assertEqual(before, after)
        self.assertEqual(refreshed[0]["status"], "available")
        self.assertEqual(refreshed[0]["image"]["digest"], DIGEST)
        self.assertEqual(
            refreshed[0]["image"]["reference"],
            f"127.0.0.1:5000/apexfabric/traffic-edge-runtime@{DIGEST}",
        )

    def test_failed_refresh_is_safe_and_marks_unavailable(self):
        def fail(*_args):
            raise CatalogError("cannot read rtsp://operator:camera-secret@example/live")

        service = ManagementService(self.sessions, self.keyring, catalog_resolver=fail)
        service.seed_solution_catalog(DELIVERY, "127.0.0.1:5000")
        result = service.refresh_solutions()
        self.assertEqual(result[0]["status"], "unavailable")
        self.assertIsNone(result[0]["image"]["digest"])
        self.assertNotIn("camera-secret", result[0]["last_error"])

    def test_registry_digest_is_verified_against_manifest_bytes(self):
        registry = "127.0.0.1:5000"
        with patch(
            "apexfabric.solution_management.catalog.urlopen",
            return_value=FakeManifestResponse(),
        ):
            self.assertEqual(
                resolve_registry_digest(registry, "apexfabric/traffic", "v4"),
                DIGEST,
            )
        with patch(
            "apexfabric.solution_management.catalog.urlopen",
            return_value=FakeManifestResponse("sha256:" + "0" * 64),
        ):
            with self.assertRaisesRegex(CatalogError, "digest mismatch"):
                resolve_registry_digest(registry, "apexfabric/traffic", "v4")

    def test_solution_api_lists_and_refreshes_safe_metadata(self):
        registry = "127.0.0.1:5000"
        ManagementService(self.sessions, self.keyring).seed_solution_catalog(
            DELIVERY, registry
        )
        app = create_app(self.sessions, self.keyring)
        list_route = next(
            route.endpoint
            for route in app.routes
            if route.path == "/api/v1/solutions" and "GET" in route.methods
        )
        refresh_route = next(
            route.endpoint
            for route in app.routes
            if route.path == "/api/v1/solutions/refresh" and "POST" in route.methods
        )

        with patch(
            "apexfabric.solution_management.catalog.urlopen",
            return_value=FakeManifestResponse(),
        ):
            listed = list_route()
            refreshed = refresh_route(
                SimpleNamespace(state=SimpleNamespace(request_id="api:test")), None
            )
        self.assertEqual(len(listed), 1)
        body = refreshed[0]
        self.assertEqual(body["status"], "available")
        self.assertNotIn("credentials", json.dumps(body).lower())


if __name__ == "__main__":
    unittest.main()
