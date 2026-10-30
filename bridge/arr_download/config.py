import json
import re

from bridge.arr_import.config import http_url, inside, remote_path
from bridge.config import secret

# Settings of the earlier qB-seeding design. They are ignored, not silently reinterpreted.
RETIRED = ("qb", "seed_after_check", "import_after")


def load(path):
    c = json.loads(path.read_text(encoding="utf-8"))
    if type(c.get("enabled")) is not bool:
        raise ValueError("enabled 必须为布尔值")
    # Accept the key names of the earlier deployment.
    if "arr_download_root" not in c and "qb_download_root" in c:
        c["arr_download_root"] = c.pop("qb_download_root")
    if "categories" not in c and "offline_categories" in c:
        c["categories"] = c.pop("offline_categories")
    c["retired_keys"] = sorted(k for k in RETIRED if c.pop(k, None) is not None)
    c.setdefault("host", "0.0.0.0")
    for key, default, low, high in (("port", 8097, 1024, 65535), ("poll_seconds", 10, 1, 300),
                                   ("settle_seconds", 30, 0, 3600), ("timeout_seconds", 86400, 60, 604800),
                                   ("max_errors", 12, 1, 1000)):
        c.setdefault(key, default)
        if type(c[key]) is not int or not low <= c[key] <= high:
            raise ValueError(f"{key} 应为 {low}–{high} 之间的整数")
    for key, default in (("send_trackers", True), ("delete_files", False)):
        c.setdefault(key, default)
        if type(c[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    if not c.get("username") or len(secret(c["password"])) < 24:
        raise ValueError("下载代理需要独立用户名和至少 24 字符的密码")
    http_url(c["cloud"]["url"])
    if not secret(c["cloud"]["token"]):
        raise ValueError("需要 CD2 API token")
    c["cloud"] = {**c["cloud"], "username": "", "password": ""}
    for key in ("account_root", "cloud_download_root", "arr_download_root"):
        remote_path(c[key])
    if not inside(c["cloud_download_root"], c["account_root"]) or c["cloud_download_root"] == c["account_root"]:
        raise ValueError("离线目录必须位于指定网盘账号下")
    categories = c.get("categories")
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", v) for v in categories)
            or len(set(categories)) != len(categories)):
        raise ValueError("categories 必须为不重复的分类名称列表")
    warm = c.setdefault("warm", {})
    if not isinstance(warm, dict):
        raise ValueError("warm 必须是对象")
    warm.setdefault("enabled", False)
    warm.setdefault("mount_root", c["arr_download_root"])
    if type(warm["enabled"]) is not bool:
        raise ValueError("warm.enabled 必须为布尔值")
    remote_path(warm["mount_root"])
    for key, default, low, high in (("head_mb", 16, 1, 1024), ("tail_mb", 4, 0, 1024),
                                   ("timeout_seconds", 1800, 60, 86400), ("retry_seconds", 60, 5, 3600)):
        warm.setdefault(key, default)
        if type(warm[key]) is not int or not low <= warm[key] <= high:
            raise ValueError(f"warm.{key} 应为 {low}–{high} 之间的整数")
    arr = c.setdefault("arr", [])
    if not isinstance(arr, list):
        raise ValueError("arr 必须是列表")
    for instance in arr:
        if (not isinstance(instance, dict) or instance.get("category") not in categories
                or not secret(instance.get("api_key", ""))):
            raise ValueError("arr 每项需要已配置的 category、url 和 api_key")
        http_url(instance["url"])
    if len({i["category"] for i in arr}) != len(arr):
        raise ValueError("arr 中每个分类只能对应一个实例")
    return c
