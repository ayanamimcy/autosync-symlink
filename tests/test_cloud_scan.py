"""CD2 scan regression tests. No real CD2/115 requests are made."""
import copy
from dataclasses import replace
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from bridge.cloud import CloudClient, CloudFile, cloud_file
from bridge.cloud_scan import CloudScanError, CloudScanner, mapped_cloud_path
from bridge.config import atomic_json, load, validate
from bridge.protocol import fields, frame, text_field, varint
from bridge.service import Service
from test_bridge import Fixture


def number(key, value):
    return varint(key << 3) + varint(value)


def blob(key, value):
    return varint(key << 3 | 2) + varint(len(value)) + value


def encoded(entry):
    seconds, nanos = divmod(entry.mtime_ns, 1000000000)
    return (text_field(2, entry.name) + text_field(3, entry.path) + number(4, entry.size)
            + number(5, 2 if entry.other else (0 if entry.directory else 1))
            + blob(7, number(1, seconds % (1 << 64)) + number(2, nanos)))


def trailer(status=0):
    value = f"grpc-status: {status}\r\n".encode()
    return b"\x80" + len(value).to_bytes(4, "big") + value


class DirectoryProtocolTests(unittest.TestCase):
    def test_streamed_directory_replies_use_cache_by_default_and_close_connection(self):
        entries = [CloudFile("A", "/cloud/A", True, 0, 0),
                   CloudFile("电影.mkv", "/cloud/电影.mkv", False, 1234, 1700000000123456789)]
        response = b"".join(frame(blob(1, encoded(e))) for e in entries) + trailer()
        client, connection = CloudClient({}), Mock()
        client.open = Mock(return_value=(connection, BytesIO(response)))
        self.assertEqual(client.list_files("/cloud", "secret"), entries)
        method, request = client.open.call_args.args
        self.assertEqual(method, "GetSubFiles")
        self.assertEqual(dict(fields(request)), {1: b"/cloud", 2: 0})
        self.assertEqual(client.open.call_args.kwargs, {"token": "secret"})
        connection.close.assert_called_once()

    def test_error_or_missing_trailer_never_returns_partial_directory(self):
        entry = CloudFile("A.mkv", "/cloud/A.mkv", False, 1, 0)
        for ending, exception in ((b"", EOFError), (trailer(8), ConnectionError)):
            client, connection = CloudClient({}), Mock()
            client.open = Mock(return_value=(connection, BytesIO(frame(blob(1, encoded(entry))) + ending)))
            with self.subTest(ending=ending), self.assertRaises(exception):
                client.list_files("/cloud", "secret")
            connection.close.assert_called_once()

    def test_unsafe_paths_forbidden_entries_and_missing_timestamps_fail(self):
        for path in ("/other/A.mkv", "/cloud/../A.mkv", "/cloud//A.mkv", "/cloud/A.mkv\x00", "/cloud/B.mkv"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                cloud_file(encoded(CloudFile("A.mkv", path, False, 1, 0)), "/cloud")
        data = encoded(CloudFile("A.mkv", "/cloud/A.mkv", False, 1, 0))
        with self.assertRaises(PermissionError):
            cloud_file(data + number(36, 1), "/cloud")
        with self.assertRaises(ValueError):
            cloud_file(text_field(2, "A.mkv") + text_field(3, "/cloud/A.mkv") + number(5, 1), "/cloud")

    def test_duplicate_entries_are_not_a_complete_snapshot(self):
        entry = encoded(CloudFile("A.mkv", "/cloud/A.mkv", False, 1, 0))
        client = CloudClient({})
        client.open = Mock(return_value=(Mock(), BytesIO(frame(blob(1, entry) * 2) + trailer())))
        with self.assertRaisesRegex(ValueError, "重复"):
            client.list_files("/cloud", "token", force_refresh=True)
        self.assertEqual(dict(fields(client.open.call_args.args[1]))[2], 1)

    def test_other_file_type_is_retained_to_distinguish_presence_from_absence(self):
        entry = CloudFile("A.mkv", "/cloud/A.mkv", False, 0, 0, other=True)
        self.assertEqual(cloud_file(encoded(entry), "/cloud"), entry)


class ApiScanTests(Fixture):
    def setUp(self):
        super().setUp()
        cooldowns = patch.object(CloudScanner, "_cooldowns", {})
        cooldowns.start(); self.addCleanup(cooldowns.stop)
        self.config["sync"].update(scan_backend="clouddrive", cloud_list_interval_ms=100)
        self.config["cloud"]["token"] = "test-token"
        self.lib["cloud_path"] = "/115open/pt"

    def inventory(self):
        result = {}
        for folder, _, names in os.walk(self.source):
            relative = Path(folder).relative_to(self.source).as_posix()
            parent = self.lib["cloud_path"] + ("/" + relative if relative != "." else "")
            entries = []
            with os.scandir(folder) as items:
                for item in items:
                    st = item.stat(follow_symlinks=False)
                    entries.append(CloudFile(item.name, parent + "/" + item.name,
                                             item.is_dir(follow_symlinks=False), st.st_size, st.st_mtime_ns))
            result[parent] = entries
        return result

    @contextmanager
    def api(self, cached=None, fresh=None):
        cached = self.inventory() if cached is None else cached
        fresh = cached if fresh is None else fresh
        def listing(client, path, token, force_refresh=False):
            self.assertEqual(token, "test-token")
            value = (fresh if force_refresh else cached)[path]
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(CloudClient, "list_files", autospec=True, side_effect=listing) as rpc:
            yield rpc

    def test_api_inventory_never_walks_or_stats_video_paths_on_mount(self):
        video = self.file("Season/episode.RMVB")
        cached, snapshots = self.inventory(), []
        self.engine.scan_progress = snapshots.append
        original_stat, original_open = Path.stat, Path.open
        def stat(path, *args, **kwargs):
            if path in (video, video.parent):
                raise AssertionError("video/parent source stat is forbidden")
            return original_stat(path, *args, **kwargs)
        def open_file(path, *args, **kwargs):
            if path == video:
                raise AssertionError("video read is forbidden")
            return original_open(path, *args, **kwargs)
        with self.api(cached) as rpc, patch("bridge.engine.walk", side_effect=AssertionError("filesystem walk")):
            with patch("os.scandir", side_effect=AssertionError("source enumeration")):
                with patch.object(Path, "stat", stat), patch.object(Path, "open", open_file):
                    first = self.engine.scan(self.lib)
                    second = self.engine.scan(self.lib)
        self.assertEqual(first["scan_backend"], "clouddrive")
        self.assertEqual(first["change_count"], 1)
        self.assertEqual(second["change_count"], 0)
        self.assertEqual(os.readlink(self.output/"Season/episode.RMVB"), str(video))
        self.assertTrue(any(p["stage"] == "CD2 API 读取目录" for p in snapshots))
        self.assertTrue(all(not c.kwargs["force_refresh"] for c in rpc.call_args_list))

    def test_metadata_copy_signatures_match_filesystem_backend(self):
        self.file("电影/片名.mkv"); self.file("电影/movie.nfo", b"<movie/>")
        with self.api():
            result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 2)
        self.assertFalse(result["errors"])
        self.assertEqual((self.output/"电影/movie.nfo").read_bytes(), b"<movie/>")
        self.config["sync"]["scan_backend"] = "filesystem"
        self.assertEqual(self.engine.scan(self.lib)["change_count"], 0)

    def test_incomplete_directory_aborts_before_any_file_write_or_cleanup(self):
        self.file("A/old.mkv")
        with self.api(): self.engine.scan(self.lib)
        self.file("B/new.mkv")
        cached = self.inventory()
        cached["/115open/pt/A"] = ConnectionError("CloudDrive HTTP 429")
        with self.api(cached) as rpc, patch("bridge.engine.walk") as filesystem:
            with self.assertRaisesRegex(CloudScanError, "429"):
                self.engine.scan(self.lib, cleanup=True)
        filesystem.assert_not_called()
        self.assertFalse(os.path.lexists(self.output/"B/new.mkv"))
        self.assertTrue(os.path.lexists(self.output/"A/old.mkv"))
        self.assertTrue(all(r["missing_since"] is None for r in self.state.rows("pt")))
        self.assertLessEqual(rpc.call_count, 3)

    def test_cache_omission_is_refreshed_and_does_not_delete_live_link(self):
        self.file("Film/old.mkv")
        with self.api(): self.engine.scan(self.lib)
        fresh = self.inventory(); cached = copy.deepcopy(fresh)
        cached["/115open/pt/Film"] = []
        self.state.execute("UPDATE files SET missing_since=1")
        with self.api(cached, fresh) as rpc:
            result = self.engine.scan(self.lib, cleanup=True)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["change_count"], 0)
        self.assertTrue(os.path.lexists(self.output/"Film/old.mkv"))
        self.assertIsNone(self.state.rows("pt")[0]["missing_since"])
        refreshes = [c.args[1] for c in rpc.call_args_list if c.kwargs["force_refresh"]]
        self.assertEqual(refreshes, ["/115open/pt", "/115open/pt/Film"])

    def test_deleted_subtree_requires_fresh_confirmation_on_each_observation(self):
        old = self.file("Gone/old.mkv")
        self.file("Keep/live.mkv")
        with self.api(): self.engine.scan(self.lib)
        old.unlink(); old.parent.rmdir()
        with self.api() as rpc:
            first = self.engine.scan(self.lib, cleanup=True)
            self.assertTrue(os.path.lexists(self.output/"Gone/old.mkv"))
            second = self.engine.scan(self.lib, cleanup=True)
        self.assertEqual(first["changes"], [{"action":"开始缺失宽限期", "path":"Gone/old.mkv"}])
        self.assertEqual(second["changes"], [{"action":"删除", "path":"Gone/old.mkv"}])
        self.assertFalse(os.path.lexists(self.output/"Gone/old.mkv"))
        self.assertEqual([c.args[1] for c in rpc.call_args_list if c.kwargs["force_refresh"]],
                         ["/115open/pt", "/115open/pt"])

    def test_refresh_failure_does_not_start_or_finish_any_deletion_grace(self):
        old = self.file("Film/old.mkv")
        with self.api(): self.engine.scan(self.lib)
        old.unlink()
        cached = self.inventory(); fresh = copy.deepcopy(cached)
        fresh["/115open/pt/Film"] = ConnectionError("CloudDrive gRPC 状态 8")
        for missing_since in (None, 1):
            self.state.execute("UPDATE files SET missing_since=?", (missing_since,))
            with self.api(cached, fresh), self.assertRaises(CloudScanError):
                self.engine.scan(self.lib, cleanup=True)
            self.assertEqual(self.state.rows("pt")[0]["missing_since"], missing_since)
            self.assertTrue(os.path.lexists(self.output/"Film/old.mkv"))

    def test_exclusions_scopes_and_strm_paths(self):
        self.lib.update(mode="strm", strm_prefix="http://media.test")
        self.file("Movie/片 名.mkv"); self.file("_trash/old.mkv"); self.file("Other/keep.mkv")
        with self.api() as rpc:
            result = self.engine.scan(self.lib, scope="Movie")
            self.assertEqual([c.args[1] for c in rpc.call_args_list], ["/115open/pt", "/115open/pt/Movie"])
        self.assertEqual(result["seen"], 1)
        self.assertEqual((self.output/"Movie/片 名.strm").read_text(), "http://media.test/Movie/%E7%89%87%20%E5%90%8D.mkv\n")
        with self.api() as rpc:
            self.engine.scan(self.lib)
            self.assertNotIn("/115open/pt/_trash", [c.args[1] for c in rpc.call_args_list])

    def test_missing_health_marker_in_api_result_rejects_mapping(self):
        self.file("A.mkv"); cached = self.inventory()
        cached["/115open/pt"] = [e for e in cached["/115open/pt"] if e.name != self.lib["sentinel"]]
        with self.api(cached), self.assertRaisesRegex(CloudScanError, "健康标记"):
            self.engine.scan(self.lib)
        self.assertFalse(self.output.exists())

    def test_deleted_scoped_directory_does_not_affect_other_library_paths(self):
        old = self.file("Gone/old.mkv"); self.file("Keep/live.mkv")
        with self.api(): self.engine.scan(self.lib)
        old.unlink(); old.parent.rmdir()
        with self.api():
            self.engine.scan(self.lib, scope="Gone", cleanup=True)
            self.engine.scan(self.lib, scope="Gone", cleanup=True)
        self.assertFalse(os.path.lexists(self.output/"Gone/old.mkv"))
        self.assertTrue(os.path.lexists(self.output/"Keep/live.mkv"))

    def test_newly_excluded_directories_are_neither_refreshed_nor_cleaned(self):
        self.file("Excluded/old.mkv")
        with self.api(): self.engine.scan(self.lib)
        self.config["sync"]["exclude_directories"].append("Excluded")
        with self.api() as rpc:
            result = self.engine.scan(self.lib, cleanup=True)
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(result["change_count"], 0)
        self.assertTrue(os.path.lexists(self.output/"Excluded/old.mkv"))
        self.assertFalse(any(c.kwargs["force_refresh"] for c in rpc.call_args_list))

    def test_other_file_type_never_counts_as_freshly_confirmed_absence(self):
        self.file("old.mkv")
        with self.api(): self.engine.scan(self.lib)
        cached = self.inventory()
        cached["/115open/pt"] = [replace(e, other=True) if e.name == "old.mkv" else e for e in cached["/115open/pt"]]
        with self.api(cached), self.assertRaisesRegex(CloudScanError, "类型不受支持"):
            self.engine.scan(self.lib, cleanup=True)
        self.assertTrue(os.path.lexists(self.output/"old.mkv"))
        self.assertIsNone(self.state.rows("pt")[0]["missing_since"])

    def test_shutdown_interrupts_rate_wait_without_next_rpc(self):
        def stopped(*_):
            raise InterruptedError("stopping")
        scanner = CloudScanner(self.config, self.lib, stopped)
        scanner.next_request = time.monotonic() + 60
        with patch.object(CloudClient, "list_files") as rpc, self.assertRaises(InterruptedError):
            scanner.listing(scanner.root)
        rpc.assert_not_called()

    def test_rate_limit_cooldown_is_shared_across_new_scans_and_expires(self):
        clock = [100]
        with patch("bridge.cloud_scan.time.monotonic", side_effect=lambda: clock[0]):
            with patch.object(CloudClient, "list_files", side_effect=ConnectionError("CloudDrive HTTP 429")) as rpc:
                scanner = CloudScanner(self.config, self.lib, lambda *_: None)
                with self.assertRaisesRegex(CloudScanError, "429"):
                    scanner.listing(scanner.root)
                with self.assertRaisesRegex(CloudScanError, "冷却"):
                    CloudScanner(self.config, self.lib, lambda *_: None)
                rpc.assert_called_once()
                clock[0] = 161
                CloudScanner(self.config, self.lib, lambda *_: None)

    def test_primary_rpc_error_is_preserved_when_peer_worker_cancels(self):
        self.file("A/movie.mkv"); self.file("B/movie.mkv")
        entries = self.inventory()
        scanner = CloudScanner(self.config, self.lib, lambda *_: None)
        def listing(client, path, token, force_refresh=False):
            if path == scanner.root: return entries[path]
            raise ConnectionError("CloudDrive HTTP 429")
        with patch.object(CloudClient, "list_files", autospec=True, side_effect=listing):
            with self.assertRaisesRegex(CloudScanError, "429"):
                list(scanner.walk())

    def test_rate_limit_spacing_and_bounded_concurrency(self):
        for i in range(4): self.file(f"{i}/movie.mkv")
        entries, starts, lock = self.inventory(), [], threading.Lock()
        scanner = CloudScanner(self.config, self.lib, lambda *_: None)
        active, peak = 0, 0
        def listing(client, path, token, force_refresh=False):
            nonlocal active, peak
            with lock:
                starts.append(time.monotonic()); active += 1; peak = max(peak, active)
            try:
                if path != scanner.root: time.sleep(.18)
                return entries[path]
            finally:
                with lock: active -= 1
        with patch.object(CloudClient, "list_files", autospec=True, side_effect=listing):
            self.assertEqual(len(list(scanner.walk())), 5)  # Four videos plus marker.
        self.assertEqual(peak, 2)
        self.assertTrue(all(b-a >= .09 for a,b in zip(starts, starts[1:])))

    def test_real_http_grpc_web_round_trip_and_service_progress(self):
        self.file("Film/movie.mkv"); self.file("Film/movie.nfo", b"<movie/>")
        entries, requests = self.inventory(), []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                f = dict(fields(body[5:])); path = f[1].decode()
                requests.append((self.path, path, f[2], self.headers.get("Authorization")))
                data = b"".join(frame(blob(1, encoded(e))) for e in entries[path]) + trailer()
                self.send_response(200); self.send_header("Content-Type", "application/grpc-web+proto")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            self.config["cloud"]["url"] = f"http://127.0.0.1:{server.server_port}"
            atomic_json(self.data/"config.json", self.config)
            service = Service(self.data)
            try:
                result = service.perform("scan", "pt")[0]
                status = service.status()["libraries"][0]["scan"]
            finally:
                service.state.db.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
        self.assertEqual(result["change_count"], 2)
        self.assertEqual(status["latest"]["status"], "complete")
        self.assertIsNotNone(status["last_full_success"])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r[0].endswith("/GetSubFiles") and r[2] == 0 and r[3] == "Bearer test-token" for r in requests))


