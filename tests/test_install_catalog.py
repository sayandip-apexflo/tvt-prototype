import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apexfabric.control_plane.server import Controller
from apexfabric.solution_management.catalog import SolutionCatalog
from apexfabric.solution_management.install_catalog import build_manifest, register, validate_manifest, verify

ROOT = Path(__file__).resolve().parents[1]
DIGEST = 'sha256:' + 'a' * 64


class InstallCatalogTests(unittest.TestCase):
    def setUp(self):
        self.selection = json.loads((ROOT / 'deploy/single-box/solution-packs.json').read_text())
        self.inventory = {'images': [
            {'repository': 'apexfabric/tvt-mills-pilot', 'tag': 'intel-285h-2026.09.18-v2-ungated-enroll', 'digest': DIGEST},
        ]}
        self.manifest = build_manifest(ROOT, self.selection, self.inventory)

    def test_bundle_requires_every_selected_image(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            build_manifest(ROOT, self.selection, {'images': []})

    def test_manifest_rejects_changed_contracts_and_paths(self):
        changed = copy.deepcopy(self.manifest)
        changed['solutions'][0]['files']['image-contract.yaml'] = 'bad'
        with self.assertRaisesRegex(ValueError, 'Changed'):
            validate_manifest(ROOT, changed)
        changed['solutions'][0]['directory'] = '../../etc'
        with self.assertRaisesRegex(ValueError, 'inside'):
            validate_manifest(ROOT, changed)

    def test_registration_is_idempotent_and_verifies_every_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = SolutionCatalog(Path(directory) / 'catalog.sqlite3')
            with patch('apexfabric.solution_management.install_catalog.resolve_registry_digest', return_value=DIGEST), patch('apexfabric.solution_management.catalog.resolve_registry_digest', return_value=DIGEST):
                register(catalog, ROOT, self.manifest, '127.0.0.1:5000')
                register(catalog, ROOT, self.manifest, '127.0.0.1:5000')
            self.assertEqual(len(catalog.list()), 1)
            self.assertEqual(len(verify(catalog, ROOT, self.manifest, '127.0.0.1:5000')), 1)
            with catalog._connect() as connection:
                connection.execute("UPDATE solutions SET status='unavailable' WHERE name='tvt-mills-pilot'")
            with self.assertRaisesRegex(ValueError, 'not available'):
                verify(catalog, ROOT, self.manifest, '127.0.0.1:5000')

    def test_wrong_image_fails_before_catalog_rows_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = SolutionCatalog(Path(directory) / 'catalog.sqlite3')
            with patch('apexfabric.solution_management.install_catalog.resolve_registry_digest', return_value='sha256:' + 'b' * 64):
                with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                    register(catalog, ROOT, self.manifest, '127.0.0.1:5000')
            self.assertEqual(catalog.list(), [])

    def test_server_uses_bundled_manifest_instead_of_default_packs(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = dict(self.manifest, solutions=self.manifest['solutions'][:1])
            manifest_path = Path(directory) / 'manifest.json'
            manifest_path.write_text(json.dumps(manifest))
            with patch.dict('os.environ', {'APEXFABRIC_CATALOG_MANIFEST': str(manifest_path)}, clear=True):
                controller = Controller(Path(directory) / 'control')
            self.assertEqual([x['name'] for x in controller.catalog.list()], ['tvt-mills-pilot'])


if __name__ == '__main__':
    unittest.main()
