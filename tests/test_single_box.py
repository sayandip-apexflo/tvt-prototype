import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock

from apexfabric.control_plane.server import Controller

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('registry_verify', ROOT / 'scripts/verify-registry-export.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class SingleBoxTests(unittest.TestCase):
    def test_customer_summary_excludes_pod_secrets_and_unmanaged_workloads(self):
        controller = object.__new__(Controller)
        controller.site_id = Mock(return_value='test-site')
        controller.camera_inventory = Mock(return_value={'cameras': []})
        managed = {'metadata': {'name': 'traffic-runtime', 'labels': {
            'app.kubernetes.io/managed-by': 'apexfabric-node-agent',
            'apexfabric.com/deployment-id': 'traffic',
        }}, 'spec': {'replicas': 1, 'template': {'env': {'SECRET': 'do-not-return'}}},
            'status': {'readyReplicas': 1}}
        controller.kubectl = Mock(return_value=Mock(stdout=json.dumps({'items': [managed, {'metadata': {'name': 'unmanaged'}}]})))
        result = controller.customer_summary()
        self.assertEqual(result['deployments'], [{'name': 'traffic-runtime', 'solution': 'traffic', 'status': 'Running', 'ready_replicas': 1, 'desired_replicas': 1}])
        self.assertNotIn('do-not-return', json.dumps(result))
        managed['spec']['replicas'] = 0
        controller.kubectl.return_value.stdout = json.dumps({'items': [managed]})
        self.assertEqual(controller.customer_summary()['deployments'][0]['status'], 'Stopped')

    def archive(self, directory, extra=None, corrupt=False, omit=False):
        data = b'{"schemaVersion":2,"layers":[]}'
        digest = hashlib.sha256(data).hexdigest()
        path = Path(directory) / 'registry.tar'
        with tarfile.open(path, 'w') as archive:
            entries = {'docker/registry/v2/repositories/apexfabric/test/_manifests/tags/v1/current/link': ('sha256:' + digest).encode()}
            if not omit:
                entries[f'docker/registry/v2/blobs/sha256/{digest[:2]}/{digest}/data'] = b'corrupt' if corrupt else data
            if extra:
                entries[extra] = b'bad'
            for name, value in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
        inventory = Path(directory) / 'registry.json'
        inventory.write_text(json.dumps({'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'images': [{'repository': 'apexfabric/test', 'tag': 'v1', 'digest': 'sha256:' + digest}]}))
        return path, inventory

    def test_export_validates_contents_and_excludes_other_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier.verify(*self.archive(directory))
            for extra in ['docker/registry/v2/repositories/smart-city/demo/link', '../../etc/passwd']:
                with self.assertRaises(ValueError):
                    verifier.verify(*self.archive(directory, extra=extra))
            for options in [{'corrupt': True}, {'omit': True}]:
                with self.assertRaises(ValueError):
                    verifier.verify(*self.archive(directory, **options))

    def test_missing_inventory_can_be_recovered_from_complete_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, inventory = self.archive(directory)
            inventory.unlink()
            verifier.recover_inventory(archive, inventory)
            verifier.verify(archive, inventory)
            self.assertEqual(json.loads(inventory.read_text())['blob_count'], 1)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                verifier.recover_inventory(archive, inventory)

    def test_recovery_does_not_certify_incomplete_or_corrupt_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            for options in [{'corrupt': True}, {'omit': True},
                            {'extra': 'docker/registry/v2/repositories/smart-city/demo/link'}]:
                archive, inventory = self.archive(directory, **options)
                inventory.unlink()
                with self.assertRaises(ValueError):
                    verifier.recover_inventory(archive, inventory)
                self.assertFalse(inventory.exists())

    def test_export_ignores_stale_links_but_keeps_current_image_references(self):
        export_spec = importlib.util.spec_from_file_location('registry_export', ROOT / 'scripts/export-apexfabric-registry.py')
        exporter = importlib.util.module_from_spec(export_spec)
        export_spec.loader.exec_module(exporter)
        with tempfile.TemporaryDirectory() as directory:
            stale = 'docker/registry/v2/repositories/apexfabric/test/_layers/sha256/' + 'a' * 64 + '/link'
            archive, _ = self.archive(directory)
            with tarfile.open(archive, 'a') as target:
                data = ('sha256:' + 'a' * 64).encode()
                info = tarfile.TarInfo(stale)
                info.size = len(data)
                target.addfile(info, io.BytesIO(data))
            repositories = archive.read_bytes()
            digests, manifests, images = exporter.inspect_repositories(repositories)
            self.assertEqual(len(images), 1)
            self.assertEqual(digests, manifests)
            self.assertNotIn('sha256:' + 'a' * 64, digests)
            clean, _ = self.archive(directory)
            exporter.append_live_metadata(clean, repositories, digests)
            self.assertEqual(verifier.inspect_archive(clean)['blob_count'], 1)
            with tarfile.open(clean) as result:
                self.assertNotIn(stale, result.getnames())

    def test_failed_export_does_not_leave_a_final_archive(self):
        from unittest.mock import patch
        import subprocess
        export_spec = importlib.util.spec_from_file_location('registry_export', ROOT / 'scripts/export-apexfabric-registry.py')
        exporter = importlib.util.module_from_spec(export_spec)
        export_spec.loader.exec_module(exporter)
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = self.archive(directory)
            repository_data = archive.read_bytes()
            destination = Path(directory) / 'new.tar'
            manifest = b'{"schemaVersion":2,"layers":[]}'

            def interrupted(command, stdout, check):
                stdout.write(b'incomplete archive')
                raise subprocess.CalledProcessError(1, command)

            with patch('sys.argv', ['export', '--out', str(destination)]), \
                    patch.object(exporter.subprocess, 'check_output', side_effect=[repository_data, manifest]), \
                    patch.object(exporter.subprocess, 'run', side_effect=interrupted):
                with self.assertRaises(subprocess.CalledProcessError):
                    exporter.main()
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix('.json').exists())
