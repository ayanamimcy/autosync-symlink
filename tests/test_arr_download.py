import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
import http.cookiejar

from bridge.arr_download.clients import OfflineCloud, UpstreamError
from bridge.arr_download.config import load
from bridge.arr_download.metadata import magnet, magnet_parts, metainfo, offline_magnet
from bridge.arr_download.service import Downloader, Store, complete
from bridge.arr_download.web import Server
from bridge.arr_import.cloud import OperationRejected, number
from bridge.protocol import text_field, varint

MB = 1024 * 1024


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


def torrent(private=False, padding=False):
    files = [{b"path": [b"Show.S01E01.mkv"], b"length": 3 * MB},
             {b"path": [b"release.nfo"], b"length": 4}]
    if padding:
        files.insert(1, {b"path": [b".pad", b"100"], b"length": 100, b"attr": b"p"})
    size = sum(f[b"length"] for f in files)
    info = {b"name": "Show & 电影".encode(), b"piece length": MB, b"pieces": b"p" * 20 * -(-size // MB),
            b"files": files}
    if private:
        info[b"private"] = 1
    return bencode({b"announce": b"https://tracker.example/announce",
                    b"announce-list": [[b"udp://open.example:1337"], [b"wss://ignored.example"]], b"info": info})


def config():
    return {"enabled": True, "host": "127.0.0.1", "port": 0,
            "username": "arr", "password": "gateway-secret-" * 3,
            "cloud": {"url": "http://cd2:19798", "token": "cloud-secret"},
            "account_root": "/115open", "cloud_download_root": "/115open/pt/download",
            "arr_download_root": "/downloads", "categories": ["radarr", "sonarr"],
            "poll_seconds": 1, "settle_seconds": 2, "timeout_seconds": 86400, "max_errors": 3,
            "send_trackers": True, "delete_files": True, "retired_keys": [],
            "arr": [{"category": "radarr", "url": "http://radarr:7878", "api_key": "radarr-key"}]}


def cloud_file(path, size, sha1="a" * 40):
    return {"path": path, "name": path.rsplit("/", 1)[1], "id": "id-" + path, "directory": False,
            "local": False, "size": size, "hashes": {2: sha1}}


class FakeCloud:
    """115 offline as CD2 shows it: a task list per folder and the files it produced."""

    def __init__(self):
        self.tasks, self.files, self.calls = {}, {}, []
        self.reject = False
        self.lose_add_reply = False

    def mkdir(self, path, account):
        self.calls.append(("mkdir", path))

    def offline(self, folder):
        return copy.deepcopy(self.tasks.get(folder, []))

    def add_offline(self, link, folder):
        self.calls.append(("add", link, folder))
        if self.reject or any(t["hash"] == magnet(link) for tasks in self.tasks.values() for t in tasks):
            raise OperationRejected("rejected")  # 115: "任务已存在" for a known infohash
        self.tasks[folder] = [{"hash": magnet(link), "name": magnet_parts(link)[0], "size": 0,
                               "status": 0, "percent": 0.0, "peers": 0}]
        if self.lose_add_reply:
            raise TimeoutError("reply lost")

    def remove_offline(self, infohash, path):
        self.calls.append(("remove_offline", infohash, path))
        for folder in self.tasks:
            self.tasks[folder] = [t for t in self.tasks[folder] if t["hash"] != infohash]

    def progress(self, folder, status, percent=0.0, size=0):
        self.tasks[folder][0].update(status=status, percent=percent, size=size or self.tasks[folder][0]["size"])

    def land(self, folder, files):
        for rel, size in files.items():
            self.files[folder + "/" + rel] = cloud_file(folder + "/" + rel, size)

    def walk(self, path):
        found = {p[len(path) + 1:]: copy.deepcopy(f) for p, f in self.files.items() if p.startswith(path + "/")}
        return found or None

    def get_file(self, path):
        if path in self.files or any(p.startswith(path + "/") for p in self.files):
            return {"path": path}
        return None

    def delete(self, path):
        self.calls.append(("delete", path))
        self.files = {p: f for p, f in self.files.items() if not p.startswith(path + "/")}


class FakeArr:
    def __init__(self):
        self.failed = []

    def __call__(self, instance):
        return self

    def mark_failed(self, infohash):
        self.failed.append(infohash)
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "downloads.sqlite3")
        self.addCleanup(lambda: self.store.close())
        self.cloud, self.arr = FakeCloud(), FakeArr()
        self.time = 1000.0
        self.cfg = config()
        self.d = Downloader(self.cfg, self.store, lambda _: self.cloud, self.arr, lambda: self.time)

    def ticks(self, n=1, step=1):
        for _ in range(n):
            self.d.tick()
            self.time += step

    def job(self, infohash):
        return self.store.get(infohash)


