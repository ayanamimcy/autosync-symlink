import hashlib
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from scripts.package import package


class PackageTests(unittest.TestCase):
    def test_reproducible_packages_exclude_runtime_files_and_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = {"run.py": b"print('service')", "bridge/service.py": b"# module", "README.md": b"docs",
                     ".env.example": b"PUID=1000", ".github/workflows/build.yml": b"name: Build",
                     ".env": b"SECRET", ".local-demo/data/admin.key": b"SECRET", "data/config.json": b"SECRET",
                     "bridge/__pycache__/service.pyc": b"SECRET", "bridge/private.key": b"SECRET",
                     "bridge/.env": b"SECRET", "bridge/data/config.json": b"SECRET", ".DS_Store": b"SECRET"}
            for name, content in files.items():
                p = root/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
            archives = package(root, root/"dist", "v0.1.0")
            first = [hashlib.sha256(p.read_bytes()).hexdigest() for p in archives]
            repeated = package(root, root/"dist", "v0.1.0")
            self.assertEqual(first, [hashlib.sha256(p.read_bytes()).hexdigest() for p in repeated])
            with tarfile.open(archives[0]) as tar:
                names = tar.getnames()
                self.assertEqual(len(names), 5)
                self.assertIn("autosync-symlink-v0.1.0/.env.example", names)
                self.assertIn("autosync-symlink-v0.1.0/.github/workflows/build.yml", names)
                for member in tar:
                    self.assertNotIn(b"SECRET", tar.extractfile(member).read())
            with zipfile.ZipFile(archives[1]) as zipin:
                self.assertEqual(set(zipin.namelist()), set(names))
            self.assertEqual(len((root/"dist/SHA256SUMS").read_text().splitlines()), 2)

    def test_version_cannot_escape_output_or_inject_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            for version in ("../secret", "v1;touch secret", "a/b", "", "-option"):
                with self.assertRaises(ValueError):
                    package(folder, Path(folder)/"dist", version)

    def test_missing_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            package(folder, Path(folder)/"dist", "v0.1.0")

    def test_source_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/"run.py").write_text("# service")
            (root/"bridge").mkdir(); (root/"secret").write_text("secret")
            (root/"bridge/module.py").symlink_to(root/"secret")
            with self.assertRaises(ValueError):
                package(root, root/"dist", "v0.1.0")


if __name__ == "__main__":
    unittest.main()
