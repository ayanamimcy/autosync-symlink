"""Create an isolated, loopback-only demo. Sample MKV files are placeholders, not videos."""
import copy
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.config import DEFAULT, atomic_json
from bridge.engine import Engine
from bridge.state import State


def create(root):
    root = Path(root).resolve()
    data = root/"data"
    if (data/"config.json").exists():
        raise ValueError("演示配置已存在；不会覆盖已有演示或数据")
    source = root/"source"; source.mkdir(parents=True, exist_ok=True); data.mkdir(exist_ok=True)
    samples = {"示例电影 (2026)/示例电影.mkv": b"DEMO PLACEHOLDER - NOT PLAYABLE",
               "示例电影 (2026)/movie.nfo": '<movie><title>示例电影</title><year>2026</year></movie>'.encode(),
               "示例电影 (2026)/示例电影.zh.srt": b"1\n00:00:00,000 --> 00:00:01,000\nDemo subtitle\n"}
    for name, content in samples.items():
        p = source/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
    config = copy.deepcopy(DEFAULT)
    config["web"] = {"host": "127.0.0.1", "port": 18095}
    config["sync"].update(watch_mode="poll", watch_poll_seconds=1, settle_seconds=0, interval_seconds=10)
    config["libraries"][0].update(enabled=True, source=str(source), output=str(root/"library"), name="本地演示")
    state = State(data/"state.sqlite3")
    try:
        Engine(state, config).initialize(config["libraries"][0])
    finally:
        state.db.close()
    atomic_json(data/"config.json", config)
    return data


if __name__ == "__main__":
    directory = create(Path(__file__).resolve().parents[1]/".local-demo")
    print("演示已创建（示例视频不可播放）。启动命令：")
    print(f'python run.py --data-dir "{directory}" serve')
    print("页面：http://127.0.0.1:18095；启动后从 data/admin.key 读取管理密钥。")
