"""The subset of the qBittorrent v2 Web API that Radarr and Sonarr use."""
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
from urllib.parse import parse_qs, urlsplit

from bridge.config import secret
from .clients import UpstreamError
from .metadata import MAX_META

VERSION, WEBAPI_VERSION = b"v5.1.4", b"2.11.4"
# Accepted and ignored: there is no queue, seeding or piece selection behind 115 offline.
NOOPS = {"torrents/start", "torrents/stop", "torrents/resume", "torrents/pause", "torrents/setShareLimits",
         "torrents/setForceStart", "torrents/addTags", "torrents/removeTags", "torrents/recheck",
         "torrents/reannounce", "torrents/bottomPrio", "torrents/increasePrio", "torrents/decreasePrio"}


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
        super().__init__((downloader.config["host"], downloader.config["port"]), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "ArrDownload/2.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type="application/json", cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (OSError, TimeoutError):
            pass

    def text(self, body=b"Ok."):
        self.reply(200, body, "text/plain; charset=UTF-8")

    def authorized(self):
        password = secret(self.server.downloader.config["password"]).encode()
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].encode(), password):
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        sid = cookie.get("SID")
        now = time.monotonic()
        with self.server.session_lock:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > now}
            return bool(sid and sid.value in self.server.sessions)

    def login(self, values):
        config = self.server.downloader.config
        if (not hmac.compare_digest(values.get("username", "").encode(), config["username"].encode())
                or not hmac.compare_digest(values.get("password", "").encode(), secret(config["password"]).encode())):
            self.text(b"Fails.")
            return
        sid = secrets.token_hex(32)
        with self.server.session_lock:
            now = time.monotonic()
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > now}
            if len(self.server.sessions) >= 1000:
                self.reply(503, {"error": "会话已满"})
                return
            self.server.sessions[sid] = now + 3600
        self.reply(200, b"Ok.", "text/plain; charset=UTF-8", "SID=" + sid + "; Path=/; HttpOnly; SameSite=Strict")

    def hashes(self, value):
        d = self.server.downloader
        if value == "all":
            return [j["hash"] for j in d.store.all()]
        hashes = [h.lower() for h in value.split("|") if h]
        if not hashes or any(not re.fullmatch(r"[a-f0-9]{40}", h) for h in hashes):
            raise ValueError("必须指定有效的 hashes")
        return hashes

    def job(self, query):
        job = self.server.downloader.store.get(query.get("hash", [""])[0].lower())
        return job if job and not job.get("removed") else None

    def categories(self):
        d = self.server.downloader
        names = set(d.config["categories"]) | d.extra_categories | {j["category"] for j in d.store.all()}
        return {n: {"name": n, "savePath": d.config["arr_download_root"]} for n in sorted(names)}

    def get(self, path, query):
        d = self.server.downloader
        if path == "app/version":
            self.text(VERSION)
        elif path == "app/webapiVersion":
            self.text(WEBAPI_VERSION)
        elif path == "app/preferences":
            self.reply(200, {"save_path": d.config["arr_download_root"], "temp_path_enabled": False, "dht": True,
                             "max_ratio_enabled": False, "max_ratio": -1, "max_seeding_time_enabled": False,
                             "max_seeding_time": -1, "max_inactive_seeding_time_enabled": False,
                             "max_inactive_seeding_time": -1, "max_ratio_act": 0, "queueing_enabled": False})
        elif path == "transfer/info":
            self.reply(200, {"connection_status": "connected", "dl_info_speed": 0, "up_info_speed": 0,
                             "dl_info_data": 0, "up_info_data": 0, "dht_nodes": 0})
        elif path == "torrents/categories":
            self.reply(200, self.categories())
        elif path == "torrents/tags":
            self.reply(200, sorted({t.strip() for j in d.store.all() for t in j.get("tags", "").split(",") if t.strip()}))
        elif path == "torrents/info":
            category = query.get("category", [None])[0]
            hashes = query.get("hashes", [None])[0]
            rows = d.rows(category, set(self.hashes(hashes)) if hashes else None)
            selected = query.get("filter", ["all"])[0]
            if selected in ("completed", "seeding", "stopped", "paused"):
                rows = [r for r in rows if r["state"] == "stoppedUP"]
            elif selected in ("downloading", "active", "running", "resumed"):
                rows = [r for r in rows if r["state"] != "stoppedUP"]
            elif selected == "errored":
                rows = [r for r in rows if r["state"] == "error"]
            if "sort" in query and query["sort"][0] in ("name", "hash", "added_on", "progress", "size"):
                key = query["sort"][0]
                rows.sort(key=lambda r: r[key], reverse=query.get("reverse", ["false"])[0] == "true")
            offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [0])[0])
            self.reply(200, rows[max(offset, 0):max(offset, 0) + limit] if limit > 0 else rows[max(offset, 0):])
        elif path in ("torrents/properties", "torrents/files", "torrents/trackers"):
            job = self.job(query)
            if not job:
                self.reply(404, {"error": "种子不存在"})
            elif path == "torrents/properties":
                self.reply(200, d.properties(job))
            elif path == "torrents/files":
                self.reply(200, d.files(job))
            else:
                self.reply(200, [])
        else:
            self.reply(404, {"error": "该接口不在下载代理支持范围内"})

    def post(self, path, values, torrent):
        d = self.server.downloader
        if torrent is not None and path != "torrents/add":
            raise ValueError("此接口不接受文件")
        if path == "torrents/add":
            if torrent is not None and values.get("urls"):
                raise ValueError("不能同时提交种子文件和 URL")
            try:
                d.submit(values, torrent)
            except FileExistsError:
                self.reply(409, b"Torrent already exists.", "text/plain; charset=UTF-8")
                return
            self.text()
        elif path == "torrents/delete":
            d.remove(self.hashes(values.get("hashes", "")), values.get("deleteFiles", "false").lower() in ("true", "1"))
            self.text(b"")
        elif path == "torrents/setCategory":
            category = values.get("category", "")
            if category and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", category):
                raise ValueError("分类名称无效")
            d.set_category(self.hashes(values.get("hashes", "")), category)
            self.text(b"")
        elif path == "torrents/createCategory":
            category = values.get("category", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", category):
                raise ValueError("分类名称无效")
            d.extra_categories.add(category)
            self.text(b"")
        elif path == "torrents/topPrio":
            # Same reply as qB with queueing disabled; Arr ignores 409 here.
            self.reply(409, b"Torrent queueing must be enabled", "text/plain; charset=UTF-8")
        elif path in NOOPS:
            self.text(b"")
        else:
            self.reply(405, {"error": "该接口不在下载代理支持范围内"})

    def dispatch(self, post=False):
        d = self.server.downloader
        url = urlsplit(self.path)
        try:
            if not post and url.path == "/healthz":
                healthy = bool(d.thread and d.thread.is_alive())
                self.reply(200 if healthy else 503, {"ok": healthy})
                return
            values, torrent = {}, None
            if post:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("不支持分块请求")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= MAX_META + 1024 * 1024:
                    raise ValueError("请求体超过限制")
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError("请求体截断")
                if body:
                    values, torrent = form(body, self.headers.get("Content-Type", "application/x-www-form-urlencoded"))
            if post and url.path == "/api/v2/auth/login":
                self.login(values)
                return
            if not self.authorized():
                self.reply(403, b"Forbidden", "text/plain; charset=UTF-8")
                return
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=100)
            if url.path == "/api/v2/auth/logout":
                self.reply(200, b"", "text/plain", "SID=; Path=/; Max-Age=0")
                return
            if not post and url.path == "/v1/downloads":
                self.reply(200, [d.public(j) for j in d.store.all()])
                return
            route = re.fullmatch(r"/v1/downloads/([a-f0-9]{40})(/retry)?", url.path)
            if route:
                infohash, retry = route.groups()
                job = d.store.get(infohash)
                if post != bool(retry):
                    self.reply(405, {"error": "请求方法不匹配"})
                elif not job:
                    self.reply(404, {"error": "任务不存在"})
                else:
                    self.reply(200, d.retry(infohash) if retry else d.public(job))
                return
            if not url.path.startswith("/api/v2/"):
                self.reply(404, {"error": "接口不存在"})
                return
            path = url.path.removeprefix("/api/v2/")
            if post:
                self.post(path, values, torrent)
            else:
                self.get(path, query)
        except ValueError as error:
            # Only fixed local validation messages; never stringify arbitrary upstream exceptions.
            self.reply(400, {"error": str(error) if type(error) is ValueError else "请求编码或内容无效"})
        except UpstreamError as error:
            self.reply(503, {"error": str(error)})
        except Exception:
            self.reply(503, {"error": "下载服务暂不可用"})

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(True)
