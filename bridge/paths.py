from pathlib import Path, PurePosixPath
import os
import stat
import time


def is_link(path: Path):
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def no_link_ancestors(path: Path):
    for p in (path, *path.parents):
        if is_link(p):
            raise ValueError(f"路径经过符号链接或 junction，已拒绝: {p}")


def safe_join(root: Path, relative: str):
    if "\\" in relative or ":" in relative or "\x00" in relative:
        raise ValueError("不安全的相对路径")
    p = PurePosixPath(relative)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("相对路径不能越界")
    target = root.joinpath(*p.parts)
    no_link_ancestors(target.parent)
    return target


def retry_io(operation):
    for attempt in range(4):
        try:
            return operation()
        except OSError as e:
            if attempt==3 or not (isinstance(e,FileNotFoundError) or getattr(e,"winerror",None) in (50,64,1117)):
                raise
            time.sleep((0.25,0.75,1.5)[attempt])


def walk(root: Path, errors=None, progress=None, exclude_dirs=()):
    """Optionally collect failures; callers must suppress cleanup if any occur."""
    no_link_ancestors(root)
    if not root.is_dir():
        raise OSError(f"目录不可访问: {root}")
    stack = [root]
    excluded = {x.casefold() for x in exclude_dirs}
    while stack:
        folder = stack.pop()
        if progress:
            progress("读取目录",str(folder))
        def enumerate_folder():
            try:
                with os.scandir(folder) as it:
                    return list(it)
            except OSError as e:
                if getattr(e,"winerror",None)==2 and folder.is_dir():
                    # Some virtual Windows filesystems omit dot entries for empty
                    # directories. FindFirstFile then returns FILE_NOT_FOUND;
                    # listdir follows Windows' empty-directory convention. Confirm
                    # both directory existence and the independent empty listing.
                    if not os.listdir(folder):
                        return []
                raise
        try:
            entries = retry_io(enumerate_folder)
        except OSError as e:
            if errors is None:
                raise
            errors.append(f"无法枚举 {folder}: {e}")
            continue
        for entry in entries:
            p = Path(entry.path)
            try:
                # DirEntry caches Windows FindFirstFile data. Calling Path.lstat
                # here would force a costly extra virtual-drive query per file.
                st = retry_io(lambda:entry.stat(follow_symlinks=False))
                if stat.S_ISLNK(st.st_mode) or getattr(st,"st_file_attributes",0)&0x400:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if entry.name.casefold() not in excluded:
                        stack.append(p)
                elif stat.S_ISREG(st.st_mode):
                    yield p, st
            except OSError as e:
                if errors is None:
                    raise
                errors.append(f"无法读取文件信息 {p}: {e}")
                continue


def file_hash(path: Path):
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
