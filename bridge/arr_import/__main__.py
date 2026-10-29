import argparse
import json
from pathlib import Path

from .cloud import Cloud
from .config import inside, load, remote_path
from .service import Importer, Server, Store, identity


def main():
    parser = argparse.ArgumentParser(description="独立的 Radarr/Sonarr → CD2 入库整理服务")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("arr-import-data"))
    parser.add_argument("--check", action="store_true", help="只校验配置，不连接云盘")
    parser.add_argument("--probe", metavar="CD2_PATH", help="只读检查云端文件与上传任务")
    args = parser.parse_args()
    c = load(args.config)
    if args.check:
        print("配置有效；未连接云盘或执行文件操作")
        return
    if args.probe:
        path = remote_path(args.probe)
        if not inside(path, c["account_root"]):
            parser.error("probe 路径必须位于配置的网盘账号内")
        cloud = Cloud(c["cloud"])
        info, uploads = cloud.get_file(path), cloud.uploads(path)
        print(json.dumps({"file": info, "uploads": uploads,
                          "remote_ready": bool(identity(info)) and all(u["state"] == 5 for u in uploads)}, ensure_ascii=False))
        return
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(args.data_dir / "imports.sqlite3")
    importer = Importer(c, store)
    server = Server(importer)
    importer.start()
    try:
        print(f"Arr import service listening on {c['host']}:{c['port']}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        importer.close()
        if not importer.thread.is_alive():
            store.close()


if __name__ == "__main__":
    main()
