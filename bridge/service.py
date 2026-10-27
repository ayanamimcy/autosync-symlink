import copy
import ntpath
from pathlib import Path, PurePosixPath
import threading
import time
import uuid

from .cloud import CloudClient, CloudMonitor
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
        self.native = Monitor(self.config,self.schedule,self.state.log)
        self.thread = threading.Thread(target=self.run,name="media-worker",daemon=True)
        self.notifier = threading.Thread(target=self.notify_run,name="media-notifier",daemon=True)

    def new_monitor(self):
        return CloudMonitor(self.config["cloud"],self.change,self.reconnected,self.state.log)

    def start(self):
        self.state.log("INFO","服务启动；等待源目录标记和 CloudDrive 配置")
        self.monitor.start(); self.native.start(); self.thread.start(); self.notifier.start()

    def schedule(self, lib, scope=""):
        with self.queue_lock:
            key = (lib,scope)
            if (lib,"") in self.pending:
                return
            if not scope or len([k for k in self.pending if k[0]==lib]) >= 64:
                self.pending = {k:v for k,v in self.pending.items() if k[0] != lib}
                key = (lib,"")
            self.pending.setdefault(key,time.monotonic()+self.config["sync"]["debounce_seconds"])

    def reconnected(self):
        # The monitor thread must not wait for the worker's long scan lock.
        for lib in self.config["libraries"]:
            if lib["enabled"]:
                self.schedule(lib["id"])

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
        for lib in self.config["libraries"]:
            if not lib["enabled"]:
                continue
            cp = self.cloud_paths.get(lib["id"])
            if cp is None:
                self.schedule(lib["id"]); continue
            for p in (event["path"],event["new_path"]):
                if not p or ".." in p.split("/") or "\\" in p:
                    continue
                if p == cp or p.startswith(cp+"/"):
                    rel = p[len(cp):].lstrip("/")
                    scope = str(PurePosixPath(rel).parent)
                    self.schedule(lib["id"],"" if scope=="." else scope)

    def submit(self, action, library=None, **options):
        if action not in ("scan","cleanup","preview","initialize","transfer_initialize"):
            raise ValueError("不支持的任务")
        with self.queue_lock:
            if len(self.queue) >= 20:
                raise ValueError("任务队列已满，请等待当前任务完成")
            key = uuid.uuid4().hex
            self.jobs[key] = {"id":key,"action":action,"status":"排队中","created":time.time()}
            self.queue.append((key,action,library,options))
            if len(self.jobs)>100:
                for old in list(self.jobs):
                    if self.jobs[old]["status"] in ("完成","失败"):
                        self.jobs.pop(old); break
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
                            scan_progress=update_progress if scan_id is not None else None)
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
                    self.schedule(lib["id"],options.get("scope",""))
        return results

    def progress(self, stage, path):
        if self.stop_event.is_set():
            raise InterruptedError("服务正在停止，下次启动将重新对账")
        self.activity = {"stage":stage,"path":path,"at":time.time()}

    def health(self):
        return {"ok": self.thread.is_alive() and not self.stop_event.is_set()}

    def save_config(self, config):
        validate(config,self.data_dir)
        with self.lock:
            # A manifest is tied to its exact source/output and generation mode.
            for lib in config["libraries"]:
                rows = self.state.rows(lib["id"])
                if rows:
                    old = next((x for x in self.config["libraries"] if x["id"]==lib["id"]),{})
                    if any(lib[k] != old.get(k) for k in ("source","output","mode")):
                        raise ValueError("已有清单的媒体库不能修改源/输出/模式，请使用新的 id")
            atomic_json(self.config_path,config)
            old_monitor = self.monitor
            old_monitor.stop()
            self.native.stop()
            self.config = copy.deepcopy(config)
            self.cloud_paths = {}
            with self.queue_lock:
                self.pending.clear()
            self.monitor = self.new_monitor()
            self.native = Monitor(self.config,self.schedule,self.state.log)
            self.monitor.start()
            self.native.start()
            self.discover()
            self.reconnected()
            self.state.log("INFO","配置已保存并应用；Web 监听地址或端口变化需重启")

    def run(self):
        next_scan, next_transfer, next_discover = 0,0,0
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    now = time.monotonic()
                    if now >= next_discover:
                        self.discover(); next_discover = now+300
                    if now >= next_scan:
                        self.reconnected(); next_scan = now+self.config["sync"]["interval_seconds"]
                    item = None
                    with self.queue_lock:
                        if self.queue:
                            item = self.queue.pop(0)
                        due = [(k,v) for k,v in self.pending.items() if v <= now]
                        if not item and due:
                            (library,scope),_ = due[0]
                            self.pending.pop((library,scope))
                            item = (None,"scan",library,{"scope":scope,"cleanup":self.config["sync"]["cleanup_enabled"]})
                    if item:
                        key, action, library, options = item
                        self.busy = True
                        if key:
                            self.jobs[key]["status"] = "运行中"
                        try:
                            result = self.perform(action,library,**options)
                            if key:
                                self.jobs[key].update(status="完成",result=result)
                        except Exception as e:
                            message = str(e)
                            self.state.log("ERROR",f"{action}: {message}")
                            if key:
                                self.jobs[key].update(status="失败",error=message)
                            elif isinstance(e,OSError):
                                self.schedule(library,options.get("scope",""))
                        finally:
                            self.busy = False
                            self.activity = None
                    if now >= next_transfer:
                        for task in self.config["transfers"]:
                            if task["enabled"]:
                                try:
                                    if self.transfers.scan(task):
                                        self.reconnected()
                                except Exception as e:
                                    self.state.log("WARN",f"转存 {task['id']}: {e}")
                        next_transfer = now+15
            except Exception as e:
                self.state.log("ERROR",f"后台任务异常 ({type(e).__name__})")
            self.stop_event.wait(0.5)

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
        native = self.native.summary()
        cloud = ((native+"；"+self.monitor.status) if self.config["cloud"]["enabled"] else native) if native else self.monitor.status
        return {"cloud":cloud,"native_watch":native,"grpc":self.monitor.status,"busy":self.busy,"activity":self.activity,"pending_jobs":pending,
                "managed":self.state.query("SELECT library,kind,COUNT(*) AS count FROM files GROUP BY library,kind"),
                "pending_notifications":self.state.query("SELECT COUNT(*) AS n FROM notices")[0]["n"],
                "libraries":[{"id":x["id"],"name":x["name"],"enabled":x["enabled"],"source":x["source"],
                              "output":x["output"],"mode":x["mode"],"cloud_path":self.cloud_paths.get(x["id"]),
                              "last":self.last_results.get(x["id"]),"scan":self.state.scan_status(x["id"]),
                              "progress":live_progress.get(x["id"])} for x in self.config["libraries"]],
                "jobs":jobs}

    def stop(self):
        self.stop_event.set(); self.monitor.stop(); self.native.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        if self.notifier.is_alive():
            self.notifier.join(timeout=2)
        self.state.log("INFO","服务已停止")