class OfflineTests(Base):
    def test_torrent_offline_verify_ready_and_qb_views(self):
        meta = metainfo(torrent())
        job = self.d.submit({"category": "radarr"}, torrent())
        folder = "/115open/pt/download/.arr-offline/" + meta["hash"]
        self.ticks()
        add = [c for c in self.cloud.calls if c[0] == "add"]
        self.assertEqual(len(add), 1)
        self.assertEqual(add[0][2], folder)
        self.assertEqual(magnet(add[0][1]), meta["hash"])
        self.assertIn("tracker.example", add[0][1])
        self.assertNotIn("wss", add[0][1])
        self.assertEqual(self.d.row(self.job(job["hash"]))["state"], "metaDL")
        self.cloud.progress(folder, 1, 40.0)
        self.ticks()
        row = self.d.row(self.job(job["hash"]))
        self.assertEqual(row["state"], "downloading")
        self.assertAlmostEqual(row["progress"], 0.396)
        self.cloud.progress(folder, 2, 100.0)
        self.ticks()
        self.assertEqual(self.job(job["hash"])["status"], "verifying")
        # 115 kept the torrent's root folder but skipped the tiny .nfo.
        self.cloud.land(folder, {"Show & 电影/Show.S01E01.mkv": 3 * MB})
        self.ticks(3, step=2)
        done = self.job(job["hash"])
        self.assertEqual(done["status"], "ready")
        row = self.d.row(done)
        self.assertEqual(row["state"], "stoppedUP")
        self.assertEqual(row["save_path"], "/downloads/.arr-offline/" + meta["hash"])
        self.assertEqual(row["content_path"], "/downloads/.arr-offline/" + meta["hash"] + "/Show & 电影")
        self.assertEqual(row["ratio_limit"], 0)  # lets Arr move instead of copy when it removes completed items
        self.assertEqual(self.d.files(done)[0]["name"], "Show & 电影/Show.S01E01.mkv")
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add"]), 1)

    def test_single_file_without_root_folder_and_several_top_entries(self):
        link = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=Movie.2026.mkv&tr=udp%3A%2F%2Ft.example%3A1"
        self.d.submit({"category": "radarr", "urls": link})
        folder = "/115open/pt/download/.arr-offline/" + "c" * 40
        self.ticks()
        self.cloud.progress(folder, 2, 100.0, size=5 * MB)
        self.ticks()
        self.cloud.land(folder, {"Movie.2026.mkv": 2 * MB})
        self.ticks(3, step=2)
        self.assertEqual(self.job("c" * 40)["status"], "verifying")  # total size not reached yet
        self.cloud.land(folder, {"Sample/sample.mkv": 3 * MB})
        self.ticks(3, step=2)
        job = self.job("c" * 40)
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["size"], 5 * MB)
        self.assertEqual(self.d.paths(job), ("/downloads/.arr-offline", "/downloads/.arr-offline/" + "c" * 40))
        self.assertEqual(sorted(f["name"] for f in self.d.files(job)),
                         ["c" * 40 + "/Movie.2026.mkv", "c" * 40 + "/Sample/sample.mkv"])

    def test_listing_must_stay_unchanged_for_settle_time(self):
        job = self.d.submit({"category": "radarr"}, torrent())
        folder = "/115open/pt/download/.arr-offline/" + job["hash"]
        self.ticks()
        self.cloud.progress(folder, 2, 100.0)
        self.ticks()
        self.cloud.land(folder, {"Show.S01E01.mkv": 3 * MB})
        self.ticks(1)
        self.cloud.files[folder + "/Show.S01E01.mkv"]["hashes"][2] = "b" * 40
        self.ticks(1, step=1)
        self.assertEqual(self.job(job["hash"])["status"], "verifying")
        self.ticks(2, step=2)
        self.assertEqual(self.job(job["hash"])["status"], "ready")

    def test_offline_failure_marks_arr_failed_once(self):
        job = self.d.submit({"category": "radarr"}, torrent())
        folder = "/115open/pt/download/.arr-offline/" + job["hash"]
        self.ticks()
        self.cloud.progress(folder, 3)
        self.ticks(3)
        failed = self.job(job["hash"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.d.row(failed)["state"], "error")
        self.assertEqual(self.arr.failed, [job["hash"]])
        self.assertIn("已通知 Arr", failed["message"])

    def test_existing_115_record_is_cleared_once_then_resubmitted(self):
        job = self.d.submit({"category": "radarr"}, torrent())
        self.cloud.tasks["/115open/elsewhere"] = [{"hash": job["hash"], "name": "", "size": 0, "status": 2,
                                                    "percent": 100.0, "peers": 0}]
        self.ticks()
        self.assertIn(("remove_offline", job["hash"], "/115open"), self.cloud.calls)
        self.assertEqual(self.job(job["hash"])["status"], "queued")
        self.ticks(2)
        self.assertEqual(self.job(job["hash"])["status"], "downloading")
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add"]), 2)

    def test_rejected_submit_fails_and_sonarr_without_arr_config_is_not_notified(self):
        self.cloud.reject = True
        job = self.d.submit({"category": "sonarr"}, torrent())
        self.ticks(3)
        self.assertEqual(self.job(job["hash"])["status"], "failed")
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "remove_offline"]), 1)
        self.assertEqual(self.arr.failed, [])
        self.cloud.reject = False
        self.d.retry(job["hash"])
        self.ticks()
        self.assertEqual(self.job(job["hash"])["status"], "queued")
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add"]), 3)

    def test_lost_submit_reply_is_reconciled_by_listing_never_resubmitted(self):
        self.cloud.lose_add_reply = True
        job = self.d.submit({"category": "radarr"}, torrent())
        self.ticks()
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "downloads.sqlite3")
        self.d = Downloader(self.cfg, self.store, lambda _: self.cloud, self.arr, lambda: self.time)
        self.ticks(3)
        self.assertEqual(self.job(job["hash"])["status"], "downloading")
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add"]), 1)

    def test_submitted_task_never_listed_fails_after_grace(self):
        job = self.d.submit({"category": "sonarr"}, torrent())
        self.ticks()
        self.cloud.tasks.clear()
        self.ticks(1, step=601)
        self.ticks(1)
        self.assertEqual(self.job(job["hash"])["status"], "failed")
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add"]), 1)

    def test_timeout_and_transient_errors(self):
        job = self.d.submit({"category": "sonarr"}, torrent())
        with patch.object(self.cloud, "offline", side_effect=UpstreamError("CD2 ListOfflineFilesByPath：gRPC 14")):
            self.ticks(2)
            self.assertEqual(self.job(job["hash"])["status"], "queued")
            self.assertIn("gRPC 14", self.job(job["hash"])["message"])
            self.ticks(1)
        self.assertEqual(self.job(job["hash"])["status"], "failed")
        other = self.d.submit({"category": "sonarr", "urls": "magnet:?xt=urn:btih:" + "d" * 40}, None)
        self.time += 86401
        self.ticks()
        self.assertEqual(self.job(other["hash"])["status"], "failed")

    def test_remove_with_delete_files_recycles_only_its_folder(self):
        job = self.d.submit({"category": "radarr"}, torrent())
        folder = "/115open/pt/download/.arr-offline/" + job["hash"]
        self.ticks()
        self.cloud.progress(folder, 2, 100.0)
        self.cloud.land(folder, {"Show.S01E01.mkv": 3 * MB})
        self.cloud.files["/115open/pt/download/other/keep.mkv"] = cloud_file("/115open/pt/download/other/keep.mkv", 1)
        self.ticks(4, step=2)
        self.d.remove([job["hash"]], delete_data=True)
        self.assertEqual(self.d.rows(), [])
        self.ticks()
        self.assertEqual([c for c in self.cloud.calls if c[0] == "delete"], [("delete", folder)])
        self.assertIn(("remove_offline", job["hash"], "/115open"), self.cloud.calls)
        self.assertIn("/115open/pt/download/other/keep.mkv", self.cloud.files)
        self.assertFalse(self.job(job["hash"])["remove_data"])
        self.ticks()
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "delete"]), 1)

    def test_remove_while_downloading_stops_115_task_but_keeps_files_without_delete_option(self):
        self.cfg["delete_files"] = False
        job = self.d.submit({"category": "radarr"}, torrent())
        self.ticks()
        self.d.remove([job["hash"]], delete_data=True)
        self.ticks(2)
        self.assertFalse(any(c[0] == "delete" for c in self.cloud.calls))
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "remove_offline"]), 1)
        self.assertEqual(self.cloud.tasks["/115open/pt/download/.arr-offline/" + job["hash"]], [])
        again = self.d.submit({"category": "radarr"}, torrent())
        self.ticks()
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add"]), 2)
        self.assertEqual(self.job(again["hash"])["status"], "queued")

    def test_cleanup_failure_after_removal_never_fails_or_notifies_arr(self):
        job = self.d.submit({"category": "radarr"}, torrent())
        folder = "/115open/pt/download/.arr-offline/" + job["hash"]
        self.ticks()
        self.cloud.progress(folder, 2, 100.0)
        self.cloud.land(folder, {"Show.S01E01.mkv": 3 * MB})
        self.ticks(4, step=2)
        self.d.remove([job["hash"]], delete_data=True)
        with patch.object(self.cloud, "delete", side_effect=UpstreamError("CD2 DeleteFile：gRPC 7")):
            self.ticks(5)
        done = self.job(job["hash"])
        self.assertEqual(done["status"], "ready")
        self.assertFalse(done["remove_data"])
        self.assertIn("删除权限", done["message"])
        self.assertEqual(self.arr.failed, [])
        self.ticks(2)
        self.assertEqual(self.arr.failed, [])

    def test_remove_during_worker_pass_is_not_lost(self):
        job = self.d.submit({"category": "radarr"}, torrent())
        original = self.cloud.offline

        def offline(folder):
            self.d.remove([job["hash"]], delete_data=False)
            return original(folder)
        with patch.object(self.cloud, "offline", side_effect=offline):
            self.ticks()
        self.assertTrue(self.job(job["hash"])["removed"])

    def test_duplicate_category_disabled_and_regrab_after_removal(self):
        job = self.d.submit({"category": "radarr"}, torrent())
        with self.assertRaises(FileExistsError):
            self.d.submit({"category": "radarr"}, torrent())
        with self.assertRaises(ValueError):
            self.d.submit({"category": "other"}, torrent())
        with self.assertRaises(ValueError):
            self.d.submit({"category": "radarr"}, torrent(private=True))
        self.d.remove([job["hash"]], False)
        again = self.d.submit({"category": "radarr"}, torrent())
        self.assertEqual(again["status"], "queued")
        self.assertFalse(again["removed"])
        self.cfg["enabled"] = False
        with self.assertRaises(ValueError):
            self.d.submit({"category": "radarr", "urls": "magnet:?xt=urn:btih:" + "e" * 40})

    def test_trackers_can_be_withheld(self):
        self.cfg["send_trackers"] = False
        job = self.d.submit({"category": "radarr"}, torrent())
        self.assertNotIn("tr=", job["magnet"])
        self.assertIn("dn=", job["magnet"])

    def test_completeness_rules(self):
        job = {"files": [{"path": "a/x.mkv", "size": 2 * MB}, {"path": "a/x.mkv.nfo", "size": 10}]}
        self.assertTrue(complete(job, {"x.mkv": cloud_file("/f/x.mkv", 2 * MB)}))
        self.assertFalse(complete(job, {"x.mkv": cloud_file("/f/x.mkv", MB)}))
        self.assertFalse(complete(job, {"x.mkv": {**cloud_file("/f/x.mkv", 2 * MB), "local": True}}))
        self.assertFalse(complete({"size": 10 * MB}, {"x.mkv": cloud_file("/f/x.mkv", 2 * MB)}))
        self.assertTrue(complete({"size": 0}, {"x.mkv": cloud_file("/f/x.mkv", 1)}))
        self.assertFalse(complete({"size": 0}, {}))


