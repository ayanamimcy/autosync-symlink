import base64
import copy
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import posixpath
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
import http.cookiejar

from bridge.arr_download.clients import OfflineCloud, QB, UpstreamError, multipart
from bridge.arr_download.config import load
from bridge.arr_download.metadata import magnet, metainfo, offline_magnet
from bridge.arr_download.service import Downloader, Store, deliverable
from bridge.arr_download.web import Server, form


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


def torrent(private=False):
    info = {b"name": "Show & 电影".encode(), b"piece length": 16, b"pieces": b"p" * 40,
            b"files": [{b"path": [b"Show.S01E01.mkv"], b"length": 20},
                       {b"path": [b"release.nfo"], b"length": 4}]}
    if private:
        info[b"private"] = 1
    return bencode({b"announce": b"https://tracker.example/announce", b"info": info})


def config():
    return {"enabled": True, "host": "127.0.0.1", "port": 0,
            "username": "arr", "password": "gateway-secret-" * 3,
            "qb": {"url": "http://qb:8080", "username": "admin", "password": "qb-secret"},
            "cloud": {"url": "http://cd2:19798", "token": "cloud-secret"},
            "account_root": "/115open", "cloud_download_root": "/115open/pt/download",
            "qb_download_root": "/downloads", "offline_categories": ["radarr", "sonarr"],
            "seed_after_check": True, "poll_seconds": 1, "settle_seconds": 2,
            "timeout_seconds": 86400, "max_errors": 3}


class FakeQB:
    def __init__(self):
        self.rows, self.manifests, self.calls = {}, {}, []
        self.exported = torrent()
        self.lose_start_response = False

    def info(self, infohash):
        return copy.deepcopy(self.rows.get(infohash))

    def add(self, job, raw):
        self.calls.append(("add", job["hash"]))
        self.rows[job["hash"]] = {"hash": job["hash"], "category": job["category"],
                                  "save_path": job["qb_path"], "tags": job["tag"], "state": "stoppedDL",
                                  "progress": 0, "amount_left": 24, "auto_tmm": False}
        if raw:
            self.metadata(job["hash"], raw)

    def metadata(self, infohash, raw):
        meta = metainfo(raw, allow_private=True)
        self.exported = raw
        self.manifests[infohash] = [{"name": f["path"], "size": f["size"], "progress": 0, "priority": 1}
                                    for f in meta["files"]]

    def export(self, infohash):
        return self.exported

    def get(self, path, **params):
        if path == "torrents/info":
            return copy.deepcopy(list(self.rows.values()))
        if path == "torrents/files":
            return copy.deepcopy(self.manifests.get(params["hash"], []))
        if path == "torrents/categories":
            return {"radarr": {"savePath": "/downloads"}, "sonarr": {"savePath": "/downloads"}}
        raise AssertionError(path)

    def post(self, path, **values):
        self.calls.append((path, values))
        infohash = values.get("hashes", "")
        if path == "torrents/recheck":
            self.rows[infohash]["state"] = "checkingDL"
        elif path == "torrents/start":
            self.rows[infohash]["state"] = "stalledUP"
            if self.lose_start_response:
                self.lose_start_response = False
                raise UpstreamError("qB 连接异常（TimeoutError）")
        elif path == "torrents/stop":
            self.rows[infohash]["state"] = "stoppedDL" if self.rows[infohash]["progress"] < 1 else "stoppedUP"
        elif path == "torrents/delete":
            assert values["deleteFiles"] == "false"
            self.rows.pop(infohash, None)
        return b""

    def finish(self, infohash, success=True):
        self.rows[infohash].update(state="stoppedUP" if success else "stoppedDL",
                                  progress=1 if success else 0.5, amount_left=0 if success else 12)
        for row in self.manifests[infohash]:
            row["progress"] = 1 if success else 0.5

    def raw(self, path, data=None, content_type=None):
        if path == "/api/v2/app/version":
            return b"v5.1.4", "text/plain"
        if path == "/api/v2/app/webapiVersion":
            return b"2.11.5", "text/plain"
        if path == "/api/v2/app/preferences":
            return json.dumps({"save_path": "/downloads", "temp_path_enabled": False,
                               "web_ui_password": "MUST-NOT-LEAK"}).encode(), "application/json"
        self.calls.append(("raw", path))
        return b"Ok.", "text/plain"


