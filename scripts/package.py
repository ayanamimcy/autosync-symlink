"""Build source distributions from an explicit allowlist, never runtime data."""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile
import zipfile

ROOT_FILES = {"README.md", "LICENSE", "REFERENCES.md", "Dockerfile", "compose.yaml",
              "requirements.txt", "run.py", "config.example.json", "config.linux.example.json",
              ".env.example", ".gitignore", ".dockerignore"}
ROOT_DIRS = {"bridge", "scripts", "tests", "docs", "deploy", ".github"}
EXCLUDE = {".git", ".venv", ".local-demo", "data", "dist", "build", "__pycache__", ".pytest_cache"}
EXTENSIONS = {".py", ".ps1", ".js", ".css", ".html", ".md", ".txt", ".json", ".yaml", ".yml"}


def source_files(root):
    root = Path(root)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDE for part in relative.parts):
            continue
        allowed = ((len(relative.parts) == 1 and relative.name in ROOT_FILES)
                   or (relative.parts[0] in ROOT_DIRS and path.suffix in EXTENSIONS))
        if not allowed or path.name.startswith(".env") and relative.name != ".env.example":
            continue
        if path.is_symlink():
            raise ValueError(f"发布包不接受源码符号链接：{relative}")
        if path.is_file():
            yield relative, path.read_bytes()


def package(root, output, version):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,80}", version):
        raise ValueError("版本只能包含字母、数字、点、下划线、加号和连字符")
    files = list(source_files(root))
    if not any(str(name) == "run.py" for name, _ in files):
        raise ValueError("缺少 run.py，拒绝生成不完整发布包")
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    prefix = "autosync-symlink-"+version
    archive = output/(prefix+".tar.gz")
    with archive.open("wb") as target, gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as stream:
        with tarfile.open(fileobj=stream, mode="w") as tar:
            for name, content in files:
                info = tarfile.TarInfo(prefix+"/"+name.as_posix())
                info.size = len(content); info.mode = 0o644; info.mtime = 0
                tar.addfile(info, io.BytesIO(content))
    zipped = output/(prefix+".zip")
    with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as zipout:
        for name, content in files:
            info = zipfile.ZipInfo(prefix+"/"+name.as_posix(), (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16; info.compress_type = zipfile.ZIP_DEFLATED
            zipout.writestr(info, content)
    checksums = "".join(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+p.name+"\n" for p in (archive, zipped))
    (output/"SHA256SUMS").write_text(checksums)
    return archive, zipped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    for path in package(Path(__file__).resolve().parents[1], args.output, args.version):
        print(path)
