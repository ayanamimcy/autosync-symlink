"""CD2 directory inventory without walking/stat-ing files on the FUSE mount."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import ntpath
from pathlib import Path, PurePosixPath
import posixpath
import re
import threading
import time

from .cloud import CloudClient, virtual_path


class CloudScanError(Exception):
    # Deliberately not OSError: the service must not immediately retry an API
    # failure through its transient filesystem-error rescheduling path.
    pass


@dataclass(frozen=True)
class CloudStat:
    st_size: int
    st_mtime_ns: int

    @property
    def st_mtime(self):
        return self.st_mtime_ns / 1000000000


def mapped_cloud_path(lib, mounts):
    for mount in sorted(mounts, key=lambda m: len(m["mount"]), reverse=True):
        if not mount["mounted"] or not mount["mount"]:
            continue
        pathmod = ntpath if "\\" in mount["mount"] or ":" in mount["mount"] else posixpath
        try:
            relative = pathmod.relpath(lib["source"], mount["mount"])
        except ValueError:
            continue
        if relative == ".." or relative.startswith(("../", "..\\")):
            continue
        root = virtual_path(mount["source"])
        return virtual_path(posixpath.join(root, relative.replace("\\", "/"))) if relative != "." else root
    raise ValueError("未找到媒体源的 CD2 挂载映射，请手填该库的 cloud_path")


class CloudScanner:
    _cooldown_lock = threading.Lock()
    _cooldowns = {}

    def __init__(self, config, lib, progress, cloud_path=None):
        self.config, self.lib, self.progress = config, lib, progress
        self.stopped = threading.Event()
        self.rate_lock, self.next_request = threading.Lock(), 0
        self.failure_lock, self.failures = threading.Lock(), []
        self.interval = config["sync"]["cloud_list_interval_ms"] / 1000
        self.workers = config["sync"]["cloud_list_workers"]
        try:
            self.check_cooldown()
            client = CloudClient(config["cloud"])
            self.token = client.token()
            self.root = virtual_path(lib["cloud_path"] or cloud_path or mapped_cloud_path(lib, client.mount_points()))
        except Exception as error:
            raise CloudScanError(f"CD2 API 扫描配置/连接失败：{error}") from error

    def check_cooldown(self):
        with self._cooldown_lock:
            remaining = self._cooldowns.get(self.config["cloud"]["url"], 0) - time.monotonic()
        if remaining > 0:
            raise CloudScanError(f"CD2 API 限流冷却中（剩余 {int(remaining)+1} 秒），跳过本次请求")

    def listing(self, path, refresh=False):
        try:
            # Space request starts across all directory workers, including fresh
            # confirmation requests. Waiting remains interruptible on shutdown.
            with self.rate_lock:
                while True:
                    if self.stopped.is_set():
                        raise InterruptedError("CD2 API 扫描已停止")
                    self.check_cooldown()
                    self.progress("CD2 API 刷新校验" if refresh else "CD2 API 读取目录", path)
                    delay = self.next_request - time.monotonic()
                    if delay <= 0:
                        self.next_request = time.monotonic() + self.interval
                        break
                    self.stopped.wait(min(delay, .1))
            # Each concurrent request owns its connection. A failed request is
            # never retried or replaced by a filesystem walk in this scan.
            entries = CloudClient(self.config["cloud"]).list_files(path, self.token, force_refresh=refresh)
            if path == self.root and not any(e.name == self.lib["sentinel"] and not e.directory and not e.other for e in entries):
                raise ValueError("CD2 源目录列表缺少健康标记，请核对 cloud_path 和缓存")
            return entries
        except BaseException as error:
            failure = error if isinstance(error, (InterruptedError, KeyboardInterrupt, SystemExit)) else CloudScanError(
                f"CD2 API 目录枚举失败：{path}；{error}；本次停止，不回退扫盘")
            with self.failure_lock:
                # Preserve the originating RPC failure before waking/cancelling
                # peers. Otherwise a peer's InterruptedError could hide 429 and
                # enter the service's immediate OSError retry path.
                if not self.failures:
                    self.failures.append(failure)
                if re.match(r"CloudDrive (?:HTTP 429|gRPC 状态 8)(?:$|[；\s])", str(error)):
                    with self._cooldown_lock:
                        self._cooldowns[self.config["cloud"]["url"]] = time.monotonic() + 60
                self.stopped.set()
            if failure is error:
                raise
            raise failure from error

    def walk(self, scope=""):
        parts = PurePosixPath(scope).parts
        if ("\\" in scope or "\x00" in scope or PurePosixPath(scope).is_absolute()
                or ".." in parts):
            raise ValueError("CD2 扫描范围必须是安全的相对路径")
        base = posixpath.join(self.root, *parts) if parts else self.root
        stack, pending = [base], set()
        excluded = {x.casefold() for x in self.config["sync"]["exclude_directories"]}
        if parts:
            # Verify scoped existence through parent listings, never interpret a
            # GetSubFiles NOT_FOUND/error as an authoritative empty directory.
            parent = self.root
            for part in parts:
                entry = next((e for e in self.listing(parent) if e.name == part), None)
                if entry is None:
                    return
                if not entry.directory or entry.other:
                    raise CloudScanError("CD2 API 扫描范围不再是目录，已停止处理")
                parent = entry.path
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="cd2-list") as pool:
            try:
                while stack or pending:
                    while stack and len(pending) < self.workers:
                        pending.add(pool.submit(self.listing, stack.pop()))
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        for entry in future.result():
                            if entry.directory:
                                if entry.name.casefold() not in excluded:
                                    stack.append(entry.path)
                            elif not entry.other:
                                relative = PurePosixPath(entry.path).relative_to(self.root)
                                # Purely lexical mapping: no per-file lstat/stat
                                # calls on mounted source ancestors here.
                                yield Path(self.lib["source"]).joinpath(*relative.parts), CloudStat(entry.size, entry.mtime_ns)
            except BaseException:
                if self.failures:
                    raise self.failures[0]
                raise
            finally:
                self.stopped.set()
                for future in pending:
                    future.cancel()

    def confirm_missing(self, relatives):
        """Only freshly confirmed absence may start/finish deletion grace.

        Refresh shared ancestors once, not the whole library. Check from the
        known healthy root so a NOT_FOUND/permission/network error is never
        mistaken for proof that a deleted subtree is absent.
        """
        self.stopped.clear()
        refreshed, missing = {}, set()
        for relative in relatives:
            parts, parent = PurePosixPath(relative).parts, self.root
            if not parts or ".." in parts or relative.startswith("/") or any(c in relative for c in "\\\x00"):
                raise ValueError("清理清单包含不安全的相对路径")
            for index, part in enumerate(parts):
                if parent not in refreshed:
                    refreshed[parent] = {entry.name: entry for entry in self.listing(parent, refresh=True)}
                entry = refreshed[parent].get(part)
                if entry is None:
                    missing.add(relative)
                    break
                if entry.other:
                    raise CloudScanError("CD2 清理校验发现源路径类型不受支持，已保留输出并停止清理")
                if index < len(parts) - 1:
                    if not entry.directory:
                        raise CloudScanError("CD2 清理校验发现目录被文件替换，已停止清理")
                    parent = entry.path
        return missing
