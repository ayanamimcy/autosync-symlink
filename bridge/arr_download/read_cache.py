"""Bounded caching for advisory app reads, never for torrent completion or writes."""
import threading
import time

from .clients import UpstreamError


class AppReadCache:
    POLICIES = {"app/version": 3600, "app/webapiVersion": 3600, "app/preferences": 5}

    def __init__(self, clock=time.monotonic, wait_seconds=17):
        self.clock, self.wait_seconds = clock, wait_seconds
        self.lock = threading.Lock()
        self.refresh = {key: threading.Lock() for key in self.POLICIES}
        self.entries, self.failures = {}, {}

    def get(self, key, fetch):
        ttl = self.POLICIES[key]
        with self.lock:
            entry = self.entries.get(key)
            if entry and self.clock() - entry[0] <= ttl:
                return (*entry[1], "hit")
        refresh = self.refresh[key]
        acquired = refresh.acquire(blocking=False)
        if not acquired:
            # Serve an existing bounded snapshot immediately during refresh.
            with self.lock:
                entry = self.entries.get(key)
                if entry and self.clock() - entry[0] <= ttl + 60:
                    return (*entry[1], "stale")
            acquired = refresh.acquire(timeout=self.wait_seconds)
        if not acquired:
            raise UpstreamError("qB 应用信息首次读取尚未完成；未使用未经验证的版本或设置")
        try:
            with self.lock:
                entry = self.entries.get(key)
                if entry and self.clock() - entry[0] <= ttl:
                    return (*entry[1], "hit")
                failure = self.failures.get(key)
                if failure and self.clock() < failure[0]:
                    if entry and self.clock() - entry[0] <= ttl + 60:
                        return (*entry[1], "stale")
                    raise failure[1]
            try:
                value = fetch()
            except UpstreamError as error:
                with self.lock:
                    self.failures[key] = (self.clock() + 3, error)
                    entry = self.entries.get(key)
                    if entry and self.clock() - entry[0] <= ttl + 60:
                        return (*entry[1], "stale")
                raise
            with self.lock:
                self.entries[key] = (self.clock(), value)
                self.failures.pop(key, None)
            return (*value, "live")
        finally:
            refresh.release()

    def diagnostics(self):
        with self.lock:
            return {key: {"age_seconds": round(self.clock() - value[0], 3),
                          "refresh_failed": key in self.failures}
                    for key, value in self.entries.items()}
