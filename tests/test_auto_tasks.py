import copy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bridge.config import atomic_json
from bridge.service import Service
from bridge.watch import Monitor
from test_bridge import Fixture


def entry(name, mtime, directory=True):
    return SimpleNamespace(name=name, mtime_ns=mtime, size=0, directory=directory)


class AutoTaskTests(Fixture):
    def service(self, **sync):
        config = copy.deepcopy(self.config)
        config['sync'].update(scan_on_startup=False, scan_on_reconnect=False, interval_seconds=43200,
                              watch_mode='off', **sync)
        config['cloud']['enabled'] = True
        config['libraries'][0]['cloud_path'] = '/115/pt'
        atomic_json(self.data/'config.json', config)
        service = Service(self.data)
        self.addCleanup(service.state.db.close)
        return service

    def run_once(self, service):
        with patch.object(service.stop_event, 'wait', side_effect=lambda _: service.stop_event.set()):
            service.run()

    def test_automatic_scans_are_recorded_with_merged_triggers(self):
        service = self.service()
        service.discover()
        service.change({'path': '/115/pt/Film/a.mkv', 'new_path': '', 'type': 0})
        service.schedule('pt', 'Film', '定期目录检查')
        waiting = service.status()['waiting']
        self.assertEqual([(w['scope'], w['trigger']) for w in waiting], [('Film', 'CD2 文件通知、定期目录检查')])
        service.pending[('pt', 'Film')]['due'] = 0
        with patch.object(service, 'perform', return_value=[{'seen': 3, 'changes': 1, 'problems': ['x']}]) as perform:
            self.run_once(service)
        perform.assert_called_once()
        job = service.status()['jobs'][-1]
        self.assertEqual((job['status'], job['trigger'], job['library'], job['scope'], job['auto']),
                         ('完成', 'CD2 文件通知、定期目录检查', 'pt', 'Film', True))
        self.assertEqual(job['result'], [{'seen': 3, 'changes': 1}])
        self.assertEqual(service.submit('scan', 'pt')['trigger'], '手动')

    def test_full_scan_absorbs_scoped_requests_and_their_triggers(self):
        service = self.service()
        service.schedule('pt', 'A', 'CD2 文件通知')
        service.schedule('pt', '', '定时全量扫描')
        service.schedule('pt', 'B', '延迟重试（文件尚未稳定）')
        self.assertEqual(list(service.pending), [('pt', '')])
        self.assertEqual(service.pending[('pt', '')]['reasons'],
                         {'CD2 文件通知', '定时全量扫描', '延迟重试（文件尚未稳定）'})

    def test_failed_automatic_scan_with_os_error_is_retried(self):
        service = self.service()
        service.schedule('pt', 'Film', 'CD2 文件通知')
        service.pending[('pt', 'Film')]['due'] = 0
        with patch.object(service, 'perform', side_effect=OSError('mount gone')):
            self.run_once(service)
        self.assertEqual(service.status()['jobs'][-1]['status'], '失败')
        self.assertEqual(service.pending[('pt', 'Film')]['reasons'], {'出错重试'})

    def test_push_statistics_count_ignored_paths(self):
        service = self.service()
        service.discover()
        service.change({'path': '/115/pt/Film/a.mkv', 'new_path': '', 'type': 0})
        service.change({'path': '/elsewhere/b.mkv', 'new_path': '', 'type': 0})
        push = service.status()['push']
        self.assertEqual((push['received'], push['matched'], push['ignored']), (2, 1, 1))
        self.assertEqual(push['ignored_samples'], ['/elsewhere/b.mkv'])

    def test_directory_check_scans_new_and_changed_top_folders_only(self):
        service = self.service()
        listings = [[entry('A', 1), entry('B', 1), entry('.media-bridge-ready', 1, False)],
                    [entry('A', 1), entry('B', 2), entry('C', 5), entry('.media-bridge-ready', 1, False)],
                    [entry('A', 1), entry('C', 5), entry('.media-bridge-ready', 1, False)]]
        client = Mock()
        client.token.return_value = 't'
        client.list_files.side_effect = listings
        with patch('bridge.service.CloudClient', return_value=client):
            service.cloud_check('启动后目录检查')
            self.assertFalse(service.pending)  # first listing is only the baseline
            service.cloud_check('定期目录检查')
            self.assertEqual(set(service.pending), {('pt', 'B'), ('pt', 'C')})
            self.assertEqual(service.check_status['scheduled'], 2)
            service.pending.clear()
            service.cloud_check('定期目录检查')
            self.assertFalse(service.pending)  # removals need cleanup, which is off
        self.assertTrue(client.list_files.call_args.kwargs['force_refresh'])

    def test_snapshot_survives_restart_so_downtime_changes_are_found(self):
        service = self.service()
        client = Mock()
        client.token.return_value = 't'
        client.list_files.return_value = [entry('A', 1)]
        with patch('bridge.service.CloudClient', return_value=client):
            service.cloud_check('启动后目录检查')
        service.state.db.close()
        restarted = Service(self.data)
        self.addCleanup(restarted.state.db.close)
        client.list_files.return_value = [entry('A', 2)]
        with patch('bridge.service.CloudClient', return_value=client):
            restarted.cloud_check('启动后目录检查')
        self.assertEqual(restarted.pending[('pt', 'A')]['reasons'], {'启动后目录检查'})

    def test_push_connection_requests_an_incremental_check(self):
        service = self.service()
        service.monitor.connected()
        self.assertTrue(service.check_now.is_set())
        self.assertEqual(service.check_reason, '推送连接后增量补扫')
        service.check_now.clear()
        service.monitor.connected()
        self.assertEqual(service.check_reason, '推送重连后增量补扫')
        self.assertFalse(service.pending)  # full rescans stay governed by scan_on_reconnect

    def test_inotify_is_not_used_on_fuse_mounts(self):
        config = copy.deepcopy(self.config)
        config['sync']['watch_mode'] = 'auto'
        with patch('bridge.watch.os.name', 'posix'), patch('bridge.watch.sys.platform', 'linux'), \
                patch('bridge.mounts.filesystem_type', return_value='fuse'), \
                patch('bridge.linux_watch.InotifyWatch') as watcher:
            monitor = Monitor(config, Mock(), Mock())
        watcher.assert_not_called()
        self.assertIn('FUSE', monitor.summary())
