from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

DEFAULT = {
    "web": {"host": "0.0.0.0", "port": 8095},
    "cloud": {"enabled": False, "url": "http://127.0.0.1:19798", "token": "",
              "username": "", "password": "", "reconnect_seconds": 10},
    "sync": {"interval_seconds": 900, "debounce_seconds": 3, "settle_seconds": 15,
             "scan_on_startup": True, "scan_on_reconnect": True,
             "scan_backend": "filesystem", "cloud_list_workers": 2, "cloud_list_interval_ms": 500,
             "scan_workers": 4, "file_workers": 4,
             "cleanup_enabled": False, "watch_mode": "auto", "watch_poll_seconds": 30,
             "metadata_verify_seconds": 3600,
             "delete_grace_seconds": 3600, "max_delete_fraction": 0.2,
             "allow_empty_cleanup": False, "metadata_max_mb": 32,
             "exclude_directories": ["_trash", ".deletedByTMM", ".Trash", "$RECYCLE.BIN", "System Volume Information", "@Recycle", "#recycle"]},
    "libraries": [{"id": "pt", "name": "PT 媒体库", "enabled": False,
                   "source": "/mnt/clouddrive/pt", "output": "/srv/media/library/pt",
                   "mount_root": "", "output_mount_root": "",
                   "cloud_path": "", "mode": "symlink", "strm_prefix": "",
                   "sentinel": ".media-bridge-ready"}],
    "servers": [
        {"id": "plex", "kind": "plex", "enabled": False,
         "url": "http://127.0.0.1:32400", "token": "", "section_id": "",
         "libraries": ["pt"], "path_from": "", "path_to": ""},
        {"id": "jellyfin", "kind": "jellyfin", "enabled": False,
         "url": "http://127.0.0.1:8096", "token": "", "section_id": "",
         "libraries": ["pt"], "path_from": "", "path_to": ""}],
    "transfers": [],
}

SECRET_KEYS = {"token", "password"}
MASK = "********"


def atomic_json(path: Path, value):
    import uuid
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def redact(value):
    if isinstance(value, dict):
        return {k: MASK if k in SECRET_KEYS and v else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def restore_secrets(new, old):
    if isinstance(new, dict):
        return {k: old.get(k, "") if k in SECRET_KEYS and v == MASK
                else restore_secrets(v, old.get(k, {} if isinstance(v, dict) else []))
                for k, v in new.items()}
    if isinstance(new, list):
        indexed = {x.get("id"): x for x in old if isinstance(x, dict)}
        return [restore_secrets(x, indexed.get(x.get("id"), {}))
                if isinstance(x, dict) else x for x in new]
    return new


def secret(value):
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}", value)
    if match:
        result = os.environ.get(match[1], "")
        if not result:
            raise ValueError(f"环境变量 {match[1]} 未设置")
        return result
    return value


def normalized(path):
    import ntpath
    if re.match(r"^[a-zA-Z]:", path) or path.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(path)).replace("\\", "/").rstrip("/")
    return str(Path(path).resolve()).rstrip("/")


