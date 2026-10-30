import argparse
from pathlib import Path

from .config import load
from .service import Downloader, Store
from .web import Server


def main():
    parser = argparse.ArgumentParser(description="可选的公开 BT → CD2 离线 → qB 完整校验和做种代理")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("arr-download-data"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = load(args.config)
    if args.check:
        print("下载代理配置有效；未连接 qB/CD2 或创建任务")
        return
    store = Store(args.data_dir / "downloads.sqlite3")
    downloader = Downloader(config, store)
    server = Server(downloader)
    downloader.start()
    try:
        print(f"Arr download proxy listening on {config['host']}:{config['port']}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        downloader.close()
        if not downloader.thread.is_alive():
            store.close()


if __name__ == "__main__":
    main()
