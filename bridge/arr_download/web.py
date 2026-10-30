import email.parser
import email.policy
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from bridge.config import secret
from .clients import UpstreamError
from .metadata import MAX_META
from .read_cache import AppReadCache
from .service import deliverable

READS = {"app/version", "app/webapiVersion", "app/preferences", "torrents/properties",
         "torrents/files", "torrents/trackers", "torrents/categories", "torrents/tags", "transfer/info"}
WRITES = {"torrents/start", "torrents/stop", "torrents/resume", "torrents/pause", "torrents/delete",
          "torrents/recheck", "torrents/setShareLimits", "torrents/setCategory", "torrents/createCategory"}
PREFERENCES = {"save_path", "temp_path_enabled", "temp_path", "max_ratio_act", "max_ratio_enabled",
               "max_ratio", "max_seeding_time_enabled", "max_seeding_time", "queueing_enabled",
               "max_active_downloads", "max_active_torrents", "max_active_uploads"}


def form(body, content_type):
    if content_type.startswith("application/x-www-form-urlencoded"):
        raw = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
        if any(len(v) != 1 for v in raw.values()):
            raise ValueError("请求字段不能重复")
        return {k: v[0] for k, v in raw.items()}, None
    if content_type.startswith("multipart/form-data"):
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        if not message.is_multipart() or message.defects:
            raise ValueError("multipart 请求无效")
        values, torrent = {}, None
        for part in message.iter_parts():
            key = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True)
            if not key or part.is_multipart() or data is None or part.defects:
                raise ValueError("multipart 字段无效")
            if key == "torrents":
                if torrent is not None or not data or len(data) > MAX_META:
                    raise ValueError("每次请求只接受一个非空 .torrent 文件")
                torrent = data
            else:
                if key in values or len(data) > 65536 or len(values) >= 100:
                    raise ValueError("请求字段重复或过大")
                values[key] = data.decode("utf-8")
        return values, torrent
    raise ValueError("请求必须使用 form 或 multipart")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, downloader):
        self.downloader = downloader
        self.sessions, self.session_lock = {}, threading.RLock()
        self.app_cache = AppReadCache(wait_seconds=downloader.config["qb"].get("timeout_seconds", 15) + 2)
        super().__init__((downloader.config["host"], downloader.config["port"]), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "ArrDownload/1.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type="application/json", cookie=None, headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (OSError, TimeoutError):
            pass

    def session(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        sid = cookie.get("SID")
        now = time.monotonic()
        with self.server.session_lock:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > now}
            return sid.value if sid and sid.value in self.server.sessions else None

    def dispatch(self, post=False):
        d = self.server.downloader
        url = urlsplit(self.path)
        path = url.path.removeprefix("/api/v2/")
        try:
            body = b""
            if post:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("不支持分块请求")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= MAX_META + 1024 * 1024:
                    raise ValueError("请求体超过限制")
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError("请求体截断")
            if not post and url.path == "/healthz":
                healthy = bool(d.thread and d.thread.is_alive())
                self.reply(200 if healthy else 503, {"ok": healthy})
                return
            content_type = self.headers.get("Content-Type", "application/x-www-form-urlencoded")
            if post and url.path == "/api/v2/auth/login":
                values, _ = form(body, content_type)
                if (not hmac.compare_digest(values.get("username", "").encode(), d.config["username"].encode())
                        or not hmac.compare_digest(values.get("password", "").encode(), secret(d.config["password"]).encode())):
                    self.reply(200, b"Fails.", "text/plain")
                    return
                sid = secrets.token_hex(32)
                with self.server.session_lock:
                    now = time.monotonic()
                    self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > now}
                    if len(self.server.sessions) >= 1000:
                        self.reply(503, {"error": "会话已满"})
                        return
                    self.server.sessions[sid] = now + 3600
                self.reply(200, b"Ok.", "text/plain", "SID=" + sid + "; Path=/; HttpOnly; SameSite=Strict")
                return
            sid = self.session()
            if not sid:
                self.reply(403, {"error": "需要登录下载代理"})
                return
            if post and url.path == "/api/v2/auth/logout":
                with self.server.session_lock:
                    self.server.sessions.pop(sid, None)
                self.reply(200, b"", "text/plain", "SID=; Path=/; Max-Age=0")
                return
            if not post and url.path == "/v1/diagnostics":
                self.reply(200, {"qb": d.qb.diagnostics(), "app_cache": self.server.app_cache.diagnostics()})
                return
            job_route = re.fullmatch(r"/v1/downloads/([a-f0-9]{40})(/retry)?", url.path)
            if job_route:
                infohash, retry = job_route.groups()
                if post != bool(retry):
                    self.reply(405, {"error": "请求方法不匹配"})
                    return
                job = d.store.get(infohash)
                if not job:
                    self.reply(404, {"error": "任务不存在"})
                else:
                    self.reply(200, d.retry(infohash) if retry else d.public(job))
                return
            if not url.path.startswith("/api/v2/"):
                self.reply(404, {"error": "接口不存在"})
                return
            if post and path == "torrents/add":
                values, torrent = form(body, content_type)
                if torrent is not None and values.get("urls"):
                    raise ValueError("不能同时提交种子文件和 URL")
                job = d.submit(values, torrent)
                if job is None:
                    payload, typ = d.qb.raw(url.path, body, content_type)
                    self.reply(200, payload, typ)
                else:
                    self.reply(200, b"Ok.", "text/plain")
                return
            if not post and path == "torrents/info":
                query = parse_qs(url.query, keep_blank_values=True, max_num_fields=100)
                category = query.get("category", [None])[0]
                hashes = query.get("hashes", [None])[0]
                hashes = set(hashes.split("|")) if hashes else None
                rows = d.qb.get("torrents/info")
                if category is not None:
                    rows = [row for row in rows if row.get("category") == category]
                if hashes is not None:
                    rows = [row for row in rows if row["hash"] in hashes]
                rows = d.overlay(rows, category, hashes)
                filter_name = query.get("filter", ["all"])[0]
                if filter_name in ("seeding", "completed"):
                    rows = [row for row in rows if row.get("progress") == 1]
                elif filter_name == "downloading":
                    rows = [row for row in rows if row.get("progress", 0) < 1]
                elif filter_name not in ("all", ""):
                    raise ValueError("第一版只支持 all/downloading/completed/seeding 过滤")
                if "sort" in query:
                    key = query["sort"][0]
                    if key not in ("name", "hash", "added_on", "progress", "size"):
                        raise ValueError("不支持的排序字段")
                    rows.sort(key=lambda r: r.get(key, "" if key in ("name", "hash") else 0),
                              reverse=query.get("reverse", ["false"])[0] == "true")
                offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [0])[0])
                if offset < 0 or limit < 0:
                    raise ValueError("分页参数无效")
                self.reply(200, rows[offset:offset + limit] if limit else rows[offset:])
                return
            if not post and path in READS:
                if path in AppReadCache.POLICIES:
                    def fetch_app():
                        payload, typ = d.qb.raw("/api/v2/" + path)
                        if path == "app/preferences":
                            # Do not retain qB authentication settings in the cache.
                            try:
                                preferences = json.loads(payload)
                                if not isinstance(preferences, dict):
                                    raise ValueError()
                            except (ValueError, UnicodeError):
                                raise UpstreamError("qB app/preferences 返回无效 JSON 对象") from None
                            payload = json.dumps({k: v for k, v in preferences.items() if k in PREFERENCES}).encode()
                            typ = "application/json"
                        elif not re.fullmatch(rb"v?[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?", payload.strip()):
                            raise UpstreamError("qB 版本接口返回无效版本号；未更新缓存")
                        return payload, typ
                    payload, typ, status = self.server.app_cache.get(path, fetch_app)
                    self.reply(200, payload, typ, headers={"X-Arr-App-Cache": status})
                    return
                if path in ("torrents/properties", "torrents/files", "torrents/trackers"):
                    query = parse_qs(url.query, max_num_fields=100)
                    job = d.store.get(query.get("hash", [""])[0])
                    if job and job.get("delivery_detached"):
                        self.reply(404, {"error": "下载交付记录已移除；后台做种任务保留"})
                        return
                    if job and job["status"] not in ("completed", "cancelled"):
                        # Arr checks properties immediately after add, before the worker's first tick.
                        if path == "torrents/properties":
                            self.reply(200, {"save_path": job["qb_path"], "total_size": (job.get("meta") or {}).get("size", 0),
                                             "creation_date": -1, "addition_date": int(job["started"]),
                                             "completion_date": int(job.get("qb_checked_at") or job.get("cloud_ready_at") or 0) if deliverable(job) else -1,
                                             "seeding_time": 0, "share_ratio": 0,
                                             "total_downloaded": 0, "total_uploaded": 0, "pieces_have": 0,
                                             "comment": job["message"]})
                            return
                        if deliverable(job) and path == "torrents/files":
                            self.reply(200, [{"index": index, "name": f["path"], "size": f["size"],
                                              "progress": 1, "priority": 1, "is_seed": False}
                                             for index, f in enumerate(job["meta"]["files"])])
                            return
                        if not d.qb.info(job["hash"]):
                            self.reply(200, [])
                            return
                payload, typ = d.qb.raw(url.path + ("?" + url.query if url.query else ""))
                self.reply(200, payload, typ)
                return
            if post and path in WRITES:
                values, torrent = form(body, content_type)
                if torrent is not None:
                    raise ValueError("此接口不接受文件")
                if path == "torrents/createCategory":
                    if values.get("category") not in d.config["offline_categories"]:
                        raise ValueError("只能通过代理创建已配置的离线分类")
                    if values.get("savePath", "").rstrip("/") not in ("", d.config["qb_download_root"]):
                        raise ValueError("分类保存路径必须与 qb_download_root 一致")
                else:
                    value = values.get("hashes", "")
                    hashes = value.split("|")
                    if value == "all":
                        hashes = list({r["hash"] for r in d.qb.get("torrents/info")} | {j["hash"] for j in d.store.all()})
                    if not hashes or any(not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", h) for h in hashes):
                        raise ValueError("必须指定有效的 hashes")
                    if path == "torrents/delete" and values.get("deleteFiles", "false").lower() not in ("false", "0"):
                        if any(d.store.get(h) for h in hashes):
                            raise ValueError("代理不删除离线做种源文件；仅支持 deleteFiles=false")
                    action = {"resume": "start", "pause": "stop"}.get(path.split("/")[1], path.split("/")[1])
                    hashes = d.control(hashes, action)
                    if not hashes:
                        self.reply(200, b"", "text/plain")
                        return
                    values["hashes"] = "|".join(hashes)
                    path = "torrents/" + action
                payload = d.qb.post(path, **values)
                self.reply(200, payload, "text/plain")
                return
            self.reply(404 if not post else 405, {"error": "该接口不在下载代理支持范围内"})
        except ValueError as error:
            # Only fixed local validation messages; never stringify arbitrary upstream exceptions.
            message = str(error) if type(error) is ValueError else "请求编码或内容无效"
            self.reply(400, {"error": message})
        except UpstreamError as error:
            self.reply(503, {"error": str(error)})
        except Exception:
            self.reply(503, {"error": "下载服务暂不可用；检查任务状态与上游连接"})

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(True)
