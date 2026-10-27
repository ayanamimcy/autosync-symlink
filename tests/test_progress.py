import os
import threading
import time
from unittest.mock import patch

from bridge.config import atomic_json
from bridge.engine import Engine
from bridge.service import Service
from test_bridge import Fixture


class ProgressTests(Fixture):
    def service(self):
        atomic_json(self.data/"config.json", self.config)
        service = Service(self.data)
        self.addCleanup(service.state.db.close)
        return service

    def start_scan(self, service):
        failures = []
        def run():
            try:
                service.perform("scan", "pt")
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, failures

    def test_discovered_files_are_visible_before_enumeration_finishes(self):
        media = self.file("Movie.mkv"); discovered = threading.Event(); release = threading.Event()
        service = self.service()
        def listing(*_, **__):
            yield media, media.stat()
            discovered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release enumeration")
        with patch("bridge.engine.walk", side_effect=listing):
            thread, failures = self.start_scan(service)
            try:
                self.assertTrue(discovered.wait(3))
                library = service.status()["libraries"][0]
                self.assertEqual(library["scan"]["latest"]["status"], "running")
                self.assertIsNone(library["scan"]["last_full_success"])
                self.assertEqual(library["progress"]["seen"], 1)
                self.assertEqual(library["progress"]["processed"], 0)
                self.assertIsNone(library["progress"]["total"])
                self.assertFalse(self.output.exists())
            finally:
                release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(failures, [])
        library = service.status()["libraries"][0]
        self.assertIsNone(library["progress"])
        self.assertEqual(library["scan"]["latest"]["seen"], 1)
        self.assertIsNotNone(library["scan"]["last_full_success"])

    def test_completed_workers_report_while_first_future_is_still_blocked(self):
        self.config["sync"]["file_workers"] = 2
        self.file("a.mkv"); self.file("b.mkv")
        service = self.service(); entered = threading.Event(); release = threading.Event()
        original = Engine.apply_file
        def apply(engine, lib, item, *args):
            if item[2] == "a.mkv":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release first file")
            return original(engine, lib, item, *args)
        with patch.object(Engine, "apply_file", apply):
            thread, failures = self.start_scan(service)
            try:
                self.assertTrue(entered.wait(3))
                until = time.monotonic()+3
                while time.monotonic() < until:
                    library = service.status()["libraries"][0]
                    if library["progress"] and library["progress"]["processed"] == 1:
                        break
                    time.sleep(.01)
                else:
                    self.fail("completed file was not reported while the coordinator was waiting")
                self.assertEqual(library["progress"]["seen"], 2)
                self.assertEqual(library["progress"]["total"], 2)
                self.assertEqual(library["progress"]["changes"], 1)
                self.assertEqual(library["progress"]["scan_id"], library["scan"]["latest"]["id"])
                self.assertTrue((self.output/"b.mkv").is_symlink())
                self.assertFalse(os.path.lexists(self.output/"a.mkv"))
            finally:
                release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(failures, [])

    def test_failed_enumeration_retains_partial_counts_without_full_success(self):
        media = self.file("Movie.mkv"); service = self.service()
        def listing(*_, **__):
            yield media, media.stat()
            raise PermissionError("remaining subtree is unavailable")
        with patch("bridge.engine.walk", side_effect=listing), self.assertRaises(PermissionError):
            service.perform("scan", "pt")
        library = service.status()["libraries"][0]
        self.assertEqual(library["scan"]["latest"]["status"], "failed")
        self.assertEqual(library["scan"]["latest"]["seen"], 1)
        self.assertIsNone(library["scan"]["last_full_success"])
        self.assertIsNone(library["progress"])
        self.assertFalse(self.output.exists())

    def test_many_file_events_do_not_commit_progress_for_every_file(self):
        for i in range(100):
            self.file(f"{i}.mkv")
        service = self.service()
        with patch("bridge.service.time.monotonic", return_value=100):
            with patch.object(service.state, "update_scan_progress", wraps=service.state.update_scan_progress) as checkpoint:
                service.perform("scan", "pt")
        self.assertLessEqual(checkpoint.call_count, 6)
        self.assertEqual(service.state.scan_status("pt")["latest"]["seen"], 100)

    def test_late_checkpoint_does_not_overwrite_a_finished_scan(self):
        scan_id = self.state.begin_scan("pt", "")
        progress = {"seen":3, "changes":2, "deferred":0}
        self.state.update_scan_progress(scan_id, progress)
        self.assertEqual(self.state.scan_status("pt")["latest"]["seen"], 3)
        self.state.finish_scan(scan_id, {"seen":4, "change_count":3, "scan_complete":True})
        self.state.update_scan_progress(scan_id, progress)
        self.assertEqual(self.state.scan_status("pt")["latest"]["seen"], 4)

    def test_parallel_completion_counters_are_consistent(self):
        for i in range(32):
            self.file(f"{i}.mkv")
        snapshots = []
        engine = Engine(self.state, self.config, scan_progress=snapshots.append)
        result = engine.scan(self.lib)
        completed = [p for p in snapshots if p["stage"] == "处理文件"]
        self.assertEqual([p["processed"] for p in completed], list(range(33)))
        self.assertTrue(all(p["seen"] == p["total"] == 32 for p in completed))
        self.assertEqual(snapshots[-1]["changes"], result["change_count"])
        self.assertEqual(snapshots[-1]["seen"], 32)

    def test_oversized_metadata_explains_limit_and_keeps_partial_status(self):
        self.config["sync"]["metadata_max_mb"] = 1
        self.file("Movie.mkv")
        self.file("Other/Scan.png", b"x"*(2*1024*1024))
        service = self.service()
        result = service.perform("scan", "pt")[0]
        self.assertEqual(result["change_count"], 1)
        self.assertIn("2.00 MiB", result["errors"][0])
        self.assertIn("上限 1 MiB", result["errors"][0])
        self.assertTrue((self.output/"Movie.mkv").is_symlink())
        self.assertFalse((self.output/"Other/Scan.png").exists())
        library = service.status()["libraries"][0]
        self.assertEqual(library["scan"]["latest"]["status"], "partial")
        self.assertIsNone(library["scan"]["last_full_success"])
