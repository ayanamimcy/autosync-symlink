import base64
import json
import os
import posixpath
import re
import sqlite3
import threading
import time

from bridge.arr_import.cloud import OperationRejected
from .clients import OfflineCloud, QB, UpstreamError
from .metadata import magnet, metainfo, offline_magnet

TERMINAL = {"completed", "failed", "cancelled"}
STOPPED = {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}
CHECKING = {"checkingDL", "checkingUP", "checkingResumeData", "queuedForChecking"}


def deliverable(job):
    """115 delivery and the real qB seeding task have independent lifetimes."""
    if job.get("delivery_blocked") or job["status"] == "cancelled":
        return False
    if job["status"] == "completed":
        return True  # Also supports tasks created before separate delivery states.
    if job.get("import_after", "qb_checked") == "cloud_ready":
        return bool(job.get("cloud_ready"))
    return bool(job.get("qb_checked"))


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS downloads (hash TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.commit()

    def get(self, infohash):
        with self.lock:
            row = self.db.execute("SELECT body FROM downloads WHERE hash=?", (infohash,)).fetchone()
            return json.loads(row[0]) if row else None

    def all(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM downloads ORDER BY rowid")]

    def save(self, job):
        with self.lock, self.db:
            self.db.execute("INSERT INTO downloads VALUES(?,?) ON CONFLICT(hash) DO UPDATE SET body=excluded.body",
                            (job["hash"], json.dumps(job)))

    def close(self):
        self.db.close()


class Downloader:
    def __init__(self, config, store, qb=None, cloud_factory=OfflineCloud, clock=time.time):
        self.config, self.store, self.clock = config, store, clock
        self.qb = qb or QB(config["qb"])
        self.cloud_factory = cloud_factory
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None

    def submit(self, values, torrent=None):
        category = values.get("category", "")
        if category not in self.config["offline_categories"] or not self.config["enabled"]:
            # Turning the mode off must not resume an existing offline job via qB.
            active = {j["hash"] for j in self.store.all() if j["status"] not in ("completed", "cancelled")}
            if active:
                infohash = metainfo(torrent, allow_private=True)["hash"] if torrent is not None else magnet(values.get("urls", ""))
                if infohash in active:
                    raise ValueError("已有离线任务；不能通过直通模式重新启动同一种子")
            return None
        if values.get("savepath", "").rstrip("/") not in ("", self.config["qb_download_root"]):
            raise ValueError("离线任务 savepath 必须与 qb_download_root 一致")
        if values.get("rename"):
            raise ValueError("离线接种不接受下载时改名；由入库脚本处理媒体命名")
        share_limits = {}
        for key in ("ratioLimit", "seedingTimeLimit", "inactiveSeedingTimeLimit"):
            if key in values:
                pattern = r"-?[0-9]{1,9}(?:\.[0-9]{1,6})?" if key == "ratioLimit" else r"-?[0-9]{1,9}"
                if not re.fullmatch(pattern, values[key]):
                    raise ValueError("做种分享限制无效")
                share_limits[key] = values[key]
        meta = metainfo(torrent) if torrent is not None else None
        link = values.get("urls", "")
        infohash = meta["hash"] if meta else magnet(link)
        with self.lock:
            previous = self.store.get(infohash)
            if previous:
                if previous["category"] != category:
                    raise ValueError("同一种子已属于其他分类的离线任务")
                if previous["status"] == "cancelled":
                    raise ValueError("已取消任务不能自动重新提交")
                return previous
            if self.qb.info(infohash):
                raise ValueError("qB 已存在同 infohash 任务；不接管现有种子")
            suffix = "/.arr-offline/" + infohash
            job = {"hash": infohash, "category": category, "tag": "arr-offline-" + infohash,
                   "qb_path": self.config["qb_download_root"] + suffix,
                   "cloud_path": self.config["cloud_download_root"] + suffix,
                   "magnet": link if meta is None else offline_magnet(infohash),
                   "torrent": base64.b64encode(torrent).decode() if torrent is not None else "",
                   "meta": meta, "phase": "new", "status": "new", "message": "等待建立 qB 元数据任务",
                   "share_limits": share_limits,
                   "started": self.clock(), "errors": 0, "paused": False, "progress": 0,
                   "seed_after_check": self.config["seed_after_check"],
                   "import_after": self.config.get("import_after", "qb_checked"),
                   "cloud_ready": False, "qb_checked": False, "delivery_detached": False}
            self.store.save(job)
            return job

    @staticmethod
    def public(job):
        result = {key: job[key] for key in ("hash", "category", "status", "phase", "message", "progress", "paused")}
        result.update(import_after=job.get("import_after", "qb_checked"),
                      delivery_status="removed" if job.get("delivery_detached") else
                      "ready" if deliverable(job) else "blocked" if job.get("delivery_blocked") else "waiting",
                      seeding_status="failed" if job["status"] == "failed" else
                      "cancelled" if job["status"] == "cancelled" else
                      "seeding_requested" if job["status"] == "completed" and job["seed_after_check"] else
                      "stopped" if job["status"] == "completed" else job["phase"],
                      qb_checked=bool(job.get("qb_checked", job["status"] == "completed")))
        if deliverable(job) and job["status"] == "failed":
            result["delivery_warning"] = "交付条件已满足，但后台 qB 校验或做种失败；已入库副本保留，请检查"
        return result

    def state(self, job, phase, message):
        job.update(phase=phase, status=phase, message=message)

    def owned(self, job, row):
        if (not row or row.get("hash", "").lower() != job["hash"]
                or row.get("save_path", "").rstrip("/") != job["qb_path"]
                or row.get("category") != job["category"]
                or job["tag"] not in {t.strip() for t in row.get("tags", "").split(",")}
                or row.get("auto_tmm") is True):
            job["delivery_blocked"] = True
            raise ValueError("qB 任务身份、分类或保存路径已改变；拒绝接管")

    def files(self, job):
        rows = self.qb.get("torrents/files", hash=job["hash"])
        if not rows:
            return None
        expected = {entry["path"]: entry["size"] for entry in job["meta"]["files"]}
        actual = {}
        for row in rows:
            name = row.get("name")
            if name in actual or row.get("priority", 0) <= 0:
                raise ValueError("qB 文件重复或有跳过的文件；不确认完整做种")
            actual[name] = row.get("size")
        if expected != actual:
            raise ValueError("qB 文件结构与原种子不一致")
        return rows

    def remote_ready(self, job, cloud):
        # Cache only within this pass. Every later pass refreshes the provider.
        parents = {}
        fingerprint = []
        for entry in job["meta"]["files"]:
            path = job["cloud_path"] + "/" + entry["path"]
            parent = posixpath.dirname(path)
            if parent not in parents:
                try:
                    parents[parent] = {f["path"]: f for f in cloud.list_files(parent)}
                except UpstreamError as error:
                    if error.grpc_status == 5:
                        return None
                    raise
            f = parents[parent].get(path)
            if (not f or f["directory"] or f["local"] or not f["id"] or f["size"] != entry["size"]
                    or (entry["size"] > 0 and not re.fullmatch(r"[a-fA-F0-9]{40}", f["hashes"].get(2, "")))):
                return None
            fingerprint.append((path, f["size"], f["hashes"].get(2, "").lower()))
        if any(row["state"] != 5 for row in cloud.uploads(job["cloud_path"])):
            return None
        return json.dumps(fingerprint, ensure_ascii=False)

    def advance(self, job):
        if self.clock() - job["started"] > self.config["timeout_seconds"]:
            raise ValueError("离线、元数据获取或校验超时；任务和源文件保留")
        row = self.qb.info(job["hash"])
        if job["phase"] == "metadata_reset":
            # qB's stop() clears MetadataReceived. Re-add only an empty metadata task
            # instead of resuming it without the stop condition and downloading payload.
            if row:
                self.owned(job, row)
                if self.qb.get("torrents/files", hash=job["hash"]):
                    self.state(job, "metadata", "元数据已到达，保留现有任务")
                elif row["state"] not in STOPPED:
                    self.qb.post("torrents/stop", hashes=job["hash"])
                else:
                    self.qb.post("torrents/delete", hashes=job["hash"], deleteFiles="false")
            else:
                job["qb_requested"] = False
                self.state(job, "new", "重新添加仅获取元数据的磁力任务")
            return
        if job["phase"] == "new":
            if row:
                if not job.get("qb_requested"):
                    raise ValueError("qB 出现同 infohash 任务；拒绝覆盖")
                self.owned(job, row)
                self.state(job, "metadata", "等待 qB 元数据和原始文件清单")
                return
            job["qb_requested"] = True
            self.store.save(job)
            self.qb.add(job, base64.b64decode(job["torrent"]) if job["torrent"] else None)
            return
        self.owned(job, row)
        if job["phase"] == "metadata":
            rows = self.qb.get("torrents/files", hash=job["hash"])
            if not rows:
                return
            if row["state"] not in STOPPED:
                self.qb.post("torrents/stop", hashes=job["hash"])
                return
            if job["meta"] is None:
                raw = self.qb.export(job["hash"])
                meta = metainfo(raw)
                if meta["hash"] != job["hash"]:
                    raise ValueError("磁力元数据 infohash 不一致")
                job.update(meta=meta, torrent=base64.b64encode(raw).decode())
            self.files(job)
            self.state(job, "offline", "等待 115 离线任务")
            return
        if job["phase"] not in ("checking", "verified") and row["state"] not in STOPPED:
            self.qb.post("torrents/stop", hashes=job["hash"])
            raise ValueError("校验完成前 qB 被启动；已请求停止，请检查直接操作或自动规则")
        rows = self.files(job)
        if not rows:
            raise ValueError("qB 原始文件清单已丢失；拒绝继续离线或做种")
        cloud = self.cloud_factory(self.config["cloud"])
        if job["phase"] == "offline":
            cloud.mkdir(job["cloud_path"], self.config["account_root"])
            matches = [item for item in cloud.offline(job["cloud_path"]) if item["hash"] == job["hash"]]
            if len(matches) > 1:
                raise ValueError("115 返回同 infohash 的多个离线任务")
            if not matches:
                if not job.get("offline_requested"):
                    job["offline_requested"] = True
                    self.store.save(job)
                    try:
                        cloud.add_offline(offline_magnet(job["hash"]), job["cloud_path"])
                    except OperationRejected:
                        job["offline_requested"] = False
                        raise UpstreamError("CD2 AddOfflineFiles 未返回成功") from None
                # A lost response is reconciled by listing; never submit blindly again.
                return
            status = matches[0]["status"]
            if status == 3:
                raise ValueError("115 离线任务失败；保留任务，未启动 qB 下载")
            if status not in (0, 1, 2):
                raise ValueError("115 离线状态未知，拒绝确认完成")
            if status != 2:
                return
            self.state(job, "remote_verify", "离线完成；核验完整文件集合和云端稳定性")
            job["progress"] = 0.9
            return
        if job["phase"] == "remote_verify":
            fingerprint = self.remote_ready(job, cloud)
            if not fingerprint:
                job.pop("stable_since", None)
                return
            if job.get("fingerprint") != fingerprint or "stable_since" not in job:
                job.update(fingerprint=fingerprint, stable_since=self.clock())
                return
            if self.clock() - job["stable_since"] < self.config["settle_seconds"]:
                return
            job.update(progress=0.95, progress_before_check=row.get("progress", 0), checking_seen=False,
                       check_requested=True, check_attempts=1, check_started=self.clock(),
                       cloud_ready=True, cloud_ready_at=self.clock(), delivery_blocked=False)
            self.state(job, "checking", "已请求 qB 完整分块校验；等待 100%")
            self.store.save(job)
            self.qb.post("torrents/recheck", hashes=job["hash"])
            return
        if job["phase"] == "checking":
            if row["state"] in CHECKING:
                job["checking_seen"] = True
                return
            if row["state"] not in STOPPED:
                self.qb.post("torrents/stop", hashes=job["hash"])
                raise ValueError("qB 校验后意外进入下载状态；已请求停止")
            if not (job.get("checking_seen") or (job.get("progress_before_check", 1) < 1 and row.get("progress", 0) >= 1)):
                # On restart a lost recheck response is safe to repeat while stopped.
                if job.get("check_attempts", 0) >= 3:
                    raise ValueError("未观察到完整校验完成；保持停止，请检查 qB 校验状态")
                job["check_attempts"] = job.get("check_attempts", 0) + 1
                self.store.save(job)
                self.qb.post("torrents/recheck", hashes=job["hash"])
                return
            if (row.get("progress") != 1 or row.get("amount_left") != 0
                    or not rows or any(f.get("progress") != 1 for f in rows)):
                raise ValueError("qB 完整校验未达到 100%；保持停止，未自动补下载")
            # Verify the remote set did not change during the potentially long check.
            if self.remote_ready(job, cloud) != job["fingerprint"]:
                job["delivery_blocked"] = True
                raise ValueError("校验期间云端文件变化；拒绝做种和确认完成")
            job.update(qb_checked=True, qb_checked_at=self.clock())
            self.state(job, "verified", "完整分块校验通过，等待设置做种状态")
            return
        if job["phase"] == "verified":
            if row.get("progress") != 1 or row.get("amount_left") != 0 or any(f.get("progress") != 1 for f in rows):
                raise ValueError("已校验数据的 qB 完成状态发生变化")
            if row["state"] not in STOPPED and row["state"] not in ("uploading", "stalledUP", "queuedUP", "forcedUP"):
                self.qb.post("torrents/stop", hashes=job["hash"])
                raise ValueError("已校验任务的 qB 状态异常；已请求停止")
            if job["seed_after_check"]:
                if row["state"] in STOPPED:
                    self.qb.post("torrents/start", hashes=job["hash"])
            elif row["state"] not in STOPPED:
                self.qb.post("torrents/stop", hashes=job["hash"])
            self.state(job, "completed", "qB 完整校验通过，已请求做种" if job["seed_after_check"] else "qB 完整校验通过，保持停止")
            job["progress"] = 1

    def tick(self):
        with self.lock:
            for job in self.store.all():
                if self.stop.is_set():
                    break
                if job["status"] in TERMINAL or job["paused"] or not self.config["enabled"]:
                    continue
                try:
                    self.advance(job)
                except Exception as error:
                    job["errors"] += 1
                    detail = str(error) if type(error) in (ValueError, UpstreamError) else type(error).__name__
                    job["message"] = detail
                    if type(error) is ValueError or job["errors"] >= self.config["max_errors"]:
                        job["status"] = "failed"
                else:
                    job["errors"] = 0
                self.store.save(job)

    def control(self, hashes, action):
        with self.lock:
            jobs = [self.store.get(h) for h in hashes]
            forward = [h for h, j in zip(hashes, jobs)
                       if (not j or j["status"] in ("completed", "cancelled") or action == "setShareLimits")
                       and not (j and action == "delete" and deliverable(j))]
            if any(j and j["status"] not in ("completed", "cancelled") for j in jobs) and action not in ("stop", "start", "delete", "setShareLimits"):
                raise ValueError("活动离线任务不允许移动、改名或更改文件优先级")
            for infohash, job in zip(hashes, jobs):
                if job and action == "delete" and deliverable(job):
                    # Arr cleanup removes only its completed delivery view, never the
                    # independent qB check/seeding task or the data it is using.
                    job["delivery_detached"] = True
                    self.store.save(job)
                    continue
                if not job or job["status"] in ("completed", "cancelled"):
                    continue
                if action == "start":
                    if job["status"] == "failed" or not self.config["enabled"]:
                        raise ValueError("失败或停用的离线任务需通过任务重试接口恢复")
                    job["paused"] = False
                    if job["phase"] == "metadata" and job["meta"] is None:
                        self.state(job, "metadata_reset", "安全恢复仅获取元数据的磁力任务")
                elif action == "stop":
                    row = self.qb.info(infohash)
                    if row:
                        self.owned(job, row)
                        self.qb.post("torrents/stop", hashes=infohash)
                    job["paused"] = True
                elif action == "delete":
                    row = self.qb.info(infohash)
                    if row:
                        self.owned(job, row)
                        self.qb.post("torrents/stop", hashes=infohash)
                        self.qb.post("torrents/delete", hashes=infohash, deleteFiles="false")
                    job.update(status="cancelled", paused=True, message="已取消；云端文件和离线任务保留")
                self.store.save(job)
            return forward

    def retry(self, infohash):
        with self.lock:
            job = self.store.get(infohash)
            if not job or job["status"] != "failed":
                raise ValueError("只能重试已有失败任务")
            if job["phase"] in ("checking", "verified"):
                self.state(job, "remote_verify", "重新核验云端文件后再次执行 qB 完整校验")
                job.update(cloud_ready=False, qb_checked=False)
                job.pop("stable_since", None)
            job.update(status=job["phase"], errors=0, started=self.clock(), paused=False,
                       message="已安排重试；保留原离线提交意图")
            self.store.save(job)
            return self.public(job)

    def overlay(self, rows, category=None, hashes=None):
        rows = {row["hash"]: dict(row) for row in rows}
        with self.lock:
            for job in self.store.all():
                if job.get("delivery_detached"):
                    rows.pop(job["hash"], None)
                    continue
                if job["status"] in ("completed", "cancelled"):
                    continue
                if category is not None and category != job["category"]:
                    continue
                if hashes is not None and job["hash"] not in hashes:
                    continue
                meta = job.get("meta") or {"name": job["hash"], "size": 0}
                row = rows.setdefault(job["hash"], {"hash": job["hash"], "name": meta["name"],
                                                        "category": job["category"], "save_path": job["qb_path"],
                                                        "content_path": job["qb_path"] + "/" + meta["name"]})
                if deliverable(job):
                    # This is the 115 download delivery view. It does not claim that
                    # the separate real qB torrent has finished checking or is seeding.
                    row.update(state="stoppedUP", progress=1, amount_left=0, size=meta["size"],
                               total_size=meta["size"], completed=meta["size"],
                               content_path=job["qb_path"] + "/" + meta["name"],
                               completion_on=int(job.get("qb_checked_at") or job.get("cloud_ready_at") or self.clock()),
                               dlspeed=0, upspeed=0, eta=0,
                               arr_delivery_status="ready", arr_seeding_status=self.public(job)["seeding_status"])
                    continue
                row.update(state="error" if job["status"] == "failed" else
                           "stoppedDL" if job["paused"] or not self.config["enabled"] else
                           "checkingDL" if job["phase"] == "checking" else "downloading",
                           progress=min(0.99, job["progress"]), size=meta["size"], total_size=meta["size"],
                           amount_left=max(1, int(meta["size"] * (1 - job["progress"]))),
                           dlspeed=0, upspeed=0, eta=8640000, added_on=int(job["started"]),
                           downloaded=row.get("downloaded", 0), uploaded=row.get("uploaded", 0))
        return list(rows.values())

    def start(self):
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
