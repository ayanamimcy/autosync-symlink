import http.client
import threading
from urllib.parse import urlsplit
from .config import secret
from .protocol import decode_change, fields, frame, messages, text_field


class CloudClient:
    def __init__(self, config):
        self.config = config
        self.connection = None

    def open(self, method, data=b"", token="", timeout=30):
        u = urlsplit(self.config["url"])
        factory = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = factory(u.hostname,u.port,timeout=timeout)
        self.connection = conn
        headers = {"Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1",
                   "Accept": "application/grpc-web+proto", "Accept-Encoding": "identity"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            conn.request("POST",u.path.rstrip("/")+"/clouddrive.CloudDriveFileSrv/"+method,frame(data),headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise ConnectionError(f"CloudDrive HTTP {resp.status}")
            if "application/grpc-web" not in resp.getheader("Content-Type", ""):
                raise ConnectionError("CloudDrive 未返回 gRPC-Web；请检查端口/反向代理")
            status = resp.getheader("grpc-status")
            if status and status != "0":
                raise ConnectionError(f"CloudDrive gRPC 状态 {status}")
            return conn, resp
        except Exception:
            conn.close()
            raise

    def token(self):
        token = secret(self.config["token"])
        if token:
            return token
        username, password = secret(self.config["username"]),secret(self.config["password"])
        if not username or not password:
            raise ValueError("尚未配置 CloudDrive API Token 或登录信息")
        conn, resp = self.open("GetToken",text_field(1,username)+text_field(2,password))
        try:
            results = list(messages(resp))
            if not results:
                raise ConnectionError("CloudDrive 登录响应为空")
            result = dict(fields(results[0]))
            if result.get(1) != 1 or not result.get(3):
                raise ConnectionError("CloudDrive 登录失败，请检查账号/密码/两步验证")
            return result[3].decode("utf-8")
        finally:
            conn.close()

    def ready(self):
        conn, resp = self.open("GetSystemInfo",token=self.token())
        try:
            result = list(messages(resp))
            f = dict(fields(result[0])) if result else {}
            return bool(f.get(1)) and bool(f.get(3))
        finally:
            conn.close()

    def mount_points(self):
        conn, resp = self.open("GetMountPoints",token=self.token())
        try:
            result = []
            for payload in messages(resp):
                for number, value in fields(payload):
                    if number == 1 and isinstance(value,bytes):
                        f = dict(fields(value))
                        result.append({"mount":f.get(1,b"").decode("utf-8"),
                                       "source":f.get(2,b"").decode("utf-8"),
                                       "mounted":bool(f.get(9,1))})
            return result
        finally:
            conn.close()

    def subscribe(self, callback, connected, stop):
        conn, resp = self.open("PushMessage",token=self.token(),timeout=75)
        try:
            # An idle event stream is healthy. Do not force reconnects/full scans
            # simply because the media library has no changes for a minute.
            if conn.sock:
                conn.sock.settimeout(None)
            connected()
            for payload in messages(resp):
                if stop.is_set():
                    return
                event = decode_change(payload)
                if event:
                    callback(event)
        finally:
            conn.close()

    def close(self):
        conn = self.connection
        if conn:
            try:
                if conn.sock:
                    import socket
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()


class CloudMonitor:
    def __init__(self, config, on_change, on_reconnect, log):
        self.config, self.on_change = config, on_change
        self.on_reconnect, self.log = on_reconnect, log
        self.stop_event = threading.Event()
        self.client = CloudClient(config)
        self.status = "未启用" if not config["enabled"] else "等待连接"
        self.thread = None

    def start(self):
        if self.config["enabled"]:
            self.thread = threading.Thread(target=self.run,name="clouddrive-push",daemon=True)
            self.thread.start()

    def connected(self):
        self.status = "已连接，等待文件事件"
        self.log("INFO","CloudDrive 文件推送已连接；按配置决定是否安排全量补扫")
        self.on_reconnect()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.status = "正在连接"
                self.client.subscribe(self.on_change,self.connected,self.stop_event)
                if not self.stop_event.is_set():
                    raise ConnectionError("文件推送流结束")
            except Exception as e:
                if not self.stop_event.is_set():
                    self.status = f"未连接：{type(e).__name__}"
                    self.log("WARN",f"CloudDrive 文件推送断开 ({type(e).__name__})，稍后重连；请检查会员文件通知和 Token")
            self.stop_event.wait(self.config["reconnect_seconds"])

    def stop(self):
        self.stop_event.set(); self.client.close()
        if self.thread:
            self.thread.join(timeout=3)
