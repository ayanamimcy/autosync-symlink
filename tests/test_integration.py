"""Real local process/HTTP/filesystem test; no external service credentials required."""
import copy
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from bridge.config import DEFAULT, atomic_json
from bridge.engine import Engine
from bridge.state import State


class LocalIntegration(unittest.TestCase):
    def test_process_start_full_scan_incremental_update_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); source = root/"source"; data = root/"data"
            source.mkdir(); data.mkdir()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
            config = copy.deepcopy(DEFAULT)
            config["web"] = {"host": "127.0.0.1", "port": port}
            config["sync"].update(watch_mode="poll", watch_poll_seconds=1, debounce_seconds=1,
                                   settle_seconds=0, interval_seconds=86400)
            lib = config["libraries"][0]
            lib.update(enabled=True, source=str(source), output=str(root/"library"))
            atomic_json(data/"config.json", config)
            state = State(data/"state.sqlite3")
            Engine(state, config).initialize(lib); state.db.close()
            (source/"first.mkv").write_bytes(b"video-placeholder")
            (source/"movie.nfo").write_bytes(b"old")
            # Keep a traceback if a shared runner stalls during service startup.
            launch = ("import faulthandler, runpy; "
                      "faulthandler.dump_traceback_later(20, repeat=True); "
                      "runpy.run_path('run.py', run_name='__main__')")
            command = [sys.executable, "-c", launch, "--data-dir", str(data), "serve"]
            base = f"http://127.0.0.1:{port}"
            service_log = tempfile.TemporaryFile()
            self.addCleanup(service_log.close)
            def start():
                return subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                        stdout=service_log, stderr=subprocess.STDOUT)
            process = start()
            # These requests must stay on loopback even when a runner configures
            # environment or macOS system proxies.
            opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def diagnostics():
                service_log.seek(0)
                return service_log.read().decode(errors="replace")[-8000:]
            def request(path, body=None):
                encoded = json.dumps(body).encode() if body is not None else None
                req = Request(base+path, encoded, {"X-Media-Bridge": "1", "Content-Type": "application/json"})
                with opener.open(req, timeout=3) as response:
                    return json.load(response)
            def wait_for(check, timeout=12):
                until = time.monotonic()+timeout
                last_error = None
                while time.monotonic() < until:
                    if process.poll() is not None:
                        self.fail("service process exited:\n"+diagnostics())
                    try:
                        if check():
                            return
                    except (URLError, FileNotFoundError, ConnectionError) as error:
                        last_error = error
                    time.sleep(.15)
                self.fail(f"local integration condition timed out; last error: {last_error!r}\n{diagnostics()}")
            try:
                wait_for(lambda: request("/healthz")["ok"], timeout=60)
                request("/api/login", {"key": (data/"admin.key").read_text()})
                wait_for(lambda: request("/api/status")["libraries"][0]["scan"]["last_full_success"])
                self.assertTrue((root/"library/first.mkv").is_symlink())
                self.assertEqual((root/"library/movie.nfo").read_bytes(), b"old")
                (source/"movie.nfo").write_bytes(b"new metadata")
                (source/"second.mkv").write_bytes(b"video-placeholder")
                wait_for(lambda: (root/"library/second.mkv").is_symlink()
                         and (root/"library/movie.nfo").read_bytes() == b"new metadata")
                (source/"first.mkv").unlink()
                time.sleep(2.5)
                self.assertTrue(os.path.lexists(root/"library/first.mkv"), "default cleanup must stay disabled")
                process.terminate(); process.wait(timeout=10)
                self.assertEqual(process.returncode, 0)
                process = start()
                wait_for(lambda: request("/healthz")["ok"], timeout=60)
                request("/api/login", {"key": (data/"admin.key").read_text()})
                self.assertIsNotNone(request("/api/status")["libraries"][0]["scan"]["last_full_success"])
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
