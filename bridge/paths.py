from pathlib import Path, PurePosixPath, PureWindowsPath
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
    if "\x00" in relative:
        raise ValueError(f"不安全的相对路径 {relative!r}：包含 NUL 字符")
    if "\\" in relative:
        raise ValueError(f"不安全的相对路径 {relative!r}：不支持反斜杠，请使用正斜杠分隔目录")
    # A colon is a literal filename character on POSIX, but can select a drive
    # or an NTFS alternate data stream on Windows. Check the target path flavor.
    if isinstance(root, PureWindowsPath) and ":" in relative:
        raise ValueError(f"不安全的 Windows 相对路径 {relative!r}：包含冒号、盘符或数据流")
    p = PurePosixPath(relative)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"相对路径不能越界：{relative!r}")
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


def _read_directory(folder, excluded):
    files, children, failures = [], [], []
    def enumerate_folder():
        try:
            with os.scandir(folder) as it:
                return list(it)
        except OSError as e:
            if getattr(e,"winerror",None)==2 and folder.is_dir():
                # Cloud virtual filesystems can report FILE_NOT_FOUND on an
                # empty directory. Confirm it through an independent listing.
                if not os.listdir(folder):
                    return []
            raise
    try:
        entries = retry_io(enumerate_folder)
    except OSError as e:
        return files, children, [(f"无法枚举 {folder}: {e}", e)]
    for entry in entries:
        p = Path(entry.path)
        try:
            # Use cached DirEntry stat data rather than querying each path again.
            st = retry_io(lambda:entry.stat(follow_symlinks=False))
            if stat.S_ISLNK(st.st_mode) or getattr(st,"st_file_attributes",0)&0x400:
                continue
            if stat.S_ISDIR(st.st_mode):
                if entry.name.casefold() not in excluded:
                    children.append(p)
            elif stat.S_ISREG(st.st_mode):
                files.append((p, st))
        except OSError as e:
            failures.append((f"无法读取文件信息 {p}: {e}", e))
    return files, children, failures


def walk(root: Path, errors=None, progress=None, exclude_dirs=(), workers=1):
    """Enumerate directories concurrently; collect failures before any cleanup."""
    no_link_ancestors(root)
    if not root.is_dir():
        raise OSError(f"目录不可访问: {root}")
    stack = [root]
    excluded = {x.casefold() for x in exclude_dirs}
    def accept(result):
        files, children, failures = result
        if failures and errors is None:
            raise failures[0][1]
        if errors is not None:
            errors.extend(message for message, _ in failures)
        stack.extend(children)
        return files
    if workers == 1:
        while stack:
            folder = stack.pop()
            if progress:
                progress("读取目录", str(folder))
            yield from accept(_read_directory(folder, excluded))
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media-scan") as pool:
        pending = set()
        try:
            while stack or pending:
                while stack and len(pending) < workers * 2:
                    folder = stack.pop()
                    if progress:
                        progress("读取目录", str(folder))
                    pending.add(pool.submit(_read_directory, folder, excluded))
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    yield from accept(future.result())
        finally:
            for future in pending:
                future.cancel()


def file_hash(path: Path):
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
