import json
import re
import struct
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bridge.arr_import.cloud import Cloud, OperationRejected, number
from bridge.config import secret
from bridge.protocol import fields, text_field


class UpstreamError(ConnectionError):
    """Only locally generated, credential-free diagnostics are exposed."""


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

    def walk(self, path, limit=20000):
        """All regular files below path as {relative path: info}; None if path is absent."""
        files, pending = {}, [path]
        while pending:
            folder = pending.pop()
            try:
                children = self.list_files(folder)  # GetSubFiles with forceRefresh
            except UpstreamError as error:
                if error.grpc_status == 5 and folder == path:
                    return None
                raise
            for info in children:
                if info["directory"]:
                    pending.append(info["path"])
                else:
                    files[info["path"][len(path) + 1:]] = info
                if len(files) > limit:
                    raise ValueError("离线目录文件过多")
        return files

    def add_offline(self, link, folder):
        # checkFolderAfterSecs=0: do not let CD2 poll the folder on our behalf.
        try:
            self.operation("AddOfflineFiles", text_field(1, link) + text_field(2, folder) + number(3, 0))
        except UpstreamError as error:
            if error.grpc_status is not None:
                # The server answered: a definite rejection (115 code 10008 "任务已存在" arrives as
                # gRPC 13), not a lost reply.
                raise OperationRejected(str(error)) from None
            raise

    def remove_offline(self, infohash, path):
        """Drop the 115 offline record of infohash (stopping it if running); files are kept.

        path only selects the cloud account; deleteFiles (field 3) is left false.
        """
        self.operation("RemoveOfflineFiles", text_field(4, infohash) + text_field(5, path))

    def offline(self, folder):
        rows = []
        for reply in self.rpc("ListOfflineFilesByPath", text_field(1, folder)):
            for key, value in fields(reply):
                if key == 1:
                    f = dict(fields(value))
                    done = f.get(9)  # double percendDone, wire type 1
                    rows.append({"hash": f.get(5, b"").decode().lower(), "name": f.get(1, b"").decode(),
                                 "size": f.get(2, 0), "status": f.get(4, 0), "peers": f.get(10, 0),
                                 "percent": struct.unpack("<d", done)[0] if isinstance(done, bytes) and len(done) == 8 else 0.0})
        return rows

    def delete(self, path):
        # DeleteFile moves to the provider's recycle bin; never the permanent variant.
        self.operation("DeleteFile", text_field(1, path))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward the API key to a redirected destination.


class Arr:
    """Marks a grab as failed so Arr blocklists the release and searches again."""

    def __init__(self, instance, timeout=15):
        self.url, self.key, self.timeout = instance["url"].rstrip("/"), secret(instance["api_key"]), timeout

    def call(self, path, method="GET", **params):
        req = Request(self.url + path + ("?" + urlencode(params) if params else ""), method=method,
                      data=b"" if method == "POST" else None, headers={"X-Api-Key": self.key})
        try:
            with build_opener(NoRedirect()).open(req, timeout=self.timeout) as response:
                body = response.read(4 * 1024 * 1024)
                return json.loads(body) if body.strip() else None
        except HTTPError as error:
            error.close()
            raise UpstreamError(f"Arr HTTP {error.code}；{path}") from None
        except (OSError, ValueError) as error:
            raise UpstreamError(f"Arr {path} 异常（{type(error).__name__}）") from None

    def mark_failed(self, infohash):
        # eventType 1 = grabbed. Radarr and Sonarr both expose history/failed/{id} in v3.
        page = self.call("/api/v3/history", downloadId=infohash.upper(), eventType=1, pageSize=10)
        records = page.get("records", []) if isinstance(page, dict) else []
        if not records:
            return False
        self.call(f"/api/v3/history/failed/{int(records[0]['id'])}", "POST")
        return True
