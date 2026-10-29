import json
from urllib.parse import urlsplit

from bridge.config import secret


def remote_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or value == "/"
            or any(c in value for c in ("\\", "\0", "\r", "\n"))
            or any(p in ("", ".", "..") for p in value[1:].split("/"))):
        raise ValueError("路径必须是规范的 POSIX 绝对路径，不能使用根目录")
    return value


def inside(path, root):
    return path == root or path.startswith(root + "/")


def overlap(a, b):
    return inside(a, b) or inside(b, a)


def http_url(value):
    u = urlsplit(value)
    if (u.scheme not in ("http", "https") or not u.hostname or u.username
            or u.password or u.query or u.fragment):
        raise ValueError("API 地址必须是无凭据的 http/https URL")
    return value


def load(path):
    c = json.loads(path.read_text(encoding="utf-8"))
    if type(c.get("enabled")) is not bool:
        raise ValueError("enabled 必须是布尔值")
    c.setdefault("host", "0.0.0.0")
    c.setdefault("port", 8096)
    for key, default, low, high in (("port", 8096, 1024, 65535),
                                   ("poll_seconds", 5, 1, 300),
                                   ("settle_seconds", 20, 1, 3600),
                                   ("timeout_seconds", 7200, 30, 86400),
                                   ("max_errors", 12, 1, 1000)):
        c.setdefault(key, default)
        if type(c[key]) is not int or not low <= c[key] <= high:
            raise ValueError(f"{key} 应为 {low}–{high} 之间的整数")
    if len(secret(c["token"])) < 24:
        raise ValueError("独立服务 token 至少需要 24 个字符")
    http_url(c["cloud"]["url"])
    if not secret(c["cloud"]["token"]):
        raise ValueError("需要 CloudDrive API token")
    c["cloud"] = {**c["cloud"], "username": "", "password": ""}
    account = remote_path(c["account_root"])
    stage = remote_path(c["staging_root"])
    if stage == account or not inside(stage, account):
        raise ValueError("暂存目录必须在指定网盘账号下")
    if not isinstance(c["rules"], list) or not c["rules"]:
        raise ValueError("至少配置一条路径规则")
    ids = set()
    for r in c["rules"]:
        if (not isinstance(r.get("id"), str) or not r["id"] or r["id"] in ids
                or r.get("app") not in ("radarr", "sonarr") or type(r.get("enabled")) is not bool):
            raise ValueError("规则 id 必须唯一，app 为 radarr/sonarr，enabled 为布尔值")
        ids.add(r["id"])
        for key in ("source_root", "destination_root", "cloud_source_root", "cloud_destination_root"):
            remote_path(r[key])
        for key in ("cloud_source_root", "cloud_destination_root"):
            if r[key] == account or not inside(r[key], account) or overlap(r[key], stage):
                raise ValueError("云端路径需位于同一账号内，且不能与暂存目录重叠")
        if overlap(r["source_root"], r["destination_root"]) or overlap(r["cloud_source_root"], r["cloud_destination_root"]):
            raise ValueError("下载暂存目录与媒体目录不能重叠")
    for i, a in enumerate(c["rules"]):
        for b in c["rules"][i+1:]:
            if a["app"] == b["app"] and overlap(a["source_root"], b["source_root"]) and overlap(a["destination_root"], b["destination_root"]):
                raise ValueError("规则匹配范围存在歧义")
    return c


def route(c, request):
    app = request["app"]
    source, destination = remote_path(request["source"]), remote_path(request["destination"])
    if app not in ("radarr", "sonarr"):
        raise ValueError("app 仅支持 radarr/sonarr")
    matches = [r for r in c["rules"] if r["app"] == app
               and inside(source, r["source_root"]) and source != r["source_root"]
               and inside(destination, r["destination_root"]) and destination != r["destination_root"]]
    if len(matches) != 1:
        raise ValueError("源和目标必须精确匹配一条已配置规则；不自动回退文件复制")
    r = matches[0]
    cloud_source = r["cloud_source_root"] + source[len(r["source_root"]):]
    cloud_destination = r["cloud_destination_root"] + destination[len(r["destination_root"]):]
    if cloud_source == cloud_destination:
        raise ValueError("源和目标不能相同")
    return {"rule": r["id"], "source": cloud_source, "destination": cloud_destination,
            "native": not c["enabled"] or not r["enabled"]}
