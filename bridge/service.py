import collections
import copy
import ntpath
from pathlib import Path, PurePosixPath
import threading
import time
import uuid

from .cloud import CloudClient, CloudMonitor
from .cloud_scan import CloudScanner
from .config import atomic_json, load, validate
from .engine import Engine
from .notify import deliver
from .paths import safe_join
from .state import State
from .transfer import Transfers
from .watch import Monitor


class Service:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).absolute()
        self.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.config_path = self.data_dir/"config.json"
        self.config = load(self.config_path)
        self.state = State(self.data_dir/"state.sqlite3")
        self.lock = threading.RLock()
        self.queue_lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self.scan_progress = {}
        self.pending = {}
        self.jobs = {}
        self.queue = []
        self.stop_event = threading.Event()
        self.cloud_paths = {}
        self.last_results = {}
        self.busy = False
        self.activity = None
        self.transfers = Transfers(self.state)
        self.monitor = self.new_monitor()
        self.native = self.new_native()
        # CD2 push events: how many arrived and how many matched a library.
        self.push = {"received": 0, "matched": 0, "ignored": 0, "last_at": None,
                     "ignored_samples": collections.deque(maxlen=5)}
        self.check_now, self.check_reason = threading.Event(), None
        self.check_status = {"at": None, "reason": None, "scheduled": 0, "error": None}
        self.checker = threading.Thread(target=self.check_run, name="cloud-check", daemon=True)
        self.thread = threading.Thread(target=self.run,name="media-worker",daemon=True)
        self.notifier = threading.Thread(target=self.notify_run,name="media-notifier",daemon=True)

    def new_monitor(self):
        return CloudMonitor(self.config["cloud"],self.change,self.monitor_ready(),self.state.log)

    def new_native(self):
        return Monitor(self.config, lambda lib, scope="": self.schedule(lib, scope, "目录监控"),
                       self.state.log, ready=self.monitor_ready())

    def monitor_ready(self):
        # Connection readiness is distinct from actual file-change events.
        seen, lock = set(), threading.Lock()
        def ready(library=None):
            with lock:
                reconnect = library in seen
                seen.add(library)
            setting = "scan_on_reconnect" if reconnect else "scan_on_startup"
            if library is None:
                # Changes made while the push stream was down produce no events.
                self.request_check("推送重连后增量补扫" if reconnect else "推送连接后增量补扫")
            if not self.config["sync"][setting]:
                self.state.log("INFO",f"{library or 'CloudDrive'} 通知已就绪；按 {setting}=false 跳过全量补扫")
                return
            if library is None:
                self.reconnected("推送重连后全量补扫" if reconnect else "启动全量补扫")
            else:
                self.schedule(library, "", "目录监控就绪")
        return ready

    def start(self):
        self.state.log("INFO","服务启动；等待源目录标记和 CloudDrive 配置")
        self.monitor.start(); self.native.start(); self.thread.start(); self.notifier.start()
        self.checker.start()

    def schedule(self, lib, scope="", reason="自动"):
        """Queue an automatic scan; reasons of merged requests are kept for display."""
        with self.queue_lock:
            def add(key, reasons):
                entry = self.pending.setdefault(key, {"due": time.monotonic()+self.config["sync"]["debounce_seconds"],
                                                      "since": time.time(), "reasons": set()})
                entry["reasons"] |= reasons
            if (lib,"") in self.pending:
                add((lib,""), {reason}); return
            if not scope or len([k for k in self.pending if k[0]==lib]) >= 64:
                merged = {reason}
                for key in [k for k in self.pending if k[0]==lib]:
                    merged |= self.pending.pop(key)["reasons"]
                add((lib,""), merged); return
            add((lib,scope), {reason})

    def reconnected(self, reason="定时全量扫描"):
        # The monitor thread must not wait for the worker's long scan lock.
        for lib in self.config["libraries"]:
            if lib["enabled"]:
                self.schedule(lib["id"], "", reason)

    def discover(self):
        libs = [x for x in self.config["libraries"] if x["enabled"]]
        self.cloud_paths = {x["id"]:x["cloud_path"].rstrip("/") for x in libs if x["cloud_path"]}
        if not self.config["cloud"]["enabled"] or len(self.cloud_paths) == len(libs):
            return
        try:
            mounts = CloudClient(self.config["cloud"]).mount_points()
            for lib in libs:
                if lib["id"] in self.cloud_paths:
                    continue
                for m in sorted(mounts,key=lambda x:len(x["mount"]),reverse=True):
                    if not m["mounted"] or not m["mount"]:
                        continue
                    try:
                        pathmod = ntpath if "\\" in m["mount"] or ":" in m["mount"] else __import__("posixpath")
                        rel = pathmod.relpath(lib["source"],m["mount"])
                    except ValueError:
                        continue
                    if rel == ".." or rel.startswith(("..\\", "../")):
                        continue
                    self.cloud_paths[lib["id"]] = (m["source"].rstrip("/")+"/"+("" if rel=="." else rel.replace("\\","/"))).rstrip("/")
                    break
        except Exception as e:
            self.state.log("WARN",f"挂载映射未读取 ({type(e).__name__})；可手填 cloud_path；未知映射的事件将触发该库补扫")

    def change(self, event):
        self.push["received"] += 1
        self.push["last_at"] = time.time()
        matched = False
        for lib in self.config["libraries"]:
            if not lib["enabled"]:
                continue
            cp = self.cloud_paths.get(lib["id"])
            if cp is None:
                self.schedule(lib["id"], "", "CD2 文件通知（库映射未知）"); matched = True; continue
            for p in (event["path"],event["new_path"]):
                if not p or ".." in p.split("/") or "\\" in p:
                    continue
                if p == cp or p.startswith(cp+"/"):
                    rel = p[len(cp):].lstrip("/")
                    scope = str(PurePosixPath(rel).parent)
                    self.schedule(lib["id"],"" if scope=="." else scope, "CD2 文件通知")
                    matched = True
        if matched:
            self.push["matched"] += 1
        else:
            self.push["ignored"] += 1
            self.push["ignored_samples"].append(event["path"] or event["new_path"])

    def submit(self, action, library=None, **options):
        if action not in ("scan","cleanup","preview","initialize","transfer_initialize"):
            raise ValueError("不支持的任务")
        with self.queue_lock:
            if len(self.queue) >= 20:
                raise ValueError("任务队列已满，请等待当前任务完成")
            key = uuid.uuid4().hex
            self.jobs[key] = {"id":key,"action":action,"status":"排队中","created":time.time(),
                              "trigger":"手动","library":library or "","scope":options.get("scope","")}
            self.queue.append((key,action,library,options))
            self.prune_jobs()
            return copy.deepcopy(self.jobs[key])

    def prune_jobs(self):
        # Caller holds queue_lock. Keep the newest 100 records; never drop live ones.
        finished = [k for k, j in self.jobs.items() if j["status"] in ("完成", "失败")]
        while len(self.jobs) > 100 and finished:
            self.jobs.pop(finished.pop(0))

    def submit_config(self, config):
        # HTTP requests must not wait for the worker's long-running scan lock.
        config = copy.deepcopy(config)
        self.validate_config_update(config)
        with self.queue_lock:
            if any(job["action"] == "config" and job["status"] in ("排队中", "运行中")
                   for job in self.jobs.values()):
                raise ValueError("已有配置正在等待应用，请等待结果后再保存")
            if len(self.queue) >= 20:
                raise ValueError("任务队列已满，请等待当前任务完成")
            key = uuid.uuid4().hex
            self.jobs[key] = {"id":key, "action":"config", "status":"排队中", "created":time.time(), "trigger":"手动"}
            # Apply before the next scan, without interrupting the current one.
            self.queue.insert(0, (key, "config", None, {"config":config}))
            self.prune_jobs()
            return copy.deepcopy(self.jobs[key])

    def libs(self, library=None):
        libs = [x for x in self.config["libraries"] if x["enabled"] and (not library or x["id"]==library)]
        if not libs:
            raise ValueError("没有匹配的已启用媒体库")
        return libs

    def perform(self, action, library, **options):
        engine = Engine(self.state,self.config,self.progress)
        if action == "transfer_initialize":
            tasks = [x for x in self.config["transfers"] if x["id"]==library]
            if not tasks:
                raise ValueError("转存任务不存在")
            self.transfers.initialize(tasks[0]); return {"initialized":library}
        results = []
        for lib in self.libs(library):
            if action == "initialize":
                engine.initialize(lib); results.append({"initialized":lib["id"]}); continue
            cleanup = action in ("cleanup","preview") or options.get("cleanup",False)
            scope = options.get("scope", "")
            scan_id = self.state.begin_scan(lib["id"], scope) if action != "preview" else None
            last_checkpoint, last_stage = 0, None
            def update_progress(snapshot):
                nonlocal last_checkpoint, last_stage
                with self.progress_lock:
                    self.scan_progress[lib["id"]] = {**snapshot, "scan_id":scan_id}
                now = time.monotonic()
                # Keep UI counters current without committing SQLite for each file.
                if scan_id is not None and (now-last_checkpoint >= 1 or snapshot["stage"] != last_stage):
                    self.state.update_scan_progress(scan_id, snapshot)
                    last_checkpoint, last_stage = now, snapshot["stage"]
            engine = Engine(self.state, self.config, self.progress,
                            scan_progress=update_progress if scan_id is not None else None,
                            cloud_paths=self.cloud_paths)
            try:
                result = engine.scan(lib,scope=scope,cleanup=cleanup,dry_run=action=="preview")
            except Exception as e:
                if scan_id is not None:
                    with self.progress_lock:
                        partial = self.scan_progress.get(lib["id"], {})
                    self.state.finish_scan(scan_id, {"seen":partial.get("seen",0),
                                                    "change_count":partial.get("changes",0),
                                                    "deferred":partial.get("deferred",0)}, error=e)
                raise
            else:
                if scan_id is not None:
                    self.state.finish_scan(scan_id, result)
            finally:
                if scan_id is not None:
                    with self.progress_lock:
                        self.scan_progress.pop(lib["id"], None)
            results.append(result)
            if action != "preview" and not result.get("skipped"):
                self.last_results[lib["id"]] = {"at":time.time(),**result}
                if result["deferred"]:
                    # A fresh create event can arrive before the settling window.
                    # Retry soon instead of waiting for the 15-minute fallback scan.
                    self.schedule(lib["id"],options.get("scope",""), "延迟重试（文件尚未稳定）")
        return results

    def progress(self, stage, path):
        if self.stop_event.is_set():
            raise InterruptedError("服务正在停止，下次启动将重新对账")
        self.activity = {"stage":stage,"path":path,"at":time.time()}

    def health(self):
        return {"ok": self.thread.is_alive() and not self.stop_event.is_set()}

    def validate_config_update(self, config):
        validate(config,self.data_dir)
        current = self.config
        for lib in config["libraries"]:
            if self.state.rows(lib["id"]):
                old = next((x for x in current["libraries"] if x["id"] == lib["id"]), {})
                if any(lib[k] != old.get(k) for k in ("source", "output", "mode")):
                    raise ValueError("已有清单的媒体库不能修改源/输出/模式，请使用新的 id")

    def save_config(self, config):
        with self.lock:
            # A manifest is tied to its exact source/output and generation mode.
            self.validate_config_update(config)
            atomic_json(self.config_path,config)
            old_monitor = self.monitor
            old_monitor.stop()
            self.native.stop()
            self.config = copy.deepcopy(config)
            self.cloud_paths = {}
            with self.queue_lock:
                self.pending.clear()
            self.monitor = self.new_monitor()
            self.native = self.new_native()
            self.monitor.start()
            self.native.start()
            self.discover()
            if self.config["sync"]["scan_on_startup"]:
                self.reconnected("应用配置后全量扫描")
            self.state.log("INFO","配置已保存并应用；Web 监听地址或端口变化需重启")

    def run(self):
        next_scan = 0 if self.config["sync"]["scan_on_startup"] else time.monotonic()+self.config["sync"]["interval_seconds"]
        next_transfer, next_discover = 0,0
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    now = time.monotonic()
                    if now >= next_discover:
                        self.discover(); next_discover = now+300
                    if now >= next_scan:
                        self.reconnected("定时全量扫描"); next_scan = now+self.config["sync"]["interval_seconds"]
                    item = None
                    with self.queue_lock:
                        if self.queue:
                            item = self.queue.pop(0)
                        due = [(k,v) for k,v in self.pending.items() if v["due"] <= now]
                        if not item and due:
                            (library,scope),entry = due[0]
                            self.pending.pop((library,scope))
                            key = uuid.uuid4().hex
                            self.jobs[key] = {"id":key,"action":"scan","status":"排队中","created":entry["since"],
                                              "trigger":"、".join(sorted(entry["reasons"])),"library":library,
                                              "scope":scope,"auto":True}
                            self.prune_jobs()
                            item = (key,"scan",library,{"scope":scope,"cleanup":self.config["sync"]["cleanup_enabled"]})
                    if item:
                        key, action, library, options = item
                        self.busy = True
                        if key:
                            self.jobs[key].update(status="运行中", started=time.time())
                        try:
                            if action == "config":
                                self.save_config(options["config"])
                                next_scan = time.monotonic()+self.config["sync"]["interval_seconds"]
                                result = {"applied":True, "restart_needed":"Web 监听地址/端口修改后需重启"}
                            else:
                                result = self.perform(action,library,**options)
                            if key:
                                if self.jobs[key].get("auto"):
                                    # Keep automatic records small: they are frequent.
                                    result = [{k: v for k, v in r.items() if isinstance(v, (int, float, bool))}
                                              for r in result]
                                self.jobs[key].update(status="完成",result=result,finished=time.time())
                        except Exception as e:
                            message = str(e)
                            self.state.log("ERROR",f"{action}: {message}")
                            if key:
                                self.jobs[key].update(status="失败",error=message,finished=time.time())
                            if key and self.jobs[key].get("auto") and isinstance(e,OSError):
                                self.schedule(library,options.get("scope",""), "出错重试")
                        finally:
                            self.busy = False
                            self.activity = None
                    if now >= next_transfer:
                        for task in self.config["transfers"]:
                            if task["enabled"]:
                                try:
                                    if self.transfers.scan(task):
                                        self.reconnected("转存后补扫")
                                except Exception as e:
                                    self.state.log("WARN",f"转存 {task['id']}: {e}")
                        next_transfer = now+15
            except Exception as e:
                self.state.log("ERROR",f"后台任务异常 ({type(e).__name__})")
            self.stop_event.wait(0.5)

    def request_check(self, reason):
        self.check_reason = reason
        self.check_now.set()

    def check_run(self):
        reason = "启动后目录检查"
        while not self.stop_event.is_set():
            seconds = self.config["sync"]["cloud_check_seconds"]
            if self.config["cloud"]["enabled"] and seconds:
                try:
                    self.cloud_check(reason)
                except Exception as e:
                    if not self.check_status.get("error"):
                        self.state.log("WARN", f"CD2 目录变化检查失败 ({type(e).__name__})；下次继续")
                    self.check_status = {**self.check_status, "at": time.time(), "reason": reason,
                                         "error": type(e).__name__}
            self.check_now.wait(seconds or 300)
            reason = (self.check_reason or "按需目录检查") if self.check_now.is_set() else "定期目录检查"
            self.check_now.clear(); self.check_reason = None

    def cloud_check(self, reason):
        """Compare each library's CD2 root listing with the stored snapshot and scan what changed.

        Only direct children are compared: a new or changed top-level folder is scanned.
        Changes deeper down that do not touch the top-level folder's time are left to
        push events and the periodic full scan.
        """
        url = self.config["cloud"]["url"]
        if CloudScanner._cooldowns.get(url, 0) > time.monotonic():
            return  # CD2 rate-limit cooldown from a scan
        client = CloudClient(self.config["cloud"])
        token = client.token()
        excluded = {x.casefold() for x in self.config["sync"]["exclude_directories"]}
        scheduled = 0
        for lib in self.config["libraries"]:
            root = (self.cloud_paths.get(lib["id"]) or lib["cloud_path"] or "").rstrip("/")
            if not lib["enabled"] or not root:
                continue
            # force_refresh: changes made through another CD2 (e.g. the Arr host) are not in this cache.
            current = {e.name: [e.mtime_ns, e.size, e.directory]
                       for e in client.list_files(root, token, force_refresh=True)}
            previous = self.state.snapshot(lib["id"], root)
            self.state.save_snapshot(lib["id"], root, current)
            if previous is None:
                continue  # first look: becomes the baseline
            scopes = {name if value[2] else "" for name, value in current.items()
                      if previous.get(name) != value and name.casefold() not in excluded}
            if self.config["sync"]["cleanup_enabled"] and set(previous) - set(current):
                scopes.add("")
            for scope in sorted(scopes):
                self.schedule(lib["id"], scope, reason)
            scheduled += len(scopes)
        self.check_status = {"at": time.time(), "reason": reason, "scheduled": scheduled, "error": None}

    def notify_run(self):
        while not self.stop_event.is_set():
            try:
                # Config is replaced atomically; slow servers never block file sync.
                deliver(self.state,self.config)
            except Exception as e:
                self.state.log("WARN",f"通知任务异常 ({type(e).__name__})，等待重试")
            self.stop_event.wait(5)

    def status(self):
        with self.progress_lock:
            live_progress = copy.deepcopy(self.scan_progress)
        with self.queue_lock:
            jobs = copy.deepcopy(list(self.jobs.values())[-20:])
            pending = len(self.pending)+len(self.queue)
            now = time.monotonic()
            waiting = sorted(({"library": k[0], "scope": k[1], "trigger": "、".join(sorted(v["reasons"])),
                               "since": v["since"], "due_in": max(0, round(v["due"] - now))}
                              for k, v in self.pending.items()), key=lambda x: x["due_in"])
        native = self.native.summary()
        cloud = ((native+"；"+self.monitor.status) if self.config["cloud"]["enabled"] else native) if native else self.monitor.status
        return {"cloud":cloud,"native_watch":native,"grpc":self.monitor.status,"busy":self.busy,"activity":self.activity,"pending_jobs":pending,
                "managed":self.state.query("SELECT library,kind,COUNT(*) AS count FROM files GROUP BY library,kind"),
                "pending_notifications":self.state.query("SELECT COUNT(*) AS n FROM notices")[0]["n"],
                "libraries":[{"id":x["id"],"name":x["name"],"enabled":x["enabled"],"source":x["source"],
                              "output":x["output"],"mode":x["mode"],"cloud_path":self.cloud_paths.get(x["id"]),
                              "last":self.last_results.get(x["id"]),"scan":self.state.scan_status(x["id"]),
                              "progress":live_progress.get(x["id"])} for x in self.config["libraries"]],
                "jobs":jobs, "waiting":waiting,
                "push":{**{k: v for k, v in self.push.items() if k != "ignored_samples"},
                        "ignored_samples": list(self.push["ignored_samples"])},
                "cloud_check":dict(self.check_status)}

    def stop(self):
        self.stop_event.set(); self.check_now.set(); self.monitor.stop(); self.native.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        if self.notifier.is_alive():
            self.notifier.join(timeout=2)
        if self.checker.is_alive():
            self.checker.join(timeout=2)
        self.state.log("INFO","服务已停止")