class ApiConfigTests(Fixture):
    def test_existing_configuration_migrates_without_switching_backend(self):
        for key in ("scan_backend", "cloud_list_workers", "cloud_list_interval_ms"):
            del self.config["sync"][key]
        atomic_json(self.data/"config.json", self.config)
        config = load(self.data/"config.json")
        self.assertEqual(config["sync"]["scan_backend"], "filesystem")
        for key, bad in (("scan_backend", "auto"), ("cloud_list_workers", 5),
                         ("cloud_list_workers", True), ("cloud_list_interval_ms", 0),
                         ("cloud_list_interval_ms", 60001), ("cloud_list_interval_ms", 0.5)):
            c = copy.deepcopy(config); c["sync"][key] = bad
            with self.subTest(key=key, value=bad), self.assertRaises(ValueError): validate(c)

    def test_mount_mapping_uses_longest_match_and_respects_boundaries(self):
        mounts = [{"mount":"/cloud", "source":"/115", "mounted":True},
                  {"mount":"/cloud/movies", "source":"/115open/pt", "mounted":True}]
        self.assertEqual(mapped_cloud_path({"source":"/cloud/movies/Film"}, mounts), "/115open/pt/Film")
        with self.assertRaises(ValueError): mapped_cloud_path({"source":"/cloud-old/Film"}, mounts)
        self.assertEqual(mapped_cloud_path({"source":r"M:\pt\Film"},
                         [{"mount":"M:\\", "source":"/115", "mounted":True}]), "/115/pt/Film")


if __name__ == "__main__": unittest.main()
