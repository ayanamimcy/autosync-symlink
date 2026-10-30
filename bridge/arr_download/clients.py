import http.cookiejar
import json
import re
import secrets
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

from bridge.arr_import.cloud import Cloud, number
from bridge.config import secret
from bridge.protocol import fields, text_field


class UpstreamError(ConnectionError):
    """Only locally generated, credential-free diagnostics are exposed."""


def multipart(values, torrent=None):
    boundary = "arr-" + secrets.token_hex(24)
    body = bytearray()
    for key, value in values.items():
        body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
    if torrent is not None:
        body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="download.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode())
        body.extend(torrent + b"\r\n")
    body.extend((f"--{boundary}--\r\n").encode())
    return bytes(body), "multipart/form-data; boundary=" + boundary


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class QB:
    def __init__(self, config):
        self.config, self.lock = config, threading.RLock()
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def raw(self, path, data=None, content_type="application/x-www-form-urlencoded", method=None):
        with self.lock:
            for attempt in range(2):
                if not self.logged_in:
                    self.login()
                try:
                    return self._raw(path, data, content_type, method)
                except HTTPError as error:
                    code = error.code
                    error.close()
                    if code in (401, 403) and not attempt:
                        self.logged_in = False
                        continue
                    raise UpstreamError(f"qB HTTP {code}") from None
                except UpstreamError:
                    raise
                except Exception as error:
                    raise UpstreamError("qB 连接异常（" + type(error).__name__ + "）") from None

    def _raw(self, path, data, content_type, method=None):
        url = self.config["url"].rstrip("/")
        origin = urlsplit(url)
        req = Request(url + path, data=data, method=method,
                      headers={"Content-Type": content_type, "Referer": url + "/",
                               "Origin": origin.scheme + "://" + origin.netloc})
        with self.opener.open(req, timeout=15) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise UpstreamError("qB 响应过大")
            return payload, response.headers.get("Content-Type", "application/json")

    def login(self):
        data = urlencode({"username": secret(self.config["username"]), "password": secret(self.config["password"])}).encode()
        try:
            payload, _ = self._raw("/api/v2/auth/login", data, "application/x-www-form-urlencoded")
        except Exception:
            raise UpstreamError("qB 登录连接失败") from None
        if payload.strip() != b"Ok.":
            raise UpstreamError("qB 登录失败")
        self.logged_in = True

    def get(self, path, **params):
        payload, _ = self.raw("/api/v2/" + path + ("?" + urlencode(params) if params else ""))
        return json.loads(payload)

    def post(self, path, **params):
        return self.raw("/api/v2/" + path, urlencode(params).encode())[0]

    def info(self, infohash):
        rows = self.get("torrents/info", hashes=infohash)
        if not isinstance(rows, list) or len(rows) > 1:
            raise UpstreamError("qB 返回了不明确的种子记录")
        return rows[0] if rows else None

    def add(self, job, torrent=None):
        version = self.raw("/api/v2/app/version")[0].decode().strip()
        if not re.fullmatch(r"v?5\.1\.[0-9]+", version):
            raise UpstreamError("离线接种第一版要求 qB 5.1.x 稳定版，接口按 5.1.4 核对")
        values = {"savepath": job["qb_path"], "category": job["category"], "tags": job["tag"],
                  "stopped": "true" if torrent else "false", "autoTMM": "false", "useDownloadPath": "false",
                  "skip_checking": "false", "contentLayout": "Original",
                  "stopCondition": "FilesChecked" if torrent else "MetadataReceived"}
        values.update(job.get("share_limits", {}))
        if torrent is None:
            values["urls"] = job["magnet"]
        body, content_type = multipart(values, torrent)
        if self.raw("/api/v2/torrents/add", body, content_type)[0].strip() != b"Ok.":
            raise UpstreamError("qB 拒绝添加接种任务")

    def export(self, infohash):
        return self.raw("/api/v2/torrents/export?" + urlencode({"hash": infohash}))[0]


class OfflineCloud(Cloud):
    def rpc(self, method, payload=b""):
        try:
            return super().rpc(method, payload)
        except Exception as error:
            match = re.match(r"CloudDrive gRPC 状态 ([0-9]{1,2})(?:；|$)", str(error))
            detail = "gRPC " + match[1] if match else type(error).__name__
            failure = UpstreamError("CD2 " + method + "：" + detail)
            failure.grpc_status = int(match[1]) if match else None
            raise failure from None

    def get_file(self, path):
        try:
            return super().get_file(path)
        except UpstreamError as error:
            if error.grpc_status == 5:
                return None
            raise

    def add_offline(self, link, folder):
        self.operation("AddOfflineFiles", text_field(1, link) + text_field(2, folder) + number(3, 0))

    def offline(self, folder):
        rows = []
        for reply in self.rpc("ListOfflineFilesByPath", text_field(1, folder)):
            for key, value in fields(reply):
                if key == 1:
                    f = dict(fields(value))
                    rows.append({"hash": f.get(5, b"").decode().lower(), "status": f.get(4, 0)})
        return rows
