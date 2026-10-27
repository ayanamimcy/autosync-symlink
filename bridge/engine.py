from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
import time
from urllib.parse import quote
import uuid

from .paths import file_hash, is_link, no_link_ancestors, safe_join, walk
from .mounts import identities, assert_unchanged

VIDEOS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".webm", ".iso"}
METADATA = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".xml"}


class Engine:
    def __init__(self, state, config, progress=None):
        self.state, self.config = state, config
        self.progress = progress or (lambda *_:None)

    def health(self, lib):
        identities(lib)
        source = Path(lib["source"])
        no_link_ancestors(source)
        if not source.is_dir():
            raise OSError("CloudDrive 挂载或媒体源不可访问")
        marker = source/lib["sentinel"]
        if is_link(marker) or marker.read_text(encoding="utf-8").strip() != "media-bridge:"+lib["id"]:
            raise OSError("源目录健康标记缺失或不匹配，请先初始化标记")

    def initialize(self, lib):
        identities(lib)
        source = Path(lib["source"])
        no_link_ancestors(source)
        if not source.is_dir():
            raise OSError("源目录尚未挂载；不会自动创建源目录")
        marker = source/lib["sentinel"]
        text = "media-bridge:"+lib["id"]
        if marker.exists() or marker.is_symlink():
            self.health(lib)
            return
        with marker.open("x",encoding="utf-8") as f:
            f.write(text+"\n")
        self.state.log("INFO",f"已为 {lib['id']} 初始化源目录健康标记")

    def owned(self, dest, row):
        if row["kind"] == "symlink":
            return dest.is_symlink() and os.readlink(dest) == row["fingerprint"]
        return (not is_link(dest) and dest.is_file()
                and dest.stat().st_size <= self.config["sync"]["metadata_max_mb"]*1024*1024
                and file_hash(dest) == row["fingerprint"])

    def mkdirs(self, root, dest):
        no_link_ancestors(root)
        root.mkdir(parents=True,exist_ok=True)
        cur = root
        for part in dest.relative_to(root).parts[:-1]:
            cur /= part
            if not cur.exists():
                cur.mkdir()
                self.state.execute("INSERT OR IGNORE INTO dirs VALUES(?,?)",(str(root),cur.relative_to(root).as_posix()))
            no_link_ancestors(cur)

    def scan(self, lib, scope="", cleanup=False, dry_run=False):
        mount_ids = identities(lib)
        self.health(lib)
        source, output = Path(lib["source"]),Path(lib["output"])
        no_link_ancestors(output)
        excluded = self.config["sync"]["exclude_directories"]
        if scope and any(part.casefold() in {x.casefold() for x in excluded} for part in Path(scope).parts):
            return {"library":lib["id"],"scope":scope,"seen":0,"changes":[],"change_count":0,
                    "deferred":0,"scan_complete":True,"errors":[],"dry_run":dry_run,"skipped":True}
        if scope:
            base = safe_join(source,scope)
        else:
            base = source
        # Missing scoped directory is valid only after checking the full source health.
        listing_errors = []
        try:
            mode = base.stat().st_mode
        except FileNotFoundError:
            files = []
        else:
            if not stat.S_ISDIR(mode):
                raise OSError("扫描范围不再是目录，已停止处理")
            files = list(walk(base,errors=listing_errors,progress=self.progress,exclude_dirs=excluded))
        assert_unchanged(lib, mount_ids)
        seen, changes, errors, deferred = set(), [], list(listing_errors), 0
        rows = {r["rel"]:r for r in self.state.rows(lib["id"])}
        if any(r["source_root"] != str(source) or r["output_root"] != str(output) for r in rows.values()):
            raise ValueError("该库已有文件清单，修改目录时请新建不同 id 的库")
        now = time.time()
        # Build and validate the entire listing before applying anything.
        planned, dests = [], set()
        for p, st in files:
            ext = p.suffix.lower()
            if ext not in VIDEOS | METADATA:
                continue
            rel = p.relative_to(source).as_posix(); seen.add(rel)
            if now-st.st_mtime < self.config["sync"]["settle_seconds"]:
                deferred += 1
                continue
            kind = lib["mode"] if ext in VIDEOS else "metadata"
            dest_rel = str(Path(rel).with_suffix(".strm")).replace("\\","/") if kind == "strm" else rel
            # Case-fold catches Windows collisions even in tests on POSIX.
            if dest_rel.casefold() in dests:
                raise ValueError(f"输出文件名冲突，整个扫描已停止: {dest_rel}")
            dests.add(dest_rel.casefold())
            safe_join(output,dest_rel)
            planned.append((p,st,rel,kind,dest_rel))
        for p, st, rel, kind, dest_rel in planned:
            assert_unchanged(lib, mount_ids)
            self.progress("复制元数据" if kind=="metadata" else "生成索引",str(p))
            row = rows.get(rel)
            dest = safe_join(output,dest_rel)
            sig = f"{st.st_size}:{st.st_mtime_ns}:{kind}:{lib['strm_prefix']}"
            exists = os.path.lexists(dest)
            if row and row["dest"] != dest_rel:
                errors.append(f"{rel}: 修改输出模式请使用新的媒体库 id"); continue
            if exists and (not row or not self.owned(dest,row)):
                errors.append(f"{dest_rel}: 保留非托管或已被修改的文件"); continue
            verify_due = (kind == "metadata" and row and
                          now-row.get("verified_at", 0) >= self.config["sync"]["metadata_verify_seconds"])
            if row and exists and row["source_sig"] == sig and not verify_due:
                if row["missing_since"] is not None and not dry_run:
                    self.state.execute("UPDATE files SET missing_since=NULL WHERE library=? AND rel=?",(lib["id"],rel))
                continue
            if kind == "symlink":
                payload = str(p.absolute()); fingerprint = payload
            elif kind == "strm":
                prefix = lib["strm_prefix"]
                text = prefix.rstrip("/")+"/"+quote(rel,safe="/") if prefix else str(p.absolute())
                payload = (text+"\n").encode("utf-8")
                fingerprint = hashlib.sha256(payload).hexdigest()
            else:
                if st.st_size > self.config["sync"]["metadata_max_mb"]*1024*1024:
                    errors.append(f"{rel}: 元数据超出大小限制"); continue
                payload = None; fingerprint = None
            changes.append({"action":"更新" if row else "创建", "path":dest_rel})
            if dry_run:
                continue
            self.mkdirs(output,dest)
            temp = dest.with_name(".bridge-"+uuid.uuid4().hex+".tmp")
            try:
                if kind == "symlink":
                    os.symlink(payload,temp)
                    # Windows may expose an NT/extended path prefix via readlink.
                    # Persist the OS representation so future ownership checks match.
                    fingerprint = os.readlink(temp)
                elif kind == "strm":
                    with temp.open("xb") as f:
                        f.write(payload); f.flush(); os.fsync(f.fileno())
                else:
                    # Bound reads even if the source grows after stat().
                    with p.open("rb") as f:
                        payload = f.read(self.config["sync"]["metadata_max_mb"]*1024*1024+1)
                    if len(payload) > self.config["sync"]["metadata_max_mb"]*1024*1024:
                        raise OSError("元数据读取超限")
                    after = p.stat()
                    if (after.st_size,after.st_mtime_ns) != (st.st_size,st.st_mtime_ns):
                        raise OSError("源文件仍在变动，等待下次重试")
                    fingerprint = hashlib.sha256(payload).hexdigest()
                    if row and exists and fingerprint == row["fingerprint"]:
                        self.state.record(self.config,(lib["id"],rel,str(output),dest_rel,str(source),sig,kind,fingerprint))
                        changes.pop()
                        continue
                    with temp.open("xb") as f:
                        f.write(payload); f.flush(); os.fsync(f.fileno())
                # Re-check external changes immediately before replacing.
                assert_unchanged(lib, mount_ids)
                if os.path.lexists(dest) and (not row or not self.owned(dest,row)):
                    raise OSError("输出文件被其他程序修改，已保留")
                os.replace(temp,dest)
                self.state.record(self.config,(lib["id"],rel,str(output),dest_rel,str(source),sig,kind,fingerprint),changed=True)
            except Exception as e:
                changes.pop()
                errors.append(f"{dest_rel}: {type(e).__name__} {e}")
            finally:
                temp.unlink(missing_ok=True)
        if cleanup and listing_errors:
            errors.append("目录快照不完整：本次仅创建/更新可读文件，已暂停所有清理")
        if cleanup and not listing_errors:
            assert_unchanged(lib, mount_ids)
            self.health(lib)
            eligible = [r for r in rows.values() if not scope or r["rel"].startswith(scope.rstrip("/")+"/")]
            missing = [r for r in eligible if r["rel"] not in seen]
            # Cleanup never treats an inaccessible/empty mount as authoritative.
            if missing and not seen and not self.config["sync"]["allow_empty_cleanup"]:
                errors.append("空快照保护：本次不删除任何托管文件")
            elif missing and len(missing)/max(len(rows),1) > self.config["sync"]["max_delete_fraction"]:
                errors.append(f"删除熔断：缺失 {len(missing)}/{len(rows)}，超出 max_delete_fraction")
            else:
                for row in missing:
                    if row["missing_since"] is None:
                        if not dry_run:
                            self.state.execute("UPDATE files SET missing_since=? WHERE library=? AND rel=?",(now,lib["id"],row["rel"]))
                        changes.append({"action":"开始缺失宽限期", "path":row["dest"]})
                        continue
                    if now-row["missing_since"] < self.config["sync"]["delete_grace_seconds"]:
                        continue
                    dest = safe_join(output,row["dest"])
                    if os.path.lexists(dest) and not self.owned(dest,row):
                        errors.append(f"{row['dest']}: 保留已修改文件，未清理"); continue
                    changes.append({"action":"删除", "path":row["dest"]})
                    if not dry_run:
                        assert_unchanged(lib, mount_ids)
                        self.health(lib)
                        dest.unlink(missing_ok=True)
                        self.state.remove(self.config,row)
                if not dry_run:
                    self.prune(output)
        result = {"library":lib["id"],"scope":scope,"seen":len(seen),"changes":changes[:500],
                  "change_count":len(changes),"deferred":deferred,"scan_complete":not listing_errors,"errors":errors[:100],"dry_run":dry_run}
        if not dry_run:
            self.state.log("WARN" if errors else "INFO",f"{lib['name']}: 扫描 {len(seen)}，变更 {len(changes)}，问题 {len(errors)}" + ("；"+"；".join(errors[:3]) if errors else ""))
        return result

    def prune(self, root):
        rows = self.state.query("SELECT rel FROM dirs WHERE root=?",(str(root),))
        for row in sorted(rows,key=lambda x:len(x["rel"]),reverse=True):
            p = safe_join(root,row["rel"])
            if is_link(p):
                continue
            try:
                p.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                continue
            self.state.execute("DELETE FROM dirs WHERE root=? AND rel=?",(str(root),row["rel"]))
