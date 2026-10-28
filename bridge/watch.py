"""Portable watcher selection and metadata-only polling fallback."""
import os
from pathlib import Path, PurePosixPath
import sys
import threading

from .paths import walk


def event_scope(relative):
    if "\\" in relative or "\x00" in relative:
        raise ValueError("不安全的事件路径")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("事件路径越界")
    parent = str(path.parent)
    return "" if parent == "." else parent


class PollingWatch:
    def __init__(self, library, callback, log, seconds, excludes, ready=None):
        self.library, self.callback, self.log = library, callback, log
        self.ready = ready if ready is not None else lambda lib: callback(lib, "")
        self.seconds, self.excludes = seconds, excludes
        self.stop_event = threading.Event(); self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="poll-"+library["id"])
        self.previous = None

    def snapshot(self):
        root = Path(self.library["source"])
        return {p.relative_to(root).as_posix(): (st.st_size, st.st_mtime_ns, st.st_ino)
                for p, st in walk(root, exclude_dirs=self.excludes)}

    def once(self):
        current = self.snapshot()
        if self.previous is None:
            self.ready(self.library["id"])
        else:
            scopes = {event_scope(p) for p in current.keys() | self.previous.keys()
                      if current.get(p) != self.previous.get(p)}
            for scope in scopes:
                self.callback(self.library["id"], scope)
        self.previous = current
        self.ready_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.once()
            except (OSError, ValueError) as e:
                self.ready_event.clear(); self.previous = None
                self.log("WARN", f"{self.library['id']} 轮询暂不可用 ({type(e).__name__})，等待重连")
            self.stop_event.wait(self.seconds)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)


class Monitor:
    def __init__(self, config, callback, log, ready=None):
        self.impl = None; self.watchers = []; self.kind = "目录监控关闭；使用定期对账"
        mode = config["sync"]["watch_mode"]
        if mode == "off":
            return
        if mode == "auto" and os.name == "nt":
            from .native_watch import NativeMonitor
            self.impl = NativeMonitor(config["libraries"], callback, log, ready=ready)
            return
        if mode == "auto" and sys.platform == "linux":
            from .linux_watch import InotifyWatch
            self.kind = "Linux inotify"
            factory = lambda lib: InotifyWatch(lib, callback, log, config["sync"]["exclude_directories"], ready=ready)
        else:
            self.kind = "目录轮询（非云端实时保证）"
            factory = lambda lib: PollingWatch(lib, callback, log, config["sync"]["watch_poll_seconds"],
                                             config["sync"]["exclude_directories"], ready=ready)
        self.watchers = [factory(lib) for lib in config["libraries"] if lib["enabled"]]

    def start(self):
        if self.impl:
            self.impl.start()
        for watcher in self.watchers:
            watcher.start()

    def stop(self):
        if self.impl:
            self.impl.stop()
        for watcher in self.watchers:
            watcher.stop()

    def summary(self):
        if self.impl:
            return self.impl.summary()
        count = sum(w.ready_event.is_set() for w in self.watchers)
        return f"{self.kind} {count}/{len(self.watchers)} 已连接" if self.watchers else self.kind