class FakeCloud:
    def __init__(self):
        self.calls, self.tasks, self.files = [], {}, {}
        self.upload_tasks = []
        self.hide_task = False
        self.lose_offline_response = False

    def mkdir(self, path, account):
        self.calls.append(("mkdir", path))

    def offline(self, folder):
        return [] if self.hide_task else copy.deepcopy(self.tasks.get(folder, []))

    def add_offline(self, link, folder):
        self.calls.append(("add_offline", link, folder))
        self.tasks[folder] = [{"hash": magnet(link), "status": 1}]
        if self.lose_offline_response:
            self.lose_offline_response = False
            raise UpstreamError("CD2 AddOfflineFiles：TimeoutError")

    def complete(self, job):
        self.tasks[job["cloud_path"]] = [{"hash": job["hash"], "status": 2}]
        for entry in job["meta"]["files"]:
            path = job["cloud_path"] + "/" + entry["path"]
            self.files[path] = {"path": path, "size": entry["size"], "directory": False,
                                "local": False, "id": "115-file-id", "hashes": {2: "a" * 40}}

    def list_files(self, parent):
        return copy.deepcopy([f for path, f in self.files.items() if posixpath.dirname(path) == parent])

    def uploads(self, path):
        return copy.deepcopy(self.upload_tasks)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "downloads.sqlite3")
        self.addCleanup(lambda: self.store.close())
        self.cfg, self.qb, self.cloud, self.now = config(), FakeQB(), FakeCloud(), 1000
        self.d = Downloader(self.cfg, self.store, self.qb, lambda _: self.cloud, lambda: self.now)

    def submit(self, raw=None):
        return self.d.submit({"category": "radarr"}, raw or torrent())["hash"]

    def tick(self, count=1):
        for _ in range(count):
            self.d.tick()
            self.now += 3

    def job(self, infohash):
        return self.store.get(infohash)

    def checking(self, infohash):
        self.tick(4)
        self.cloud.complete(self.job(infohash))
        self.tick(4)
        self.assertEqual(self.job(infohash)["phase"], "checking")

    def test_offline_full_check_then_seed_and_arr_completion_gate(self):
        infohash = self.submit()
        self.checking(infohash)
        self.assertEqual(self.job(infohash)["status"], "checking")
        rows = self.d.overlay(self.qb.get("torrents/info"))
        self.assertLess(rows[0]["progress"], 1)
        self.assertGreater(rows[0]["amount_left"], 0)
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))
        source = copy.deepcopy(self.cloud.files)
        self.qb.finish(infohash)
        self.tick(2)
        self.assertEqual(self.job(infohash)["status"], "completed")
        self.assertEqual(self.qb.rows[infohash]["state"], "stalledUP")
        self.assertEqual(self.cloud.files, source)
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add_offline"]), 1)
        self.assertEqual(self.submit(), infohash)
        self.assertTrue(all(c[0] in ("mkdir", "add_offline") for c in self.cloud.calls))

    def test_missing_extra_file_or_upload_in_progress_prevents_recheck(self):
        infohash = self.submit()
        self.tick(4)
        self.cloud.complete(self.job(infohash))
        extra = next(path for path in self.cloud.files if path.endswith(".nfo"))
        f = self.cloud.files.pop(extra)
        self.tick(6)
        self.assertEqual(self.job(infohash)["phase"], "remote_verify")
        self.cloud.files[extra] = f
        self.cloud.upload_tasks = [{"state": 1}]
        self.tick(5)
        self.assertFalse(any(c[0] == "torrents/recheck" for c in self.qb.calls))
        self.cloud.upload_tasks = []
        self.tick(3)
        self.assertEqual(self.job(infohash)["phase"], "checking")

    def test_partial_check_fails_without_starting_download(self):
        infohash = self.submit()
        self.checking(infohash)
        self.tick()  # Observe checking before completion.
        self.qb.finish(infohash, False)
        self.tick()
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.assertIn("100%", self.job(infohash)["message"])
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))

    def test_checked_delivery_does_not_wait_for_seeding_start(self):
        infohash = self.submit()
        self.checking(infohash)
        self.assertFalse(deliverable(self.job(infohash)))
        self.qb.finish(infohash)
        self.tick()
        self.assertEqual(self.job(infohash)["phase"], "verified")
        self.assertTrue(deliverable(self.job(infohash)))
        self.assertEqual(self.d.overlay(self.qb.get("torrents/info"))[0]["progress"], 1)
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))
        self.tick()
        self.assertEqual(self.qb.rows[infohash]["state"], "stalledUP")

    def test_cloud_ready_delivery_and_cleanup_preserve_background_check_and_seed(self):
        self.cfg["import_after"] = "cloud_ready"
        infohash = self.submit()
        self.cfg["import_after"] = "qb_checked"  # Existing jobs keep their chosen mode.
        self.tick(4)
        self.cloud.complete(self.job(infohash))
        self.tick(2)
        self.assertFalse(deliverable(self.job(infohash)))  # Still stabilizing.
        self.tick(2)
        public = self.d.public(self.job(infohash))
        self.assertEqual(public["delivery_status"], "ready")
        self.assertEqual(public["seeding_status"], "checking")
        self.assertFalse(public["qb_checked"])
        row = self.d.overlay(self.qb.get("torrents/info"))[0]
        self.assertEqual((row["progress"], row["amount_left"]), (1, 0))
        self.assertEqual(self.qb.rows[infohash]["state"], "checkingDL")
        before = copy.deepcopy(self.qb.calls)
        self.assertEqual(self.d.control([infohash], "delete"), [])
        self.assertEqual(self.qb.calls, before)
        self.assertEqual(self.d.overlay(self.qb.get("torrents/info")), [])
        self.d = Downloader(self.cfg, self.store, self.qb, lambda _: self.cloud, lambda: self.now)
        self.tick()
        self.qb.finish(infohash)
        self.tick(2)
        self.assertEqual(self.job(infohash)["status"], "completed")
        self.assertEqual(self.qb.rows[infohash]["state"], "stalledUP")
        self.assertEqual(self.d.public(self.job(infohash))["delivery_status"], "removed")
        self.assertEqual(self.d.overlay(self.qb.get("torrents/info")), [])
        self.assertFalse(any(c[0] in ("torrents/delete", "torrents/stop") for c in self.qb.calls))

    def test_parallel_check_failure_reports_warning_and_retry_rechecks(self):
        self.cfg["import_after"] = "cloud_ready"
        infohash = self.submit()
        self.checking(infohash)
        self.tick()
        self.qb.finish(infohash, False)
        before = copy.deepcopy(self.cloud.files)
        self.tick()
        public = self.d.public(self.job(infohash))
        self.assertEqual((public["delivery_status"], public["seeding_status"]), ("ready", "failed"))
        self.assertIn("delivery_warning", public)
        self.assertEqual(self.cloud.files, before)
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))
        self.d.retry(infohash)
        self.assertFalse(deliverable(self.job(infohash)))
        self.tick(2)
        self.assertEqual(self.job(infohash)["phase"], "checking")
        self.assertEqual(len([c for c in self.qb.calls if c[0] == "torrents/recheck"]), 2)
        self.qb.finish(infohash)
        self.tick(2)
        self.assertEqual(self.job(infohash)["status"], "completed")

    def test_parallel_remote_change_blocks_further_delivery(self):
        self.cfg["import_after"] = "cloud_ready"
        infohash = self.submit()
        self.checking(infohash)
        self.assertTrue(deliverable(self.job(infohash)))
        next(iter(self.cloud.files.values()))["hashes"][2] = "b" * 40
        self.qb.finish(infohash)
        self.tick()
        self.assertEqual(self.d.public(self.job(infohash))["delivery_status"], "blocked")
        self.assertLess(self.d.overlay(self.qb.get("torrents/info"))[0]["progress"], 1)
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))

    def test_completed_delivery_cleanup_keeps_real_seeder(self):
        infohash = self.submit()
        self.checking(infohash)
        self.qb.finish(infohash)
        self.tick(2)
        self.assertEqual(self.d.control([infohash], "delete"), [])
        self.assertEqual(self.qb.rows[infohash]["state"], "stalledUP")
        self.assertEqual(self.d.overlay(self.qb.get("torrents/info")), [])

    def test_remote_stability_restarts_after_upload_reappears(self):
        infohash = self.submit()
        self.tick(4)
        self.cloud.complete(self.job(infohash))
        self.tick(2)
        self.assertIn("stable_since", self.job(infohash))
        self.cloud.upload_tasks = [{"state": 1}]
        self.tick()
        self.assertNotIn("stable_since", self.job(infohash))
        self.cloud.upload_tasks = []
        self.tick(2)
        self.assertEqual(self.job(infohash)["phase"], "checking")
        self.assertEqual(self.job(infohash)["errors"], 0)

    def test_uncertain_offline_submit_reconciles_after_restart_without_resubmit(self):
        infohash = self.submit()
        self.cloud.lose_offline_response = True
        self.tick(4)
        self.cloud.hide_task = True
        self.d = Downloader(self.cfg, self.store, self.qb, lambda _: self.cloud, lambda: self.now)
        self.tick(5)
        self.assertEqual(len([c for c in self.cloud.calls if c[0] == "add_offline"]), 1)
        self.cloud.hide_task = False
        self.cloud.complete(self.job(infohash))
        self.tick(4)
        self.qb.finish(infohash)
        self.tick(2)
        self.assertEqual(self.job(infohash)["status"], "completed")

    def test_lost_start_response_recovers_without_stopping_verified_seeder(self):
        infohash = self.submit()
        self.checking(infohash)
        self.qb.finish(infohash)
        self.qb.lose_start_response = True
        self.tick(3)
        self.assertEqual(self.job(infohash)["status"], "completed")
        self.assertEqual(len([c for c in self.qb.calls if c[0] == "torrents/start"]), 1)
        self.assertFalse(any(c[0] == "torrents/stop" for c in self.qb.calls))

    def test_changed_remote_content_during_check_prevents_seed(self):
        infohash = self.submit()
        self.checking(infohash)
        next(iter(self.cloud.files.values()))["hashes"][2] = "b" * 40
        self.qb.finish(infohash)
        self.tick()
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))

    def test_existing_qb_torrent_is_never_taken_over(self):
        infohash = metainfo(torrent())["hash"]
        self.qb.rows[infohash] = {"state": "stalledUP"}
        with self.assertRaises(ValueError):
            self.submit()
        self.assertEqual(self.qb.calls, [])
        self.assertEqual(self.store.all(), [])

    def test_magnet_waits_for_metadata_and_rejects_private_before_offline(self):
        raw = torrent(True)
        infohash = metainfo(raw, allow_private=True)["hash"]
        self.d.submit({"category": "sonarr", "urls": offline_magnet(infohash)})
        self.tick(5)
        self.assertEqual(self.job(infohash)["phase"], "metadata")
        self.assertEqual(self.cloud.calls, [])
        self.qb.metadata(infohash, raw)
        self.tick()
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.assertEqual(self.cloud.calls, [])

    def test_public_magnet_exports_original_metadata_then_offline(self):
        raw = torrent()
        infohash = metainfo(raw)["hash"]
        self.d.submit({"category": "sonarr", "urls": offline_magnet(infohash) + "&tr=https://tracker.example"})
        self.tick(3)
        self.qb.metadata(infohash, raw)
        self.tick(2)
        self.assertEqual(self.job(infohash)["meta"]["hash"], infohash)
        self.assertEqual(self.cloud.calls[-1][0], "add_offline")
        self.assertNotIn("tracker", self.cloud.calls[-1][1])

    def test_pause_cancel_preserves_cloud_data_and_blocks_unsafe_controls(self):
        infohash = self.submit()
        self.tick(4)
        self.cloud.complete(self.job(infohash))
        before = copy.deepcopy(self.cloud.files)
        self.assertEqual(self.d.control([infohash], "stop"), [])
        self.tick(3)
        self.assertTrue(self.job(infohash)["paused"])
        for action in ("setCategory", "recheck", "setLocation"):
            with self.assertRaises(ValueError):
                self.d.control([infohash], action)
        self.assertEqual(self.d.control([infohash], "setShareLimits"), [infohash])
        self.assertEqual(self.d.control([infohash], "delete"), [])
        self.tick(2)
        self.assertEqual(self.job(infohash)["status"], "cancelled")
        self.assertEqual(self.cloud.files, before)
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))

    def test_independent_native_mode_and_seed_switch(self):
        infohash = self.submit()
        self.assertIsNone(self.d.submit({"category": "native"}, torrent(True)))
        self.cfg["enabled"] = False
        with self.assertRaises(ValueError):
            self.submit()
        self.tick(3)
        self.assertEqual(self.qb.calls, [])
        self.cfg["enabled"] = True
        job = self.job(infohash)
        job["seed_after_check"] = False
        self.store.save(job)
        self.checking(infohash)
        self.qb.finish(infohash)
        self.tick(2)
        self.assertEqual(self.job(infohash)["status"], "completed")
        self.assertEqual(self.qb.rows[infohash]["state"], "stoppedUP")

    def test_errors_are_bounded_safe_and_failed_jobs_need_explicit_retry(self):
        infohash = self.submit()
        self.tick(3)
        self.cloud.offline = lambda _: (_ for _ in ()).throw(UpstreamError("CD2 ListOfflineFilesByPath：gRPC 7"))
        self.tick(3)
        job = self.job(infohash)
        self.assertEqual(job["status"], "failed")
        self.assertIn("ListOfflineFilesByPath", job["message"])
        self.assertIn("7", job["message"])
        self.assertEqual(self.submit(), infohash)
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.d.retry(infohash)
        self.assertNotEqual(self.job(infohash)["status"], "failed")
        self.cloud.offline = lambda _: (_ for _ in ()).throw(ConnectionError("DO-NOT-EXPOSE-TOKEN"))
        self.tick(3)
        self.assertNotIn("DO-NOT", json.dumps(self.job(infohash)))

    def test_identity_path_or_file_selection_change_never_starts(self):
        infohash = self.submit()
        self.tick(3)
        self.qb.rows[infohash]["save_path"] = "/elsewhere"
        self.tick()
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.assertEqual(self.cloud.calls, [])

    def test_stop_then_resume_magnet_recreates_metadata_stop_condition(self):
        infohash = metainfo(torrent())["hash"]
        self.d.submit({"category": "radarr", "urls": offline_magnet(infohash)})
        self.tick(3)
        self.d.control([infohash], "stop")
        self.d.control([infohash], "start")
        self.tick(4)
        self.assertEqual(self.job(infohash)["phase"], "metadata")
        self.assertEqual(len([c for c in self.qb.calls if c[0] == "add"]), 2)
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))
        self.assertEqual(self.cloud.calls, [])

    def test_no_check_evidence_never_becomes_completed(self):
        infohash = self.submit()
        self.checking(infohash)
        self.qb.post = lambda path, **params: b""  # Recheck is acknowledged but never runs.
        self.qb.rows[infohash].update(state="stoppedDL", progress=0)
        self.tick(5)
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.assertNotEqual(self.d.overlay(self.qb.get("torrents/info"))[0]["progress"], 1)

    def test_share_limits_preserved_and_torrent_selection_change_fails(self):
        infohash = self.d.submit({"category": "radarr", "ratioLimit": "2.5", "seedingTimeLimit": "120"}, torrent())["hash"]
        self.assertEqual(self.job(infohash)["share_limits"], {"ratioLimit": "2.5", "seedingTimeLimit": "120"})
        self.tick(3)
        self.qb.manifests[infohash][1]["priority"] = 0
        self.tick()
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.assertEqual(self.cloud.calls, [])

    def test_http_arr_login_submit_queue_status_and_guarded_mutations(self):
        server = Server(self.d)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(path, data=None, typ="application/x-www-form-urlencoded"):
            with opener.open(Request(base + path, data=data, headers={"Content-Type": typ}), timeout=5) as response:
                return response.read()

        with self.assertRaises(HTTPError) as denied:
            request("/api/v2/torrents/info")
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        self.assertEqual(request("/api/v2/auth/login", urlencode({"username": "arr", "password": "bad"}).encode()), b"Fails.")
        self.assertEqual(request("/api/v2/auth/login", urlencode({"username": "arr", "password": self.cfg["password"]}).encode()), b"Ok.")
        body, typ = multipart({"category": "radarr"}, torrent())
        self.assertEqual(request("/api/v2/torrents/add", body, typ), b"Ok.")
        rows = json.loads(request("/api/v2/torrents/info?category=radarr"))
        self.assertEqual(len(rows), 1)
        self.assertLess(rows[0]["progress"], 1)
        infohash = rows[0]["hash"]
        properties = json.loads(request("/api/v2/torrents/properties?hash=" + infohash))
        self.assertEqual(properties["total_size"], 24)
        self.assertEqual(properties["completion_date"], -1)
        self.assertEqual(json.loads(request("/api/v2/torrents/files?hash=" + infohash)), [])
        self.assertEqual(json.loads(request("/api/v2/torrents/info?category=other")), [])
        self.assertEqual(json.loads(request("/api/v2/torrents/info?filter=completed")), [])
        self.assertNotIn(b"MUST-NOT-LEAK", request("/api/v2/app/preferences"))
        self.assertEqual(json.loads(request("/v1/downloads/" + infohash))["status"], "new")
        with self.assertRaises(HTTPError) as invalid:
            request("/api/v2/torrents/delete", urlencode({"hashes": infohash, "deleteFiles": "true"}).encode())
        self.assertEqual(invalid.exception.code, 400)
        invalid.exception.close()
        with self.assertRaises(HTTPError) as forbidden:
            request("/api/v2/app/setPreferences", b"json={}")
        self.assertEqual(forbidden.exception.code, 405)
        forbidden.exception.close()

    def test_failed_task_keeps_phase_and_restart_timeout_never_resumes(self):
        infohash = self.submit()
        self.tick(4)
        self.now += self.cfg["timeout_seconds"] + 1
        self.tick()
        self.assertEqual(self.job(infohash)["status"], "failed")
        self.assertEqual(self.job(infohash)["phase"], "offline")
        requested = self.job(infohash)["offline_requested"]
        self.d.retry(infohash)
        self.assertEqual(self.job(infohash)["offline_requested"], requested)
        self.assertFalse(any(c[0] == "torrents/start" for c in self.qb.calls))

    def test_http_parallel_delivery_files_and_cleanup_do_not_change_real_qb(self):
        self.cfg["import_after"] = "cloud_ready"
        infohash = self.submit()
        self.checking(infohash)
        server = Server(self.d)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(path, values=None):
            data = urlencode(values).encode() if values is not None else None
            with opener.open(Request(base + path, data=data), timeout=5) as response:
                return response.read()

        request("/api/v2/auth/login", {"username": "arr", "password": self.cfg["password"]})
        rows = json.loads(request("/api/v2/torrents/info?filter=completed"))
        self.assertEqual([row["hash"] for row in rows], [infohash])
        files = json.loads(request("/api/v2/torrents/files?hash=" + infohash))
        self.assertTrue(all(f["progress"] == 1 for f in files))
        properties = json.loads(request("/api/v2/torrents/properties?hash=" + infohash))
        self.assertGreater(properties["completion_date"], 0)
        self.assertTrue(all(f["progress"] == 0 for f in self.qb.manifests[infohash]))
        with self.assertRaises(HTTPError) as invalid:
            request("/api/v2/torrents/delete", {"hashes": infohash, "deleteFiles": "true"})
        self.assertEqual(invalid.exception.code, 400)
        invalid.exception.close()
        request("/api/v2/torrents/delete", {"hashes": infohash, "deleteFiles": "false"})
        self.assertEqual(json.loads(request("/api/v2/torrents/info")), [])
        self.assertEqual(self.qb.rows[infohash]["state"], "checkingDL")
        with self.assertRaises(HTTPError) as removed:
            request("/api/v2/torrents/files?hash=" + infohash)
        self.assertEqual(removed.exception.code, 404)
        removed.exception.close()
        public = json.loads(request("/v1/downloads/" + infohash))
        self.assertEqual(public["delivery_status"], "removed")
        self.assertEqual(public["seeding_status"], "checking")


