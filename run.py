"""Local and container entry point for AutoSync Symlink."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from bridge.service import Service
from bridge.web import WebServer
from bridge.cloud import CloudClient
from bridge.engine import Engine


class InstanceLock:
    def __init__(self, path):
        self.file = path.open("a+b")
        self.file.seek(0); self.file.write(b"0"); self.file.flush(); self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("已有实例正在使用此数据目录，请通过 Web 界面执行任务")


def main():
    parser = argparse.ArgumentParser(description="AutoSync Symlink")
    parser.add_argument("--data-dir",type=Path,default=Path(os.environ.get("AUTOSYNC_DATA_DIR", str(Path(__file__).parent/"data"))))
    parser.add_argument("command",nargs="?",choices=["serve","check","scan","preview","initialize","transfer-initialize"],default="serve")
    parser.add_argument("--library",help="媒体库 id；转存初始化时填写转存 id")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True,exist_ok=True)
    lock = InstanceLock(args.data_dir/"instance.lock")
    s = Service(args.data_dir)
    if args.command == "check":
        result = {"python":sys.version.split()[0],"libraries":[],"cloud":False}
        for lib in s.libs(args.library):
            item = {"id":lib["id"],"source":lib["source"],"output":lib["output"]}
            try:
                Engine(s.state,s.config).health(lib); item["source_ready"]=True
                output=Path(lib["output"])
                from bridge.paths import no_link_ancestors
                no_link_ancestors(output)
                output.mkdir(parents=True,exist_ok=True)
                import uuid
                test=output/(".bridge-check-"+uuid.uuid4().hex)
                try:
                    os.symlink(str(Path(lib["source"]).absolute()),test,target_is_directory=True)
                    item["symlink_ready"]=test.is_dir()
                finally:
                    test.unlink(missing_ok=True)
            except Exception as e:
                item["error"]=str(e)
            result["libraries"].append(item)
        if s.config["cloud"]["enabled"]:
            try:
                result["cloud"] = CloudClient(s.config["cloud"]).ready()
                result["mounts"] = CloudClient(s.config["cloud"]).mount_points()
            except Exception as e:
                result["cloud_error"] = type(e).__name__
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if all(x.get("source_ready") and x.get("symlink_ready") for x in result["libraries"]) and (result["cloud"] or not s.config["cloud"]["enabled"]) else 1
    if args.command != "serve":
        command = {"transfer-initialize":"transfer_initialize"}.get(args.command,args.command)
        print(json.dumps(s.perform(command,args.library),ensure_ascii=False,indent=2))
        return 0
    web = WebServer((s.config["web"]["host"],s.config["web"]["port"]),s)
    s.start()
    def stop(*_):
        threading.Thread(target=web.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,stop)
    if hasattr(signal,"SIGTERM"):
        signal.signal(signal.SIGTERM,stop)
    print(f"Web: http://127.0.0.1:{s.config['web']['port']}  管理密钥文件: {s.data_dir/'admin.key'}",flush=True)
    try:
        web.serve_forever(poll_interval=0.5)
    finally:
        web.server_close(); s.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"启动失败: {e}",file=sys.stderr)
        sys.exit(1)
