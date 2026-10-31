import copy
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from bridge.config import DEFAULT, atomic_json, load, validate
from bridge.engine import Engine
from bridge.linux_watch import CREATE, ISDIR, OVERFLOW, InotifyWatch, records
from bridge.mounts import mount_identity
from bridge.service import Service
from bridge.state import State
from bridge.watch import PollingWatch, event_scope
from test_bridge import Fixture


def event(wd, mask, name=""):
    data = os.fsencode(name)+b"\0" if name else b""
    data += b"\0"*((-len(data)) % 4)
    return struct.pack("iIII", wd, mask, 0, len(data))+data


class EventTests(unittest.TestCase):
    def test_unicode_records_and_multiple_events(self):
        self.assertEqual(list(records(event(3, CREATE, "电影.mkv")+event(-1, OVERFLOW))),
                         [(3, CREATE, 0, "电影.mkv"), (-1, OVERFLOW, 0, "")])

    def test_malformed_events_rejected(self):
        for data in (b"short", struct.pack("iIII", 1, CREATE, 0, 100), event(1, CREATE, "..")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                list(records(data))

    def test_event_scopes_are_bounded(self):
        self.assertEqual(event_scope("电影/A.mkv"), "电影")
        self.assertEqual(event_scope("A.mkv"), "")
        for name in ("../outside", "/etc/passwd", "x/../../etc", "x\\y"):
            with self.assertRaises(ValueError):
                event_scope(name)

    def test_files_are_processed_and_new_directory_is_registered(self):
        callback = Mock()
        watcher = InotifyWatch({"id": "a", "source": "/media"}, callback, Mock(), ["_trash"])
        watcher.directories = {1: Path("/media/Film")}; watcher.add_tree = Mock()
        watcher.process(event(1, CREATE, "a.mkv"))
        callback.assert_called_with("a", "Film")
        watcher.process(event(1, CREATE | ISDIR, "Season 1"))
        watcher.add_tree.assert_called_with(Path("/media/Film/Season 1"))
        callback.reset_mock(); watcher.process(event(1, CREATE | ISDIR, "_trash"))
        callback.assert_not_called()

    def test_overflow_requests_full_reconciliation(self):
        callback = Mock(); watcher = InotifyWatch({"id": "a", "source": "/media"}, callback, Mock())
        with self.assertRaises(OSError):
            watcher.process(event(-1, OVERFLOW))
        callback.assert_called_once_with("a", "")

    @unittest.skipUnless(sys.platform == "linux", "requires a real Linux inotify kernel")
    def test_real_recursive_create_rename_delete_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); (root/"Film").mkdir()
            received = []; changed = threading.Event()
            def callback(*value):
                received.append(value); changed.set()
            watcher = InotifyWatch({"id": "a", "source": str(root)}, callback, Mock())
            watcher.start()
            try:
                self.assertTrue(watcher.ready_event.wait(5))
                changed.clear(); (root/"Film/a.mkv").write_bytes(b"video")
                self.assertTrue(changed.wait(3)); self.assertIn(("a", "Film"), received)
                received.clear(); changed.clear(); (root/"Film/a.mkv").rename(root/"Film/b.mkv")
                self.assertTrue(changed.wait(3)); self.assertIn(("a", "Film"), received)
                time.sleep(.2); received.clear(); changed.clear(); (root/"Film/b.mkv").unlink()
                self.assertTrue(changed.wait(3)); self.assertIn(("a", "Film"), received)
            finally:
                watcher.stop()
            self.assertFalse(watcher.thread.is_alive())


class PollTests(Fixture):
    def test_poll_detects_change_rename_delete_without_reading_content(self):
        first = self.file("A/a.mkv"); callback = Mock()
        watcher = PollingWatch(self.lib, callback, Mock(), 1, [])
        watcher.once(); callback.assert_called_once_with("pt", ""); callback.reset_mock()
        with patch.object(Path, "open", side_effect=AssertionError("content read")):
            watcher.once()
        callback.assert_not_called()
        first.rename(self.source/"A/b.mkv"); watcher.once()
        callback.assert_called_with("pt", "A"); callback.reset_mock()
        (self.source/"A/b.mkv").unlink(); watcher.once(); callback.assert_called_with("pt", "A")

    def test_failed_snapshot_does_not_become_authoritative(self):
        watcher = PollingWatch(self.lib, Mock(), Mock(), 1, [])
        self.file("A/a.mkv"); watcher.once(); previous = watcher.previous
        with patch("bridge.watch.walk", side_effect=PermissionError("offline")), self.assertRaises(PermissionError):
            watcher.once()
        self.assertEqual(watcher.previous, previous)


class MountTests(unittest.TestCase):
    def test_mountpoint_must_be_actual_mount(self):
        info = "50 1 0:40 / /mnt/cloud\\040disk rw shared:4 - fuse.cloud cloud rw\n"
        self.assertEqual(mount_identity("/mnt/cloud disk", info)[-1], "fuse.cloud")
        with self.assertRaises(OSError):
            mount_identity("/mnt/cloud disk/pt", info)


