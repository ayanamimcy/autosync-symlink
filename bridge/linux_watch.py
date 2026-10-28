"""Recursive inotify adapter. Events request reconciliation; they never delete files."""
import ctypes
import os
from pathlib import Path
import select
import struct
import threading

from .mounts import identities
from .paths import is_link, no_link_ancestors
from .watch import event_scope

CREATE, DELETE, MODIFY, ATTRIB = 0x100, 0x200, 0x2, 0x4
CLOSE_WRITE, MOVED_FROM, MOVED_TO = 0x8, 0x40, 0x80
DELETE_SELF, MOVE_SELF, UNMOUNT, OVERFLOW, IGNORED, ISDIR = 0x400, 0x800, 0x2000, 0x4000, 0x8000, 0x40000000
MASK = CREATE | DELETE | MODIFY | ATTRIB | CLOSE_WRITE | MOVED_FROM | MOVED_TO | DELETE_SELF | MOVE_SELF | UNMOUNT


def records(data):
    offset = 0
    while offset < len(data):
        if len(data)-offset < 16:
            raise ValueError("inotify 记录截断")
        wd, mask, cookie, size = struct.unpack_from("iIII", data, offset)
        offset += 16
        if size > len(data)-offset:
            raise ValueError("inotify 名称截断")
        name = os.fsdecode(data[offset:offset+size].split(b"\0", 1)[0])
        offset += size
        if name in (".", "..") or "/" in name:
            raise ValueError("不安全的 inotify 名称")
        yield wd, mask, cookie, name


class InotifyWatch:
    def __init__(self, library, callback, log, excludes=(), ready=None):
        self.library, self.callback, self.log = library, callback, log
        self.ready = ready if ready is not None else lambda lib: callback(lib, "")
        self.root = Path(library["source"])
        self.excludes = {x.casefold() for x in excludes}
        self.stop_event = threading.Event(); self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="inotify-"+library["id"], daemon=True)
        self.fd = None; self.directories = {}

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def add_tree(self, directory):
        stack = [directory]
        while stack:
            folder = stack.pop()
            if is_link(folder):
                continue
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(folder), MASK | 0x01000000 | 0x02000000)
            if wd < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(folder))
            self.directories[wd] = folder
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.casefold() not in self.excludes and entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))

    def process(self, data):
        for wd, mask, _, name in records(data):
            if mask & OVERFLOW:
                self.callback(self.library["id"], "")
                raise OSError("inotify 队列溢出，重建监听并请求全量补扫")
            folder = self.directories.get(wd)
            if folder is None:
                continue
            if mask & (UNMOUNT | DELETE_SELF | MOVE_SELF):
                self.callback(self.library["id"], "")
                raise OSError("监听目录已移动或卸载，重新订阅")
            if mask & IGNORED:
                self.directories.pop(wd, None)
                continue
            if name.casefold() in self.excludes:
                continue
            target = folder/name if name else folder
            self.callback(self.library["id"], event_scope(target.relative_to(self.root).as_posix()))
            if mask & ISDIR and mask & (CREATE | MOVED_TO):
                # Watch the new subtree before the worker scans it; reconcile existing children too.
                try:
                    self.add_tree(target)
                except FileNotFoundError:
                    pass

    def listen(self):
        no_link_ancestors(self.root)
        identity = identities(self.library)
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]; self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            error = ctypes.get_errno(); raise OSError(error, os.strerror(error))
        try:
            self.directories.clear()
            self.add_tree(self.root)
            self.ready_event.set()
            self.ready(self.library["id"])
            while not self.stop_event.is_set():
                readable, _, _ = select.select([self.fd], [], [], 0.5)
                if identity != identities(self.library):
                    raise OSError("挂载身份发生变化")
                if readable:
                    try:
                        self.process(os.read(self.fd, 256*1024))
                    except BlockingIOError:
                        pass
        finally:
            self.ready_event.clear()
            os.close(self.fd); self.fd = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.listen()
            except (OSError, ValueError) as e:
                self.log("WARN", f"{self.library['id']} inotify 断开 ({e})；定期对账仍运行")
            self.stop_event.wait(5)
