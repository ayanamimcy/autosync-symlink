"""115 offline downloads presented to Radarr/Sonarr as a qBittorrent client.

There is no real torrent client behind this: every state comes from the CD2
offline task list and from the files that actually landed in the job folder.
"""
import json
import os
import sqlite3
import threading
import time

from bridge.arr_import.cloud import OperationRejected
from .clients import Arr, OfflineCloud, UpstreamError
from .metadata import magnet, magnet_parts, metainfo, offline_magnet

ACTIVE = ("queued", "downloading", "verifying")
# Fields changed by Arr requests. The worker never overwrites them with a stale copy.
CLIENT_FIELDS = ("removed", "category", "tags")
SUBMIT_GRACE = 600
LARGE = 1024 * 1024


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        # A new table: rows of the earlier qB-seeding design are left untouched.
        self.db.execute("CREATE TABLE IF NOT EXISTS offline_jobs (hash TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.commit()

    def get(self, infohash):
        with self.lock:
            row = self.db.execute("SELECT body FROM offline_jobs WHERE hash=?", (infohash,)).fetchone()
            return json.loads(row[0]) if row else None

    def all(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM offline_jobs ORDER BY rowid")]

    def save(self, job):
        with self.lock, self.db:
            self.db.execute("INSERT INTO offline_jobs VALUES(?,?) ON CONFLICT(hash) DO UPDATE SET body=excluded.body",
                            (job["hash"], json.dumps(job, ensure_ascii=False)))

    def delete(self, infohash):
        with self.lock, self.db:
            self.db.execute("DELETE FROM offline_jobs WHERE hash=?", (infohash,))

    def close(self):
        self.db.close()


def complete(job, files):
    """Whether the listed folder holds the whole torrent, whatever layout 115 chose."""
    if not files or any(f["local"] or not f["id"] for f in files.values()):
        return False
    if job.get("files"):
        # Match by file name and size: 115 may or may not keep the torrent's root folder.
        have = {}
        for rel, f in files.items():
            key = (rel.rsplit("/", 1)[-1], f["size"])
            have[key] = have.get(key, 0) + 1
        for entry in job["files"]:
            if entry["size"] < LARGE:
                continue  # 115 sometimes skips tiny extras such as .txt/.url
            key = (entry["path"].rsplit("/", 1)[-1], entry["size"])
            if not have.get(key):
                return False
            have[key] -= 1
        return True
    total = sum(f["size"] for f in files.values())
    return total > 0 and (not job.get("size") or total >= job["size"] - LARGE)


class Downloader:
    def __init__(self, config, store, cloud_factory=OfflineCloud, arr_factory=Arr, clock=time.time):
        self.config, self.store, self.clock = config, store, clock
        self.cloud_factory, self.arr_factory = cloud_factory, arr_factory
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.extra_categories = set()

    # Paths -----------------------------------------------------------------
    def folder(self, infohash):
        return self.config["cloud_download_root"] + "/.arr-offline/" + infohash

    def arr_folder(self, infohash):
        return self.config["arr_download_root"] + "/.arr-offline/" + infohash

    # Requests from Arr -----------------------------------------------------
    def submit(self, values, torrent=None):
        category = values.get("category", "")
        if not self.config["enabled"]:
            raise ValueError("离线下载已停用")
        if category not in self.config["categories"]:
            raise ValueError("分类未配置为离线下载")
        if torrent is not None:
            meta = metainfo(torrent)
            infohash, name, trackers = meta["hash"], meta["name"], meta["trackers"]
        else:
            link = values.get("urls", "").strip()
            if "\n" in link:
                raise ValueError("每次只接受一个磁力链接")
            meta, infohash = None, magnet(link)
            name, trackers = magnet_parts(link)
        if not self.config["send_trackers"]:
            trackers = []
        with self.lock:
            previous = self.store.get(infohash)
            if previous and not previous.get("removed"):
                raise FileExistsError("同一种子已在离线任务中")
            if previous:
                self.store.delete(infohash)  # Re-grab after removal: start over.
            job = {"hash": infohash, "category": category, "tags": values.get("tags", ""),
                   "name": name or infohash, "size": meta["size"] if meta else 0,
                   "files": meta["files"] if meta else None,
                   "magnet": offline_magnet(infohash, name, trackers),
                   "status": "queued", "message": "等待提交 115 离线", "progress": 0.0, "peers": 0,
                   "added": self.clock(), "errors": 0, "submitted": False, "removed": False}
            self.store.save(job)
            return job

    def remove(self, hashes, delete_data):
        with self.lock:
            for infohash in hashes:
                job = self.store.get(infohash)
                if not job or job.get("removed"):
                    continue
                job["removed"] = True
                job["remove_data"] = bool(delete_data and self.config["delete_files"])
                if job["status"] in ACTIVE:
                    job["message"] = "Arr 已移除；清除 115 离线记录（不删文件）"
                self.store.save(job)

    def set_category(self, hashes, category):
        with self.lock:
            for infohash in hashes:
                job = self.store.get(infohash)
                if job:
                    job["category"] = category
                    self.store.save(job)

    def retry(self, infohash):
        with self.lock:
            job = self.store.get(infohash)
            if not job or job["status"] != "failed":
                raise ValueError("只能重试失败的任务")
            job.update(status="downloading" if job["submitted"] else "queued", errors=0,
                       added=self.clock(), message="已安排重试", notify=False, record_cleared=False)
            job.pop("stable_since", None)
            self.store.save(job)
            return self.public(job)

    @staticmethod
    def public(job):
        return {k: job.get(k) for k in ("hash", "category", "name", "size", "status", "message",
                                        "progress", "peers", "removed", "content")}

    # Worker ----------------------------------------------------------------
    def fail(self, job, message):
        # Arr no longer tracks a removed item: never mark its grab failed.
        job.update(status="failed", message=message, notify=bool(self.instance(job)) and not job.get("removed"),
                   notify_attempts=0)

    def instance(self, job):
        return next((i for i in self.config["arr"] if i["category"] == job["category"]), None)

    def advance(self, job, cloud):
        now = self.clock()
        if now - job["added"] > self.config["timeout_seconds"]:
            self.fail(job, "离线下载或核验超时")
            return
        folder = self.folder(job["hash"])
        if job["status"] == "queued":
            cloud.mkdir(folder, self.config["account_root"])
            if not any(r["hash"] == job["hash"] for r in cloud.offline(folder)):
                if job["submitted"]:
                    if now - job["submitted_at"] > SUBMIT_GRACE:
                        self.fail(job, "已提交但 115 离线列表中没有该任务；在 CD2 中核实后重试")
                    return
                # Persist the intent first: a lost reply is reconciled by listing, never resubmitted.
                job.update(submitted=True, submitted_at=now, message="已提交 115 离线")
                self.persist(job)
                try:
                    cloud.add_offline(job["magnet"], folder)
                except OperationRejected as error:
                    job["submitted"] = False
                    if job.get("record_cleared"):
                        self.fail(job, "115 拒绝离线任务（" + str(error) + "；配额、链接无效或资源不可用）")
                        return
                    # Usually an earlier offline record of the same infohash ("任务已存在"):
                    # drop that record, keeping any files, and submit again on the next pass.
                    cloud.remove_offline(job["hash"], self.config["account_root"])
                    job.update(record_cleared=True, message="115 已有同一资源的离线记录；已清除旧记录，重新提交")
                return
            job.update(submitted=True, status="downloading")
        if job["status"] == "downloading":
            row = next((r for r in cloud.offline(folder) if r["hash"] == job["hash"]), None)
            if not row:
                self.fail(job, "115 离线任务已不存在")
                return
            if row["name"] and job["name"] == job["hash"]:
                job["name"] = row["name"]
            if row["size"] and not job["size"]:
                job["size"] = row["size"]
            if row["status"] == 3:
                self.fail(job, "115 离线下载失败")
                return
            if row["status"] != 2:
                job.update(progress=min(max(row["percent"], 0.0), 100.0) / 100 * 0.99, peers=row["peers"],
                           message="115 离线下载中" if row["status"] == 1 else "115 离线排队中")
                return
            job.update(status="verifying", progress=0.99, message="离线完成；核验离线目录")
        if job["status"] == "verifying":
            files = cloud.walk(folder) or {}
            if not complete(job, files):
                job.pop("stable_since", None)
                job["message"] = "等待离线文件完整出现在离线目录"
                return
            fingerprint = sorted([rel, f["size"], f["hashes"].get(2, "")] for rel, f in files.items())
            if job.get("fingerprint") != fingerprint:
                job.update(fingerprint=fingerprint, stable_since=now, message="离线目录稳定性核验中")
                return
            if now - job["stable_since"] < self.config["settle_seconds"]:
                return
            tops = {rel.split("/", 1)[0] for rel in files}
            job.update(status="ready", progress=1.0, ready_at=now, content=tops.pop() if len(tops) == 1 else "",
                       message="离线完成，等待 Arr 入库")

    def cleanup(self, job, cloud):
        """Arr removed the item: drop the 115 offline record and, with deleteFiles, the folder."""
        if job.get("submitted"):
            # Stops a running offline task and lets a later re-grab submit the same infohash.
            cloud.remove_offline(job["hash"], self.config["account_root"])
            job["submitted"] = False
        if job.get("remove_data"):
            folder = self.folder(job["hash"])
            if cloud.get_file(folder):
                cloud.delete(folder)
            job.update(remove_data=False, message="已按 Arr 请求删除离线目录（进入网盘回收站）")

    def notify(self, job):
        instance = self.instance(job)
        if not instance:
            job["notify"] = False
            return
        try:
            found = self.arr_factory(instance).mark_failed(job["hash"])
        except UpstreamError as error:
            job["notify_attempts"] = job.get("notify_attempts", 0) + 1
            if job["notify_attempts"] >= self.config["max_errors"]:
                job.update(notify=False, message=job["message"] + "；通知 Arr 失败（" + str(error) + "）")
            return
        job["notify"] = False
        job["message"] += "；已通知 Arr 标记失败" if found else "；Arr 中没有对应的抓取记录"

    def step(self, job):
        cloud = None
        if job.get("removed") and (job.get("submitted") or job.get("remove_data")):
            cloud = self.cloud_factory(self.config["cloud"])
            self.cleanup(job, cloud)
        if job.get("notify") and job.get("removed"):
            job["notify"] = False
        if job.get("notify"):
            self.notify(job)
        if job["status"] in ACTIVE and not job.get("removed"):
            self.advance(job, cloud or self.cloud_factory(self.config["cloud"]))

    def tick(self):
        for candidate in self.store.all():
            if self.stop.is_set():
                break
            cleanup = candidate.get("removed") and (candidate.get("submitted") or candidate.get("remove_data"))
            if not (candidate["status"] in ACTIVE and not candidate.get("removed") or cleanup or candidate.get("notify")):
                continue
            if not self.config["enabled"] and not cleanup:
                continue
            job = dict(candidate)
            try:
                self.step(job)
            except Exception as error:
                job["errors"] += 1
                detail = str(error) if type(error) in (ValueError, UpstreamError) else type(error).__name__
                job["message"] = "暂时失败：" + detail
                if job["errors"] >= self.config["max_errors"]:
                    if cleanup:
                        # Cleanup after Arr removed the item; the download itself is unaffected.
                        job.update(remove_data=False, submitted=False, errors=0,
                                   message="清理离线记录或目录失败，已放弃：" + detail + "（检查 CD2 令牌的删除权限）")
                    else:
                        self.fail(job, "连续出错：" + detail)
            else:
                job["errors"] = 0
            self.persist(job, candidate)

    def persist(self, job, candidate=None):
        with self.lock:
            # Network calls run without the lock; keep what Arr changed meanwhile.
            fresh = self.store.get(job["hash"])
            if not fresh:
                return
            job.update({k: fresh[k] for k in CLIENT_FIELDS if k in fresh})
            if candidate is None or (fresh.get("remove_data") and not candidate.get("remove_data")):
                # Mid-pass save, or a deletion Arr requested while this pass was running.
                job["remove_data"] = fresh.get("remove_data", False)
            self.store.save(job)

    def start(self):
        if self.config["retired_keys"]:
            print("已忽略旧配置项（不再使用真实 qB）：" + ", ".join(self.config["retired_keys"]), flush=True)

        def work():
            while not self.stop.is_set():
                self.tick()
                self.stop.wait(self.config["poll_seconds"])
        self.thread = threading.Thread(target=work, name="arr-download", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=60)

    # qB views --------------------------------------------------------------
    def paths(self, job):
        """(save_path, content_path) as Arr sees them."""
        base = self.arr_folder(job["hash"])
        if job["status"] != "ready":
            return base, base + "/" + job["name"]
        if job.get("content"):
            return base, base + "/" + job["content"]
        # Several top-level entries: present the job folder itself as the content.
        return self.config["arr_download_root"] + "/.arr-offline", base

    def row(self, job):
        save_path, content_path = self.paths(job)
        state = {"queued": "metaDL", "downloading": "downloading", "verifying": "checkingDL",
                 "ready": "stoppedUP", "failed": "error"}[job["status"]]
        size = job.get("size") or 0
        done = job["status"] == "ready"
        return {"hash": job["hash"], "name": job["name"], "category": job["category"], "tags": job.get("tags", ""),
                "state": state, "progress": job["progress"], "size": size, "total_size": size,
                "amount_left": 0 if done else int(size * (1 - job["progress"])),
                "completed": size if done else int(size * job["progress"]), "downloaded": 0, "uploaded": 0,
                "dlspeed": 0, "upspeed": 0, "eta": 0 if done else 8640000, "num_seeds": job.get("peers", 0),
                "num_leechs": 0, "ratio": 0, "ratio_limit": 0, "seeding_time_limit": -2,
                "inactive_seeding_time_limit": -2, "seeding_time": 0, "max_ratio": 0, "max_seeding_time": -1,
                "added_on": int(job["added"]), "completion_on": int(job.get("ready_at", 0)) if done else -1,
                "last_activity": int(job.get("ready_at") or job["added"]), "auto_tmm": False,
                "save_path": save_path, "content_path": content_path, "download_path": "",
                "magnet_uri": job["magnet"], "priority": 0, "force_start": False}

    def rows(self, category=None, hashes=None):
        return [self.row(j) for j in self.store.all()
                if not j.get("removed") and (category is None or j["category"] == category)
                and (hashes is None or j["hash"] in hashes)]

    def files(self, job):
        save_path, content_path = self.paths(job)
        prefix = content_path[len(save_path) + 1:]
        if job.get("fingerprint") and job["status"] == "ready":
            names = [(rel if job.get("content") else prefix + "/" + rel, size) for rel, size, _ in job["fingerprint"]]
        elif job.get("files"):
            names = [(f["path"], f["size"]) for f in job["files"]]
        else:
            return []
        progress = 1 if job["status"] == "ready" else 0
        return [{"index": i, "name": name, "size": size, "progress": progress, "priority": 1,
                 "is_seed": False, "piece_range": [0, 0], "availability": -1}
                for i, (name, size) in enumerate(names)]

    def properties(self, job):
        save_path, _ = self.paths(job)
        done = job["status"] == "ready"
        return {"hash": job["hash"], "name": job["name"], "save_path": save_path,
                "total_size": job.get("size") or 0, "addition_date": int(job["added"]),
                "completion_date": int(job.get("ready_at", 0)) if done else -1, "creation_date": -1,
                "seeding_time": 0, "share_ratio": 0, "total_downloaded": 0, "total_uploaded": 0,
                "pieces_have": 0, "comment": job["message"], "seeds": job.get("peers", 0)}