class MetadataTests(unittest.TestCase):
    def test_original_info_hash_trackers_and_padding(self):
        meta = metainfo(torrent())
        self.assertEqual(meta["size"], 3 * MB + 4)
        self.assertEqual(meta["name"], "Show & 电影")
        raw_info = torrent().split(b"4:info", 1)[1][:-1]
        self.assertEqual(meta["hash"], hashlib.sha1(raw_info).hexdigest())
        self.assertEqual(meta["trackers"], ["https://tracker.example/announce", "udp://open.example:1337"])
        encoded = base64.b32encode(bytes.fromhex(meta["hash"])).decode()
        self.assertEqual(magnet("magnet:?xt=urn:btih:" + encoded), meta["hash"])
        rebuilt = offline_magnet(meta["hash"], meta["name"], meta["trackers"])
        self.assertEqual(magnet(rebuilt), meta["hash"])
        self.assertEqual(magnet_parts(rebuilt), (meta["name"], meta["trackers"]))
        padded = metainfo(torrent(padding=True))
        self.assertEqual([f["path"] for f in padded["files"]], ["Show & 电影/Show.S01E01.mkv", "Show & 电影/release.nfo"])
        self.assertEqual(metainfo(torrent(private=True), allow_private=True)["trackers"], [])

    def test_private_v2_path_traversal_and_invalid_hashes_rejected(self):
        with self.assertRaises(ValueError):
            metainfo(torrent(True))
        for raw in (b"d4:infoi1ee", b"d4:infode4:infodee", torrent() + b"x", b"d4:info"):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                metainfo(raw)
        for path in (b"..", b"/absolute", b"a/b", b"a\\b", b"."):
            info = {b"name": b"show", b"piece length": 16, b"pieces": b"p" * 20,
                    b"files": [{b"path": [path], b"length": 1}]}
            with self.subTest(path=path), self.assertRaises(ValueError):
                metainfo(bencode({b"info": info}))
        for link in ("https://example/a.torrent", "magnet:?xt=urn:btmh:1220abc", "magnet:?xt=urn:btih:bad",
                     "magnet:?xt=urn:btih:" + "a" * 40 + "&xt=urn:btih:" + "b" * 40):
            with self.assertRaises(ValueError):
                magnet(link)
        info = {b"name": b"show", b"piece length": 16, b"pieces": b"p" * 20, b"length": 1, b"meta version": 2}
        with self.assertRaises(ValueError):
            metainfo(bencode({b"info": info}))


