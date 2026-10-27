import json
from pathlib import Path
import sqlite3
import threading
import time


class State:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS files (
          library TEXT, rel TEXT, output_root TEXT, dest TEXT, source_root TEXT,
          source_sig TEXT, kind TEXT, fingerprint TEXT, missing_since REAL,
          PRIMARY KEY(library,rel));
        CREATE TABLE IF NOT EXISTS dirs (root TEXT, rel TEXT, PRIMARY KEY(root,rel));
        CREATE TABLE IF NOT EXISTS notices (
          id INTEGER PRIMARY KEY, server TEXT, library TEXT, path TEXT, action TEXT,
          attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0,
          version INTEGER DEFAULT 0,
          UNIQUE(server,library,path));
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, at REAL, level TEXT, message TEXT);
        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY, library TEXT, scope TEXT, started REAL, finished REAL,
          status TEXT, seen INTEGER DEFAULT 0, changes INTEGER DEFAULT 0,
          deferred INTEGER DEFAULT 0, error TEXT);
        ''')
        if "version" not in {x[1] for x in self.db.execute("PRAGMA table_info(notices)")}:
            self.db.execute("ALTER TABLE notices ADD COLUMN version INTEGER DEFAULT 0")
        if "verified_at" not in {x[1] for x in self.db.execute("PRAGMA table_info(files)")}:
            self.db.execute("ALTER TABLE files ADD COLUMN verified_at REAL DEFAULT 0")
        self.db.execute("UPDATE scans SET status='interrupted',finished=? WHERE status='running'", (time.time(),))
        self.db.commit()

    def query(self, sql, args=()):
        with self.lock:
            return [dict(x) for x in self.db.execute(sql,args).fetchall()]

    def execute(self, sql, args=()):
        with self.lock, self.db:
            self.db.execute(sql,args)

    def log(self, level, message):
        self.execute("INSERT INTO events(at,level,message) VALUES(?,?,?)", (time.time(),level,str(message)))
        self.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-3000 FROM events)")

    def rows(self, library):
        return self.query("SELECT * FROM files WHERE library=?", (library,))

    def notice(self, config, library, path, action):
        for s in config["servers"]:
            if s["enabled"] and library in s["libraries"]:
                self.db.execute("""INSERT INTO notices(server,library,path,action) VALUES(?,?,?,?)
                  ON CONFLICT(server,library,path) DO UPDATE SET action=excluded.action,
                  attempts=0,next_at=0,version=notices.version+1""", (s["id"],library,str(path),action))

    def record(self, config, row, changed=False):
        with self.lock, self.db:
            previous = self.db.execute("SELECT rel FROM files WHERE library=? AND rel=?",(row[0],row[1])).fetchone()
            self.db.execute("""INSERT INTO files(library,rel,output_root,dest,source_root,source_sig,kind,fingerprint,missing_since,verified_at)
                VALUES(?,?,?,?,?,?,?,?,NULL,?)
                ON CONFLICT(library,rel) DO UPDATE SET output_root=excluded.output_root,
                dest=excluded.dest,source_root=excluded.source_root,source_sig=excluded.source_sig,
                kind=excluded.kind,fingerprint=excluded.fingerprint,missing_since=NULL,
                verified_at=excluded.verified_at""", (*row, time.time()))
            if changed:
                self.notice(config,row[0],Path(row[2])/row[3],"Modified" if previous else "Created")

    def remove(self, config, row):
        with self.lock, self.db:
            self.db.execute("DELETE FROM files WHERE library=? AND rel=?", (row["library"],row["rel"]))
            self.notice(config,row["library"],Path(row["output_root"])/row["dest"],"Deleted")

    def begin_scan(self, library, scope):
        with self.lock, self.db:
            cursor = self.db.execute("INSERT INTO scans(library,scope,started,status) VALUES(?,?,?,'running')",
                                     (library, scope, time.time()))
            return cursor.lastrowid

    def update_scan_progress(self, scan_id, progress):
        self.execute("UPDATE scans SET seen=?,changes=?,deferred=? WHERE id=? AND status='running'",
                     (progress["seen"], progress["changes"], progress["deferred"], scan_id))

    def finish_scan(self, scan_id, result=None, error=None):
        result = result or {}
        status = "failed" if error else ("complete" if result.get("scan_complete") and not result.get("errors")
                                         and not result.get("deferred") else "partial")
        self.execute("UPDATE scans SET finished=?,status=?,seen=?,changes=?,deferred=?,error=? WHERE id=?",
                     (time.time(), status, result.get("seen", 0), result.get("change_count", 0),
                      result.get("deferred", 0), str(error) if error else "；".join(result.get("errors", [])[:3]), scan_id))

    def scan_status(self, library):
        latest = self.query("SELECT * FROM scans WHERE library=? ORDER BY id DESC LIMIT 1", (library,))
        full = self.query("SELECT MAX(finished) AS at FROM scans WHERE library=? AND scope='' AND status='complete'", (library,))
        return {"latest": latest[0] if latest else None, "last_full_success": full[0]["at"]}
