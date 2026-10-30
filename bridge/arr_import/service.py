import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import posixpath
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from bridge.config import secret
from .cloud import Cloud, OperationRejected
from .config import route


class Conflict(ValueError):
    pass


def identity(file):
    """Require a provider identity and cloud SHA1; file size alone is insufficient."""
    if not file or file["directory"] or file["local"] or not file["id"]:
        return None
    sha1 = file["hashes"].get(2, "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha1):
        return None
    return {"size": file["size"], "sha1": sha1}


def matches(file, expected):
    return identity(file) == expected


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, destination TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_destination ON imports(destination) WHERE status NOT IN ('completed','failed')")
        self.db.commit()

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT body FROM imports WHERE id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, job):
        with self.lock, self.db:
            self.db.execute("INSERT INTO imports VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, body=excluded.body",
                            (job["id"], job["destination"], job["status"], json.dumps(job)))

    def by_destination(self, destination):
        with self.lock:
            row = self.db.execute("SELECT body FROM imports WHERE destination=? ORDER BY rowid DESC LIMIT 1",
                                  (destination,)).fetchone()
            return json.loads(row[0]) if row else None

    def active(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM imports WHERE status NOT IN ('completed','failed') ORDER BY rowid")]

    def close(self):
        self.db.close()


class Importer:
    def __init__(self, config, store, cloud_factory=Cloud, clock=time.time):
        self.config, self.store = config, store
        self.cloud_factory, self.clock = cloud_factory, clock
        self.stop = threading.Event()
        # Serialize submit/retry/worker changes to avoid overwriting job state.
        self.lock = threading.RLock()
        self.thread = None

    def submit(self, request):
        mode = request.get("transfer_mode", "copy")
        if mode not in ("copy", "move"):
            raise ValueError("transfer_mode 只能是 copy 或 move")
        if mode == "move" and request.get("resume"):
            # The hook lost track of a move whose source is already gone; find the job by its target.
            mapped = route(self.config, request)
            with self.lock:
                job = self.store.by_destination(mapped["destination"])
                if not job or job["source"] != mapped["source"] or job.get("mode") != "move":
                    raise Conflict("源文件不存在，也没有可继续的云端移动任务")
                if job["status"] == "completed":
                    job.update(status="verifying_replay", started=self.clock())
                    self.store.save(job)
                return self.public(job)
        if (type(request.get("size")) is not int or request["size"] <= 0
                or type(request.get("mtime_ns")) is not int or request["mtime_ns"] < 0
                or not isinstance(request.get("download_id", ""), str)):
            raise ValueError("需要有效的视频大小、mtime_ns 和 download_id")
        mapped = route(self.config, request)
        canonical = {k: request.get(k, "") for k in ("app", "source", "destination", "size", "mtime_ns", "download_id")}
        job_id = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        with self.lock:
            job = self.store.get(job_id)
            if mapped["native"]:
                if job:
                    raise Conflict("该文件已有云端整理任务；先处理已有任务，避免与原生导入并行")
                return {"status": "native"}
            if job:
                if (job["source"], job["destination"]) != (mapped["source"], mapped["destination"]):
                    raise Conflict("已有任务使用不同的路径映射；请恢复配置后处理")
                # Replays also verify the remote destination before reporting success.
                if job["status"] == "completed":
                    job["status"] = "verifying_replay"
                    job["started"] = self.clock()
                    self.store.save(job)
                return self.public(job)
            job = {"id": job_id, "status": "waiting_upload", "request": canonical, "mode": mode,
                   "source": mapped["source"], "destination": mapped["destination"],
                   "started": self.clock(), "errors": 0, "message": "等待云端上传完成"}
            try:
                self.store.save(job)
            except sqlite3.IntegrityError:
                raise Conflict("目标文件已有进行中的整理任务") from None
            return self.public(job)

    def retry(self, job_id):
        with self.lock:
            job = self.store.get(job_id)
            if not job:
                raise ValueError("任务不存在")
            if job["status"] == "failed":
                # Keep source digest and publishing intent for crash recovery.
                job.update(status="waiting_upload", started=self.clock(), errors=0,
                           copy_requested=False, message="已安排重试")
                try:
                    self.store.save(job)
                except sqlite3.IntegrityError:
                    raise Conflict("目标文件已有其他进行中的任务") from None
            return self.public(job)

    @staticmethod
    def public(job):
        return {k: job[k] for k in ("id", "status", "message") if k in job}

    def advance(self, job):
        """One resumable step. Never read video bytes or overwrite; the source moves only if Arr asked to move."""
        c = self.config
        if self.clock() - job["started"] > c["timeout_seconds"]:
            raise Conflict("等待或整理超时；确认上传状态后可重试")
        # Revalidate configuration on every job, including after a service restart.
        mapped = route(c, job["request"])
        if mapped["native"]:
            raise Conflict("规则已停用；暂停该任务，恢复规则后可重试")
        if (mapped["source"], mapped["destination"]) != (job["source"], job["destination"]):
            raise Conflict("任务路径映射已改变，拒绝继续")
        cloud = self.cloud_factory(c["cloud"])
        source, destination = job["source"], job["destination"]
        final = cloud.get_file(destination)
        if job["status"] == "verifying_replay":
            if not matches(final, job.get("identity")) or not job.get("identity"):
                raise Conflict("已完成任务的目标文件缺失或发生变化")
            job.update(status="completed", message="目标已重新核验")
            return
        if final:
            # Without a recorded identity, compare with the source as it is now (None while uploading).
            expected = job.get("identity") or identity(cloud.get_file(source))
            if expected and matches(final, expected):
                job.update(identity=expected, status="completed",
                           message="云端整理完成，目标已核验" if job.get("publishing")
                           else "目标已是相同内容（大小和 SHA1 一致），无需再整理")
                return
            # Arr deletes the file being upgraded just before calling the script; through
            # CD2 that deletion can take a moment to reach the provider.
            job.setdefault("target_seen", self.clock())
            if self.clock() - job["target_seen"] < c["target_wait_seconds"]:
                job.update(status="waiting_target", message="目标位置已有文件；等待 Arr 删除旧文件生效")
                return
            raise Conflict("目标文件已存在且内容不同；不会覆盖或删除")
        job.pop("target_seen", None)

        stage = c["staging_root"] + "/" + job["id"]
        original = stage + "/" + posixpath.basename(source)
        renamed = stage + "/" + posixpath.basename(destination)
        # After a cloud move the source is gone; continue from the staged file. A copy keeps
        # checking that its source is unchanged first.
        if job.get("mode") == "move" and job.get("identity") and self.publish_staged(job, cloud, original, renamed, destination):
            return

        uploads = cloud.uploads(source)
        if any(u["state"] != 5 for u in uploads):
            job.pop("ready_since", None)
            job.update(status="waiting_upload", message="源文件仍有未完成或异常的 CD2 上传任务")
            return
        remote = cloud.get_file(source)
        fingerprint = identity(remote)
        if not fingerprint or fingerprint["size"] != job["request"]["size"]:
            job.pop("ready_since", None)
            job.update(status="waiting_upload", message="等待云端文件 ID、SHA1 和大小核验；不以挂载文件存在判断完成")
            return
        if job.get("identity") and job["identity"] != fingerprint:
            raise Conflict("源文件内容已改变，拒绝继续原任务")
        job["identity"] = fingerprint
        if "ready_since" not in job:
            job["ready_since"] = self.clock()
        if self.clock() - job["ready_since"] < c["settle_seconds"]:
            job.update(status="waiting_upload", message="云端文件稳定性核验中")
            return

        cloud.mkdir(stage, c["account_root"])
        if self.publish_staged(job, cloud, original, renamed, destination):
            return
        if job.get("copy_requested"):
            job.update(status="copying", message="等待 CD2 云端移动完成" if job.get("mode") == "move" else "等待 CD2 云端复制完成")
            return
        move = job.get("mode") == "move"
        job.update(status="copying", copy_requested=True,
                   message="已提交云端移动（Arr 要求移动）" if move else "已提交云端复制；源文件保留")
        self.store.save(job)
        try:
            (cloud.move if move else cloud.copy)(source, stage)
        except OperationRejected:
            # Explicit rejection can be retried; an uncertain timeout cannot.
            job["copy_requested"] = False
            raise

    def publish_staged(self, job, cloud, original, renamed, destination):
        """Advance a verified staged file (rename, then publish). False if nothing is staged yet."""
        c, fingerprint = self.config, job["identity"]
        named_file = cloud.get_file(renamed)
        if named_file:
            if not matches(named_file, fingerprint):
                raise Conflict("暂存目标与源文件内容不符")
            if any(u["state"] != 5 for u in cloud.uploads(renamed)):
                job.update(status="verifying", message="等待暂存文件完成")
                return True
            target_dir = posixpath.dirname(destination)
            cloud.mkdir(target_dir, c["account_root"])
            # Persist intent BEFORE mutation: a timeout or restart may hide a successful move.
            job.update(status="publishing", publishing=True, message="发布已核验的云端文件")
            self.store.save(job)
            cloud.move(renamed, target_dir)
            return True
        staged = cloud.get_file(original) if original != renamed else None
        if staged:
            if not matches(staged, fingerprint):
                raise Conflict("暂存文件的 SHA1 或大小不符")
            if any(u["state"] != 5 for u in cloud.uploads(original)):
                job.update(status="verifying", message="等待暂存文件上传完成")
                return True
            job.update(status="renaming", message="修改云端文件名称")
            self.store.save(job)
            cloud.rename(original, posixpath.basename(destination))
            return True
        return False

    def tick(self):
        for candidate in self.store.active():
            if self.stop.is_set():
                break
            with self.lock:
                job = self.store.get(candidate["id"])
                if job["status"] in ("failed", "completed"):
                    continue
                try:
                    self.advance(job)
                except Conflict as e:
                    job.update(status="failed", message=str(e))
                except Exception as e:
                    job["errors"] += 1
                    # Never persist raw upstream replies/URLs/tokens.
                    job["message"] = "CD2 操作暂不可用（" + type(e).__name__ + "）；等待重试"
                    if job["errors"] >= self.config["max_errors"]:
                        job.update(status="failed", message="CD2 操作连续异常；确认权限和服务状态后重试")
                else:
                    job["errors"] = 0
                self.store.save(job)

    def start(self):
        def work():
            while not self.stop.is_set():
                self.tick()
                self.stop.wait(self.config["poll_seconds"])
        self.thread = threading.Thread(target=work, name="arr-import", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=40)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, importer):
        self.importer = importer
        self.token = secret(importer.config["token"]).encode()
        super().__init__((importer.config["host"], importer.config["port"]), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "ArrImport/1.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_request(self, post=False):
        p = urlsplit(self.path).path
        if not post and p == "/healthz":
            alive = bool(self.server.importer.thread and self.server.importer.thread.is_alive())
            self.reply(200 if alive else 503, {"ok": alive})
            return
        authorization = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(authorization, b"Bearer " + self.server.token):
            self.reply(401, {"error": "需要服务 API token"})
            return
        try:
            imp = self.server.importer
            if post and p == "/v1/imports":
                length = int(self.headers.get("Content-Length", "0"))
                if (self.headers.get("Transfer-Encoding") or not 0 < length <= 16384
                        or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                    raise ValueError("需要小于 16 KiB 的 JSON 请求")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("需要 JSON 对象")
                self.reply(202, imp.submit(request))
                return
            match = re.fullmatch(r"/v1/imports/([0-9a-f]{64})(/retry)?", p)
            if match and (not post and not match[2] or post and match[2]):
                if post:
                    result = imp.retry(match[1])
                else:
                    job = imp.store.get(match[1])
                    if not job:
                        self.reply(404, {"error": "任务不存在"})
                        return
                    result = imp.public(job)
                self.reply(200, result)
                return
            self.reply(404, {"error": "接口不存在"})
        except Conflict as e:
            self.reply(409, {"error": str(e)})
        except (ValueError, KeyError, TypeError):
            self.reply(400, {"error": "请求或路径规则无效"})
        except Exception:
            self.reply(503, {"error": "整理服务暂不可用"})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(post=True)
