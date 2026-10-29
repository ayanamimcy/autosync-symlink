import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import posixpath
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from bridge.arr_import.cloud import Cloud, OperationRejected, file_info, number
from bridge.arr_import.config import load, route
from bridge.arr_import.service import Importer, Server, Store
from bridge.protocol import frame, text_field
from scripts import arr_import as hook


def config():
    return {"enabled": True, "host": "127.0.0.1", "port": 8096,
            "token": "service-secret-" * 3,
            "cloud": {"url": "http://127.0.0.1:19798", "token": "cloud-secret"},
            "account_root": "/115", "staging_root": "/115/.arr-import",
            "poll_seconds": 1, "settle_seconds": 2, "timeout_seconds": 100, "max_errors": 3,
            "rules": [{"id": "movies", "app": "radarr", "enabled": True,
                       "source_root": "/cloud/downloads", "destination_root": "/cloud/movies",
                       "cloud_source_root": "/115/downloads", "cloud_destination_root": "/115/movies"}]}


def movie(path, sha1="a" * 40, size=100):
    return {"path": path, "name": posixpath.basename(path), "id": "cloud-" + path,
            "directory": False, "local": False, "size": size, "hashes": {2: sha1}}


class FakeCloud:
    def __init__(self):
        self.files = {"/115/downloads/release.mkv": movie("/115/downloads/release.mkv")}
        self.tasks = []
        self.calls = []
        self.move_timeout = False
        self.delayed_copy = False

    def get_file(self, path):
        return copy.deepcopy(self.files.get(path))

    def uploads(self, path):
        return copy.deepcopy(self.tasks)

    def mkdir(self, path, account):
        self.calls.append(("mkdir", path))

    def copy(self, source, directory):
        self.calls.append(("copy", source, directory))
        if self.delayed_copy:
            return
        path = directory + "/" + posixpath.basename(source)
        self.files.setdefault(path, movie(path, self.files[source]["hashes"][2]))

    def rename(self, source, name):
        self.calls.append(("rename", source, name))
        target = posixpath.dirname(source) + "/" + name
        assert target not in self.files
        self.files[target] = movie(target, self.files.pop(source)["hashes"][2])

    def move(self, source, directory):
        self.calls.append(("move", source, directory))
        target = directory + "/" + posixpath.basename(source)
        if target not in self.files:
            self.files[target] = movie(target, self.files.pop(source)["hashes"][2])
        if self.move_timeout:
            self.move_timeout = False
            raise TimeoutError("upstream secret must not be logged")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "imports.sqlite3")
        self.addCleanup(lambda: self.store.close())
        self.cloud = FakeCloud()
        self.time = 1000
        self.cfg = config()
        self.imp = Importer(self.cfg, self.store, lambda _: self.cloud, lambda: self.time)
        self.request = {"app": "radarr", "source": "/cloud/downloads/release.mkv",
                        "destination": "/cloud/movies/Movie (2026)/Movie.mkv", "size": 100,
                        "mtime_ns": 1234, "download_id": "torrent-infohash"}

    def submit(self):
        return self.imp.submit(self.request)["id"]

    def ticks(self, n=6):
        for _ in range(n):
            self.imp.tick()
            self.time += 3

    def test_copy_rename_publish_preserves_source_and_replay_is_idempotent(self):
        original = copy.deepcopy(self.cloud.files)
        job_id = self.submit()
        self.ticks()
        self.assertEqual(self.store.get(job_id)["status"], "completed")
        self.assertEqual(self.cloud.files["/115/downloads/release.mkv"], original["/115/downloads/release.mkv"])
        self.assertIn("/115/movies/Movie (2026)/Movie.mkv", self.cloud.files)
        self.assertEqual([c[0] for c in self.cloud.calls if c[0] != "mkdir"], ["copy", "rename", "move"])
        self.assertEqual(self.imp.submit(self.request)["status"], "verifying_replay")
        self.ticks(1)
        self.assertEqual(self.store.get(job_id)["status"], "completed")
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "copy"]), 1)
        del self.cloud.files["/115/movies/Movie (2026)/Movie.mkv"]
        self.imp.submit(self.request)
        self.ticks(1)
        self.assertEqual(self.store.get(job_id)["status"], "failed")

    def test_unfinished_error_and_unknown_uploads_never_copy(self):
        job_id = self.submit()
        for state in (0, 3, 4, 6, 9, 10, 999):
            self.cloud.tasks = [{"state": state}]
            self.ticks(1)
            self.assertEqual(self.store.get(job_id)["status"], "waiting_upload")
        self.assertEqual(self.cloud.calls, [])
        self.cloud.tasks = [{"state": 5}]
        self.ticks()
        self.assertEqual(self.store.get(job_id)["status"], "completed")

    def test_missing_hash_wrong_size_or_local_placeholder_never_copy(self):
        job_id = self.submit()
        source = self.cloud.files["/115/downloads/release.mkv"]
        for key, value in (("hashes", {}), ("size", 99), ("local", True), ("id", "")):
            before = source[key]
            source[key] = value
            self.ticks(1)
            source[key] = before
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.store.get(job_id)["status"], "waiting_upload")

    def test_conflicting_target_never_overwritten_even_if_same_digest(self):
        target = "/115/movies/Movie (2026)/Movie.mkv"
        self.cloud.files[target] = movie(target)
        job_id = self.submit()
        self.ticks(1)
        self.assertEqual(self.store.get(job_id)["status"], "failed")
        self.assertEqual(self.cloud.calls, [])

    def test_restart_after_copy_and_lost_publish_response_recovers(self):
        job_id = self.submit()
        self.ticks(2)  # copy accepted and persisted
        self.assertEqual(self.store.get(job_id)["status"], "copying")
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "imports.sqlite3")
        self.imp = Importer(self.cfg, self.store, lambda _: self.cloud, lambda: self.time)
        self.cloud.move_timeout = True
        self.ticks()
        self.assertEqual(self.store.get(job_id)["status"], "completed")
        self.assertNotIn("secret", self.store.get(job_id)["message"])
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "copy"]), 1)

    def test_size_change_and_cloud_replacement_fail_after_copy(self):
        job_id = self.submit()
        self.ticks(2)
        self.cloud.files["/115/downloads/release.mkv"]["hashes"][2] = "b" * 40
        self.ticks(1)
        self.assertEqual(self.store.get(job_id)["status"], "failed")
        self.assertFalse(any(c[0] == "move" for c in self.cloud.calls))

    def test_delayed_copy_not_resubmitted_and_explicit_retry_resumes(self):
        job_id = self.submit()
        self.cloud.delayed_copy = True
        self.ticks(8)
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "copy"]), 1)
        self.time += 101
        self.ticks(1)
        self.assertEqual(self.store.get(job_id)["status"], "failed")
        self.cloud.delayed_copy = False
        self.imp.retry(job_id)
        self.ticks()
        self.assertEqual(self.store.get(job_id)["status"], "completed")

    def test_only_explicitly_disabled_matched_rules_return_native(self):
        self.cfg["rules"][0]["enabled"] = False
        self.assertEqual(self.imp.submit(self.request), {"status": "native"})
        self.assertEqual(self.store.active(), [])
        for source in ("/cloud/downloads-other/a.mkv", "/cloud/downloads/../secret.mkv", "/cloud/downloads//x.mkv"):
            with self.assertRaises(ValueError):
                self.imp.submit({**self.request, "source": source})

    def test_two_different_jobs_cannot_own_same_destination(self):
        self.submit()
        with self.assertRaises(ValueError):
            self.imp.submit({**self.request, "mtime_ns": 1235})

    def test_explicit_local_native_rule_does_not_require_cloud_paths(self):
        self.cfg["rules"].append({"id": "local", "app": "radarr", "mode": "native", "enabled": True,
                                  "source_root": "/local", "destination_root": "/cloud/movies"})
        self.assertEqual(self.imp.submit({**self.request, "source": "/local/a.mkv"}), {"status": "native"})
        self.assertEqual(self.store.active(), [])
        self.assertEqual(self.cloud.calls, [])

    def test_disabling_rule_does_not_send_existing_jobs_to_native_import(self):
        self.submit()
        self.cfg["rules"][0]["enabled"] = False
        with self.assertRaises(ValueError):
            self.imp.submit(self.request)

    def test_definite_copy_rejection_retries_but_no_success_is_reported(self):
        job_id = self.submit()
        copy_file = self.cloud.copy
        failures = [True]

        def reject_once(source, directory):
            if failures:
                failures.pop()
                raise OperationRejected("mock refusal")
            copy_file(source, directory)

        self.cloud.copy = reject_once
        self.ticks(2)
        self.assertFalse(self.store.get(job_id)["copy_requested"])
        self.assertEqual(self.store.get(job_id)["status"], "copying")
        self.ticks()
        self.assertEqual(self.store.get(job_id)["status"], "completed")

    def test_sonarr_multiple_episodes_are_independent_file_imports(self):
        self.cfg["rules"][0]["app"] = "sonarr"
        jobs = []
        for number in (1, 2):
            name = f"Show.S01E{number:02}.mkv"
            self.cloud.files["/115/downloads/" + name] = movie("/115/downloads/" + name)
            jobs.append(self.imp.submit({**self.request, "app": "sonarr",
                                        "source": "/cloud/downloads/" + name,
                                        "destination": "/cloud/movies/Show/Season 01/" + name})["id"])
        self.ticks()
        self.assertTrue(all(self.store.get(j)["status"] == "completed" for j in jobs))

    def test_http_auth_submit_status_and_retry(self):
        self.cfg["port"] = 0  # Ephemeral port for an actual local HTTP test.
        server = Server(self.imp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(HTTPError) as failure:
            urlopen(base + "/v1/imports/" + "a" * 64)
        self.assertEqual(failure.exception.code, 401)
        failure.exception.close()
        hc = {"url": base, "token": self.cfg["token"]}
        result = hook.request(hc, "/v1/imports", self.request)
        self.assertEqual(result["status"], "waiting_upload")
        self.ticks()
        self.assertEqual(hook.request(hc, "/v1/imports/" + result["id"])["status"], "completed")

    def test_actual_hook_waits_for_server_and_mount_visibility(self):
        root = Path(self.tmp.name)
        downloads, movies = root / "downloads", root / "movies"
        downloads.mkdir()
        movies.mkdir()
        source, destination = downloads / "release.mkv", movies / "Movie.mkv"
        source.write_bytes(b"fixture".ljust(100, b"x"))
        self.cfg["rules"][0].update(source_root=str(downloads), destination_root=str(movies))
        self.cfg.update(port=0, poll_seconds=0.01)
        move = self.cloud.move

        def publish(src, directory):
            move(src, directory)
            destination.write_bytes(b"fixture".ljust(100, b"x"))

        self.cloud.move = publish
        server = Server(self.imp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def worker():
            import time
            while not self.imp.stop.is_set():
                self.ticks(1)
                time.sleep(0.01)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.addCleanup(lambda: t.join(timeout=2))
        self.addCleanup(self.imp.stop.set)
        hc = {"url": f"http://127.0.0.1:{server.server_port}", "token": self.cfg["token"],
              "timeout_seconds": 5, "poll_seconds": 0.01}
        env = {"Radarr_SourcePath": str(source), "Radarr_DestinationPath": str(destination)}
        self.assertEqual(hook.run_hook(hc, str(source), str(destination), env), "MoveComplete")
        self.assertTrue(source.is_file())
        self.assertEqual(source.stat().st_size, 100)
        self.assertEqual(destination.stat().st_size, 100)


class WireTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.bad_trailer = False
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                parent.calls.append((self.path, self.rfile.read(int(self.headers["Content-Length"])), self.headers.get("Authorization")))
                payload = number(1, 1)
                trailer = b"grpc-status: 7\r\n" if parent.bad_trailer else b"grpc-status: 0\r\n"
                body = frame(payload) + b"\x80" + len(trailer).to_bytes(4, "big") + trailer
                self.send_response(200)
                self.send_header("Content-Type", "application/grpc-web+proto")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cloud = Cloud({"url": f"http://127.0.0.1:{self.server.server_port}", "token": "cloud-token"})

    def test_copy_and_move_explicitly_skip_conflicts_and_exhaust_trailer(self):
        from bridge.protocol import fields
        self.cloud.copy("/115/downloads/a.mkv", "/115/stage")
        payload = dict(fields(self.calls[0][1][5:]))
        self.assertEqual(payload[3], 2)
        self.assertTrue(self.calls[0][0].endswith("/CopyFile"))
        self.assertEqual(self.calls[0][2], "Bearer cloud-token")
        self.bad_trailer = True
        with self.assertRaises(ConnectionError):
            self.cloud.move("/115/stage/a.mkv", "/115/movies")

    def test_proto_sha1_decoding(self):
        sha = text_field(2, "a" * 40) + number(1, 2)
        data = text_field(1, "115-file-id") + text_field(2, "a.mkv") + text_field(3, "/115/a.mkv")
        data += number(4, 100) + number(5, 1)
        from bridge.protocol import varint
        data += varint(70 << 3 | 2) + varint(len(sha)) + sha
        self.assertEqual(file_info(data)["hashes"], {2: "a" * 40})


class ConfigTests(unittest.TestCase):
    def test_htpc_paths_and_explicit_native_rules(self):
        example = Path(__file__).resolve().parents[1] / "deploy/arr-import.htpc.example.json"
        c = json.loads(example.read_text())
        c.update(enabled=True, token="validation-service-secret-123456")
        c["cloud"]["token"] = "validation-cloud-secret"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(c))
            loaded = load(path)
        for app, destination, cloud_dest in (("radarr", "/movies/a.mkv", "/115open/pt/movie/a.mkv"),
                                              ("sonarr", "/tv/a.mkv", "/115open/pt/tv/a.mkv")):
            for source, cloud_source in (("/downloads/a.mkv", "/115open/pt/download/a.mkv"),
                                        ("/cloud_downloads/a.mkv", "/115open/CloudDownload/a.mkv")):
                result = route(loaded, {"app": app, "source": source, "destination": destination})
                self.assertEqual(result["source"], cloud_source)
                self.assertEqual(result["destination"], cloud_dest)
                self.assertFalse(result["native"])
            self.assertTrue(route(loaded, {"app": app, "source": "/local/a.mkv", "destination": destination})["native"])
        loaded["rules"][-1]["mode"] = "typo"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(loaded))
            with self.assertRaises(ValueError):
                load(path)

    def test_account_boundary_and_staging_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for change in (None, "cross-account", "overlap", "ambiguous"):
                c = config()
                if change == "cross-account":
                    c["rules"][0]["cloud_destination_root"] = "/115-other/movies"
                elif change == "overlap":
                    c["staging_root"] = "/115/downloads/stage"
                elif change == "ambiguous":
                    c["rules"].append({**c["rules"][0], "id": "duplicate"})
                path.write_text(json.dumps(c))
                if change:
                    with self.assertRaises(ValueError):
                        load(path)
                else:
                    loaded = load(path)
                    self.assertTrue(loaded["enabled"])


if __name__ == "__main__":
    unittest.main()
