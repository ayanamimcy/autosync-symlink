import copy
from unittest.mock import Mock, patch

from bridge.config import DEFAULT, atomic_json, load, validate
from bridge.service import Service
from bridge.watch import Monitor, PollingWatch
from test_bridge import Fixture


class ScanPolicyTests(Fixture):
    def service(self, startup=False, reconnect=False):
        config = copy.deepcopy(self.config)
        config['sync'].update(scan_on_startup=startup, scan_on_reconnect=reconnect,
                              interval_seconds=43200, watch_mode='off')
        config['libraries'][0]['cloud_path'] = '/115/pt'
        atomic_json(self.data/'config.json', config)
        service = Service(self.data)
        self.addCleanup(service.state.db.close)
        return service

    def run_once(self, service):
        with patch.object(service.stop_event, 'wait', side_effect=lambda _:service.stop_event.set()):
            service.run()

    def test_disabled_startup_waits_for_interval_without_touching_source(self):
        service = self.service()
        with patch.object(service, 'perform') as perform, patch('bridge.paths.walk', side_effect=AssertionError('unexpected traversal')):
            self.run_once(service)
        perform.assert_not_called()
        self.assertFalse(service.pending)
        self.assertFalse(service.native.watchers)

    def test_periodic_scan_is_scheduled_after_the_complete_interval(self):
        service = self.service()
        with patch('bridge.service.time.monotonic', side_effect=[100, 43301, 43301]):
            self.run_once(service)
        self.assertEqual(set(service.pending), {('pt', '')})

    def test_default_startup_still_schedules_full_scan(self):
        service = self.service(startup=True)
        self.run_once(service)
        self.assertEqual(set(service.pending), {('pt', '')})

    def test_cloud_initial_connection_and_reconnection_can_both_skip_full_scan(self):
        service = self.service()
        service.monitor.connected(); service.monitor.connected()
        self.assertFalse(service.pending)
        service.discover()
        service.change({'path':'/115/pt/Film/episode.mkv', 'new_path':'', 'type':0})
        self.assertEqual(set(service.pending), {('pt', 'Film')})

    def test_reconnect_policy_is_independent_from_initial_connection(self):
        service = self.service(reconnect=True)
        service.monitor.connected()
        self.assertFalse(service.pending)
        service.monitor.connected()
        self.assertEqual(set(service.pending), {('pt', '')})

    def test_native_ready_respects_policy_but_real_root_events_still_scan(self):
        service = self.service()
        ready = service.monitor_ready()
        ready('pt'); ready('pt')
        self.assertFalse(service.pending)
        service.schedule('pt', '')
        self.assertEqual(set(service.pending), {('pt', '')})

    def test_polling_baseline_skips_index_scan_but_file_changes_are_processed(self):
        service = self.service()
        callback = Mock()
        watcher = PollingWatch(self.lib, callback, Mock(), 30, [], ready=service.monitor_ready())
        watcher.once()
        self.assertFalse(service.pending)
        callback.assert_not_called()
        self.file('Film/episode.mkv'); watcher.once()
        callback.assert_called_once_with('pt', 'Film')

    def test_monitor_wires_ready_callback_for_linux_and_windows(self):
        service = self.service()
        config = copy.deepcopy(service.config); config['sync']['watch_mode'] = 'auto'
        ready = service.monitor_ready()
        with patch('bridge.watch.os.name', 'posix'), patch('bridge.watch.sys.platform', 'linux'), patch('bridge.linux_watch.InotifyWatch') as watcher:
            Monitor(config, Mock(), Mock(), ready=ready)
            self.assertIs(watcher.call_args.kwargs['ready'], ready)
        with patch('bridge.watch.os.name', 'nt'), patch('bridge.native_watch.NativeMonitor') as watcher:
            Monitor(config, Mock(), Mock(), ready=ready)
            self.assertIs(watcher.call_args.kwargs['ready'], ready)

    def test_manual_scan_and_configuration_apply_do_not_depend_on_startup_scan(self):
        service = self.service()
        service.save_config(service.config)
        self.assertFalse(service.pending)
        job = service.submit('scan', 'pt')
        with patch.object(service, 'perform', return_value=[]) as perform:
            self.run_once(service)
        perform.assert_called_once_with('scan', 'pt')
        self.assertEqual(service.jobs[job['id']]['status'], '完成')

    def test_legacy_configuration_and_boolean_validation(self):
        config = copy.deepcopy(self.config)
        for key in ('scan_on_startup', 'scan_on_reconnect'):del config['sync'][key]
        path = self.data/'legacy.json'; atomic_json(path, config)
        upgraded = load(path)
        self.assertTrue(upgraded['sync']['scan_on_startup'])
        self.assertTrue(upgraded['sync']['scan_on_reconnect'])
        for key in ('scan_on_startup', 'scan_on_reconnect'):
            for value in (0, 1, 'false', None):
                with self.subTest(key=key, value=value):
                    invalid = copy.deepcopy(DEFAULT); invalid['sync'][key] = value
                    with self.assertRaises(ValueError):validate(invalid)
