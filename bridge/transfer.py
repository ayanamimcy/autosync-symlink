"""Same-volume, no-overwrite moves. Never copy video bytes through this host."""
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import sys
import time
from .paths import is_link, no_link_ancestors, walk


def rename_noreplace(src, dst):
    if os.name == "nt":
        os.rename(src,dst)  # Win32 MoveFile without REPLACE_EXISTING / COPY_ALLOWED.
        return
    libc = ctypes.CDLL(None,use_errno=True)
    if sys.platform == "darwin":
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint]
        result = fn(os.fsencode(src),os.fsencode(dst),4)  # RENAME_EXCL
    elif hasattr(libc,"renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        result = fn(-100,os.fsencode(src),-100,os.fsencode(dst),1)
    else:
        raise OSError("此平台不支持不覆盖原子移动；已拒绝转存")
    if result:
        e = ctypes.get_errno()
        raise OSError(e,os.strerror(e))


class Transfers:
    def __init__(self, state):
        self.state = state
        self.observed = {}

    def initialize(self, task):
        root = Path(task["source"])
        no_link_ancestors(root)
        if not root.is_dir():
            raise OSError("转存源目录不可访问；不会自动创建")
        marker = root/".media-transfer-ready"
        if marker.exists() or marker.is_symlink():
            if is_link(marker) or marker.read_text().strip() != "media-transfer:"+task["id"]:
                raise OSError("转存标记不匹配")
        else:
            with marker.open("x") as f:
                f.write("media-transfer:"+task["id"]+"\n")

    def scan(self, task):
        root, dst = Path(task["source"]),Path(task["destination"])
        no_link_ancestors(root); no_link_ancestors(dst)
        marker = root/".media-transfer-ready"
        if is_link(marker) or marker.read_text().strip() != "media-transfer:"+task["id"]:
            raise OSError("请先初始化转存源健康标记")
        if not dst.is_dir():
            raise OSError("转存目标需预先创建")
        if root.stat().st_dev != dst.stat().st_dev or (os.name == "nt" and root.drive.lower() != dst.drive.lower()):
            raise OSError("只支持同一挂载卷内转存，不执行跨卷下载或复制")
        now = time.time(); active = set(); moved = []
        for p in root.iterdir():
            if p.name.startswith(".") or p.suffix.lower() in (".tmp",".part",".partial",".!qb") or is_link(p):
                continue
            key = (task["id"],str(p)); active.add(key)
            entries = list(walk(p)) if p.is_dir() else [(p,p.stat())]
            # Refuse trees with junctions/symlinks or unfinished download files.
            blocked = False
            if p.is_dir():
                for directory, dirs, files in os.walk(p,followlinks=False):
                    if any(is_link(Path(directory)/x) for x in dirs+files):
                        blocked = True; break
            if blocked or not entries or any(f.suffix.lower() in (".tmp",".part",".partial",".!qb") for f,_ in entries):
                continue
            sig = hashlib.sha256(repr(sorted((str(f),s.st_size,s.st_mtime_ns) for f,s in entries)).encode()).hexdigest()
            latest = max(s.st_mtime for _,s in entries)
            previous = self.observed.get(key)
            if not previous or previous[0] != sig:
                self.observed[key] = (sig,now); continue
            if now-previous[1] < task["stable_seconds"] or now-latest < task["stable_seconds"]:
                continue
            target = dst/p.name
            if os.path.lexists(target):
                continue
            # Re-enumerate immediately before the move.
            check = list(walk(p)) if p.is_dir() else [(p,p.stat())]
            final = hashlib.sha256(repr(sorted((str(f),s.st_size,s.st_mtime_ns) for f,s in check)).encode()).hexdigest()
            if final != sig:
                self.observed.pop(key,None); continue
            rename_noreplace(p,target)
            self.observed.pop(key,None)
            moved.append(str(target))
            self.state.log("INFO",f"转存 {task['id']}: {p.name} 已移动到目标目录")
        for key in list(self.observed):
            if key[0] == task["id"] and key not in active:
                self.observed.pop(key,None)
        return moved
