"""Linux mount identity checks; a directory alone is not evidence of a cloud mount."""
import re
import sys
from pathlib import Path


def unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def mount_identity(root, mountinfo=None):
    if sys.platform != "linux" and mountinfo is None:
        raise OSError("mount_root 保护需要 Linux mountinfo；本地演示请将其留空")
    target = str(Path(root).absolute())
    text = Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
    result = None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields or unescape(fields[4]) != target:
            continue
        split = fields.index("-")
        result = (fields[0], fields[2], unescape(fields[3]), fields[4], fields[split+1])
    if result is None:
        raise OSError(f"所需挂载未就绪：{root}；已暂停读写和清理")
    return result


def identities(lib):
    return tuple((key, mount_identity(lib[key])) for key in ("mount_root", "output_mount_root") if lib.get(key))


def assert_unchanged(lib, previous):
    if identities(lib) != previous:
        raise OSError("扫描期间挂载身份发生变化，已停止本次操作")


def filesystem_type(path, mountinfo=None):
    """Filesystem type of the mount containing path, or None when it cannot be determined."""
    target = str(Path(path).absolute())
    try:
        text = Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
    except OSError:
        return None
    best, kind = "", None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            continue
        point = unescape(fields[4])
        if (target == point or target.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, kind = point, fields[fields.index("-") + 1]
    return kind
