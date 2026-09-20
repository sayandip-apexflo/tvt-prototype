import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apexfabric.control_plane.server import Controller
from apexfabric.solution_management.catalog import SolutionCatalog, resolve_registry_digest
from apexfabric.solution_management.renderer import render


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = b'{"schemaVersion":2}'
DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()


class ManifestResponse:
    headers = {"Docker-Content-Digest": DIGEST}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maximum):
        return MANIFEST


class SolutionCatalogTests(unittest.TestCase):
    def test_every_catalog_delivery_has_complete_schema_driven_ui_annotations(self):
        deliveries = sorted((ROOT / "solution-packs/catalog").glob("*/image-contract.yaml"))
        self.assertTrue(deliveries)
        with tempfile.TemporaryDirectory() as directory:
            catalog = SolutionCatalog(Path(directory) / "catalog.sqlite3")
            for contract_path in deliveries:
                catalog.seed_delivery(contract_path.parent, "registry.local:5000", f"apexfabric/{contract_path.parent.name}")
            for entry in catalog.list():
                camera = entry["desired_state_schema"]["properties"]["cameras"]["items"]["properties"]
                app_ids = set(camera["apps"]["items"]["enum"])
                ui = entry["contract"]["ui"]["camera"]
                self.assertEqual(set(ui["apps"]), app_ids)
                self.assertIn(ui["defaultApp"], app_ids)
                deployment = entry["contract"]["ui"]["deployment"]
                self.assertTrue({
                    "storageGi", "cpuRequest", "cpuLimit", "memoryRequestGi",
                    "memoryLimitGi", "pullPolicy", "inferenceMode",
                }.issubset(deployment))

    def test_site_ui_derives_apps_from_catalog_schema(self):
        javascript = (ROOT / "apexfabric/control_plane/static/site.js").read_text(encoding="utf-8")
        self.assertIn("cameraSchema.apps?.items?.enum", javascript)
        self.assertNotIn("const packSpecs", javascript)
        for hard_coded_app in ("wrong_way", "face_recognition", "illegal_parking", "people_counting"):
            self.assertNotIn(hard_coded_app, javascript)

    def test_controller_backfills_ui_annotations_for_existing_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            catalog = SolutionCatalog(state / "catalog.sqlite3")
            delivery = ROOT / "solution-packs/catalog/tvt-mills-pilot-2026.09.18-v1"
            catalog.seed_delivery(delivery, "registry.local:5000", "apexfabric/tvt-mills-pilot")
            with catalog._connect() as connection:
                connection.execute("""
                    INSERT INTO solutions
                    SELECT 'tvt-mills-pilot:2026.09.18-v2', name, '2026.09.18-v2', registry,
                           repository, 'intel-285h-2026.09.18-v2', digest, status,
                           json_remove(contract_json, '$.ui'), desired_state_schema_json,
                           desired_state_example_json, last_error, updated_at
                    FROM solutions WHERE catalog_id='tvt-mills-pilot:2026.09.18-v1'
                """)
            controller = Controller(state)
            version_two = controller.catalog.get("tvt-mills-pilot:2026.09.18-v2")
            self.assertEqual(version_two["contract"]["ui"]["camera"]["defaultApp"], "face_recognition")

    def test_admin_solution_types_and_catalog_images_are_catalog_driven(self):
        javascript = (ROOT / "apexfabric/control_plane/static/enhancements.js").read_text(encoding="utf-8")
        self.assertIn("function solutionTypeOptions()", javascript)
        self.assertIn("catalogEntriesFor(S.solutionType)", javascript)
        self.assertIn("selected.name", javascript)
        self.assertNotIn("192.168.", javascript)
        self.assertNotIn("Manual image entry", javascript)
        for hard_coded_app in ("wrong_way", "face_recognition", "illegal_parking", "people_counting"):
            self.assertNotIn(hard_coded_app, javascript)

    def test_both_uis_share_catalog_and_cluster_sources(self):
        site = (ROOT / "apexfabric/control_plane/static/site.js").read_text(encoding="utf-8")
        admin = (ROOT / "apexfabric/control_plane/static/enhancements.js").read_text(encoding="utf-8")
        self.assertIn("contract?.ui?.deployment", site)
        self.assertIn("contract?.ui?.deployment", admin)
        self.assertIn("desired_state_example", site)
        self.assertIn("desired_state_example", admin)
        self.assertIn("state.node_reports", site)
        self.assertIn("S.data?.node_reports", admin)
        self.assertIn("d.spec?.selector?.matchLabels", site)
        self.assertIn("d.spec?.selector?.matchLabels", admin)

    def test_seed_and_resolve_delivery_to_immutable_digest(self):
        with patch("apexfabric.solution_management.catalog.urlopen", return_value=ManifestResponse()):
            registry = "registry.local:5000"
            with tempfile.TemporaryDirectory() as directory:
                catalog = SolutionCatalog(Path(directory) / "catalog.sqlite3")
                delivery = ROOT / "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
                catalog.seed_delivery(delivery, registry, "apexfabric/traffic-edge-runtime")
                seeded = catalog.list()[0]
                self.assertEqual(seeded["status"], "unresolved")
                self.assertIn("cameras", seeded["desired_state_schema"]["properties"])
                example = seeded["desired_state_example"]
                self.assertEqual(example["revision"], 1)
                self.assertEqual(example["cameras"][0]["camera_id"], "cam-traffic-01")
                self.assertIn("zones", example["cameras"][0]["config"])
                refreshed = catalog.refresh()[0]
                self.assertEqual(refreshed["status"], "available")
                self.assertEqual(refreshed["digest"], DIGEST)
                self.assertEqual(refreshed["image"]["tag"], "intel-285h-2026.08.21-v4")

    def test_registry_digest_resolver_uses_manifest_digest_header(self):
        with patch("apexfabric.solution_management.catalog.urlopen", return_value=ManifestResponse()) as mocked:
            digest = resolve_registry_digest(
                "registry.local:5000",
                "apexfabric/traffic-edge-runtime",
                "intel-285h-2026.08.21-v4",
            )
            self.assertEqual(digest, DIGEST)
            request = mocked.call_args.args[0]
            self.assertEqual(request.full_url, "http://registry.local:5000/v2/apexfabric/traffic-edge-runtime/manifests/intel-285h-2026.08.21-v4")

    def test_retarget_moves_existing_versions_and_requires_digest_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = SolutionCatalog(Path(directory) / "catalog.sqlite3")
            delivery = ROOT / "solution-packs/catalog/traffic-edge-runtime-2026.08.21-v4"
            catalog.seed_delivery(delivery, "192.168.10.187:5000", "apexfabric/traffic-edge-runtime")
            with patch("apexfabric.solution_management.catalog.resolve_registry_digest", return_value=DIGEST):
                catalog.refresh()
            self.assertEqual(catalog.list()[0]["digest"], DIGEST)
            self.assertEqual(catalog.retarget(
                "traffic-edge-runtime", "192.168.10.31:5000", "apexfabric/traffic-edge-runtime",
            ), 1)
            moved = catalog.list()[0]
            self.assertEqual(moved["registry"], "192.168.10.31:5000")
            self.assertEqual(moved["status"], "unresolved")
            self.assertIsNone(moved["digest"])
            self.assertIsNone(moved["last_error"])

    def test_renderer_prefers_digest_over_mutable_tag(self):
        bundle = json.loads(json.dumps(__import__("yaml").safe_load(
            (ROOT / "solution-packs/traffic/tvt-mills-pilot-intel-285h.yaml").read_text()
        )))
        bundle["applications"][0]["image"]["digest"] = DIGEST
        deployment = next(item for item in render(bundle, "apexfabric") if item["kind"] == "Deployment")
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, f"__APEXFABRIC_REGISTRY__/apexfabric/tvt-mills-pilot@{DIGEST}")

    def test_catalog_selection_generates_digest_pinned_traffic_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory))
            with patch("apexfabric.solution_management.catalog.resolve_registry_digest", return_value=DIGEST):
                controller.catalog.refresh()
            generated = controller.generate_bundle({
                "solution_type": "tvt-mills-pilot",
                "catalog_id": "tvt-mills-pilot:2026.09.18-v1",
                "deployment_id": "tvt-mills-demo",
                "edge_id": "intel-box-01",
                "camera_configuration": [{"camera_id": "cam-1", "apps": ["anpr"]}],
            })
            app_image = generated["bundle"]["applications"][0]["image"]
            self.assertEqual(app_image["digest"], DIGEST)
            deployment = next(item for item in generated["objects"] if item["kind"] == "Deployment")
            self.assertTrue(deployment["spec"]["template"]["spec"]["containers"][0]["image"].endswith(f"@{DIGEST}"))


if __name__ == "__main__":
    unittest.main()
