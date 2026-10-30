"""Bounded v1 metainfo reader. Hash the original info bytes, never re-encode."""
import base64
import hashlib
import posixpath
import re
from urllib.parse import parse_qs, urlencode, urlsplit

MAX_META = 8 * 1024 * 1024


def relative(path):
    if (not isinstance(path, str) or not path or path.startswith("/")
            or any(c in path for c in "\\\0\r\n")
            or any(p in ("", ".", "..") for p in path.split("/"))):
        raise ValueError("种子包含不安全或不支持的文件路径")
    return path


def magnet(link):
    u = urlsplit(link)
    if u.scheme.lower() != "magnet" or u.netloc or u.path:
        raise ValueError("离线模式只接受 BT v1 磁力或上传的 .torrent")
    hashes = set()
    for xt in parse_qs(u.query).get("xt", []):
        if not xt.lower().startswith("urn:btih:"):
            raise ValueError("第一版离线模式仅支持 BT v1")
        value = xt[9:]
        if re.fullmatch(r"[a-fA-F0-9]{40}", value):
            hashes.add(value.lower())
        elif re.fullmatch(r"[A-Za-z2-7]{32}", value):
            hashes.add(base64.b32decode(value.upper()).hex())
        else:
            raise ValueError("磁力 infohash 无效")
    if len(hashes) != 1:
        raise ValueError("磁力必须包含唯一的 BT v1 infohash")
    return hashes.pop()


def magnet_parts(link):
    """Display name and tracker URLs of a magnet link, used only to rebuild it."""
    query = parse_qs(urlsplit(link).query)
    return query.get("dn", [""])[0], [t for t in query.get("tr", []) if re.match(r"(?i)(https?|udp)://", t)]


def offline_magnet(infohash, name="", trackers=()):
    # Only the infohash, display name and public tracker URLs; no cookies or other parameters.
    params = [("xt", "urn:btih:" + infohash)]
    if name:
        params.append(("dn", name))
    params += [("tr", t) for t in trackers]
    return "magnet:?" + urlencode(params)


def metainfo(raw, allow_private=False):
    try:
        return _metainfo(raw, allow_private)
    except (KeyError, TypeError, AttributeError, UnicodeError, OverflowError):
        raise ValueError("种子缺少必要字段或字段类型无效") from None


def _metainfo(raw, allow_private):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_META:
        raise ValueError("种子文件为空或超过 8 MiB")
    cursor, nodes, info_span = 0, 0, None

    def parse(depth=0):
        nonlocal cursor, nodes, info_span
        nodes += 1
        if depth > 32 or nodes > 200000 or cursor >= len(raw):
            raise ValueError("种子编码无效或结构过大")
        tag = raw[cursor:cursor + 1]
        if tag == b"i":
            end = raw.find(b"e", cursor + 1)
            value = raw[cursor + 1:end] if end >= 0 else b""
            if not re.fullmatch(rb"0|-?[1-9][0-9]{0,19}", value):
                raise ValueError("种子整数无效")
            cursor = end + 1
            return int(value)
        if tag in (b"d", b"l"):
            cursor += 1
            result = {} if tag == b"d" else []
            previous = None
            while cursor < len(raw) and raw[cursor:cursor + 1] != b"e":
                if tag == b"l":
                    result.append(parse(depth + 1))
                    continue
                key = parse(depth + 1)
                if not isinstance(key, bytes) or (previous is not None and key <= previous):
                    raise ValueError("种子字典键重复或未排序")
                previous = key
                value_start = cursor
                result[key] = parse(depth + 1)
                if depth == 0 and key == b"info":
                    info_span = (value_start, cursor)
            if cursor >= len(raw):
                raise ValueError("种子编码截断")
            cursor += 1
            return result
        end = raw.find(b":", cursor, cursor + 12)
        length = raw[cursor:end] if end >= 0 else b""
        if not re.fullmatch(rb"0|[1-9][0-9]{0,8}", length):
            raise ValueError("种子字符串长度无效")
        cursor = end + 1 + int(length)
        if cursor > len(raw):
            raise ValueError("种子字符串截断")
        return raw[end + 1:cursor]

    root = parse()
    if cursor != len(raw) or not isinstance(root, dict) or not info_span:
        raise ValueError("种子缺少 info 或带有额外数据")
    info = root[b"info"]
    if not isinstance(info, dict) or b"meta version" in info:
        raise ValueError("第一版离线模式仅支持纯 BT v1")
    private = info.get(b"private", 0) != 0
    if not allow_private and private:
        raise ValueError("私有种子不交给 115 离线；请在 Arr 中让该索引器使用原 qB 下载器")
    name = relative(info[b"name"].decode("utf-8"))
    if "/" in name:
        raise ValueError("种子根名称不能包含子路径")
    entries, padding = [], 0
    if b"files" in info:
        if b"length" in info or not isinstance(info[b"files"], list):
            raise ValueError("种子文件列表无效")
        for row in info[b"files"]:
            if not isinstance(row, dict):
                raise ValueError("种子文件列表无效")
            attr = row.get(b"attr", b"")
            if b"p" in attr or b"l" in attr or b"symlink path" in row:
                # BEP 47 padding files and symlinks carry no payload of their own.
                if type(row.get(b"length")) is not int or row[b"length"] < 0:
                    raise ValueError("种子填充文件长度无效")
                padding += row[b"length"]
                continue
            components = row[b"path"]
            if not isinstance(components, list) or not components:
                raise ValueError("种子路径列表无效")
            parts = [relative(part.decode("utf-8")) for part in components]
            if any("/" in part for part in parts):
                raise ValueError("种子路径分量无效")
            entries.append({"path": posixpath.join(name, *parts), "size": row[b"length"]})
    else:
        entries.append({"path": name, "size": info[b"length"]})
    if not entries or len(entries) > 20000:
        raise ValueError("种子文件数量无效")
    paths = set()
    for row in entries:
        if type(row["size"]) is not int or not 0 <= row["size"] < 2**63 or row["path"] in paths:
            raise ValueError("种子文件大小或重复路径无效")
        paths.add(row["path"])
    for path in paths:
        parent = posixpath.dirname(path)
        while parent:
            if parent in paths:
                raise ValueError("种子文件路径发生父子冲突")
            parent = posixpath.dirname(parent)
    size = sum(row["size"] for row in entries)
    piece_length, pieces = info.get(b"piece length"), info.get(b"pieces")
    span = size + padding
    if (not size or type(piece_length) is not int or not 0 < piece_length <= 64 * 1024**2
            or not isinstance(pieces, bytes) or len(pieces) != 20 * ((span + piece_length - 1) // piece_length)):
        raise ValueError("种子分块信息无效")
    trackers = []
    if not private:
        announce = [root.get(b"announce", b"")] + [url for tier in root.get(b"announce-list", []) if isinstance(tier, list)
                                                   for url in tier]
        for url in announce:
            url = url.decode("utf-8", "replace") if isinstance(url, bytes) else ""
            if re.match(r"(?i)(https?|udp)://", url) and url not in trackers and len(trackers) < 20:
                trackers.append(url)
    return {"hash": hashlib.sha1(raw[slice(*info_span)]).hexdigest(),
            "name": name, "size": size, "files": entries, "private": private, "trackers": trackers}