class MetadataTests(unittest.TestCase):
    def test_original_info_hash_and_manifest(self):
        meta = metainfo(torrent())
        self.assertEqual(meta["size"], 24)
        self.assertEqual(len(meta["files"]), 2)
        self.assertEqual(meta["name"], "Show & 电影")
        raw_info = torrent().split(b"4:info", 1)[1][:-1]
        self.assertEqual(meta["hash"], hashlib.sha1(raw_info).hexdigest())
        encoded = base64.b32encode(bytes.fromhex(meta["hash"])).decode()
        self.assertEqual(magnet("magnet:?xt=urn:btih:" + encoded), meta["hash"])
        self.assertEqual(magnet(offline_magnet(meta["hash"])), meta["hash"])

    def test_private_v2_path_traversal_duplicates_and_invalid_hashes_rejected(self):
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
        del info[b"meta version"]
        info[b"pieces"] = b"wrong"
        with self.assertRaises(ValueError):
            metainfo(bencode({b"info": info}))

    def test_qb_514_wire_options_metadata_stop_and_full_check(self):
        qb = QB(config()["qb"])
        meta = metainfo(torrent())
        job = {"hash": meta["hash"], "qb_path": "/downloads/.arr-offline/" + meta["hash"],
               "category": "radarr", "tag": "arr-offline-" + meta["hash"], "magnet": offline_magnet(meta["hash"])}
        calls = []

        def raw(path, data=None, typ=None):
            if path == "/api/v2/app/version":
                return b"v5.1.4", "text/plain"
            values, file = form(data, typ)
            calls.append((values, file))
            return b"Ok.", "text/plain"

        with patch.object(qb, "raw", side_effect=raw):
            qb.add(job, torrent())
            qb.add(job)
        self.assertEqual(calls[0][0]["stopped"], "true")
        self.assertEqual(calls[0][0]["stopCondition"], "FilesChecked")
        self.assertEqual(calls[0][1], torrent())
        self.assertEqual(calls[1][0]["stopCondition"], "MetadataReceived")
        for values, _ in calls:
            self.assertEqual(values["skip_checking"], "false")
            self.assertEqual(values["autoTMM"], "false")
            self.assertEqual(values["useDownloadPath"], "false")

    def test_cd2_safe_interface_and_status_without_upstream_secret(self):
        cloud = OfflineCloud(config()["cloud"])
        with patch.object(cloud.client, "open", side_effect=ConnectionError("CloudDrive gRPC 状态 7；SECRET-TOKEN")):
            with self.assertRaises(UpstreamError) as failure:
                cloud.offline("/115open/pt/download")
        self.assertIn("ListOfflineFilesByPath", str(failure.exception))
        self.assertIn("7", str(failure.exception))
        self.assertNotIn("SECRET", str(failure.exception))
        with patch.object(cloud.client, "open", side_effect=ConnectionError("CloudDrive gRPC 状态 5")):
            self.assertIsNone(cloud.get_file("/115open/missing/file.mkv"))
        body, typ = multipart({"category": "radarr"}, torrent())
        with self.assertRaises(ValueError):
            form(body.replace(b'name="category"', b'name="torrents"'), typ)

    def test_qb_actual_http_auth_cookie_origin_and_upstream_error_redaction(self):
        logins = []
        origins = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                logins.append(self.path)
                self.send_response(200)
                self.send_header("Set-Cookie", "SID=test-qb-session; Path=/")
                self.end_headers()
                self.wfile.write(b"Ok.")

            def do_GET(self):
                origins.append(self.headers.get("Origin"))
                valid = self.headers.get("Cookie") == "SID=test-qb-session"
                status = 200 if valid and self.path.endswith("version") else 500
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"v5.1.4" if status == 200 else b"DO-NOT-EXPOSE-TOKEN")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        qb = QB({"url": url, "username": "admin", "password": "fixture-password"})
        self.assertEqual(qb.raw("/api/v2/app/version")[0], b"v5.1.4")
        self.assertEqual(qb.raw("/api/v2/app/version")[0], b"v5.1.4")
        self.assertEqual(len(logins), 1)
        self.assertEqual(origins, [url, url])
        with self.assertRaises(UpstreamError) as failure:
            qb.raw("/api/v2/failure")
        self.assertIn("500", str(failure.exception))
        self.assertNotIn("DO-NOT", str(failure.exception))

    def test_configuration_explicit_switches_and_account_boundary(self):
        cfg = config()
        cfg["port"] = 8097
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "config.json"
            p.write_text(json.dumps(cfg))
            loaded = load(p)
            self.assertTrue(loaded["seed_after_check"])
            self.assertEqual(loaded["import_after"], "qb_checked")
            cfg["import_after"] = "cloud_ready"
            p.write_text(json.dumps(cfg))
            self.assertEqual(load(p)["import_after"], "cloud_ready")
            cfg["import_after"] = "invalid"
            p.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                load(p)
            cfg["import_after"] = "qb_checked"
            cfg["cloud_download_root"] = "/another-account/download"
            p.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                load(p)


if __name__ == "__main__":
    unittest.main()
