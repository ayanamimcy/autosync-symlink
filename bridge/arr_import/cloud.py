"""Bounded gRPC-Web operations, using the official CloudDrive protocol."""
import posixpath

from bridge.cloud import CloudClient
from bridge.protocol import fields, messages, text_field, varint
from .config import inside


class OperationRejected(ConnectionError):
    """A definite negative operation reply, as opposed to an uncertain timeout."""


def number(field, value):
    return varint(field << 3) + varint(value)


def string(data, key):
    return data.get(key, b"").decode("utf-8")


def file_info(payload):
    f = dict(fields(payload))
    hashes = {}
    for key, value in fields(payload):
        if key == 70:
            entry = dict(fields(value))
            hashes[entry.get(1, 0)] = string(entry, 2)
    return {"id": string(f, 1), "name": string(f, 2), "path": string(f, 3),
            "size": f.get(4, 0), "directory": f.get(5, 0) == 0 or bool(f.get(30)),
            "local": bool(f.get(37)), "hashes": hashes}


class Cloud:
    def __init__(self, config):
        self.client = CloudClient(config)
        self.bearer = self.client.token()

    def rpc(self, method, payload=b""):
        conn, resp = self.client.open(method, payload, token=self.bearer)
        try:
            # Exhaust the iterator: success includes the final grpc-status trailer.
            return list(messages(resp))
        finally:
            conn.close()

    def operation(self, method, payload):
        replies = self.rpc(method, payload)
        if len(replies) != 1 or dict(fields(replies[0])).get(1) != 1:
            raise OperationRejected(f"CD2 {method} 未返回成功")

    def list_files(self, path):
        result = []
        for reply in self.rpc("GetSubFiles", text_field(1, path) + number(2, 1)):
            for key, value in fields(reply):
                if key == 1:
                    info = file_info(value)
                    if posixpath.dirname(info["path"]) != path or not info["name"] or info["name"] != posixpath.basename(info["path"]):
                        raise ValueError("CD2 返回的子文件路径无效")
                    result.append(info)
        return result

    def get_file(self, path):
        try:
            return next((f for f in self.list_files(posixpath.dirname(path)) if f["path"] == path), None)
        except ConnectionError as e:
            # Only the codec's exact NOT_FOUND status denotes an absent parent.
            # Permission/auth/transport errors must never masquerade as absence.
            if str(e).startswith("CloudDrive gRPC 状态 5；"):
                return None
            raise

    def uploads(self, path):
        # Read all tasks; a name filter could miss a still-pending mounted write.
        replies = self.rpc("GetUploadFileList", number(1, 1))
        if len(replies) != 1:
            raise ConnectionError("CD2 上传列表响应无效")
        f = dict(fields(replies[0]))
        rows = [dict(fields(v)) for k, v in fields(replies[0]) if k == 2]
        if f.get(1, 0) != len(rows):
            raise ConnectionError("CD2 上传列表不完整，拒绝判断上传完成")
        result = []
        for row in rows:
            dest = string(row, 2)
            # Include parent/child paths conservatively, not just an exact filename.
            if dest and (inside(dest, path) or inside(path, dest)):
                result.append({"path": dest, "state": row.get(8, 0), "size": row.get(3, 0)})
        return result

    def mkdir(self, path, account):
        if path == account:
            return
        if not inside(path, account):
            raise ValueError("拒绝在指定账号以外创建目录")
        parent = posixpath.dirname(path)
        self.mkdir(parent, account)
        existing = self.get_file(path)
        if existing:
            if not existing["directory"]:
                raise ValueError("目标目录被文件占用")
            return
        replies = self.rpc("CreateFolder", text_field(1, parent) + text_field(2, posixpath.basename(path)))
        if len(replies) != 1:
            raise ConnectionError("CD2 创建目录响应无效")
        result = dict(fields(replies[0])).get(2)
        if not result or dict(fields(result)).get(1) != 1:
            # A concurrent import can create the same parent directory.
            existing = self.get_file(path)
            if not existing or not existing["directory"]:
                raise ConnectionError("CD2 创建目录失败")

    def copy(self, source, directory):
        # Explicit Skip: omitted conflict policy defaults to Overwrite in CD2.
        self.operation("CopyFile", text_field(1, source) + text_field(2, directory) + number(3, 2))

    def rename(self, source, name):
        self.operation("RenameFile", text_field(1, source) + text_field(2, name))

    def move(self, source, directory):
        self.operation("MoveFile", text_field(1, source) + text_field(2, directory) + number(3, 2))
