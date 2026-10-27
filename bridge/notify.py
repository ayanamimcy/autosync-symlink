import json
import ntpath
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .config import secret, normalized


def map_path(path, server):
    a, b = server["path_from"], server["path_to"]
    if not a:
        return path
    p, src = normalized(path), normalized(a)
    if p == src:
        suffix = ""
    elif p.startswith(src+"/"):
        # Keep the original case; source and actual path have normalized separators.
        suffix = path.replace("\\","/").rstrip("/")[len(a.replace("\\","/").rstrip("/")):]
    else:
        raise ValueError("通知路径未包含在 path_from 中")
    result = b.rstrip("/\\")+suffix.replace("\\","/")
    return result.replace("/","\\") if "\\" in b else result


def send(server, rows, opener=urlopen):
    headers = {"Content-Type":"application/json"}
    base = server["url"].rstrip("/")
    token = secret(server["token"])
    if server["kind"] == "plex":
        headers["X-Plex-Token"] = token
        paths = set()
        for r in rows:
            p = map_path(r["path"],server)
            parent = ntpath.dirname(p) if "\\" in p else str(Path(p).parent)
            paths.add(parent)
        for p in sorted(paths):
            req = Request(base+"/library/sections/"+str(server["section_id"])+"/refresh?"+urlencode({"path":p}),headers=headers)
            with opener(req,timeout=20) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError("Plex 扫描请求失败")
    else:
        if server["kind"] == "jellyfin":
            headers["Authorization"] = 'MediaBrowser Token="'+token+'"'
        else:
            headers["X-Emby-Token"] = token
        data = {"Updates":[{"Path":map_path(r["path"],server),"UpdateType":r["action"]} for r in rows]}
        req = Request(base+"/Library/Media/Updated",json.dumps(data).encode(),headers,method="POST")
        with opener(req,timeout=20) as response:
            if not 200 <= response.status < 300:
                raise ConnectionError("媒体变更通知失败")


def deliver(state, config):
    for server in config["servers"]:
        if not server["enabled"]:
            continue
        rows = state.query("SELECT * FROM notices WHERE server=? AND next_at<=? ORDER BY id LIMIT 100",(server["id"],time.time()))
        if not rows:
            continue
        try:
            send(server,rows)
        except Exception as e:
            for row in rows:
                delay = min(3600,10*2**min(row["attempts"],9))
                state.execute("UPDATE notices SET attempts=attempts+1,next_at=? WHERE id=? AND version=?",(time.time()+delay,row["id"],row["version"]))
            state.log("WARN",f"{server['id']} 通知未送达 ({type(e).__name__})，已保留并将重试")
        else:
            for row in rows:
                state.execute("DELETE FROM notices WHERE id=? AND version=?",(row["id"],row["version"]))
            state.log("INFO",f"{server['id']} 已接收 {len(rows)} 条媒体变更通知")
