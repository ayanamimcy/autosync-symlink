"""Fault injection for the HTTP boundary; independent of full download success."""
import concurrent.futures
from collections import Counter
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from bridge.arr_download.clients import QB, UpstreamError
from bridge.arr_download.read_cache import AppReadCache
from bridge.arr_download.service import Downloader, Store
from bridge.arr_download.web import Server
from test_arr_download import config, torrent, FakeQB, FakeCloud


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.cache = AppReadCache(clock=lambda: self.now)

    def test_cached_versions_need_no_upstream_request(self):
        calls = []
        def fetch():
            calls.append(1)
            return b"2.11.5", "text/plain"
        self.assertEqual(self.cache.get("app/webapiVersion", fetch)[2], "live")
        self.now = 60
        self.assertEqual(self.cache.get("app/webapiVersion", fetch)[2], "hit")
        self.assertEqual(len(calls), 1)

    def test_bounded_stale_settings_then_failure_and_recovery(self):
        value = (b'{"save_path":"/downloads"}', "application/json")
        self.cache.get("app/preferences", lambda: value)
        calls = []
        def fail():
            calls.append(1)
            raise UpstreamError("qB timeout")
        self.now = 6
        self.assertEqual(self.cache.get("app/preferences", fail)[2], "stale")
        self.assertEqual(self.cache.get("app/preferences", fail)[2], "stale")
        self.assertEqual(len(calls), 1)  # Failure cooldown prevents a request storm.
        self.now = 66
        with self.assertRaises(UpstreamError):
            self.cache.get("app/preferences", fail)
        self.now = 70
        self.assertEqual(self.cache.get("app/preferences", lambda: value)[2], "live")
        self.assertFalse(self.cache.diagnostics()["app/preferences"]["refresh_failed"])

    def test_cold_failure_never_invents_version_and_torrent_data_is_not_cacheable(self):
        def fail():
            raise UpstreamError("upstream unavailable")
        with self.assertRaises(UpstreamError):
            self.cache.get("app/version", fail)
        self.assertEqual(self.cache.diagnostics(), {})
        for key in ("torrents/info", "torrents/files", "torrents/add"):
            with self.assertRaises(KeyError):
                self.cache.get(key, lambda: (b"data", "text/plain"))

    def test_refresh_does_not_block_existing_snapshot(self):
        self.cache.get("app/preferences", lambda: (b"prefs", "text/plain"))
        self.now = 6
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(3)
            return b"new", "text/plain"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.cache.get, "app/preferences", slow)
            self.assertTrue(entered.wait(1))
            try:
                second = pool.submit(self.cache.get, "app/preferences", slow)
                self.assertEqual(second.result(timeout=0.5), (b"prefs", "text/plain", "stale"))
            finally:
                release.set()
            self.assertEqual(first.result()[0], b"new")


class HTTPResilienceTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.calls_lock = Counter(), threading.Lock()
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.delay_path, self.delay_body, self.error_paths = None, False, set()
        self.payloads = {}
        self.sid, self.auth_barrier = "session-1", None
        outer = self

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, payload, cookie=None):
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                if self.path == outer.delay_path and outer.delay_body:
                    self.wfile.flush()
                    outer.entered.set()
                    outer.release.wait(3)
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with outer.calls_lock:
                    outer.calls[self.path] += 1
                if self.path == "/api/v2/auth/login":
                    self.respond(200, b"Ok.", "SID=" + outer.sid + "; Path=/")
                else:
                    # Simulate a write applied upstream with its response lost.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()

            def do_GET(self):
                with outer.calls_lock:
                    outer.calls[self.path] += 1
                if self.headers.get("Cookie") != "SID=" + outer.sid:
                    if outer.auth_barrier:
                        outer.auth_barrier.wait(3)
                    self.respond(403, b"denied")
                    return
                if self.path == outer.delay_path and not outer.delay_body:
                    outer.entered.set()
                    outer.release.wait(3)
                if self.path in outer.error_paths:
                    self.respond(500, b"DO-NOT-EXPOSE-TOKEN")
                    return
                bodies = {"/api/v2/app/version": b"v5.1.4", "/api/v2/app/webapiVersion": b"2.11.5",
                          "/api/v2/app/preferences": b'{"save_path":"/downloads","web_ui_password":"DO-NOT-CACHE"}',
                          "/api/v2/torrents/info": b"[]"}
                bodies.update(outer.payloads)
                self.respond(200, bodies.get(self.path, b"ok"))

        class BackendServer(ThreadingHTTPServer):
            request_queue_size = 128
        self.backend = BackendServer(("127.0.0.1", 0), Backend)
        self.backend.daemon_threads = True
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.addCleanup(self.backend.server_close)
        self.addCleanup(self.backend.shutdown)
        self.addCleanup(self.release.set)
        self.cfg = config()
        self.cfg["qb"]["url"] = f"http://127.0.0.1:{self.backend.server_port}"
        self.qb = QB(self.cfg["qb"])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "downloads.sqlite3")
        self.addCleanup(self.store.close)
        self.d = Downloader(self.cfg, self.store, self.qb, lambda _: FakeCloud())
        self.server = Server(self.d)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        data = urlencode({"username": self.cfg["username"], "password": self.cfg["password"]}).encode()
        with self.opener.open(Request(self.base + "/api/v2/auth/login", data=data), timeout=3) as response:
            self.assertEqual(response.read(), b"Ok.")

    def request(self, path):
        with self.opener.open(self.base + path, timeout=3) as response:
            return response.read(), response.headers.get("X-Arr-App-Cache")

    def test_slow_upstream_read_does_not_serialize_other_requests_or_create_sessions(self):
        self.qb.raw("/api/v2/app/version")
        self.delay_path = "/api/v2/slow"
        self.release.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(self.qb.raw, self.delay_path)
            self.assertTrue(self.entered.wait(1))
            try:
                fast = pool.submit(self.qb.raw, "/api/v2/app/webapiVersion")
                self.assertEqual(fast.result(timeout=0.5)[0], b"2.11.5")
            finally:
                self.release.set()
            slow.result(timeout=2)
        self.assertEqual(self.calls["/api/v2/auth/login"], 1)

    def test_parallel_expired_sessions_refresh_authentication_once(self):
        self.qb.raw("/api/v2/app/version")
        self.sid, self.auth_barrier = "session-2", threading.Barrier(8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.qb.raw("/api/v2/app/version")[0], range(8)))
        self.assertEqual(results, [b"v5.1.4"] * 8)
        self.assertEqual(self.calls["/api/v2/auth/login"], 2)

    def test_read_header_and_body_timeouts_identify_stage_without_leaking_response(self):
        self.qb.raw("/api/v2/app/version")
        self.qb.config["timeout_seconds"] = 0.1
        for body in (False, True):
            with self.subTest(body=body):
                self.entered.clear()
                self.release.clear()
                self.delay_path, self.delay_body = "/api/v2/slow", body
                with self.assertLogs("bridge.arr_download.clients", level="WARNING") as logs:
                    with self.assertRaises(UpstreamError) as failure:
                        self.qb.raw(self.delay_path)
                self.assertIn("TimeoutError", str(failure.exception))
                expected = "response_body" if body else "response_headers"
                self.assertEqual(self.qb.diagnostics()["last_error"]["stage"], expected)
                self.assertIn(expected, str(failure.exception))
                self.assertNotIn("qb-secret", str(logs.output))
                self.release.set()
        self.assertEqual(self.qb.diagnostics()["failures"], 2)

    def test_lost_mutation_response_is_never_replayed(self):
        with self.assertLogs("bridge.arr_download.clients", level="WARNING"):
            with self.assertRaises(UpstreamError):
                self.qb.post("torrents/add", urls="magnet:?DO-NOT-LOG")
        self.assertEqual(self.calls["/api/v2/torrents/add"], 1)
        self.assertEqual(self.calls["/api/v2/auth/login"], 1)

    def test_proxy_version_cache_handles_bursts_without_upstream_request_storm(self):
        self.delay_path = "/api/v2/app/webapiVersion"
        self.release.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.request, "/api/v2/app/webapiVersion") for _ in range(32)]
            try:
                self.assertTrue(self.entered.wait(1))
            finally:
                self.release.set()
            results = [future.result() for future in futures]
        self.assertTrue(all(value[0] == b"2.11.5" for value in results))
        self.assertEqual(sum(value[1] == "live" for value in results), 1)
        self.assertEqual(self.calls["/api/v2/app/webapiVersion"], 1)

    def test_invalid_http_200_app_payloads_are_not_cached_or_reported_as_bad_user_settings(self):
        for path, payload in (("app/webapiVersion", b"<html>upstream error</html>"),
                              ("app/preferences", b"not-json"), ("app/preferences", b"[]")):
            with self.subTest(path=path, payload=payload):
                self.server.app_cache = AppReadCache()
                self.payloads["/api/v2/" + path] = payload
                with self.assertRaises(HTTPError) as failure:
                    self.request("/api/v2/" + path)
                self.assertEqual(failure.exception.code, 503)
                failure.exception.close()
                self.assertEqual(self.server.app_cache.diagnostics(), {})

    def test_proxy_stale_preferences_are_bounded_redacted_and_queue_errors_remain_real(self):
        now = [0]
        self.server.app_cache.clock = lambda: now[0]
        payload, status = self.request("/api/v2/app/preferences")
        self.assertNotIn(b"DO-NOT-CACHE", payload)
        self.assertEqual(status, "live")
        now[0] = 6
        self.error_paths = {"/api/v2/app/preferences", "/api/v2/torrents/info"}
        with self.assertLogs("bridge.arr_download.clients", level="WARNING"):
            stale, status = self.request("/api/v2/app/preferences")
            self.assertEqual((stale, status), (payload, "stale"))
            with self.assertRaises(HTTPError) as failure:
                self.request("/api/v2/torrents/info")
            self.assertEqual(failure.exception.code, 503)
            failure.exception.close()
            now[0] = 66
            with self.assertRaises(HTTPError) as expired:
                self.request("/api/v2/app/preferences")
            self.assertEqual(expired.exception.code, 503)
            expired.exception.close()
        raw, _ = self.request("/v1/diagnostics")
        self.assertNotIn(b"DO-NOT", raw)
        self.assertNotIn(b"qb-secret", raw)
        self.assertGreater(json.loads(raw)["qb"]["failures"], 0)

    def test_cold_proxy_failure_does_not_advertise_fabricated_version(self):
        self.error_paths.add("/api/v2/app/webapiVersion")
        with self.assertLogs("bridge.arr_download.clients", level="WARNING"):
            with self.assertRaises(HTTPError) as failure:
                self.request("/api/v2/app/webapiVersion")
        self.assertEqual(failure.exception.code, 503)
        self.assertNotIn(b"DO-NOT", failure.exception.read())
        failure.exception.close()

    def test_cd2_worker_delay_does_not_block_torrent_queue_rendering(self):
        qb, cloud = FakeQB(), FakeCloud()
        self.d.qb = qb
        self.d.cloud_factory = lambda _: cloud
        self.d.submit({"category": "radarr"}, torrent())
        for _ in range(3):
            self.d.tick()
        entered, release = threading.Event(), threading.Event()
        def blocked(_):
            entered.set()
            release.wait(3)
            return []
        cloud.offline = blocked
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(self.d.tick)
            self.assertTrue(entered.wait(1))
            try:
                pending = pool.submit(self.request, "/api/v2/torrents/info")
                rows = json.loads(pending.result(timeout=0.5)[0])
                self.assertEqual(len(rows), 1)
                self.assertLess(rows[0]["progress"], 1)
            finally:
                release.set()
            worker.result(timeout=2)
