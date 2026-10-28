import copy
import json
import threading
import time
from unittest.mock import patch

from bridge.config import atomic_json
from bridge.service import Service
from test_bridge import Fixture


class ConfigSaveTests(Fixture):
    def service(self):
        config = copy.deepcopy(self.config)
        config['sync']['watch_mode'] = 'off'
        atomic_json(self.data/'config.json', config)
        service = Service(self.data)
        def cleanup():
            service.stop()
            service.state.db.close()
        self.addCleanup(cleanup)
        return service

    def test_save_is_acknowledged_during_scan_and_applies_before_next_task(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def perform(action, library, **options):
            calls.append((library, service.config['sync']['interval_seconds']))
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test scan was not released')
            return []
        service.schedule('pt')
        service.pending[('pt', '')] = 0
        with patch.object(service, 'perform', side_effect=perform):
            service.thread.start()
            self.assertTrue(entered.wait(2))
            service.submit('scan', 'pt')
            config = copy.deepcopy(service.config)
            config['sync']['interval_seconds'] = 21600
            config['cloud']['token'] = 'private-test-token'
            started = time.monotonic()
            job = service.submit_config(config)
            self.assertLess(time.monotonic()-started, 1)
            self.assertEqual(job['status'], '排队中')
            self.assertNotIn('private-test-token', json.dumps(service.status()))
            self.assertEqual(service.config['sync']['interval_seconds'], 900)
            self.assertEqual(json.loads(service.config_path.read_text())['sync']['interval_seconds'], 900)
            with self.assertRaisesRegex(ValueError, '已有配置'):
                service.submit_config(config)
            release.set()
            deadline = time.monotonic()+4
            while time.monotonic() < deadline and len(calls) < 2:
                time.sleep(.02)
            self.assertEqual(service.jobs[job['id']]['status'], '完成')
            self.assertEqual(calls[:2], [('pt', 900), ('pt', 21600)])
            self.assertEqual(json.loads(service.config_path.read_text())['cloud']['token'], 'private-test-token')

    def test_invalid_or_retargeted_config_is_not_queued(self):
        self.file('Movie.mkv'); self.engine.scan(self.lib)
        service = self.service()
        for key, value in [('output', str(self.root/'different')), ('mode', 'strm')]:
            with self.subTest(key=key):
                config = copy.deepcopy(service.config)
                config['libraries'][0][key] = value
                with self.assertRaises(ValueError):
                    service.submit_config(config)
        config = copy.deepcopy(service.config)
        config['sync']['scan_workers'] = 0
        with self.assertRaises(ValueError):
            service.submit_config(config)
        self.assertFalse(service.queue)

    def test_apply_failure_has_a_queryable_job_error(self):
        service = self.service()
        job = service.submit_config(service.config)
        with patch.object(service, 'save_config', side_effect=OSError('configuration directory is read-only')):
            service.thread.start()
            deadline = time.monotonic()+2
            while service.jobs[job['id']]['status'] != '失败' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(service.jobs[job['id']]['status'], '失败')
            self.assertIn('read-only', service.jobs[job['id']]['error'])
        self.assertEqual(json.loads(service.config_path.read_text()), service.config)
