import unittest
"""Real HTTP/gRPC-Web metadata copying without mounted-source file reads."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import posixpath
import threading
from urllib.parse import parse_qs, quote, urlsplit
from unittest.mock import Mock, patch

from bridge.cloud import CloudFile
from bridge.cloud_scan import CloudScanner
from bridge.cloud_content import CloudContent, CloudContentError
from bridge.engine import METADATA
from bridge.protocol import fields, frame, text_field
from test_bridge import Fixture
from test_cloud_scan import blob, encoded, trailer


class ContentTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config["sync"].update(scan_backend="clouddrive", cloud_list_interval_ms=100)
        self.config["cloud"]["token"] = "api-bearer-secret"
        self.lib["cloud_path"] = "/115open/pt"
        cooldowns = patch.object(CloudScanner, "_cooldowns", {})
        cooldowns.start(); self.addCleanup(cooldowns.stop)
        delays = patch("bridge.cloud_content.RETRY_DELAYS", (0, 0, 0, 0, 0))
        delays.start(); self.addCleanup(delays.stop)
        self.calls, self.gets, self.lock = [], [], threading.Lock()
        self.http_status, self.override_length, self.body_suffix = 200, None, b""
        self.omit_length, self.encoding, self.changed, self.no_proxy_url = False, "", False, False
        self.rpc_error, self.barrier = None, None
        self.active, self.peak = 0, 0
        self.origin_prefix = ""

    def snapshot(self):
        self.entries, self.files, self.contents = {}, {}, {}
        for folder, _, _ in os.walk(self.source):
            relative = Path(folder).relative_to(self.source).as_posix()
            parent = self.lib["cloud_path"] + ("/"+relative if relative != "." else "")
            children = []
            with os.scandir(folder) as entries:
                for entry in entries:
                    st = entry.stat(follow_symlinks=False)
                    info = CloudFile(entry.name, parent+"/"+entry.name,
                                     entry.is_dir(follow_symlinks=False), st.st_size, st.st_mtime_ns)
                    children.append(info)
                    self.files[info.path] = info
                    if not info.directory and Path(entry.name).suffix.lower() in METADATA:
                        self.contents[info.path] = Path(entry.path).read_bytes()
            self.entries[parent] = children

    @contextmanager
    def cd2(self):
        self.snapshot()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, data, status=200, headers=None):
                self.send_response(status)
                for name, value in (headers or {}).items(): self.send_header(name, str(value))
                self.end_headers()
                try: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                f = dict(fields(raw[5:])); method = self.path.rsplit("/", 1)[-1]
                path = f[1].decode()
                with fixture.lock:
                    fixture.calls.append((method, path, f, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer api-bearer-secret":
                    data = trailer(16)
                elif method == "GetSubFiles":
                    data = b"".join(frame(blob(1, encoded(e))) for e in fixture.entries[path]) + trailer()
                elif fixture.rpc_error is not None:
                    data = trailer(fixture.rpc_error)
                elif method == "GetDownloadUrlPath":
                    location = "/static/{SCHEME}/{HOST}/{PREVIEW}/file?token=signed-url-secret&path=" + quote(path, safe="")
                    data = frame((b"" if fixture.no_proxy_url else text_field(1, location))
                                 + text_field(3, "https://outside.invalid/?secret=direct-url-secret")) + trailer()
                elif method == "FindFileByPath":
                    info = fixture.files[posixpath.join(path, f[2].decode())]
                    if fixture.changed: info = replace(info, mtime_ns=info.mtime_ns+1)
                    data = frame(encoded(info)) + trailer()
                else:
                    data = trailer(12)
                self.reply(data, headers={"Content-Type":"application/grpc-web+proto", "Content-Length":len(data)})
            def do_GET(self):
                path = parse_qs(urlsplit(self.path).query)["path"][0]
                with fixture.lock:
                    fixture.gets.append((self.path, path, self.headers.get("Authorization")))
                    fixture.active += 1; fixture.peak = max(fixture.peak, fixture.active)
                try:
                    if fixture.barrier: fixture.barrier.wait(timeout=5)
                    payload = fixture.contents[path] + fixture.body_suffix
                    headers = {"Connection":"close"}
                    if not fixture.omit_length:
                        headers["Content-Length"] = len(payload) if fixture.override_length is None else fixture.override_length
                    if fixture.encoding: headers["Content-Encoding"] = fixture.encoding
                    if fixture.http_status == 302: headers["Location"] = "https://outside.invalid/?token=redirect-secret"
                    self.reply(payload, status=fixture.http_status, headers=headers)
                finally:
                    with fixture.lock: fixture.active -= 1
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.config["cloud"]["url"] = f"http://127.0.0.1:{server.server_port}{self.origin_prefix}"
        try: yield
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_copies_unicode_metadata_without_source_stat_or_open_and_keeps_signatures(self):
        relative = "电影/影片 #1 %?.nfo"
        metadata = self.file(relative, b"<movie>title</movie>")
        self.file("电影/movie.mkv")
        progress = []
        self.engine.progress = lambda stage,path:progress.append((stage,path))
        self.origin_prefix = "/cd2"
        original_open, original_stat = Path.open, Path.stat
        def guarded_open(path, *args, **kwargs):
            if path == metadata: raise AssertionError("mounted metadata open")
            return original_open(path, *args, **kwargs)
        def guarded_stat(path, *args, **kwargs):
            if path in (metadata, metadata.parent): raise AssertionError("mounted metadata stat")
            return original_stat(path, *args, **kwargs)
        with self.cd2():
            with patch.object(Path, "open", guarded_open), patch.object(Path, "stat", guarded_stat):
                first = self.engine.scan(self.lib)
                second = self.engine.scan(self.lib)
        self.assertEqual(first["change_count"], 2); self.assertEqual(first["errors"], [])
        self.assertEqual(second["change_count"], 0)
        self.assertEqual((self.output/relative).read_bytes(), b"<movie>title</movie>")
        self.assertEqual(len(self.gets), 1)
        target, api_path, authorization = self.gets[0]
        self.assertTrue(target.startswith("/cd2/static/http/127.0.0.1:"))
        self.assertEqual(api_path, self.lib["cloud_path"]+"/"+relative)
        self.assertIsNone(authorization)
        self.assertIn(("CD2 元数据同步", self.lib["cloud_path"]+"/"+relative), progress)
        lookup = next(call for call in self.calls if call[0] == "GetDownloadUrlPath")
        self.assertEqual([lookup[2][key] for key in (2, 3, 4)], [0, 0, 0])
        self.config["sync"]["scan_backend"] = "filesystem"
        self.assertEqual(self.engine.scan(self.lib)["change_count"], 0)

    def test_verification_period_uses_http_and_hash_without_replacing_unchanged_output(self):
        self.file("movie.nfo", b"original")
        with self.cd2():
            self.engine.scan(self.lib)
            self.config["sync"]["metadata_verify_seconds"] = 0
            with patch("bridge.engine.os.replace", side_effect=AssertionError("unchanged output replaced")):
                result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0); self.assertFalse(result["errors"])
        self.assertEqual(len(self.gets), 2)

    def test_local_scraper_edits_are_preserved_without_downloading_again(self):
        self.file("movie.nfo", b"original")
        with self.cd2():
            self.engine.scan(self.lib)
            (self.output/"movie.nfo").write_bytes(b"edited locally")
            self.config["sync"]["metadata_verify_seconds"] = 0
            result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0); self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(self.gets), 1)
        self.assertEqual((self.output/"movie.nfo").read_bytes(), b"edited locally")

    def test_changed_source_keeps_previous_output_and_manifest(self):
        self.file("movie.nfo", b"original")
        with self.cd2():
            self.engine.scan(self.lib)
            before = self.state.rows("pt")[0]
            self.config["sync"]["metadata_verify_seconds"] = 0
            self.changed = True
            result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0)
        self.assertIn("源文件仍在变动", result["errors"][0])
        self.assertEqual((self.output/"movie.nfo").read_bytes(), b"original")
        self.assertEqual(self.state.rows("pt")[0]["fingerprint"], before["fingerprint"])
        self.assertFalse(list(self.output.rglob(".bridge-*.tmp")))

    def test_declared_response_length_is_checked_before_body_is_accepted(self):
        self.file("movie.nfo", b"original")
        self.override_length = 100000000
        with self.cd2(): result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0)
        self.assertIn("长度", result["errors"][0])
        self.assertFalse(self.state.rows("pt"))

    def test_unknown_response_length_is_bounded_and_rejects_extra_bytes(self):
        self.file("movie.nfo", b"original")
        self.omit_length, self.body_suffix = True, b"x"*10000
        with self.cd2(): result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0)
        self.assertIn("长度", result["errors"][0])
        self.assertFalse(os.path.lexists(self.output/"movie.nfo"))

    def test_compressed_content_is_not_decompressed_past_the_limit(self):
        self.file("movie.nfo", b"original"); self.encoding = "gzip"
        with self.cd2(): result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0)
        self.assertIn("压缩", result["errors"][0])

    def test_redirects_and_direct_urls_are_not_used_or_logged(self):
        self.file("movie.nfo", b"original"); self.http_status = 302
        with self.cd2(): result = self.engine.scan(self.lib)
        self.assertEqual(len(self.gets), 1); self.assertIn("HTTP 302", result["errors"][0])
        text = repr(result)+repr(self.state.query("SELECT message FROM events"))
        for secret in ("signed-url-secret", "redirect-secret", "direct-url-secret", "api-bearer-secret"):
            self.assertNotIn(secret, text)

    def test_missing_proxy_url_does_not_use_direct_url_or_mounted_file(self):
        metadata = self.file("movie.nfo", b"original"); self.no_proxy_url = True
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path == metadata: raise AssertionError("filesystem fallback")
            return original(path, *args, **kwargs)
        with self.cd2(), patch.object(Path, "open", guarded): result = self.engine.scan(self.lib)
        self.assertEqual(self.gets, []); self.assertEqual(result["change_count"], 0)
        self.assertIn("下载路径", result["errors"][0])

    def test_rate_limit_stops_followup_metadata_requests(self):
        for i in range(6): self.file(f"{i}.nfo", b"original")
        self.http_status = 429
        with self.cd2(): result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 0); self.assertEqual(len(result["errors"]), 6)
        self.assertLessEqual(len(self.gets), 2)
        self.assertTrue(any("429" in message for message in result["errors"]))

    def test_rpc_permission_failure_is_reported_without_downloading_or_fallback(self):
        self.file("movie.nfo", b"original"); self.rpc_error = 7
        with self.cd2(): result = self.engine.scan(self.lib)
        self.assertEqual(self.gets, []); self.assertEqual(result["change_count"], 0)
        self.assertIn("gRPC 状态 7", result["errors"][0])

    def test_oversized_metadata_and_dry_run_never_request_download_urls(self):
        self.config["sync"]["metadata_max_mb"] = 1
        self.file("oversized.png", b"x"*(1024*1024+1))
        self.file("movie.nfo", b"original")
        with self.cd2():
            result = self.engine.scan(self.lib, dry_run=True)
        self.assertEqual(result["change_count"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertFalse(self.gets)
        self.assertTrue(all(call[0] == "GetSubFiles" for call in self.calls))
        self.assertFalse(self.output.exists())

    def test_download_concurrency_is_bounded_by_api_workers(self):
        self.config["sync"].update(file_workers=4, cloud_list_workers=2)
        for i in range(4): self.file(f"{i}.nfo", b"original")
        self.barrier = threading.Barrier(2, timeout=5)
        with self.cd2(): result = self.engine.scan(self.lib)
        self.assertEqual(result["change_count"], 4); self.assertFalse(result["errors"])
        self.assertEqual(self.peak, 2)

    def test_transport_errors_never_include_signed_download_urls(self):
        reader = CloudContent(CloudScanner(self.config, self.lib, lambda *_:None))
        connection = Mock()
        connection.getresponse.side_effect = TimeoutError("/static?token=signed-url-secret")
        with patch("bridge.cloud_content.http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(CloudContentError) as failure:
                reader.download("/static?token=signed-url-secret", 8, lambda:None)
        self.assertNotIn("signed-url-secret", str(failure.exception))
        connection.close.assert_called_once()

    def test_invalid_download_paths_never_open_http_connections(self):
        reader = CloudContent(CloudScanner(self.config, self.lib, lambda *_:None))
        with patch("bridge.cloud_content.http.client.HTTPConnection") as connect:
            for target in ("https://outside.invalid/file", "//outside.invalid/file", "/file\r\nX-Token:x",
                           "/file#fragment", "/file\\..", "/file/{UNRECOGNIZED}"):
                with self.subTest(target=target), self.assertRaises(CloudContentError):
                    reader.download(target, 8, lambda:None)
        connect.assert_not_called()


class RetryTests(unittest.TestCase):
    def content(self, retries):
        from types import SimpleNamespace
        from bridge.cloud_content import CloudContent
        scanner = SimpleNamespace(config={"sync": {"metadata_max_mb": 1, "file_workers": 2, "metadata_retries": retries}},
                                  workers=2, root="/115open/pt", interval=0)
        return CloudContent(scanner)

    def test_transient_failures_are_retried_others_are_not(self):
        from types import SimpleNamespace
        from bridge.cloud_content import CloudContentError
        expected = SimpleNamespace(st_size=4, st_mtime_ns=1)
        with patch("bridge.cloud_content.RETRY_DELAYS", (0, 0, 0, 0, 0)):
            c = self.content(2)
            replies = [CloudContentError("CD2 元数据下载失败（TimeoutError）；不回退挂载盘读取"), b"data"]
            with patch.object(c, "fetch", side_effect=replies) as fetch:
                self.assertEqual(c.read("a/b.nfo", expected, lambda: None), b"data")
            self.assertEqual(fetch.call_count, 2)
            with patch.object(c, "fetch", side_effect=CloudContentError("CD2 源文件仍在变动，保留原输出并等待下次校验")) as fetch:
                with self.assertRaises(CloudContentError):
                    c.read("a/b.nfo", expected, lambda: None)
            self.assertEqual(fetch.call_count, 1)
            timeout = CloudContentError("CD2 元数据下载失败（TimeoutError）；不回退挂载盘读取")
            with patch.object(c, "fetch", side_effect=[timeout] * 3) as fetch:
                with self.assertRaises(CloudContentError):
                    c.read("a/b.nfo", expected, lambda: None)
            self.assertEqual(fetch.call_count, 3)
