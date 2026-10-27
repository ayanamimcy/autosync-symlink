"""Authenticated LAN management UI, using Python's standard HTTP server."""
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .config import redact, restore_secrets
from .covers import generate

STATIC = Path(__file__).parent/"static"


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        self.service = service
        self.auth_lock = threading.Lock()
        self.sessions = {}
        self.attempts = {}
        key_path = service.data_dir/"admin.key"
        if not key_path.exists():
            with key_path.open("x",encoding="utf-8") as f:
                f.write(secrets.token_urlsafe(32))
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
        self.admin_key = key_path.read_text().strip()
        if len(self.admin_key)<24:
            raise ValueError("admin.key 太短，请删除后重新启动以生成随机管理密钥")
        super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaBridge/1.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        pass  # Avoid ever persisting cookies, credentials or request bodies.

    def reply(self, code, data, content_type="application/json; charset=utf-8", headers=None):
        if not isinstance(data,bytes):
            data = json.dumps(data,ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        for k,v in {"Content-Type":content_type,"Content-Length":str(len(data)),"Cache-Control":"no-store",
                    "X-Content-Type-Options":"nosniff", "X-Frame-Options":"DENY",
                    "Referrer-Policy":"no-referrer",
                    "Content-Security-Policy":"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                    **(headers or {})}.items():
            self.send_header(k,v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):
            pass

    def body(self):
        n = int(self.headers.get("Content-Length","0"))
        if not 0 < n <= 256*1024 or self.headers.get("Transfer-Encoding"):
            raise ValueError("请求大小无效或超出 256KB")
        if self.headers.get("Content-Type","").split(";")[0] != "application/json":
            raise ValueError("需要 JSON 请求")
        result = json.loads(self.rfile.read(n))
        if not isinstance(result,dict):
            raise ValueError("需要 JSON 对象")
        return result

    def authenticated(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie",""))
            sid = cookie["mb_session"].value
            with self.server.auth_lock:
                return self.server.sessions.get(sid,0)>time.time()
        except Exception:
            return False

    def origin_ok(self):
        # JSON + custom header forces cross-origin browser preflight, which is refused.
        if self.headers.get("X-Media-Bridge") != "1":
            return False
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == self.headers.get("Host")

    def do_OPTIONS(self):
        self.reply(403,{"error":"跨域请求未允许"})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            status = self.server.service.health()
            self.reply(200 if status["ok"] else 503, status)
            return
        if path in ("/","/app.js","/style.css"):
            f = STATIC/({"/":"index.html"}.get(path,path.lstrip("/")))
            self.reply(200,f.read_bytes(),mimetypes.guess_type(f.name)[0]+"; charset=utf-8")
            return
        if not self.authenticated():
            self.reply(401,{"error":"请先登录"}); return
        s = self.server.service
        if path == "/api/status":
            self.reply(200,s.status())
        elif path == "/api/config":
            self.reply(200,redact(s.config))
        elif path == "/api/logs":
            self.reply(200,s.state.query("SELECT * FROM events ORDER BY id DESC LIMIT 200"))
        elif path.startswith("/api/jobs/"):
            job = s.jobs.get(path.rsplit("/",1)[1])
            self.reply(200 if job else 404,job or {"error":"任务不存在"})
        elif path.startswith("/covers/"):
            name = path.rsplit("/",1)[1]
            if name not in {x["id"]+".png" for x in s.config["libraries"]}:
                self.reply(404,{"error":"封面不存在"}); return
            f = s.data_dir/"covers"/name
            if f.is_file():
                self.reply(200,f.read_bytes(),"image/png")
            else:
                self.reply(404,{"error":"尚未生成封面"})
        else:
            self.reply(404,{"error":"接口不存在"})

    def do_POST(self):
        if not self.origin_ok():
            self.reply(403,{"error":"来源校验失败"}); return
        path = urlsplit(self.path).path
        try:
            if path == "/api/login":
                self.login(); return
            if not self.authenticated():
                self.reply(401,{"error":"请先登录"}); return
            data = self.body()
            s = self.server.service
            if path == "/api/logout":
                cookie = SimpleCookie(self.headers.get("Cookie",""))
                with self.server.auth_lock:
                    self.server.sessions.pop(cookie["mb_session"].value,None)
                self.reply(200,{"ok":True},headers={"Set-Cookie":"mb_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
            elif path == "/api/config":
                s.save_config(restore_secrets(data,s.config))
                self.reply(200,{"ok":True,"restart_needed":"Web 监听地址/端口修改后需重启"})
            elif path == "/api/actions":
                if data.get("action") in ("initialize","transfer_initialize") and data.get("confirm") is not True:
                    raise ValueError("初始化源目录标记需要明确确认")
                self.reply(202,s.submit(data.get("action"),data.get("library")))
            elif path == "/api/covers":
                lib = next((x for x in s.config["libraries"] if x["id"]==data.get("library")),None)
                if not lib:
                    raise ValueError("媒体库不存在")
                with s.lock:
                    generate(lib,s.data_dir/"covers"/(lib["id"]+".png"),str(data.get("title",lib["name"])),
                             str(data.get("subtitle","YOUR PRIVATE CINEMA")),str(data.get("theme","teal")))
                s.state.log("INFO",f"{lib['id']} 媒体库封面已生成")
                self.reply(200,{"url":"/covers/"+lib["id"]+".png"})
            else:
                self.reply(404,{"error":"接口不存在"})
        except (ValueError,KeyError,TypeError,OSError) as e:
            self.reply(400,{"error":str(e)})
        except Exception:
            self.reply(500,{"error":"操作失败，请查看服务日志"})

    def login(self):
        ip = self.client_address[0]
        with self.server.auth_lock:
            now = time.time()
            self.server.attempts = {k:[t for t in v if now-t<900] for k,v in self.server.attempts.items() if any(now-t<900 for t in v)}
            attempts = self.server.attempts.get(ip,[])
            if len(attempts)>=10:
                self.reply(429,{"error":"登录尝试过多，请 15 分钟后重试"}); return
        data = self.body()
        if not hmac.compare_digest(str(data.get("key","")).encode(),self.server.admin_key.encode()):
            with self.server.auth_lock:
                self.server.attempts.setdefault(ip,[]).append(time.time())
            self.reply(401,{"error":"管理密钥不正确"}); return
        sid = secrets.token_urlsafe(32)
        with self.server.auth_lock:
            self.server.sessions = {k:v for k,v in self.server.sessions.items() if v>time.time()}
            self.server.sessions[sid] = time.time()+12*3600
            self.server.attempts.pop(ip,None)
        self.reply(200,{"ok":True},headers={"Set-Cookie":f"mb_session={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"})
