"""Concurrency and data-protection regression tests using real local files."""
import copy
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from bridge.config import atomic_json, load, validate
from bridge.parallel import bounded_map
from bridge.paths import _read_directory, walk
from test_bridge import Fixture


class ParallelTests(Fixture):
    def test_directory_enumeration_really_overlaps_and_respects_worker_limit(self):
        for i in range(8):
            self.file(f"Film{i}/movie.mkv")
        barrier, lock = threading.Barrier(2, timeout=5), threading.Lock()
        active, peak = 0, 0
        def read(folder, excluded):
            nonlocal active, peak
            if folder == self.source:
                return _read_directory(folder, excluded)
            with lock:
                active += 1; peak = max(peak, active)
            try:
                barrier.wait()
                return _read_directory(folder, excluded)
            finally:
                with lock:
                    active -= 1
        with patch("bridge.paths._read_directory", side_effect=read):
            files = list(walk(self.source, workers=2))
        self.assertEqual(peak, 2)
        self.assertEqual(len(files), 9)  # Includes the source identity marker.

    def test_shared_parent_creation_and_notification_records_are_race_free(self):
        self.config["sync"]["file_workers"] = 3
        self.config["servers"][1].update(enabled=True, token="test-token")
        for i in range(12):
            self.file(f"Shared/Season/{i:02}.mkv")
        original = os.symlink
        barrier, lock = threading.Barrier(3, timeout=5), threading.Lock()
        active, peak = 0, 0
        def create(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            try:
                barrier.wait()
                return original(*args, **kwargs)
            finally:
                with lock:
                    active -= 1
        with patch("bridge.engine.os.symlink", side_effect=create):
            result = self.engine.scan(self.lib)
        self.assertEqual(peak, 3)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["change_count"], 12)
        self.assertEqual(len(self.state.rows("pt")), 12)
        self.assertEqual(len(self.state.query("SELECT * FROM notices")), 12)
        self.assertEqual(len(self.state.query("SELECT * FROM dirs")), 2)
        self.assertEqual(list(self.output.rglob(".bridge-*.tmp")), [])
        self.assertEqual(self.engine.scan(self.lib)["change_count"], 0)

    def test_metadata_failure_does_not_corrupt_other_worker_results(self):
        for i in range(12):
            self.file(f"Film/{i:02}.nfo", f"metadata-{i}".encode())
        original = Path.open
        def read(path, *args, **kwargs):
            if path == self.source/"Film/05.nfo" and args and args[0] == "rb":
                raise PermissionError("unavailable metadata")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", read):
            result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 11)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("05.nfo", result["errors"][0])
        self.assertEqual(len(self.state.rows("pt")), 11)
        self.assertEqual(list(self.output.rglob(".bridge-*.tmp")), [])

    def test_parallel_metadata_verification_preserves_local_edits(self):
        self.config["sync"]["metadata_verify_seconds"] = 0
        for i in range(8):
            self.file(f"{i}.nfo", b"original")
        self.engine.scan(self.lib)
        (self.output/"0.nfo").write_bytes(b"edited locally")
        result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual((self.output/"0.nfo").read_bytes(), b"edited locally")

    def test_mount_loss_stops_other_workers_before_commit_and_never_cleans(self):
        old = self.file("old.mkv"); self.engine.scan(self.lib)
        old.unlink(); self.engine.scan(self.lib, cleanup=True)  # Start the deletion grace period.
        for i in range(8):
            self.file(f"new/{i}.mkv")
        identity = ["original"]
        self.lib["mount_root"] = str(self.source)
        original = os.symlink
        def replace_mount(*args, **kwargs):
            identity[0] = "replacement"
            return original(*args, **kwargs)
        with patch("bridge.mounts.mount_identity", side_effect=lambda _: identity[0]):
            with patch("bridge.engine.os.symlink", side_effect=replace_mount):
                with self.assertRaisesRegex(OSError, "挂载身份"):
                    self.engine.scan(self.lib, cleanup=True)
        self.assertTrue(os.path.lexists(self.output/"old.mkv"))
        self.assertFalse(list(self.output.glob("new/*.mkv")))
        self.assertEqual(list(self.output.rglob(".bridge-*.tmp")), [])

    def test_cleanup_waits_until_all_file_workers_finish(self):
        old = self.file("old.mkv"); self.engine.scan(self.lib)
        old.unlink(); self.engine.scan(self.lib, cleanup=True)
        for i in range(8):
            self.file(f"new/{i}.mkv")
        completed, lock = [], threading.Lock()
        original, remove = os.symlink, self.state.remove
        def create(*args, **kwargs):
            result = original(*args, **kwargs)
            with lock:
                completed.append(args[0])
            return result
        def checked_remove(*args, **kwargs):
            self.assertEqual(len(completed), 8)
            self.assertEqual(len(self.state.rows("pt")), 9)
            return remove(*args, **kwargs)
        with patch("bridge.engine.os.symlink", side_effect=create), patch.object(self.state, "remove", side_effect=checked_remove):
            result = self.engine.scan(self.lib, cleanup=True)
        self.assertEqual(result["change_count"], 9)
        self.assertFalse(os.path.lexists(self.output/"old.mkv"))

    def test_file_directory_collision_rejected_before_any_parallel_write(self):
        self.lib["mode"] = "strm"
        self.file("Movie.mkv"); self.file("movie.strm/episode.mkv")
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.engine.scan(self.lib)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.rows("pt"))

    def test_one_worker_uses_the_same_results_as_parallel_processing(self):
        for i in range(8):
            self.file(f"Film/{i}.mkv"); self.file(f"Film/{i}.nfo", b"metadata")
        single = copy.deepcopy(self.lib); single.update(id="single", output=str(self.root/"single"), sentinel=".single-ready")
        self.engine.initialize(single)
        self.config["sync"].update(scan_workers=1, file_workers=1)
        first = self.engine.scan(single)
        self.config["sync"].update(scan_workers=4, file_workers=4)
        second = self.engine.scan(self.lib)
        self.assertEqual(first["changes"], second["changes"])
        self.assertEqual(first["errors"], second["errors"])
        for row in self.state.rows("pt"):
            a, b = self.output/row["dest"], self.root/"single"/row["dest"]
            if a.is_symlink():
                self.assertEqual(os.readlink(a), os.readlink(b))
            else:
                self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_worker_config_validation_and_existing_config_migration(self):
        legacy = copy.deepcopy(self.config)
        for key in ("scan_workers", "file_workers"):
            legacy["sync"].pop(key)
        atomic_json(self.data/"config.json", legacy)
        migrated = load(self.data/"config.json")
        self.assertEqual(migrated["sync"]["scan_workers"], 4)
        self.assertEqual(migrated["sync"]["file_workers"], 4)
        for key in ("scan_workers", "file_workers"):
            for invalid in (0, -1, 33, True, 2.5, "4"):
                config = copy.deepcopy(migrated); config["sync"][key] = invalid
                with self.subTest(key=key, value=invalid), self.assertRaises(ValueError):
                    validate(config, self.data)


class BoundedQueueTests(unittest.TestCase):
    def test_input_iterator_is_not_eagerly_consumed(self):
        consumed = []
        def items():
            for i in range(1000):
                consumed.append(i)
                yield i
        mapped = bounded_map(lambda i: i, items(), workers=3)
        try:
            self.assertEqual(next(mapped), 0)
            self.assertEqual(len(consumed), 6)
        finally:
            mapped.close()


if __name__ == "__main__":
    unittest.main()