def overlaps(a, b):
    a, b = normalized(a), normalized(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def validate(c, data_dir=None):
    if set(c) != set(DEFAULT):
        raise ValueError("配置顶层字段必须与示例一致")
    for section in ("web", "cloud", "sync"):
        if set(c[section]) != set(DEFAULT[section]):
            raise ValueError(f"{section} 字段必须与示例一致")
    def integer(v, low, high):
        return type(v) is int and low <= v <= high
    if not integer(c["web"]["port"], 1024, 65535):
        raise ValueError("Web 端口应为 1024–65535")
    if not isinstance(c["web"]["host"], str) or not c["web"]["host"]:
        raise ValueError("Web host 无效")
    for key, low, high in [("interval_seconds", 10, 86400), ("debounce_seconds", 1, 60),
                            ("settle_seconds", 0, 3600), ("delete_grace_seconds", 0, 604800),
                            ("metadata_max_mb", 1, 256), ("watch_poll_seconds", 1, 86400),
                            ("metadata_verify_seconds", 0, 604800),
                            ("scan_workers", 1, 32), ("file_workers", 1, 32),
                            ("cloud_list_workers", 1, 4), ("cloud_list_interval_ms", 100, 60000)]:
        if not integer(c["sync"][key], low, high):
            raise ValueError(f"{key} 必须是 {low}–{high} 之间的整数")
    if type(c["sync"]["max_delete_fraction"]) not in (int, float) or not 0 < c["sync"]["max_delete_fraction"] <= 1:
        raise ValueError("max_delete_fraction 应大于 0 且不大于 1")
    if type(c["sync"]["allow_empty_cleanup"]) is not bool:
        raise ValueError("allow_empty_cleanup 必须是布尔值")
    if type(c["sync"]["cleanup_enabled"]) is not bool:
        raise ValueError("cleanup_enabled 必须是布尔值")
    for key in ("scan_on_startup", "scan_on_reconnect"):
        if type(c["sync"][key]) is not bool:
            raise ValueError(f"{key} 必须是布尔值")
    if c["sync"]["watch_mode"] not in ("auto", "poll", "off"):
        raise ValueError("watch_mode 仅支持 auto/poll/off")
    if c["sync"]["scan_backend"] not in ("filesystem", "clouddrive"):
        raise ValueError("scan_backend 仅支持 filesystem/clouddrive")
    excludes=c["sync"]["exclude_directories"]
    if not isinstance(excludes,list) or any(not isinstance(x,str) or not x or x in (".","..") or any(y in x for y in "/\\:") for x in excludes):
        raise ValueError("exclude_directories 应为需要跳过的单个目录名列表")
    if type(c["cloud"]["enabled"]) is not bool or not integer(c["cloud"]["reconnect_seconds"], 1, 300):
        raise ValueError("CloudDrive 监控配置无效")
    def url(v):
        u = urlsplit(v)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("URL 只支持 http/https，凭据需单独填写")
    url(c["cloud"]["url"])
    roots, ids = [], set()
    import ntpath
    def path(v):
        if not isinstance(v, str) or not v or not (Path(v).is_absolute() or ntpath.isabs(v)):
            raise ValueError("源和输出必须是绝对路径")
        if normalized(v) in ("", "/") or re.fullmatch(r"[a-zA-Z]:", normalized(v)):
            raise ValueError("请使用盘符下面的子目录，不能直接使用盘根目录")
    for lib in c["libraries"]:
        if set(lib) != set(DEFAULT["libraries"][0]):
            raise ValueError("媒体库字段必须与示例一致")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", lib["id"]) or lib["id"] in ids:
            raise ValueError("媒体库 id 必须唯一且只含字母、数字、下划线或连字符")
        ids.add(lib["id"])
        if type(lib["enabled"]) is not bool or lib["mode"] not in ("symlink", "strm"):
            raise ValueError("媒体库 enabled / mode 无效")
        path(lib["source"]); path(lib["output"])
        for key, child in (("mount_root", "source"), ("output_mount_root", "output")):
            if lib[key]:
                path(lib[key])
                parent, target = normalized(lib[key]), normalized(lib[child])
                if target != parent and not target.startswith(parent+"/"):
                    raise ValueError(f"{key} 必须包含对应的 {child} 目录")
        if not lib["sentinel"] or Path(lib["sentinel"]).name != lib["sentinel"] or any(x in lib["sentinel"] for x in "/\\:") or lib["sentinel"] in (".", ".."):
            raise ValueError("sentinel 必须是单个文件名")
        cp = lib["cloud_path"]
        if cp and (not cp.startswith("/") or ".." in cp.split("/") or "\\" in cp):
            raise ValueError("cloud_path 必须是 CloudDrive 虚拟绝对路径")
        if lib["mode"] == "strm" and lib["strm_prefix"]:
            url(lib["strm_prefix"])
        if lib["enabled"]:
            roots += [(lib["source"], "source"), (lib["output"], "output")]
    for i, (a, _) in enumerate(roots):
        for b, _ in roots[i+1:]:
            if overlaps(a, b):
                raise ValueError("各媒体库的源和输出目录不能重叠")
        if data_dir and overlaps(a, str(data_dir)):
            raise ValueError("数据目录不能位于媒体源或输出中")
    sids = set()
    for s in c["servers"]:
        if set(s) != set(DEFAULT["servers"][0]) or s["kind"] not in ("plex", "emby", "jellyfin"):
            raise ValueError("媒体服务器字段或 kind 无效")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", s["id"]) or s["id"] in sids:
            raise ValueError("服务器 id 必须唯一")
        sids.add(s["id"]); url(s["url"])
        if not set(s["libraries"]) <= ids or type(s["enabled"]) is not bool:
            raise ValueError("服务器 libraries / enabled 无效")
        if s["enabled"] and (not s["token"] or (s["kind"] == "plex" and not str(s["section_id"]).isdigit())):
            raise ValueError("启用通知前需填写 Token；Plex 还需要数字 section_id")
        if bool(s["path_from"]) != bool(s["path_to"]):
            raise ValueError("服务器路径映射需同时填写 path_from / path_to")
    tids = set()
    for t in c["transfers"]:
        if set(t) != {"id", "enabled", "source", "destination", "stable_seconds"}:
            raise ValueError("转存字段应为 id/enabled/source/destination/stable_seconds")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", t["id"]) or t["id"] in tids:
            raise ValueError("转存 id 必须唯一")
        tids.add(t["id"])
        path(t["source"]); path(t["destination"])
        if type(t["enabled"]) is not bool or not integer(t["stable_seconds"], 10, 86400):
            raise ValueError("转存 enabled / stable_seconds 无效")
        if overlaps(t["source"], t["destination"]):
            raise ValueError("转存源与目标不能重叠")
        if t["enabled"]:
            for p in (t["source"], t["destination"]):
                if any(overlaps(p, root) for root, kind in roots if kind == "output") or (data_dir and overlaps(p, str(data_dir))):
                    raise ValueError("转存目录不能与软链接输出或服务数据目录重叠")
            for other in c["transfers"]:
                if other["id"] != t["id"] and other["enabled"] and any(overlaps(a,b) for a in (t["source"],t["destination"]) for b in (other["source"],other["destination"])):
                    raise ValueError("不同转存任务之间不能重叠或形成循环")
    return c


def load(path):
    if not path.exists():
        atomic_json(path, DEFAULT)
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    # Only migrate fields introduced by this version; unknown fields still fail validation.
    for key in ("cleanup_enabled", "watch_mode", "watch_poll_seconds", "metadata_verify_seconds",
                "scan_workers", "file_workers", "scan_on_startup", "scan_on_reconnect",
                "scan_backend", "cloud_list_workers", "cloud_list_interval_ms"):
        config.get("sync", {}).setdefault(key, DEFAULT["sync"][key])
    for lib in config.get("libraries", []):
        lib.setdefault("mount_root", ""); lib.setdefault("output_mount_root", "")
    return validate(config, path.parent)
