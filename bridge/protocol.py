"""Small, bounded protobuf/gRPC-Web codec. Schema facts: see REFERENCES.md.

CloudDrive method/field mappings are documented in REFERENCES.md. Unknown
protobuf fields are skipped, compressed gRPC frames fail explicitly.
"""
import struct

MAX_FRAME = 16 * 1024 * 1024


def varint(n):
    data = bytearray()
    while n >= 128:
        data.append((n & 127) | 128); n >>= 7
    data.append(n)
    return bytes(data)


def text_field(number, value):
    b = value.encode("utf-8")
    return varint(number << 3 | 2) + varint(len(b)) + b


def read_varint(data, pos):
    n = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("protobuf varint 截断")
        byte = data[pos]; pos += 1; n |= (byte & 127) << shift
        if byte < 128:
            return n, pos
    raise ValueError("protobuf varint 超长")


def fields(data):
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data,pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("protobuf 字段编号为零")
        if wire == 0:
            value, pos = read_varint(data,pos)
        elif wire in (1,2,5):
            if wire == 2:
                length, pos = read_varint(data,pos)
            else:
                length = 8 if wire == 1 else 4
            if pos + length > len(data):
                raise ValueError("protobuf 字段截断")
            value = data[pos:pos+length]; pos += length
        else:
            raise ValueError("不支持的 protobuf wire type")
        yield number, value


def frame(data):
    return b"\0" + struct.pack(">I",len(data)) + data


def exact(reader, n):
    data = bytearray()
    while len(data) < n:
        chunk = reader.read(n-len(data))
        if not chunk:
            if not data:
                return b""
            raise EOFError("gRPC-Web 帧截断")
        data.extend(chunk)
    return bytes(data)


def messages(reader):
    while True:
        header = exact(reader,5)
        if not header:
            raise EOFError("gRPC-Web 未收到状态 trailer")
        flags, length = header[0], int.from_bytes(header[1:],"big")
        if length > MAX_FRAME:
            raise ValueError("gRPC-Web 帧过大")
        body = exact(reader,length) if length else b""
        if len(body) != length:
            raise EOFError("gRPC-Web payload 截断")
        if flags & 0x80:
            trailer = dict(line.split(":",1) for line in body.decode("ascii").split("\r\n") if ":" in line)
            status = trailer.get("grpc-status", "").strip()
            if status != "0":
                # Do not log arbitrary server text that could contain credentials.
                raise ConnectionError(f"CloudDrive gRPC 状态 {status or '缺失'}；检查会员权限/Token")
            return
        if flags:
            raise ValueError("不支持压缩 gRPC-Web 帧")
        yield body


def decode_change(payload):
    message = dict(fields(payload))
    if message.get(1) != 4 or not isinstance(message.get(5),bytes):
        return None
    f = dict(fields(message[5]))
    return {"type": f.get(1,0), "directory": bool(f.get(2,0)),
            "path": f.get(3,b"").decode("utf-8"),
            "new_path": f.get(4,b"").decode("utf-8")}
