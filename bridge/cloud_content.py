"""Bounded metadata downloads through CD2, without opening mounted files."""
import http.client
from pathlib import PurePosixPath
import posixpath
import re
import threading
import time
from urllib.parse import urlsplit

from .cloud import CloudClient, cloud_file, virtual_path
from .protocol import fields, messages, text_field


class CloudContentError(Exception):
    pass


# Failures worth another attempt: the network or CD2 dropped the transfer. Rate
# limits, size limits and files still changing are not retried here.
TRANSIENT = re.compile(r"Timeout|timed out|ConnectionResetError|ConnectionAbortedError|RemoteDisconnected"
                       r"|IncompleteRead|BrokenPipeError|响应不完整")
RETRY_DELAYS = (5, 15, 30, 60, 60)


class CloudContent:
    def __init__(self, scanner):
        self.scanner = scanner
        self.config = scanner.config
        self.limit = self.config["sync"]["metadata_max_mb"] * 1024 * 1024
        self.slots = threading.BoundedSemaphore(min(scanner.workers, self.config["sync"]["file_workers"]))
        self.rate_lock, self.next_start = threading.Lock(), 0

    def failure(self, context, error):
        # Exceptions from HTTP clients/proxies can contain a signed URL. Only
        # include our context, exception type and extracted RPC/HTTP status.
        status = re.search(r"CloudDrive (HTTP \d{3}|gRPC 状态 \d+)(?:$|[；\s])", str(error))
        detail = status[1] if status else type(error).__name__
        if detail in ("HTTP 429", "gRPC 状态 8"):
            self.scanner.note_rate_limit()
        return CloudContentError(f"CD2 {context}失败（{detail}）；不回退挂载盘读取")

    def rpc(self, method, payload, checkpoint):
        checkpoint()
        self.scanner.check_cooldown()
        conn = None
        try:
            conn, response = CloudClient(self.config["cloud"]).open(method, payload, token=self.scanner.token)
            replies = list(messages(response))
            if len(replies) != 1:
                raise ValueError("unexpected reply count")
            return replies[0]
        except Exception as error:
            raise self.failure(method, error) from None
        finally:
            if conn:
                conn.close()

    def wait_start(self, checkpoint):
        # Pace metadata jobs, not just directory listings. A job performs URL
        # lookup, one HTTP download and one post-download attribute check.
        with self.rate_lock:
            while True:
                checkpoint()
                self.scanner.check_cooldown()
                delay = self.next_start - time.monotonic()
                if delay <= 0:
                    self.next_start = time.monotonic() + self.scanner.interval
                    return
                time.sleep(min(delay, .1))

    def download(self, location, expected_size, checkpoint):
        origin = urlsplit(self.config["cloud"]["url"])
        target = location.replace("{SCHEME}", origin.scheme).replace("{HOST}", origin.netloc).replace("{PREVIEW}", "false")
        # Only use the CD2 proxy URL path. Never follow redirects, use directUrl
        # or send API bearer credentials to a provider/third-party host.
        if (not target.startswith("/") or target.startswith("//")
                or any(c in target for c in "\\\r\n\x00{}")
                or urlsplit(target).scheme or urlsplit(target).netloc or urlsplit(target).fragment):
            raise CloudContentError("CD2 返回无效的元数据下载路径；不回退挂载盘读取")
        checkpoint()
        self.scanner.check_cooldown()
        factory = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
        conn = factory(origin.hostname, origin.port, timeout=30)
        try:
            conn.request("GET", origin.path.rstrip("/") + target, headers={"Accept-Encoding":"identity"})
            response = conn.getresponse()
            if response.status != 200:
                if response.status == 429:
                    self.scanner.note_rate_limit()
                raise CloudContentError(f"CD2 元数据下载 HTTP {response.status}；不跟随重定向或回退挂载盘读取")
            if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise CloudContentError("CD2 元数据响应被压缩，已停止读取")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isascii() or not length.isdigit() or int(length) != expected_size):
                raise CloudContentError("CD2 元数据响应长度与目录清单不一致，保留原输出并等待下次校验")
            payload = response.read(min(self.limit, expected_size) + 1)
            if len(payload) > self.limit:
                raise CloudContentError("CD2 元数据读取超出大小限制")
            if len(payload) != expected_size:
                raise CloudContentError("CD2 元数据内容长度不一致或响应不完整，保留原输出")
            return payload
        except CloudContentError:
            raise
        except Exception as error:
            raise self.failure("元数据下载", error) from None
        finally:
            conn.close()

    def read(self, relative, expected, checkpoint):
        parts = PurePosixPath(relative).parts
        if (not parts or relative.startswith("/") or ".." in parts
                or any(c in relative for c in "\\\x00")):
            raise ValueError("元数据路径必须是安全的相对路径")
        if not 0 <= expected.st_size <= self.limit:
            raise CloudContentError("CD2 元数据超过大小限制，已跳过")
        path = virtual_path(posixpath.join(self.scanner.root, *parts))
        retries = self.config["sync"].get("metadata_retries", 0)
        for attempt in range(retries + 1):
            try:
                return self.fetch(path, expected, checkpoint)
            except CloudContentError as error:
                if attempt == retries or not TRANSIENT.search(str(error)):
                    raise
            deadline = time.monotonic() + RETRY_DELAYS[attempt]
            while time.monotonic() < deadline:
                checkpoint()  # stays interruptible on shutdown
                time.sleep(.2)

    def fetch(self, path, expected, checkpoint):
        with self.slots:
            self.wait_start(checkpoint)
            # Explicitly request a full original file via the CD2 proxy.
            reply = self.rpc("GetDownloadUrlPath", text_field(1, path) + b"\x10\x00\x18\x00\x20\x00", checkpoint)
            try:
                location = dict(fields(reply)).get(1, b"").decode("utf-8")
            except (UnicodeError, AttributeError, ValueError):
                raise CloudContentError("CD2 元数据下载路径编码无效") from None
            payload = self.download(location, expected.st_size, checkpoint)
            reply = self.rpc("FindFileByPath", text_field(1, posixpath.dirname(path)) + text_field(2, posixpath.basename(path)), checkpoint)
            after = cloud_file(reply, posixpath.dirname(path))
            if (after.directory or after.other or after.path != path
                    or (after.size, after.mtime_ns) != (expected.st_size, expected.st_mtime_ns)):
                raise CloudContentError("CD2 源文件仍在变动，保留原输出并等待下次校验")
            return payload
