"""Windows directory notification adapter for CloudDrive2 mounted folders."""
import ctypes
import os
from pathlib import PurePosixPath
import struct
import threading


def parse_notifications(data):
    offset = 0
    while offset < len(data):
        if len(data)-offset < 12:
            raise ValueError("目录通知记录截断")
        next_offset, action, size = struct.unpack_from("<III",data,offset)
        end = offset+12+size
        if size % 2 or end > len(data):
            raise ValueError("目录通知文件名截断")
        name = data[offset+12:end].decode("utf-16le").replace("\\","/")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or ":" in name or "\x00" in name:
            raise ValueError("不安全的目录通知路径")
        if action in (1,2,3,4,5):
            yield name
        if not next_offset:
            return
        if next_offset < 12+size or next_offset % 4 or offset+next_offset >= len(data):
            raise ValueError("目录通知偏移无效")
        offset += next_offset


class DirectoryWatch:
    def __init__(self, library, callback, log):
        self.library, self.callback, self.log = library, callback, log
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.status = "等待订阅"
        self.thread = threading.Thread(target=self.run,name="watch-"+library["id"],daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.listen()
            except Exception as e:
                if not self.stop_event.is_set():
                    self.status = f"未连接 ({type(e).__name__})"
                    self.log("WARN",f"{self.library['id']} Windows 文件通知断开 ({e})；稍后重连，低频补扫保持运行")
            self.ready_event.clear()
            self.stop_event.wait(10)
        self.status = "已停止"

    def listen(self):
        from ctypes import wintypes as w
        from pathlib import Path
        from .paths import no_link_ancestors
        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal",ctypes.c_size_t),("InternalHigh",ctypes.c_size_t),
                        ("Offset",w.DWORD),("OffsetHigh",w.DWORD),("hEvent",w.HANDLE)]
        k = ctypes.WinDLL("kernel32",use_last_error=True)
        k.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.CreateEventW.argtypes = [w.LPVOID,w.BOOL,w.BOOL,w.LPCWSTR]; k.CreateEventW.restype = w.HANDLE
        k.ReadDirectoryChangesW.argtypes = [w.HANDLE,w.LPVOID,w.DWORD,w.BOOL,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.POINTER(Overlapped),w.LPVOID]
        k.ReadDirectoryChangesW.restype = w.BOOL
        k.GetOverlappedResult.argtypes = [w.HANDLE,ctypes.POINTER(Overlapped),ctypes.POINTER(w.DWORD),w.BOOL]
        k.GetOverlappedResult.restype = w.BOOL
        k.CancelIoEx.argtypes = [w.HANDLE,ctypes.POINTER(Overlapped)]; k.CancelIoEx.restype = w.BOOL
        k.WaitForSingleObject.argtypes = [w.HANDLE,w.DWORD]; k.WaitForSingleObject.restype = w.DWORD
        k.ResetEvent.argtypes = [w.HANDLE]; k.ResetEvent.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]; k.CloseHandle.restype = w.BOOL
        source = Path(self.library["source"])
        no_link_ancestors(source)
        handle = k.CreateFileW(str(source),1,7,None,3,0x02000000|0x40000000,None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        event = None
        try:
            event = k.CreateEventW(None,True,False,None)
            if not event:
                raise ctypes.WinError(ctypes.get_last_error())
            # DWORD array guarantees the alignment required by ReadDirectoryChangesW.
            buffer = (w.DWORD*(65536//4))()
            first = True
            while not self.stop_event.is_set():
                ov = Overlapped(); ov.hEvent = event
                k.ResetEvent(event)
                ok = k.ReadDirectoryChangesW(handle,buffer,65536,True,0x1|0x2|0x8|0x10,None,ctypes.byref(ov),None)
                if not ok and ctypes.get_last_error()!=997:
                    raise ctypes.WinError(ctypes.get_last_error())
                if first:
                    self.status = "已订阅"
                    self.ready_event.set()
                    self.log("INFO",f"{self.library['id']} Windows 目录通知已订阅")
                    self.callback(self.library["id"],"")
                    first = False
                while True:
                    wait = k.WaitForSingleObject(event,500)
                    if wait == 0:
                        break
                    if wait != 258:
                        k.CancelIoEx(handle,ctypes.byref(ov))
                        raise ctypes.WinError(ctypes.get_last_error())
                    if self.stop_event.is_set():
                        k.CancelIoEx(handle,ctypes.byref(ov))
                        k.WaitForSingleObject(event,0xFFFFFFFF)
                        return
                count = w.DWORD()
                if not k.GetOverlappedResult(handle,ctypes.byref(ov),ctypes.byref(count),False):
                    code = ctypes.get_last_error()
                    if code == 1022:
                        self.callback(self.library["id"],""); continue
                    raise ctypes.WinError(code)
                if not count.value:
                    self.callback(self.library["id"],""); continue
                try:
                    paths = list(parse_notifications(ctypes.string_at(buffer,count.value)))
                except (ValueError,UnicodeError):
                    self.callback(self.library["id"],""); continue
                for name in paths:
                    parent = str(PurePosixPath(name).parent)
                    self.callback(self.library["id"],"" if parent=="." else parent)
        finally:
            k.CloseHandle(handle)
            if event:
                k.CloseHandle(event)


class NativeMonitor:
    def __init__(self, libraries, callback, log):
        self.watchers = [DirectoryWatch(lib,callback,log) for lib in libraries if lib["enabled"]] if os.name=="nt" else []

    def start(self):
        for w in self.watchers:
            w.start()

    def stop(self):
        for w in self.watchers:
            w.stop()

    def summary(self):
        if not self.watchers:
            return None
        n = sum(w.ready_event.is_set() for w in self.watchers)
        return f"Windows 目录通知 {n}/{len(self.watchers)} 已连接"
