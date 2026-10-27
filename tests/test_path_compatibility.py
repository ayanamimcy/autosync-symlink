import os
from pathlib import PureWindowsPath
import unittest

from bridge.paths import safe_join
from test_bridge import Fixture


class PathCompatibilityTests(Fixture):
    @unittest.skipIf(os.name == "nt", "colon filenames require a POSIX filesystem")
    def test_colons_in_posix_media_and_metadata_names_are_preserved(self):
        video = self.file("Film: Part 1 (2024)/Episode: 01.mkv")
        self.file("Film: Part 1 (2024)/Episode: 01.nfo", b"metadata")
        result = self.engine.scan(self.lib)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["change_count"], 2)
        link = self.output/"Film: Part 1 (2024)/Episode: 01.mkv"
        self.assertEqual(link.resolve(), video)
        self.assertEqual((self.output/"Film: Part 1 (2024)/Episode: 01.nfo").read_bytes(), b"metadata")
        self.assertEqual(self.engine.scan(self.lib)["change_count"], 0)

    @unittest.skipIf(os.name == "nt", "colon filenames require a POSIX filesystem")
    def test_colons_in_posix_strm_names_and_url_encoding(self):
        self.lib.update(mode="strm", strm_prefix="http://media.test/stream")
        self.file("A: Movie/Part: One.mkv")
        result = self.engine.scan(self.lib)
        self.assertFalse(result["errors"])
        content = (self.output/"A: Movie/Part: One.strm").read_text()
        self.assertEqual(content, "http://media.test/stream/A%3A%20Movie/Part%3A%20One.mkv\n")

    def test_windows_drive_and_alternate_data_stream_paths_remain_rejected(self):
        root = PureWindowsPath("Y:/library")
        for relative in ("C:/outside", "C:outside", "Film/movie.nfo:stream", "Film: Title/a.mkv"):
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, "Windows"):
                safe_join(root, relative)

    def test_rejected_path_error_identifies_the_problematic_name(self):
        relative = "Film/bad\\name.mkv"
        with self.assertRaises(ValueError) as caught:
            safe_join(self.output, relative)
        self.assertIn(repr(relative), str(caught.exception))
        self.assertIn("反斜杠", str(caught.exception))
        for relative in ("../outside", "/outside", "Film/../../outside", "bad\x00name.mkv"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                safe_join(self.output, relative)

    def test_missing_marker_error_explains_initialization_and_does_not_create_it(self):
        marker = self.source/self.lib["sentinel"]; marker.unlink()
        with self.assertRaises(OSError) as caught:
            self.engine.scan(self.lib, cleanup=True)
        message = str(caught.exception)
        self.assertIn(str(marker), message)
        self.assertIn("先确认 CloudDrive 挂载", message)
        self.assertIn("宿主机", message)
        self.assertFalse(marker.exists())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
