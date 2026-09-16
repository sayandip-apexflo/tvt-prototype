import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from apexfabric.control_plane.alerts import AlertStore, validate_rule
from apexfabric.control_plane.telemetry import TelemetryStore, RetentionPolicy


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1))
        self.alerts = AlertStore(self.store)
        self.rule = self.alerts.save(dict(name='Busy entrance', event_type='count', field='data.count', operator='gt', threshold=20, cooldown_seconds=0))

    def event(self, id, value, camera='front', deployment='pack', **extra):
        return self.store.ingest(deployment, dict(id=id, type='count', data={'count':value, 'camera_id':camera}, **extra))

    def test_crossings_and_camera_isolation(self):
        self.event('1', 21); self.event('2', 22)
        self.assertEqual(len(self.alerts.list()), 1)
        self.event('3', 25, camera='rear')
        self.event('4', 20); self.event('5', 30)
        self.assertEqual(len(self.alerts.list()), 3)
        self.event('6', 90, deployment='other')
        self.assertEqual(len(self.alerts.list()), 4)

    def test_restart_duplicate_and_concurrent_delivery(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: self.event('same', 25), range(8)))
        restored = TelemetryStore(Path(self.directory.name), RetentionPolicy(minimum_free_bytes=1))
        restored.ingest('pack', {'id':'same', 'type':'count', 'data':{'count':25,'camera_id':'front'}})
        restored.ingest('pack', {'id':'new', 'type':'count', 'data':{'count':30,'camera_id':'front'}})
        self.assertEqual(len(AlertStore(restored).list()), 1)

    def test_scope_disabled_and_bad_value(self):
        self.rule.update(camera_id='front', deployment_id='pack')
        self.alerts.save(self.rule)
        for index, value in enumerate([True, '40', None, float('nan')]):
            self.event(str(index), value)
        self.event('rear', 99, camera='rear'); self.event('other', 99, deployment='other')
        self.assertEqual(self.alerts.list(), [])
        self.rule['enabled'] = False; self.alerts.save(self.rule)
        self.event('disabled', 99)
        self.assertEqual(self.alerts.list(), [])

    def test_cooldown_requires_rearm(self):
        self.rule['cooldown_seconds'] = 300; self.alerts.save(self.rule)
        import time
        now=time.time()
        with patch('apexfabric.control_plane.telemetry.time.time', return_value=now): self.event('1', 25)
        with patch('apexfabric.control_plane.telemetry.time.time', return_value=now+1): self.event('2', 0)
        with patch('apexfabric.control_plane.telemetry.time.time', return_value=now+2): self.event('3', 25)
        with patch('apexfabric.control_plane.telemetry.time.time', return_value=now+400): self.event('4', 25)
        self.assertEqual(len(self.alerts.list()),1)
        with patch('apexfabric.control_plane.telemetry.time.time', return_value=now+401): self.event('5', 0)
        with patch('apexfabric.control_plane.telemetry.time.time', return_value=now+402): self.event('6', 25)
        self.assertEqual(len(self.alerts.list()),2)

    def test_atomic_evaluation_failure_rolls_back_event(self):
        with patch('apexfabric.control_plane.telemetry.evaluate_alerts', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError): self.event('retry',25)
        with self.store._connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        self.event('retry',25)
        self.assertEqual(len(self.alerts.list()),1)

    def test_snapshot_failure_preserves_event_and_alert(self):
        def unavailable(*args): raise OSError('missing image')
        self.store.ingest('pack', {'id':'snapshot', 'type':'count', 'data':{'count':30}, 'snapshot':'/snapshots/missing.jpg'}, unavailable)
        self.assertEqual(len(self.alerts.list()),1)
        with self.store._connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],0)

    def test_acknowledgement_and_deleted_rule_preserve_evidence(self):
        self.event('1',25)
        item=self.alerts.list()[0]
        self.alerts.acknowledge(item['id']); self.alerts.acknowledge(item['id'])
        self.alerts.delete(self.rule['id'])
        item=self.alerts.list()[0]
        self.assertIsNotNone(item['acknowledged_at'])
        self.assertEqual(item['evidence']['rule']['name'],'Busy entrance')
        with self.store._connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM alert_rule_state').fetchone()[0],0)

    def test_rule_validation(self):
        for change in [{'threshold':True},{'threshold':float('inf')},{'field':'x[0]'},{'operator':'exec'},{'enabled':1},{'cooldown_seconds':-1}]:
            with self.assertRaises(ValueError): validate_rule({**self.rule, **change})
        with self.assertRaises(ValueError): self.alerts.save({**self.rule,'id':'f'*32})

    def test_existing_events_are_not_backfilled(self):
        self.alerts.delete(self.rule['id']); self.event('old',30)
        self.rule.pop('id'); self.alerts.save(self.rule)
        self.event('old',30)
        self.assertEqual(self.alerts.list(),[])

    def test_http_rule_and_ack_routes(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import Mock
        from apexfabric.control_plane.server import Handler
        handler = object.__new__(Handler)
        handler.controller = SimpleNamespace(alerts=self.alerts)
        handler.json_response = Mock()
        handler.path = '/apexfabricdashboard/api/alert-rules'
        handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        self.event('api', 30)
        alert_id = self.alerts.list()[0]['id']
        handler.path = '/apexfabricdashboard/api/alerts/acknowledge'
        body = json.dumps({'id':alert_id}).encode()
        handler.headers = {'Content-Length':str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 200)
        self.assertIsNotNone(self.alerts.list()[0]['acknowledged_at'])
        body = json.dumps({'id':'missing'}).encode()
        handler.headers = {'Content-Length':str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.do_POST()
        self.assertEqual(handler.json_response.call_args.args[0], 400)

    def test_evidence_survives_source_event_retention(self):
        self.event('expired',30)
        with self.store._connect() as c:
            c.execute('DELETE FROM events')
        self.assertEqual(self.alerts.list()[0]['evidence']['value'],30)
