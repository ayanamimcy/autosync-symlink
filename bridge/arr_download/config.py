import json
import re

from bridge.arr_import.config import http_url, inside, remote_path
from bridge.config import secret


def load(path):
    c = json.loads(path.read_text(encoding="utf-8"))
    if type(c.get("enabled")) is not bool or type(c.get("seed_after_check")) is not bool:
        raise ValueError("enabled 和 seed_after_check 必须为布尔值")
    c.setdefault("import_after", "qb_checked")
    if c["import_after"] not in ("cloud_ready", "qb_checked"):
        raise ValueError("import_after 必须为 cloud_ready 或 qb_checked")
    c.setdefault("host", "0.0.0.0")
    for key, default, low, high in (("port", 8097, 1024, 65535), ("poll_seconds", 5, 1, 300),
                                   ("settle_seconds", 20, 1, 3600), ("timeout_seconds", 86400, 60, 604800),
                                   ("max_errors", 12, 1, 1000)):
        c.setdefault(key, default)
        if type(c[key]) is not int or not low <= c[key] <= high:
            raise ValueError("下载服务时间或端口配置无效")
    if not c.get("username") or len(secret(c["password"])) < 24:
        raise ValueError("下载代理需要独立用户名和至少 24 字符的密码")
    for key in ("qb", "cloud"):
        http_url(c[key]["url"])
    if not secret(c["qb"].get("username", "")) or not secret(c["qb"]["password"]) or not secret(c["cloud"]["token"]):
        raise ValueError("需要 qB 登录信息及 CD2 API token")
    c["cloud"] = {**c["cloud"], "username": "", "password": ""}
    for key in ("account_root", "cloud_download_root", "qb_download_root"):
        remote_path(c[key])
    if not inside(c["cloud_download_root"], c["account_root"]) or c["cloud_download_root"] == c["account_root"]:
        raise ValueError("离线目录必须位于指定网盘账号下")
    categories = c.get("offline_categories")
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", v) for v in categories)
            or len(set(categories)) != len(categories)):
        raise ValueError("offline_categories 必须为不重复的分类名称列表")
    return c