class ReconciliationTests(Fixture):
    def test_mount_replaced_during_listing_never_writes(self):
        self.file("Film/a.mkv"); self.lib["mount_root"] = str(self.source)
        identity = ["first"]
        from bridge.paths import walk
        def replacing(*args, **kwargs):
            yield from walk(*args, **kwargs)
            identity[0] = "replacement"
        with patch("bridge.mounts.mount_identity", side_effect=lambda _: identity[0]):
            with patch("bridge.engine.walk", side_effect=replacing), self.assertRaises(OSError):
                self.engine.scan(self.lib, cleanup=True)
        self.assertFalse(self.output.exists()); self.assertFalse(self.state.rows("pt"))

    def test_same_size_and_timestamp_metadata_is_periodically_verified(self):
        p = self.file("movie.nfo", b"old"); self.engine.scan(self.lib)
        st = p.stat(); p.write_bytes(b"new"); os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.config["sync"]["metadata_verify_seconds"] = 0
        result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 1)
        self.assertEqual((self.output/"movie.nfo").read_bytes(), b"new")

    def test_verification_of_unchanged_metadata_does_not_notify(self):
        self.file("movie.nfo", b"old"); self.engine.scan(self.lib)
        self.config["sync"]["metadata_verify_seconds"] = 0
        self.assertEqual(self.engine.scan(self.lib)["change_count"], 0)

    def test_scope_permission_error_never_cleans_up(self):
        self.file("Film/a.mkv"); self.engine.scan(self.lib)
        original = Path.stat
        def denied(path, *args, **kwargs):
            if path == self.source/"Film":
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        with patch.object(Path, "stat", denied), self.assertRaises(PermissionError):
            self.engine.scan(self.lib, scope="Film", cleanup=True)
        self.assertIsNone(self.state.rows("pt")[0]["missing_since"])

    def test_scan_status_survives_restart_and_interruption(self):
        atomic_json(self.data/"config.json", self.config); self.file("Film/a.mkv")
        service = Service(self.data)
        try:
            service.perform("scan", "pt")
            self.assertIsNotNone(service.state.scan_status("pt")["last_full_success"])
            scan_id = service.state.begin_scan("pt", "Film")
        finally:
            service.state.db.close()
        restored = State(self.data/"state.sqlite3")
        try:
            self.assertEqual(restored.scan_status("pt")["latest"]["status"], "interrupted")
            self.assertIsNotNone(restored.scan_status("pt")["last_full_success"])
        finally:
            restored.db.close()

    def test_failed_scan_has_durable_failure_status(self):
        atomic_json(self.data/"config.json", self.config); service = Service(self.data)
        try:
            (self.source/self.lib["sentinel"]).unlink()
            with self.assertRaises(OSError):
                service.perform("scan", "pt")
            self.assertEqual(service.state.scan_status("pt")["latest"]["status"], "failed")
            self.assertIsNone(service.state.scan_status("pt")["last_full_success"])
        finally:
            service.state.db.close()

    def test_background_cleanup_is_disabled_by_default(self):
        atomic_json(self.data/"config.json", self.config); service = Service(self.data)
        service.discover = Mock()
        def performed(action, library, **options):
            self.assertFalse(options["cleanup"]); service.stop_event.set()
        service.perform = Mock(side_effect=performed)
        try:
            with patch("bridge.service.time.monotonic", return_value=100):
                service.pending[("pt", "")] = {"due": 0, "since": 0, "reasons": {"测试"}}
                service.run()
            service.perform.assert_called_once()
        finally:
            service.state.db.close()

    def test_linux_mount_mapping_is_case_sensitive_and_bounded(self):
        atomic_json(self.data/"config.json", self.config); service = Service(self.data)
        service.config["cloud"]["enabled"] = True
        service.config["libraries"][0]["source"] = "/mnt/cloud/pt/movie"
        try:
            with patch("bridge.service.CloudClient") as client:
                client.return_value.mount_points.return_value = [
                    {"mount": "/mnt/cloud", "source": "/115", "mounted": True}]
                service.discover()
                self.assertEqual(service.cloud_paths["pt"], "/115/pt/movie")
                service.config["libraries"][0]["source"] = "/mnt/cloud2/movie"
                service.discover(); self.assertNotIn("pt", service.cloud_paths)
        finally:
            service.state.db.close()

    def test_legacy_config_adds_new_fields_but_rejects_unknowns(self):
        legacy = copy.deepcopy(self.config)
        for key in ("cleanup_enabled", "watch_mode", "watch_poll_seconds", "metadata_verify_seconds"):
            legacy["sync"].pop(key)
        for lib in legacy["libraries"]:
            lib.pop("mount_root"); lib.pop("output_mount_root")
        atomic_json(self.data/"config.json", legacy)
        self.assertFalse(load(self.data/"config.json")["sync"]["cleanup_enabled"])
        legacy["surprise"] = True; atomic_json(self.data/"config.json", legacy)
        with self.assertRaises(ValueError):
            load(self.data/"config.json")

    def test_mount_guard_must_contain_source(self):
        self.lib["mount_root"] = "/another/mount"
        with self.assertRaises(ValueError):
            validate(self.config, self.data)


if __name__ == "__main__":
    unittest.main()