class CloudWireTests(unittest.TestCase):
    def test_offline_list_decodes_double_progress_and_errors_are_redacted(self):
        task = (text_field(1, "Movie") + number(2, 5 * MB) + number(4, 1) + text_field(5, "AB" * 20)
                + varint(9 << 3 | 1) + struct.pack("<d", 42.5) + number(10, 7))
        reply = varint(1 << 3 | 2) + varint(len(task)) + task
        cloud = OfflineCloud.__new__(OfflineCloud)
        with patch("bridge.arr_import.cloud.Cloud.rpc", return_value=[reply]):
            row = cloud.offline("/115open/pt/download/x")[0]
        self.assertEqual((row["hash"], row["status"], row["percent"], row["peers"], row["size"]),
                         ("ab" * 20, 1, 42.5, 7, 5 * MB))
        cloud = OfflineCloud(config()["cloud"])
        with patch.object(cloud.client, "open", side_effect=ConnectionError("CloudDrive gRPC 状态 7；SECRET-TOKEN")):
            with self.assertRaises(UpstreamError) as failure:
                cloud.offline("/115open/pt/download")
        self.assertIn("gRPC 7", str(failure.exception))
        self.assertNotIn("SECRET", str(failure.exception))
        with patch.object(cloud.client, "open", side_effect=ConnectionError("CloudDrive gRPC 状态 5")):
            self.assertIsNone(cloud.get_file("/115open/missing/file.mkv"))
            self.assertIsNone(cloud.walk("/115open/missing"))


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.d.thread = threading.current_thread()  # healthz: the worker is not started in tests
        self.server = Server(self.d)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, data=None, content_type="application/x-www-form-urlencoded"):
        if isinstance(data, dict):
            data = urlencode(data).encode()
        req = Request(self.url + path, data=data, headers={"Content-Type": content_type})
        with self.opener.open(req, timeout=5) as response:
            body = response.read()
        try:
            return json.loads(body)
        except ValueError:
            return body

    def login(self):
        self.assertEqual(self.call("/api/v2/auth/login", {"username": "arr", "password": self.cfg["password"]}), b"Ok.")

    def test_arr_flow_over_http(self):
        with self.assertRaises(HTTPError) as denied:
            self.call("/api/v2/torrents/info")
        self.assertEqual(denied.exception.code, 403)
        self.assertEqual(self.call("/api/v2/auth/login", {"username": "arr", "password": "wrong"}), b"Fails.")
        self.login()
        self.assertEqual(self.call("/api/v2/app/webapiVersion"), b"2.11.4")
        self.assertEqual(self.call("/api/v2/app/version"), b"v5.1.4")
        preferences = self.call("/api/v2/app/preferences")
        self.assertTrue(preferences["dht"])
        self.assertFalse(preferences["queueing_enabled"])
        self.assertEqual(self.call("/api/v2/torrents/categories")["radarr"]["savePath"], "/downloads")
        boundary = "x" * 16
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"category\"\r\n\r\nradarr\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"a.torrent\"\r\n"
                "Content-Type: application/x-bittorrent\r\n\r\n").encode() + torrent() + f"\r\n--{boundary}--\r\n".encode()
        self.assertEqual(self.call("/api/v2/torrents/add", body, "multipart/form-data; boundary=" + boundary), b"Ok.")
        with self.assertRaises(HTTPError) as duplicate:
            self.call("/api/v2/torrents/add", body, "multipart/form-data; boundary=" + boundary)
        self.assertEqual(duplicate.exception.code, 409)
        infohash = metainfo(torrent())["hash"]
        properties = self.call("/api/v2/torrents/properties?hash=" + infohash.upper())
        self.assertEqual(properties["save_path"], "/downloads/.arr-offline/" + infohash)
        rows = self.call("/api/v2/torrents/info?category=radarr")
        self.assertEqual([(r["hash"], r["state"]) for r in rows], [(infohash, "metaDL")])
        self.assertEqual(self.call("/api/v2/torrents/info?category=sonarr"), [])
        with self.assertRaises(HTTPError) as top:
            self.call("/api/v2/torrents/topPrio", {"hashes": infohash})
        self.assertEqual(top.exception.code, 409)
        self.assertEqual(self.call("/api/v2/torrents/setShareLimits", {"hashes": infohash, "ratioLimit": "1"}), b"")
        self.assertEqual(self.call("/api/v2/torrents/createCategory", {"category": "radarr-imported"}), b"")
        self.assertIn("radarr-imported", self.call("/api/v2/torrents/categories"))
        self.assertEqual(self.call("/v1/downloads/" + infohash)["status"], "queued")
        self.assertEqual(self.call("/api/v2/torrents/delete", {"hashes": infohash, "deleteFiles": "true"}), b"")
        self.assertEqual(self.call("/api/v2/torrents/info"), [])
        with self.assertRaises(HTTPError) as missing:
            self.call("/api/v2/torrents/properties?hash=" + infohash)
        self.assertEqual(missing.exception.code, 404)

    def test_bearer_password_and_validation_errors(self):
        req = Request(self.url + "/api/v2/torrents/info", headers={"Authorization": "Bearer " + self.cfg["password"]})
        with build_opener().open(req, timeout=5) as response:
            self.assertEqual(json.loads(response.read()), [])
        self.login()
        with self.assertRaises(HTTPError) as bad:
            self.call("/api/v2/torrents/add", {"category": "radarr", "urls": "https://example/a.torrent"})
        self.assertEqual(bad.exception.code, 400)
        with self.assertRaises(HTTPError) as unknown:
            self.call("/api/v2/torrents/setLocation", {"hashes": "a" * 40, "location": "/x"})
        self.assertEqual(unknown.exception.code, 405)
        self.assertEqual(self.call("/healthz"), {"ok": True})


class ConfigTests(unittest.TestCase):
    def test_old_key_names_are_accepted_and_retired_keys_reported(self):
        cfg = config()
        for key in ("retired_keys", "send_trackers", "delete_files"):
            del cfg[key]
        cfg["qb_download_root"] = cfg.pop("arr_download_root")
        cfg["offline_categories"] = cfg.pop("categories")
        cfg.update(qb={"url": "http://qb:8080"}, seed_after_check=False, import_after="cloud_ready", port=8097)
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "config.json"
            p.write_text(json.dumps(cfg))
            loaded = load(p)
            self.assertEqual(loaded["arr_download_root"], "/downloads")
            self.assertEqual(loaded["categories"], ["radarr", "sonarr"])
            self.assertEqual(loaded["retired_keys"], ["import_after", "qb", "seed_after_check"])
            self.assertTrue(loaded["send_trackers"])
            self.assertFalse(loaded["delete_files"])
            for change in ({"cloud_download_root": "/another-account/download"},
                           {"arr": [{"category": "unknown", "url": "http://r", "api_key": "k"}]},
                           {"password": "short"}):
                p.write_text(json.dumps({**cfg, **change}))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load(p)


if __name__ == "__main__":
    unittest.main()
