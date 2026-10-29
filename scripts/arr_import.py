#!/usr/bin/env python3
"""Standalone Arr import hook; install this file inside each Arr container."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer token to a redirected destination.


def hook_config(path):
    c = json.loads(Path(path).read_text(encoding="utf-8"))
    u = urlsplit(c["url"])
    if (u.scheme not in ("http", "https") or not u.hostname or u.username
            or u.password or u.query or u.fragment):
        raise ValueError("hook 服务地址无效")
    token = c["token"]
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}", token)
    if match:
        token = os.environ.get(match[1], "")
    if len(token) < 24:
        raise ValueError("hook token 至少需要 24 个字符")
    c["token"] = token
    for key, default, low, high in (("timeout_seconds", 180, 1, 86400), ("poll_seconds", 3, 1, 300)):
        c.setdefault(key, default)
        if type(c[key]) is not int or not low <= c[key] <= high:
            raise ValueError("hook 时间配置无效")
    return c


def request(c, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(c["url"].rstrip("/") + path, data=data,
                  headers={"Authorization": "Bearer " + c["token"], "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect()).open(req, timeout=15) as response:
            return json.loads(response.read(65536))
    except HTTPError as e:
        if e.code >= 500:
            raise URLError("整理服务暂不可用") from None
        raise ValueError(f"整理服务拒绝请求（HTTP {e.code}）") from None


def snapshot(path):
    info = Path(path).stat()
    if not stat.S_ISREG(info.st_mode) or Path(path).is_symlink() or info.st_size <= 0:
        raise ValueError("需要可访问的非空常规视频文件")
    return info.st_size, info.st_mtime_ns


def run_hook(c, source, destination, env=os.environ):
    prefixes = [p for p in ("Radarr", "Sonarr") if env.get(p + "_SourcePath")]
    if len(prefixes) != 1:
        raise ValueError("必须由 Radarr/Sonarr 的 Import Using Script 调用")
    prefix = prefixes[0]
    if source != env[prefix + "_SourcePath"] or destination != env.get(prefix + "_DestinationPath"):
        raise ValueError("脚本参数与 Arr 导入环境不一致")
    size, modified = snapshot(source)
    payload = {"app": prefix.lower(), "source": source, "destination": destination,
               "size": size, "mtime_ns": modified, "download_id": env.get(prefix + "_Download_Id", "")}
    deadline = time.monotonic() + c["timeout_seconds"]
    result = None
    while time.monotonic() < deadline:
        try:
            if result is None:
                result = request(c, "/v1/imports", payload)
            elif result["status"] not in ("native", "completed", "failed"):
                result = request(c, "/v1/imports/" + result["id"])
        except (URLError, TimeoutError, OSError):
            time.sleep(c["poll_seconds"])
            continue
        if result["status"] == "native":
            return "DeferMove"
        if result["status"] == "failed":
            # The job ID is useful for status/retry; never print raw server replies.
            raise ValueError("云端整理失败，任务 " + result["id"] + "；查看整理服务状态")
        if result["status"] == "completed":
            if snapshot(source) != (size, modified):
                raise ValueError("整理期间源文件已改变；拒绝确认导入")
            try:
                target_size, _ = snapshot(destination)
            except OSError:
                target_size = None
            if target_size == size:
                return "MoveComplete"
            # The API succeeded but the consuming mount may still have stale metadata.
        time.sleep(c["poll_seconds"])
    raise ValueError("等待整理或挂载可见性超时；任务在服务端保留，Arr 下次导入可继续")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?")
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--config", default=os.environ.get("ARR_IMPORT_CONFIG", "/config/arr-import-hook.json"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", metavar="JOB_ID")
    group.add_argument("--retry", metavar="JOB_ID")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        c = hook_config(args.config)
        if args.check:
            print("hook 配置有效；尚未连接服务")
        elif args.status or args.retry:
            job_id = args.status or args.retry
            if not re.fullmatch(r"[a-f0-9]{64}", job_id):
                raise ValueError("任务 ID 无效")
            suffix = "/retry" if args.retry else ""
            print(json.dumps(request(c, "/v1/imports/" + job_id + suffix, {} if args.retry else None), ensure_ascii=False))
        else:
            if not args.source or not args.destination:
                raise ValueError("需要源路径和目标路径")
            result = run_hook(c, args.source, args.destination)
            print("[MoveStatus] " + result)
        return 0
    except Exception as e:
        # ValueErrors above are local, fixed descriptions, not remote server text.
        message = str(e) if type(e) is ValueError else type(e).__name__
        print("Arr CD2 import: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
